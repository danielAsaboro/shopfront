"""The hosted public demo: Shopfront's own pages over a made-up shop, read-only, for anyone with the link.

    SHOPFRONT_PUBLIC_DEMO=1 .venv/bin/uvicorn app.public_demo:app --port 8404     # locally
    Handler: app.public_demo.handler                                              # Lambda (infra/template.yaml)

At start (a Lambda cold start) it builds the one-command demo's shop (scripts/demo.py, reusing its
functions): the demo day is the most recent open day before today whose synthetic door leaves a scan the
door can answer; samples/orders.csv is imported for that day; one signed `package_delivery` webhook replay
announces the flour courier; and the "business closed" scan for the dairy order is picked and checked
through the app itself, so the links in the banner open a story the pages actually tell.

What a visitor can and can't do, from the outside in:

- `PublicDemoApp` lets through only GET/HEAD reads on READ_PATHS (deny by default: a route added to
  app.main later is refused here until someone adds it). Every other method on every path gets a
  "not saved in the public demo" answer before the app sees the request, and the body is never read.
  The business-closed check and the dispute pack still work: they are GETs that compute an answer
  in memory and store nothing.
- The owner gate is PublicDemoGate (app/owner.py): nobody is the owner, nothing pairs, and only a request
  this allowlist admitted passes it, so app.main's own checks refuse whatever slipped past.
- The stores the app reads are swapped for ones that raise ReadOnly on any write, and the Ring source is
  pinned to the synthetic generator: a pasted token can't switch it to live Ring, because pasting is refused
  and the source never reads a token file.
- No Bedrock: the rules classifier labels events, Ask shows its recorded example (the page's own fallback),
  the screenshot reader says it isn't available, and the agent factory raises. The Lambda's role has no
  Bedrock permission either.
- Every HTML page gets a banner: "Public demo: made-up shop, made-up deliveries."

tests/test_public_demo.py walks every route of app.main with every method and checks that nothing is
written and nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import urlencode

log = logging.getLogger(__name__)

ENV_FLAG = "SHOPFRONT_PUBLIC_DEMO"

# GET/HEAD reads a visitor may make. Everything else is refused before it reaches the app.
READ_PATHS = frozenset({"/", "/dispute", "/dispute.zip", "/setup", "/healthz", "/favicon.ico",
                        "/api/deliveries", "/api/events", "/api/briefing", "/api/attempt", "/api/dispute",
                        "/api/webhooks"})
READ_PREFIXES = ("/frames/",)
READ_METHODS = frozenset({"GET", "HEAD"})

# Why a page or action is switched off, in the visitor's words
OFF = {
    "/connect": "Connecting a Ring doorbell is switched off in the public demo. On the shop's own computer, "
                "this is where the owner pastes a Ring sign-in.",
    "/pair": "Pairing a phone is switched off in the public demo: there is no owner here to pair with.",
    "/live": "Live view needs the shop's own Ring doorbell, so it is switched off in the public demo.",
    "/api/ask": "Ask Shopfront runs on Amazon Bedrock and is switched off in the public demo; the receiving "
                "desk shows a recorded example answer instead.",
    "/scan/extract": "Reading a tracking-page screenshot uses Amazon Bedrock and is switched off in the public "
                     "demo. Type the scan into the form instead; that works.",
}
NOT_SAVED = ("This is the public demo, so nothing is saved. On the shop's own computer this would {what}; "
             "here the made-up shop stays as it is for the next visitor.")
WHAT = (("/deliveries/", "record what staff found when they unpacked the order"),
        ("/deliveries", "add an order"), ("/orders/import", "import an orders file"),
        ("/setup", "save the shop's name and hours"), ("/frames/", "delete a door frame"),
        ("/webhooks/ring", "store a Ring webhook"), ("/connect", "save or forget a Ring sign-in"),
        ("/pair", "pair a phone"), ("/checks", "keep or remove a scan check in the carrier record"))

BANNER = ('<div class="banner note" role="note" id="public-demo"><strong>Public demo: made-up shop, made-up '
          'deliveries.</strong> The door events are generated and the one Ring alert is a signed replay; '
          'nothing here calls Ring or Bedrock, and nothing you do is saved.{links}</div>')


class ReadOnly(PermissionError):
    """A write reached a store in the public demo. The allowlist should have stopped it first."""

    def __init__(self, what: str):
        super().__init__(f"public demo: {what} is not saved")


@dataclass
class Demo:
    """The made-up shop the demo serves, and the links its banner gives."""

    day: date
    data: Path
    scan: str | None = None  # the "business closed" scan time picked for the dairy order
    check: str = ""
    links: dict[str, str] = field(default_factory=dict)


# --- stores that refuse writes ---------------------------------------------------------------------------------

def _read_only_stores():
    from app.cache import ClassificationCache
    from app.checks import CheckBook
    from app.deliveries import DeliveryBook, FrameStore
    from app.live import LiveLog
    from app.ring import RingSource
    from app.ring.webhooks import WebhookLog
    from app.shop import ShopProfile

    class Book(DeliveryBook):
        def _write(self, rows):
            raise ReadOnly("an order change")

    class Profile(ShopProfile):
        def save(self, p):
            raise ReadOnly("the shop profile")

    class Frames(FrameStore):
        def save(self, event_id, jpeg):
            raise ReadOnly("a door frame")

    class Webhooks(WebhookLog):
        def handle(self, raw, signature, key, replayed=False):
            raise ReadOnly("a webhook")

    class Live(LiveLog):
        def start(self, device_id, session_url):
            raise ReadOnly("a live view")

        def frame(self, *a, **kw):
            raise ReadOnly("a live view frame")

        def stop(self, session_id):
            raise ReadOnly("a live view")

        def _expire(self):
            pass

    class Checks(CheckBook):
        def _write(self, rows):
            raise ReadOnly("a kept check")

    class Cache(ClassificationCache):
        def put(self, event_id, row):
            raise ReadOnly("a classification")

    class Source(RingSource):
        """Always the synthetic generator: never reads a token file, never accepts one."""

        def pasted_token(self) -> str:
            return ""

        def effective_settings(self):
            return replace(self.settings, ring_mode="synthetic", ring_access_token="", ring_refresh_token="",
                           ring_client_id="", ring_client_secret="")

        def save_token(self, token):
            raise ReadOnly("a Ring sign-in")

        def forget_token(self):
            raise ReadOnly("a Ring sign-in")

    return Book, Profile, Frames, Webhooks, Live, Cache, Source, Checks


class _NoBedrock:
    """Stands in for the Ask probe and the screenshot reader: unavailable, without asking AWS."""

    reason = "PUBLIC_DEMO"

    def state(self) -> str:
        return self.reason

    def mark(self, reason: str) -> None:
        pass

    def available(self) -> bool:
        return False

    def read(self, *a, **kw):
        raise ReadOnly("a screenshot")


def _no_agent(*a, **kw):
    raise ReadOnly("a question to Bedrock")


class _Direct:
    """What scripts.demo.feed_package_alert posts through: straight into the webhook log, no HTTP."""

    def __init__(self, log_, key: str):
        self.log, self.key = log_, key

    def post(self, path, content, headers):
        from app.ring.webhooks import WebhookStatus

        status = self.log.handle(content, headers.get("X-Signature"), self.key)
        return type("R", (), {"status_code": 200 if status == WebhookStatus.ACCEPTED else 400,
                              "text": status.value})()


class _InProcess:
    """The httpx-style `get` scripts.demo.walk reads the app through, served in this process (no socket).
    Each request runs on its own event loop in a worker thread, because uvicorn builds the app from inside
    its running loop."""

    def __init__(self, asgi):
        self.asgi = asgi

    def get(self, path: str, params: dict | None = None):
        import httpx

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.asgi),
                                         base_url="http://public-demo") as c:
                return await c.get(path, params=params)
        with ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, go()).result()


# --- building the made-up shop -------------------------------------------------------------------------------

def seed(data: Path, tz, today: date) -> tuple[date, str]:
    """Write the demo's orders and its one webhook replay into `data`, exactly as scripts/demo.py does.
    Returns the demo day and the dated orders CSV."""
    from app.ring.webhooks import WebhookLog
    from scripts import demo

    day = demo.demo_day(today, usable=lambda d: demo.has_scan_slot(d, tz))
    orders = demo.dated_orders(demo.SAMPLE.read_text(), day)
    demo._import_orders(data, orders)
    key = secrets.token_hex(16)  # signs this one replay and is then forgotten; the app holds no webhook key
    demo.feed_package_alert(_Direct(WebhookLog(data / "ring-webhooks.jsonl"), key), day, tz, key)
    demo.seed_past_checks(data, day, tz)  # the carrier record's history, marked demo
    return day, orders


def install(main: ModuleType, data: Path, today: date | None = None, setattr=setattr) -> tuple[Demo, str]:
    """Seed `data` and point app.main at it through stores that refuse writes; returns the demo and its
    dated orders CSV. `setattr` lets tests use monkeypatch.setattr, so their app.main is restored after."""
    from app.classify.rules import RulesClassifier
    from app.owner import PublicDemoGate
    from scripts import demo as demo_script

    tz = main.settings.tz
    data.mkdir(parents=True, exist_ok=True)
    day, orders = seed(data, tz, today or datetime.now(tz).date())
    Book, Profile, Frames, Webhooks, Live, Cache, Source, Checks = _read_only_stores()
    for name, value in {
        "DATA": data,
        "profile": Profile(data / "shop.json", demo_script.SHOP_NAME, demo_script.SHOP_HOURS),
        "book": Book(data / "expected-deliveries.json"),
        "checks": Checks(data / "checks.json"),
        "frames": Frames(data),
        "classifier": RulesClassifier(),
        "cache": Cache(None),
        "webhook_log": Webhooks(data / "ring-webhooks.jsonl"),
        "source": Source(main.settings, data / "ring-access-token"),
        "live": Live(data, tz),
        "scan_reader": _NoBedrock(),
        "ask_probe": _NoBedrock(),
        "_ask_state": lambda: _NoBedrock.reason,
        "agent_factory": _no_agent,
        "_housekeeping": lambda: None,  # nothing to prune, and no deletes
        "gate": PublicDemoGate(hosts=main.settings.allowed_hosts.split(",")),
    }.items():
        if not hasattr(main, name):
            raise RuntimeError(f"app.main has no {name}; the public demo can't make it read-only")
        setattr(main, name, value)
    return Demo(day=day, data=data, links={"desk": f"/?{urlencode({'date': day.isoformat()})}"}), orders


def tell_story(main: ModuleType, d: Demo, orders: str) -> None:
    """Pick and check the dairy order's scan through the app, as scripts/demo.py does, and keep the links.
    If the door doesn't support the story, the demo still serves the desk; the banner just links less."""
    from scripts import demo as demo_script

    try:
        story = demo_script.walk(_InProcess(PublicDemoApp(main, d)), d.day, main.settings.tz, orders)
    except demo_script.DemoMismatch as exc:
        log.warning("public demo: no scan story for %s: %s", d.day, exc)
        return
    q = {**story["query"], "at": story["scan"], "claim": "business_closed"}
    d.scan, d.check = story["scan"], story["check"].get("text", "")
    d.links["check"] = "/?" + urlencode({**story["query"], "attempt": story["scan"], "claim": "business_closed"}) \
        + "#atth"
    d.links["pack"] = "/dispute?" + urlencode(q)


def build(env=os.environ) -> PublicDemoApp:
    """The demo app for uvicorn or Lambda. Pins the environment before app.main is imported, so a
    developer's .env can't switch it to live Ring, Bedrock or DynamoDB."""
    if env.get(ENV_FLAG) != "1":
        raise RuntimeError(f"app.public_demo serves a public, read-only demo; set {ENV_FLAG}=1 to run it")
    data = Path(tempfile.mkdtemp(prefix="shopfront-public-demo-"))  # Lambda: /tmp is the only writable place
    from scripts import demo as demo_script

    env.update({"SHOPFRONT_DATA_DIR": str(data), "RING_MODE": "synthetic", "RING_ACCESS_TOKEN": "",
                "RING_REFRESH_TOKEN": "", "RING_CLIENT_ID": "", "RING_CLIENT_SECRET": "", "RING_HMAC_KEY": "",
                "CLASSIFIER": "rules", "SHOPFRONT_WEBHOOK_STORE": "file", "SHOP_NAME": demo_script.SHOP_NAME,
                "SHOP_HOURS": demo_script.SHOP_HOURS})
    from app import main

    d, orders = install(main, data)
    tell_story(main, d, orders)
    log.info("public demo ready: %s, scan %s", d.day, d.scan)
    return PublicDemoApp(main, d)


# --- the allowlist ---------------------------------------------------------------------------------------------

def readable(method: str, path: str) -> bool:
    return method in READ_METHODS and (path in READ_PATHS or path.startswith(READ_PREFIXES))


def _what(path: str) -> str:
    return next((w for prefix, w in WHAT if path.startswith(prefix)), "change the shop's records")


_MAIN_OPEN, _BODY_OPEN = re.compile(rb"<main[^>]*>", re.I), re.compile(rb"<body[^>]*>", re.I)


def with_banner(page: bytes, banner: bytes) -> bytes:
    """The banner first inside <main> (the page's column), or after <body> on a page without one."""
    for tag in (_MAIN_OPEN, _BODY_OPEN):
        if tag.search(page):
            return tag.sub(lambda m: m.group(0) + banner, page, count=1)
    return page


class PublicDemoApp:
    """ASGI app: the allowlist in front of app.main, plus the demo banner on every HTML page."""

    def __init__(self, main: ModuleType, demo: Demo):
        self.main, self.demo, self.inner = main, demo, main.app
        names = (("desk", "the receiving desk"), ("check", "the business-closed check"), ("pack", "the dispute pack"))
        links = [f'<a href="{html.escape(self.demo.links[k])}">{label}</a>' for k, label in names
                 if k in self.demo.links]
        self.banner = BANNER.format(links=" Start with " + " · ".join(links) + "." if links else "").encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # answered here: app.main's own start-up tasks (Ring polling) stay off
            return await self._lifespan(receive, send)
        if scope["type"] != "http":  # no websockets
            return
        method, path = scope["method"], scope["path"]
        if path == "/robots.txt":
            return await self._plain(send, 200, b"User-agent: *\nDisallow: /\n")
        if method in READ_METHODS and path == "/" and not scope.get("query_string"):
            return await self._redirect(send, self.demo.links["desk"])
        if not readable(method, path):
            return await self._refuse(scope, receive, send)
        from app.owner import admit_visitor

        with admit_visitor():
            await self.inner(scope, receive, self._with_banner(send))

    @staticmethod
    async def _lifespan(receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                return await send({"type": "lifespan.shutdown.complete"})

    def _with_banner(self, send):
        """Buffer an HTML response, add the banner after <main> (or <body>), fix Content-Length."""
        state: dict = {}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-robots-tag"]
                headers.append((b"x-robots-tag", b"noindex, nofollow"))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    state.update(start={**message, "headers": headers}, body=b"")
                    return
                return await send({**message, "headers": headers})
            if message["type"] == "http.response.body" and "start" in state:
                state["body"] += message.get("body", b"")
                if message.get("more_body"):
                    return
                body = with_banner(state["body"], self.banner)
                start = state.pop("start")
                start["headers"] = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"] \
                    + [(b"content-length", str(len(body)).encode())]
                await send(start)
                return await send({"type": "http.response.body", "body": body})
            return await send(message)
        return wrapped

    async def _refuse(self, scope, receive, send):
        """403 before the app sees the request. HTML for a browser page or form, JSON for /api/*."""
        method, path = scope["method"], scope["path"]
        off = next((text for prefix, text in OFF.items() if path == prefix or path.startswith(prefix + "/")), None)
        if method in READ_METHODS:
            text = off or "That page isn't part of the public demo."
        else:
            text = off or NOT_SAVED.format(what=_what(path))
        if path.startswith(("/api/", "/webhooks/")) or path in ("/openapi.json",):
            import json

            body = json.dumps({"error": "PUBLIC_DEMO", "detail": text}).encode()
            return await self._send(send, 403, body, b"application/json")
        from starlette.requests import Request

        err = {"title": "Not in the public demo", "text": text, "back": (self.demo.links["desk"],
               "Back to the receiving desk"), "status": 403, "detail": f"{method} {path} (public demo)"}
        page = self.main._page(Request(scope, receive), "error.html", {"err": err}, 403)
        await self._send(send, 403, with_banner(page.body, self.banner), b"text/html; charset=utf-8")

    async def _send(self, send, status: int, body: bytes, ctype: bytes, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()),
                                (b"x-robots-tag", b"noindex, nofollow"), (b"cache-control", b"no-store"), *extra]})
        await send({"type": "http.response.body", "body": body})

    async def _plain(self, send, status: int, body: bytes):
        await self._send(send, status, body, b"text/plain; charset=utf-8")

    async def _redirect(self, send, location: str):
        await self._send(send, 302, b"", b"text/plain; charset=utf-8", [(b"location", location.encode())])


# --- entry points ----------------------------------------------------------------------------------------------

_built: dict = {}


def __getattr__(name: str):
    """`app` (uvicorn) and `handler` (Lambda, through Mangum) are built on first use, so importing this module
    for its parts (the tests do) doesn't build a demo."""
    if name not in ("app", "handler"):
        raise AttributeError(name)
    if "app" not in _built:
        _built["app"] = build()
    if name == "handler" and "handler" not in _built:
        from mangum import Mangum

        _built["handler"] = Mangum(_built["app"], lifespan="off")
    return _built[name]
