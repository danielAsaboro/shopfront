"""The owner gate: who may read and change the shop's data. Local tests only."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.owner import COOKIE, LOOPBACK_CLIENTS, OwnerGate, host_name

REMOTE = ("203.0.113.9", 5000)
EVIL = {"Origin": "https://evil.example", "Referer": "https://evil.example/x"}
HOURS = {f"h{i}": "8-18" for i in range(7)}


def remote():
    return TestClient(main.app, client=REMOTE)


OWNER_ONLY_GETS = ["/", "/api/events", "/api/briefing", "/api/deliveries", "/api/dispute?at=12:00",
                   "/dispute?at=12:00", "/api/webhooks", "/live", "/api/live/sessions", "/setup", "/connect",
                   "/api/attempt?at=2026-10-07T12:00", "/frames/x.jpg", "/dispute.zip?at=12:00"]


@pytest.mark.parametrize("path", OWNER_ONLY_GETS)
def test_outsiders_get_nothing(path):
    r = remote().get(path, follow_redirects=False)
    assert r.status_code == 403
    if path.startswith(("/api/", "/frames/", "/dispute.zip")):
        assert r.json()["error"] == "OWNER_ONLY"
    else:  # a page: the outsider is told how the owner pairs a device, not a bare JSON error
        assert "pair" in r.text.lower() and "/connect is only available" not in r.text


def test_outsiders_cannot_start_live_view_or_write():
    c = remote()
    assert c.post("/api/live/start", content="v=0\r\no=- x\r\n",
                  headers={"Content-Type": "application/sdp"}).status_code == 403
    assert c.post("/setup", data={"name": "Pwned", **HOURS}).status_code == 403
    assert c.post("/orders/import", data={"csv": "day,supplier\n2026-10-07,X\n"}).status_code == 403
    assert not main.profile.get().saved and main.book.for_day(__import__("datetime").date(2026, 10, 7)) == []


def test_webhook_and_health_stay_public():
    c = remote()
    assert c.get("/healthz").status_code == 200
    assert c.post("/webhooks/ring", content=b"{}").status_code == 503  # no key: refused by its own rule, not the gate
    assert c.post("/webhooks/ring", content=b"{}", headers={"Host": "abc.tunnel.example"}).status_code == 503


def test_the_owner_on_this_machine_needs_no_sign_in():
    c = TestClient(main.app)
    assert c.get("/").status_code == 200 and c.get("/setup").status_code == 200


def test_production_gate_does_not_trust_the_test_client():
    g = main.make_gate(main.settings, Path("/nonexistent/secret"))
    assert g.local_clients == LOOPBACK_CLIENTS and "testclient" not in g.local_clients
    assert not g.host_ok("testserver")


def test_a_local_proxy_or_tunnel_is_not_the_owner():
    g = OwnerGate(Path("/nonexistent"))
    assert g.is_local("127.0.0.1", {})
    assert not g.is_local("127.0.0.1", {"x-forwarded-for": "203.0.113.9"})
    r = TestClient(main.app).get("/api/events", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


# --- CSRF and DNS rebinding -------------------------------------------------------------------------------
def test_cross_site_posts_from_the_owners_own_browser_are_refused():
    c = TestClient(main.app)
    assert c.post("/setup", data={"name": "Pwned", **HOURS}, headers=EVIL, follow_redirects=False).status_code == 403
    assert not main.profile.get().saved
    for path, data in (("/orders/import", {"csv": "day,supplier\n2026-10-07,Injected\n"}),
                       ("/connect", {"token": "attacker-token"}), ("/connect/forget", {}),
                       ("/deliveries", {"day": "2026-10-07", "supplier": "X"})):
        assert c.post(path, data=data, headers=EVIL, follow_redirects=False).status_code == 403, path
    assert c.post("/setup", data={"name": "X", **HOURS}, headers={"Sec-Fetch-Site": "cross-site"},
                  follow_redirects=False).status_code == 403
    assert c.post("/setup", data={"name": "X", **HOURS}, headers={"Origin": "null"},
                  follow_redirects=False).status_code == 403
    assert not main.source.pasted_token() and main.book.for_day(__import__("datetime").date(2026, 10, 7)) == []


def test_same_origin_and_scripted_posts_still_work():
    c = TestClient(main.app)
    ok = c.post("/setup", data={"name": "Mine", **HOURS}, headers={"Origin": "http://testserver"},
                follow_redirects=False)
    assert ok.status_code == 303 and main.profile.get().name == "Mine"
    assert c.post("/setup", data={"name": "Script", **HOURS}, follow_redirects=False).status_code == 303


def test_api_posts_need_a_non_form_content_type(monkeypatch):
    called = []
    monkeypatch.setattr(main, "agent_factory", lambda *a: called.append(1))
    c = TestClient(main.app)
    r = c.post("/api/ask", content='{"question": "x"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and not called
    assert c.post("/api/live/start", content="v=0").status_code == 415


def test_dns_rebinding_host_is_refused():
    c = TestClient(main.app)
    r = c.get("/setup", headers={"Host": "rebind.evil.example"})
    assert r.status_code == 400 and "Corner Bakery" not in r.text
    assert c.get("/", headers={"Host": "localhost:8000"}).status_code == 200
    assert c.get("/", headers={"Host": "192.168.1.20:8000"}).status_code == 200


def test_host_names():
    assert host_name("localhost:8000") == "localhost"
    assert host_name("[::1]:8000") == "::1"
    assert host_name("EXAMPLE.com") == "example.com"
    g = OwnerGate(Path("/x"), hosts=["shop.local"])
    assert g.host_ok("shop.local:8000") and g.host_ok("[::1]:1") and g.host_ok("10.0.0.5")
    assert not g.host_ok("evil.example") and not g.host_ok("") and not g.host_ok(None)


# --- pairing a phone --------------------------------------------------------------------------------------
def test_phone_pairs_with_the_code_from_setup():
    code, _ = main.gate.pairing_code()
    assert code in TestClient(main.app).get("/setup").text  # shown on the shop computer
    assert code not in remote().get("/setup").text
    phone = remote()
    r = phone.post("/pair", data={"code": code, "next": "/setup"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    cookie = r.headers["set-cookie"].lower()
    assert COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert phone.get("/").status_code == 200 and phone.get("/api/events").status_code == 200
    ok = phone.post("/setup", data={"name": "From my phone", **HOURS}, follow_redirects=False)
    assert ok.status_code == 303 and main.profile.get().name == "From my phone"
    # the code worked once; a second phone can't reuse it
    assert remote().post("/pair", data={"code": code}, follow_redirects=False).status_code == 403
    # "forget paired devices" signs the phone out
    assert TestClient(main.app).post("/pair/forget", follow_redirects=False).status_code == 303
    assert phone.get("/api/events").status_code == 403


def test_pairing_resists_guessing_and_open_redirects():
    g = OwnerGate(Path(__import__("tempfile").mkdtemp()) / "s")
    code, _ = g.pairing_code(now=0)
    for _ in range(5):
        assert g.pair("000000" if code != "000000" else "111111", now=1) is None
    assert g.pair(code, now=2) is None  # five wrong guesses replaced the code
    code, _ = g.pairing_code(now=3)
    assert g.pair(code, now=3 + 11 * 60) is None  # expired
    code, _ = g.pairing_code(now=10_000)
    cookie = g.pair(code, now=10_001)
    assert g.check_cookie(cookie, now=10_002) and not g.check_cookie(cookie + "0", now=10_002)
    assert not g.check_cookie(cookie, now=10_001 + 181 * 86400)
    code, _ = main.gate.pairing_code()
    r = remote().post("/pair", data={"code": code, "next": "//evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_secret_file_is_private(tmp_path):
    g = OwnerGate(tmp_path / "owner-secret")
    code, _ = g.pairing_code()
    g.pair(code)
    assert oct((tmp_path / "owner-secret").stat().st_mode & 0o777) == "0o600"
