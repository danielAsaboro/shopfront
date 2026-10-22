"""The hosted public demo (app/public_demo.py, infra/template.yaml): every route, every method, no writes,
nothing leaving the process, and an owner gate with no owner."""

from __future__ import annotations

import base64
import hashlib
import re
import socket
from dataclasses import replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

import app.main as main
from app import public_demo
from app.owner import COOKIE, OwnerGate, PublicDemoGate, admit_visitor
from tests.aws_fakes import template

HOST = "abc123example.lambda-url.us-east-1.on.aws"
BANNER = "Public demo of a made-up shop."
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@pytest.fixture
def demo(tmp_path, monkeypatch):
    d, orders = public_demo.install(main, tmp_path / "public-demo", setattr=monkeypatch.setattr)
    public_demo.tell_story(main, d, orders)
    return d


@pytest.fixture
def client(demo):
    return TestClient(public_demo.PublicDemoApp(main, demo), base_url=f"https://{HOST}", follow_redirects=False)


@pytest.fixture
def outbound(monkeypatch):
    """Record (and refuse) anything that would leave the process: sockets, httpx's network transports, boto3,
    the live Ring client and the Bedrock agent."""
    calls: list[str] = []

    def refuse(name, real=None):
        def f(*a, **kw):
            if real is not None and a and getattr(a[0], "family", None) == getattr(socket, "AF_UNIX", None):
                return real(*a, **kw)  # the event loop's own wake-up pipe
            calls.append(name)
            raise ConnectionRefusedError(f"public demo reached out: {name}")
        return f

    import boto3
    import botocore.session
    import httpx

    import app.agent
    import app.ring.api

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect", socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex", socket.socket.connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse("httpx"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse("httpx async"))
    monkeypatch.setattr(boto3, "client", refuse("boto3.client"))
    monkeypatch.setattr(boto3.session.Session, "client", refuse("boto3 Session.client"))
    monkeypatch.setattr(botocore.session.Session, "create_client", refuse("botocore create_client"))
    monkeypatch.setattr(botocore.session.Session, "get_credentials", refuse("AWS credentials lookup"))
    monkeypatch.setattr(app.ring.api.RingApiClient, "__init__", refuse("live Ring client"))
    monkeypatch.setattr(app.agent, "ask", refuse("Ask agent"))
    return calls


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            for p in sorted(root.rglob("*"))}


def state() -> tuple:
    return ([e.__dict__ for e in main.book._all()], main.webhook_log.recent(10_000), main.profile.get(),
            main.live.rows(), len(main.live.active), type(main.gate), type(main.classifier).__name__)


def _example(route_path: str, demo) -> str:
    expected = main.book._all()[0].id
    path = route_path.replace("{expected_id}", expected).replace("{session_id}", "live-0001")
    return path.replace("{name}", "ring-123.jpg")


def app_routes() -> list:
    """Every route the app answers, the ones app/routes/* include as well as the app's own."""
    return [r for r in iter_route_contexts(main.app.routes) if r.path]


def all_paths(demo) -> list[str]:
    return sorted({_example(r.path, demo) for r in app_routes()})


# ---- the story a judge opens ------------------------------------------------------------------------------------

def test_the_demo_is_the_one_command_demos_shop(demo):
    from scripts import demo as script

    assert demo.day < datetime.now(main.settings.tz).date()
    assert script.open_hours(script.SHOP_HOURS, demo.day)
    refs = {e.order_ref for e in main.book.for_day(demo.day)}
    assert {script.FLOUR_ORDER, script.SCAN_ORDER} <= refs
    [hook] = main.webhook_log.recent()
    assert hook["replayed"] and hook["sub_type"] == "package_delivery"
    assert demo.scan and set(demo.links) == {"desk", "check", "pack"}
    assert main.profile.get().name == script.SHOP_NAME and main.source.get().name == "synthetic"


def test_landing_page_opens_on_the_demo_day_with_the_banner(client, demo):
    r = client.get("/")
    # round 10: "/" opens on the business-closed verdict, the check scripts/demo.py prints first
    assert r.status_code == 302 and r.headers["location"] == demo.links["check"].split("#")[0]
    page = client.get(demo.links["desk"])
    assert page.status_code == 200 and BANNER in page.text
    assert page.text.count('id="public-demo"') == 1
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert int(page.headers["content-length"]) == len(page.content)


def test_desk_shows_the_carrier_record_and_offers_the_demo_scan(client, demo):
    """The one-command demo's seeded history and its filled-in check, read-only here."""
    desk = client.get(demo.links["desk"]).text
    n = len(main.checks.all())  # earlier open days in the demo day's month: two, unless it is the month's start
    assert n == 2 or demo.day.day <= 7
    if n:
        assert "Northline Parcel: 2 &#34;business closed&#34; scans in October" in desk and "during posted hours" in desk
    # round 10: the flour order's replayed alert, said as what it saves rather than "$17.20 ... not needed"
    assert re.search(r"\d+ orders? with (its courier|their couriers) confirmed at the door: no redelivery fees? there",
                     desk) and "not needed" not in desk
    button = desk.split('class="try-demo', 1)[1].split("</form>", 1)[0]
    assert "Try the business-closed check" in button and f'name="attempt" value="{demo.scan}"' in button


def test_banner_links_open_the_check_and_the_pack(client, demo):
    check = client.get(demo.links["check"].split("#")[0])
    assert check.status_code == 200 and BANNER in check.text
    pack = client.get(demo.links["pack"])
    assert pack.status_code == 200 and BANNER in pack.text and "Northline Parcel" in pack.text
    z = client.get(demo.links["pack"].replace("/dispute?", "/dispute.zip?"))
    assert z.status_code == 200 and z.content[:2] == b"PK"
    api = client.get(demo.links["pack"].replace("/dispute?", "/api/dispute?")).json()
    assert api.get("draft") or api.get("drafts")  # the scan is disputed, as in the one-command demo


def test_ask_shows_its_labelled_example_and_no_question_box(client, demo):
    page = client.get(demo.links["desk"]).text
    assert '<span class="badge b-demo">recorded, not live</span>' in page and "<code>deliveries(" in page
    assert 'id="ask"' not in page
    assert 'action="/scan/extract"' not in page  # the screenshot reader (Bedrock) isn't offered either


# ---- every route, every method: no writes, no outbound calls ------------------------------------------------------

def test_every_route_and_method_writes_nothing_and_calls_nothing(client, demo, tmp_path, outbound):
    before_files, before_state = snapshot(tmp_path), state()
    reads = [*all_paths(demo), demo.links["desk"], demo.links["check"].split("#")[0], demo.links["pack"],
             demo.links["pack"].replace("/dispute?", "/dispute.zip?"),
             demo.links["pack"].replace("/dispute?", "/api/dispute?"),
             f"/api/attempt?at={demo.day}T{demo.scan}&claim=business_closed",
             f"/api/deliveries?date={demo.day}", f"/api/events?date={demo.day}", f"/api/briefing?date={demo.day}"]
    seen = []
    for path in reads:
        for method in METHODS:
            bare = path.split("?")[0]
            body = {"json": {"question": "who came?"}} if bare.startswith("/api/") else \
                {"data": {"token": "pasted", "name": "Mine now", "code": "123456", "status": "ok", "csv": "x"}}
            r = client.request(method, path, **({} if method in ("GET", "HEAD", "OPTIONS") else body))
            seen.append((method, path, r.status_code))
            assert r.status_code < 500, (method, path, r.text[:300])
            if public_demo.readable(method, bare):
                if method == "GET" and r.headers.get("content-type", "").startswith("text/html"):
                    assert BANNER in r.text, (method, path)
            else:
                assert r.status_code == 403, (method, path, r.status_code)
                assert method == "HEAD" or "PUBLIC_DEMO" in r.text or "Not in the public demo" in r.text, \
                    (method, path)
            assert snapshot(tmp_path) == before_files, f"{method} {path} wrote to disk"
            assert state() == before_state, f"{method} {path} changed the shop"
    assert outbound == [], outbound
    walked = {p for _, p, _ in seen}
    assert {_example(r.path, demo) for r in app_routes()} <= walked


def test_owner_actions_are_refused_before_the_app_reads_them(client, demo, outbound):
    expected, kept = main.book.for_day(demo.day)[0], main.checks.all()
    for path, data in [("/connect", {"token": "a-ring-token"}), ("/pair", {"code": "000000"}),
                       ("/setup", {"name": "Taken over"}), ("/orders/import", {"csv": "day,supplier\n"}),
                       (f"/deliveries/{expected.id}/received", {"status": "short"}),
                       (f"/deliveries/{expected.id}/remove", {}), ("/pair/forget", {}), ("/connect/forget", {}),
                       ("/checks", {"date": demo.day.isoformat(), "at": "14:30", "claim": "business_closed"}),
                       ("/checks/0123456789ab/delete", {})]:
        r = client.post(path, data=data)
        assert r.status_code == 403 and BANNER in r.text, path
        assert "nothing is saved" in r.text or "switched off" in r.text, path
    assert main.book.get(expected.id).received is None and main.checks.all() == kept
    r = client.post("/webhooks/ring", content=b"{}", headers={"X-Signature": "x"})
    assert r.status_code == 403 and r.json()["error"] == "PUBLIC_DEMO"
    r = client.post("/api/ask", json={"question": "Did the flour come?"})
    assert r.status_code == 403 and "Bedrock" in r.json()["detail"]
    assert outbound == []


@pytest.mark.parametrize("path", ["/pair", "/docs", "/openapi.json", "/api/live/sessions"])
def test_pages_outside_the_demo_say_so(client, path):
    r = client.get(path)
    assert r.status_code == 403
    assert "public demo" in r.text.lower()


def test_allowlist_names_only_real_read_routes():
    gets = {r.path for r in app_routes() if "GET" in (r.methods or set())}
    assert public_demo.READ_PATHS <= gets
    for prefix in public_demo.READ_PREFIXES:
        assert any(p.startswith(prefix) for p in gets)
    assert public_demo.READ_METHODS == {"GET", "HEAD"}  # /setup also takes a POST; only its GET is let through


def test_app_mains_start_up_tasks_stay_off(demo, monkeypatch):
    """The demo answers the ASGI lifespan itself, so nothing app.main starts in the background (polling Ring)
    runs in the public demo."""
    started = []

    async def inner(scope, receive, send):
        started.append(scope["type"])

    wrapped = public_demo.PublicDemoApp(main, demo)
    monkeypatch.setattr(wrapped, "inner", inner)
    with TestClient(wrapped, base_url=f"https://{HOST}") as c:  # runs startup and shutdown
        assert c.get("/robots.txt").status_code == 200
    assert started == []


def test_robots_are_told_to_stay_out(client):
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


# ---- the stores and the source behind it refuse writes ------------------------------------------------------------

def test_stores_refuse_writes_and_ring_stays_synthetic(demo):
    with pytest.raises(public_demo.ReadOnly):
        main.book.add(demo.day, "Sneaky Supplier")
    with pytest.raises(public_demo.ReadOnly):
        main.profile.save(replace(main.profile.get(), name="Mine"))
    with pytest.raises(public_demo.ReadOnly):
        main.source.save_token("a-token")
    with pytest.raises(public_demo.ReadOnly):
        main.frames.save("ring-1", b"\xff\xd8")
    with pytest.raises(public_demo.ReadOnly):
        main.webhook_log.handle(b"{}", "sig", "key")
    with pytest.raises(public_demo.ReadOnly):
        main.live.start("dev", "https://ring.example/whep")
    with pytest.raises(public_demo.ReadOnly):
        main.checks._write([])
    (demo.data / "ring-access-token").write_text("a-real-looking-token")  # even a token on disk is ignored
    assert main.source.get().name == "synthetic" and main.source.pasted_token() == ""
    assert main._ask_state() == "PUBLIC_DEMO" and not main.scan_reader.available()


# ---- the owner gate in public-demo mode ---------------------------------------------------------------------------

def test_public_demo_gate_has_no_owner(tmp_path):
    g = PublicDemoGate()
    owner = OwnerGate(tmp_path / "secret")
    code, _ = owner.pairing_code()
    cookie = owner.pair(code)
    assert g.public_demo and not g.public_paths
    assert not g.is_local("127.0.0.1", {})
    assert not g.is_owner("127.0.0.1", {}, {COOKIE: cookie})  # loopback and a valid owner cookie: still no
    assert not g.check_cookie(cookie) and g.pair(code) is None
    with pytest.raises(PermissionError):
        g.pairing_code()
    with pytest.raises(PermissionError):
        g.forget_devices()
    with admit_visitor():
        assert g.is_owner("203.0.113.9", {}, {})
    assert not g.is_owner("203.0.113.9", {}, {})
    assert g.host_ok(HOST) and not g.host_ok("")
    assert g.cross_site("POST", HOST, {"origin": "https://evil.example"})


def test_the_flag_alone_makes_app_main_refuse_everything(monkeypatch, outbound):
    """Started as app.main:app with SHOPFRONT_PUBLIC_DEMO=1 (no allowlist in front), the app fails closed:
    no route answers, not even Ring's webhook."""
    gate = main.make_gate(replace(main.settings, public_demo=True), Path("/unused"))
    assert isinstance(gate, PublicDemoGate)
    monkeypatch.setattr(main, "gate", gate)
    c = TestClient(main.app, base_url=f"https://{HOST}")
    assert c.get("/").status_code == 403
    assert c.get("/api/deliveries").json()["error"] == "OWNER_ONLY"
    assert c.get("/healthz").status_code == 403
    assert c.post("/webhooks/ring", content=b"{}").status_code == 403
    assert c.post("/pair", data={"code": "123456"}).status_code == 403
    assert outbound == []


def test_build_refuses_without_the_flag():
    with pytest.raises(RuntimeError, match="SHOPFRONT_PUBLIC_DEMO=1"):
        public_demo.build(env={})


# ---- the Lambda handler, with Function URL events -----------------------------------------------------------------

def url_event(method: str, path: str, query: str = "") -> dict:
    return {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": query,
            "headers": {"host": HOST, "accept": "text/html", "x-forwarded-proto": "https",
                        "x-forwarded-for": "203.0.113.9"},
            "requestContext": {"accountId": "anonymous", "apiId": "abc123example", "domainName": HOST,
                               "domainPrefix": "abc123example", "requestId": "r-1", "routeKey": "$default",
                               "stage": "$default", "time": "21/Oct/2026:09:00:00 +0000", "timeEpoch": 0,
                               "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                                        "sourceIp": "203.0.113.9", "userAgent": "judge"}},
            "isBase64Encoded": False}


def test_lambda_handler_answers_function_url_events(demo, outbound):
    from mangum import Mangum

    handler = Mangum(public_demo.PublicDemoApp(main, demo), lifespan="off")
    r = handler(url_event("GET", "/"), None)
    assert r["statusCode"] == 302 and r["headers"]["location"] == demo.links["check"].split("#")[0]
    r = handler(url_event("GET", "/", demo.links["desk"].split("?")[1]), None)
    assert r["statusCode"] == 200 and BANNER in r["body"]
    r = handler(url_event("GET", "/dispute", demo.links["pack"].split("?")[1]), None)
    assert r["statusCode"] == 200 and BANNER in r["body"]
    r = handler(url_event("GET", "/dispute.zip", demo.links["pack"].split("?")[1]), None)
    assert r["statusCode"] == 200 and r["isBase64Encoded"] and base64.b64decode(r["body"])[:2] == b"PK"
    r = handler(url_event("POST", "/connect"), None)
    assert r["statusCode"] == 403
    assert outbound == []


# ---- the stack ----------------------------------------------------------------------------------------------------

def test_template_deploys_a_capped_read_only_demo():
    t = template()
    res = t["Resources"]
    fn = res["PublicDemoFunction"]["Properties"]
    assert fn["Handler"] == "app.public_demo.handler"
    assert fn["Environment"]["Variables"]["SHOPFRONT_PUBLIC_DEMO"] == "1"
    assert fn["MemorySize"] <= 512 and fn["Timeout"] <= 15
    assert fn["ReservedConcurrentExecutions"]["If"][0] == "CapDemoConcurrency"
    assert fn["FunctionUrlConfig"]["AuthType"] == "NONE"
    assert fn["LoggingConfig"]["LogGroup"] == {"Ref": "PublicDemoLogGroup"}
    assert res["PublicDemoLogGroup"]["Properties"]["RetentionInDays"] == {"Ref": "DemoLogRetentionDays"}
    assert not re.search(r"TABLE|SECRET|BUCKET|HMAC", str(fn["Environment"]))
    # Its role can write its own logs and nothing else: no Bedrock, no table, no bucket, no secret
    [policy] = res["PublicDemoFunctionRole"]["Properties"]["Policies"]
    actions = {a for s in policy["PolicyDocument"]["Statement"] for a in s["Action"]}
    assert actions == {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert t["Outputs"]["DemoUrl"]["Value"] == {"GetAtt": "PublicDemoFunctionUrl.FunctionUrl"}
    assert t["Parameters"]["DemoConcurrency"]["Default"] <= 5


def test_demo_package_holds_the_app_and_its_seed_data(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("package_demo", Path(main.__file__).parent.parent / "infra" /
                                                  "package_demo.py")
    pkg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg)
    files = {str(p.relative_to(tmp_path)) for p in pkg.copy_code(tmp_path)}
    assert {"app/public_demo.py", "app/main.py", "app/templates/dashboard.html", "scripts/demo.py",
            "scripts/feed_webhook.py", "samples/orders.csv"} <= files
    assert not any("__pycache__" in f or f.startswith(("tests/", ".data", "evidence/")) for f in files)
    # every sample file the demo seeds from ships with it (a missing one breaks the Lambda's seeding)
    import re
    demo_src = (Path(main.__file__).parent.parent / "scripts" / "demo.py").read_text()
    samples = {f"samples/{n}" for n in re.findall(r'ROOT / "samples" / "([^"]+)"', demo_src)}
    assert samples and samples <= files, samples - files
    # what the Lambda installs is pinned like the app's own requirements
    pins = dict(line.split("#")[0].strip().split("==") for line in pkg.REQUIREMENTS.read_text().splitlines()
                if "==" in line)
    app_pins = dict(line.split("#")[0].strip().split("==") for line in
                    (Path(main.__file__).parent.parent / "requirements.txt").read_text().splitlines()
                    if "==" in line and not line.startswith("#"))
    for name in ("fastapi", "jinja2", "python-multipart", "python-dotenv", "httpx"):
        assert pins[name] == app_pins[name], name


# ---- round 10: every visible control on the hosted pages leads somewhere, never to an error page ------------------

class _Controls(HTMLParser):
    """The links, GET forms (with what they would submit) and POST forms of one page."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.get_forms: list[tuple[str, dict, int]] = []  # (action, fields, enabled submit buttons)
        self.post_forms: list[dict] = []  # {"action", "enabled": [labels of controls a visitor could press]}
        self._form: dict | None = None
        self._off = 0  # inside a disabled fieldset
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("a", "link") and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img" and a.get("src"):
            self.links.append(a["src"])
        elif tag == "form":
            self._form = {"method": (a.get("method") or "get").lower(), "action": a.get("action") or "",
                          "fields": {}, "enabled": []}
        elif self._form is not None:
            if tag == "fieldset" and "disabled" in a:
                self._off += 1
            enabled = not self._off and "disabled" not in a
            if tag == "input" and a.get("name") and a.get("type") not in ("submit", "button", "file"):
                if a.get("type") != "checkbox" or "checked" in a:
                    self._form["fields"][a["name"]] = a.get("value") or ""
            if tag == "select" and a.get("name"):
                self._select = a["name"]
            if tag == "option" and self._select and ("selected" in a or self._select not in self._form["fields"]):
                self._form["fields"][self._select] = a.get("value") or ""
            if tag == "button" and enabled and a.get("type") != "button" or \
                    tag == "input" and enabled and a.get("type") in ("submit", "image", "file", "text", "time"):
                self._form["enabled"].append(a.get("name") or tag)

    def handle_endtag(self, tag):
        if self._form is None:
            return
        if tag == "fieldset" and self._off:
            self._off -= 1
        elif tag == "select":
            self._select = None
        elif tag == "form":
            f, self._form = self._form, None
            if f["method"] == "post":
                self.post_forms.append(f)
            else:
                self.get_forms.append((f["action"], f["fields"], len(f["enabled"])))


def _same_site(url: str) -> str | None:
    """The path and query a same-site link opens, or None for another site, a fragment or a data: URI."""
    parts = urlsplit(url)
    if parts.scheme in ("data", "mailto", "javascript") or (parts.netloc and parts.netloc != HOST):
        return None
    if not parts.path and not parts.query:
        return None  # "#atth": the same page
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def test_every_link_and_button_on_the_hosted_pages_works(client, demo, outbound):
    """A judge clicking around the hosted demo never lands on a 4xx/5xx page: every same-site link, image and
    GET form (submitted as it stands) answers, and every form that would save something is shown switched off,
    with the reason on the page, instead of leading to a refusal."""
    seen, queue, pages, crawled = set(), ["/"], 0, []
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        r = client.get(path, follow_redirects=True)
        assert r.status_code < 400, (path, r.status_code, r.text[:300])
        if not r.headers.get("content-type", "").startswith("text/html"):
            continue
        pages += 1
        assert pages < 200, "the crawl should stay small; something links without end"
        crawled.append(path)
        assert r.text.count('id="public-demo"') == 1, path
        controls = _Controls()
        controls.feed(r.text)
        for form in controls.post_forms:
            assert not form["enabled"], (path, form["action"], form["enabled"])
        if controls.post_forms:
            assert public_demo.OFF_FORM in r.text or "public demo saves nothing" in r.text, path
        found = [_same_site(urljoin(str(r.url), u)) for u in controls.links]
        for action, fields, _buttons in controls.get_forms:  # every GET form, as it stands
            target = urlsplit(urljoin(str(r.url), action or str(r.url))).path
            found.append(_same_site(target + "?" + urlencode(fields)))
        queue += [u for u in found if u and u not in seen]
    assert outbound == [] and len(crawled) > 10, crawled
    # the crawl reached the story and the pages the nav names
    assert {"/connect", "/live", "/setup"} <= {urlsplit(p).path for p in seen}
    assert any(p.startswith("/dispute?") for p in seen) and any(p.startswith("/report?") for p in seen)


def test_the_root_opens_on_the_business_closed_verdict(client, demo):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["location"] == demo.links["check"].split("#")[0]
    page = client.get(r.headers["location"]).text
    assert 'aria-label="The door&#39;s answer to the scan"' in page or "aria-label=\"The door's answer to the scan\"" in page


def test_one_banner_and_no_owner_only_wording(client, demo):
    """Round 10: the public demo's banner is the only one (not also the app's "made up on this computer"), and
    the nav's Connect Ring explains instead of refusing."""
    for path in (demo.links["desk"], demo.links["check"].split("#")[0], "/setup", "/live", "/connect"):
        page = client.get(path).text
        assert page.count('class="banner') - page.count('class="banner note msg"') == 1, path
        assert "made up on this computer" not in page and "Something's not right" not in page, path
    connect = client.get("/connect")
    assert connect.status_code == 200 and "Generate Token" in connect.text and "evidence/README.md" in connect.text
    assert 'name="token"' not in connect.text and 'action="/connect"' not in connect.text
    check = client.get(demo.links["check"].split("#")[0]).text
    assert "Keep in Northline Parcel&#39;s record</button>" in check and "public demo saves nothing" in check


def test_the_demo_door_is_demo_person_alerts_not_guesses_or_ring(client, demo):
    """Round 10: every customer the demo door makes up carries Ring's motion + human sub_type shape, so the door
    log reads as demo person alerts rather than rows of "customer (guess)", and nothing credits Ring for them."""
    from app.reconcile import person

    visits = main._visits(demo.day)
    customers = [v for v in visits if v.event.truth and v.event.truth.value == "customer"]
    assert customers and all(person(v) and v.event.source == "synthetic" for v in customers)
    page = client.get(demo.links["desk"]).text
    log = page.split('id="every-event"', 1)[1]
    assert "customer (guess)" not in log
    assert log.count('<td>person</td><td class="why">Demo person alert, no camera</td>') >= len(customers)
    flat = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    assert re.search(r"· 0 labelled by Ring · \d+ by demo or replayed alerts, not Ring ·", flat), flat[-600:]
