"""Ring one-way account linking (app/ring/link.py) against an httpx MockTransport shaped like the API
reference's examples (Authentication > One-Way Account Linking, Users API, App Integrations).

Local tests of our side of the flow. They prove nothing about a real Ring AppStore install."""

import base64
import hashlib
import hmac
import json
import os
import stat
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.ring import RingSource
from app.ring.api import RingApiClient
from app.ring.link import LinkError, RingLink, compute_nonce, freshness, mask

KEY = "partner-hmac-key"
ACCOUNT = "ava1.ring.account.XXXYYY"
OTHER = "ava1.ring.account.OTHER"


class FakeRing:
    """oauth.ring.com and api.amazonvision.com, as documented."""

    def __init__(self, accounts=(ACCOUNT,), post_status=200):
        self.calls: list[tuple[str, str, dict | str]] = []
        self.accounts = list(accounts)
        self.post_status = post_status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == "https://oauth.ring.com/oauth/token":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            self.calls.append(("POST", "token", form))
            if form.get("code") == "bad":
                return httpx.Response(400, json={"error": "invalid_grant"})
            n = self.accounts.pop(0)
            return httpx.Response(200, json={"access_token": f"at-{n}", "refresh_token": f"rt-{n}", "scope": "s",
                                             "expires_in": 14400, "token_type": "Bearer"})
        auth = req.headers.get("authorization", "")
        if url.endswith("/v1/users/me"):
            self.calls.append(("GET", "users/me", auth))
            acct = auth.removeprefix("Bearer at-")
            return httpx.Response(200, headers={"x-request-id": "rid-me"},
                                  json={"data": {"type": "users", "id": acct, "attributes": {}}})
        if url.endswith("/v1/accounts/me/app-integrations"):
            body = json.loads(req.content)
            self.calls.append((req.method, "app-integrations", {"auth": auth, **body}))
            if req.method == "POST" and self.post_status != 200:
                return httpx.Response(self.post_status, json={"errors": [{"status": "400", "title": "Invalid Nonce"}]})
            status = "awaiting" if req.method == "POST" else body.get("status")
            return httpx.Response(200, headers={"x-request-id": f"rid-{req.method}"}, json={
                "data": {"type": "app-integrations", "id": "ava1.ring.client.cid", "attributes": {"status": status}},
                "meta": {"time": "2026-07-21T07:07:15Z"}})
        return httpx.Response(404)


def make_link(tmp_path, fake=None, **kw):
    fake = fake or FakeRing()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return RingLink(tmp_path, client_id="cid", client_secret="secret", hmac_key=KEY, http=http, **kw), fake


def now_ms():
    return int(time.time() * 1000)


# --- the nonce, exactly as the API reference computes it --------------------------------------------------
def test_nonce_matches_the_documented_algorithm():
    t = "1771130906289"
    want = base64.urlsafe_b64encode(hmac.new(KEY.encode(), f"{t}:{ACCOUNT}".encode(), hashlib.sha256).digest())
    got = compute_nonce(t, ACCOUNT, KEY)
    assert got == want.rstrip(b"=").decode()
    assert "=" not in got and "+" not in got and "/" not in got  # URL-safe, no padding
    # not the webhook's hex encoding of the same key
    assert got != hmac.new(KEY.encode(), f"{t}:{ACCOUNT}".encode(), hashlib.sha256).hexdigest()


def test_freshness_window_is_600_seconds_and_never_the_future():
    n = now_ms()
    assert freshness(str(n - 599_000), n) is None
    assert "10 minutes" in freshness(str(n - 601_000), n)
    assert "future" in freshness(str(n + 5_000), n)
    assert freshness("abc", n) and freshness(None, n)


def test_mask():
    assert mask("user@partner.example.com") == "u***r@partner.example.com"
    assert mask("Corner Bakery") == "C***y"
    assert mask("") == ""


# --- the flow ------------------------------------------------------------------------------------------
def test_exchange_then_claim_runs_the_documented_calls_in_order(tmp_path):
    link, fake = make_link(tmp_path)
    assert link.exchange("auth-code") == ACCOUNT
    assert fake.calls[0] == ("POST", "token", {"grant_type": "authorization_code", "client_id": "cid",
                                                "code": "auth-code", "client_secret": "secret"})
    assert fake.calls[1] == ("GET", "users/me", f"Bearer at-{ACCOUNT}")
    assert link.unclaimed_count() == 1 and link.linked() is None

    t = str(now_ms())
    nonce = compute_nonce(t, ACCOUNT, KEY)
    row = link.claim(nonce, t, "Corner Bakery")
    post, patch = fake.calls[2], fake.calls[3]
    assert post == ("POST", "app-integrations", {"auth": f"Bearer at-{ACCOUNT}", "account_identifier": "C***y",
                                                 "nonce": nonce})
    assert patch == ("PATCH", "app-integrations", {"auth": f"Bearer at-{ACCOUNT}", "status": "completed"})
    assert row["account_id"] == ACCOUNT and row["status"] == "completed"
    assert row["post_request_id"] == "rid-POST" and row["patch_request_id"] == "rid-PATCH"
    assert link.linked()["account_id"] == ACCOUNT and link.unclaimed_count() == 0
    # the refresh token lands in the file RingAuth rotates, 0600; the link record holds no token
    assert json.loads(link.token_file.read_text()) == {"refresh_token": f"rt-{ACCOUNT}"}
    for f in (link.token_file, link.link_file, link.unclaimed_file):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert "rt-" not in link.link_file.read_text() and "at-" not in link.link_file.read_text()


def test_nonce_picks_the_right_unclaimed_token(tmp_path):
    link, fake = make_link(tmp_path, FakeRing(accounts=(OTHER, ACCOUNT)))
    link.exchange("c1")
    link.exchange("c2")
    t = str(now_ms())
    link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    assert fake.calls[-2][2]["auth"] == f"Bearer at-{ACCOUNT}"
    assert link.linked()["account_id"] == ACCOUNT and link.unclaimed_count() == 1


@pytest.mark.parametrize("why", ["forged", "stale", "wrong_time"])
def test_a_bad_nonce_claims_nothing_and_calls_nothing(tmp_path, why):
    link, fake = make_link(tmp_path)
    link.exchange("c")
    t = str(now_ms())
    nonce = {"forged": compute_nonce(t, ACCOUNT, "not-the-key"), "stale": compute_nonce(t, ACCOUNT, KEY),
             "wrong_time": compute_nonce(t, ACCOUNT, KEY)}[why]
    claim_t = str(int(t) - 700_000) if why == "stale" else str(int(t) - 1) if why == "wrong_time" else t
    with pytest.raises(LinkError) as e:
        link.claim(nonce, claim_t, "Shop")
    assert e.value.step == "nonce"
    assert [c for c in fake.calls if c[1] == "app-integrations"] == []
    assert link.linked() is None and not link.token_file.exists()


def test_ring_refusing_the_post_keeps_the_token_unclaimed(tmp_path):
    link, fake = make_link(tmp_path, FakeRing(post_status=400))
    link.exchange("c")
    t = str(now_ms())
    with pytest.raises(LinkError) as e:
        link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    assert e.value.step == "app_integrations_post" and "Invalid Nonce" in e.value.detail
    assert not [c for c in fake.calls if c[0] == "PATCH"]  # never PATCH after a failed POST
    assert link.linked() is None and link.unclaimed_count() == 1


def test_exchange_refused_and_unconfigured(tmp_path):
    link, _ = make_link(tmp_path)
    with pytest.raises(LinkError) as e:
        link.exchange("bad")
    assert e.value.step == "token_exchange" and link.unclaimed_count() == 0
    bare = RingLink(tmp_path, client_id="", client_secret="", hmac_key="")
    assert bare.configured() == "RING_CLIENT_ID, RING_CLIENT_SECRET, RING_HMAC_KEY"
    with pytest.raises(LinkError):
        bare.exchange("c")


def test_unclaimed_tokens_expire(tmp_path):
    link, _ = make_link(tmp_path)
    link.exchange("c")
    pool = json.loads(link.unclaimed_file.read_text())
    pool[ACCOUNT]["received_at"] -= 4000
    link.unclaimed_file.write_text(json.dumps(pool))
    assert link.unclaimed_count() == 0


def test_removed_webhook_forgets_only_the_linked_account(tmp_path):
    link, _ = make_link(tmp_path)
    link.exchange("c")
    t = str(now_ms())
    link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    assert not link.on_webhook("app_integration_removed", OTHER) and link.linked()
    assert link.on_webhook("app_integration_added", ACCOUNT) and link.linked()["ring_confirmed_at"]
    assert link.on_webhook("app_integration_removed", ACCOUNT)
    assert link.linked() is None and not link.token_file.exists()


# --- the linked account drives the Ring client, and rotation doesn't rebuild it ---------------------------
def test_linked_account_is_live_refresh_mode_and_survives_rotation(tmp_path):
    link, _ = make_link(tmp_path)
    link.exchange("c")
    t = str(now_ms())
    link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    s = replace(main.settings, ring_mode="synthetic", ring_client_id="cid", ring_client_secret="secret",
                ring_hmac_key=KEY)
    src = RingSource(s, tmp_path / "ring-access-token", link)
    eff = src.effective_settings()
    assert eff.ring_mode == "live" and eff.ring_refresh_token == f"rt-{ACCOUNT}" and not eff.ring_access_token
    ring = src.get()
    assert isinstance(ring, RingApiClient) and ring.auth.mode == "refresh"
    link.token_file.write_text(json.dumps({"refresh_token": "rotated"}))  # RingAuth saved Ring's new one
    assert src.get() is ring
    src.save_token("playground")  # a pasted Playground token still wins
    assert src.effective_settings().ring_access_token == "playground"


# --- the HTTP routes ------------------------------------------------------------------------------------
@pytest.fixture
def linked_app(tmp_path, monkeypatch):
    link, fake = make_link(tmp_path / "link")
    monkeypatch.setattr(main, "ring_link", link)
    s = replace(main.settings, ring_client_id="cid", ring_client_secret="secret", ring_hmac_key=KEY)
    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr(main, "source", RingSource(s, tmp_path / "link" / "ring-access-token", link))
    return link, fake


@pytest.mark.parametrize("how", ["json", "form", "query"])
def test_token_exchange_url_is_public_and_reads_the_code(linked_app, how):
    link, fake = linked_app
    c = TestClient(main.app, client=("198.51.100.7", 443))  # Ring's backend: not the owner
    kw = {"json": {"code": "auth-code"}} if how == "json" else {"data": {"code": "auth-code"}} if how == "form" \
        else {"params": {"code": "auth-code"}}
    r = c.post("/ring/link/token", headers={"Host": "shop.tunnel.example"}, **kw)
    assert r.status_code == 200 and r.json() == {"status": "OK"}
    assert "token" not in r.text.replace("status", "")  # no token material in the answer
    assert link.unclaimed_count() == 1 and fake.calls[0][2]["code"] == "auth-code"


def test_token_exchange_without_a_code_or_with_a_refused_one(linked_app):
    c = TestClient(main.app, client=("198.51.100.7", 443))
    assert c.post("/ring/link/token", json={}).status_code == 400
    r = c.post("/ring/link/token", json={"code": "bad"})
    assert r.status_code == 502 and r.json()["step"] == "token_exchange"


def test_account_link_url_needs_the_owner_and_a_button_press(linked_app):
    link, fake = linked_app
    link.exchange("auth-code")
    t = str(now_ms())
    nonce = compute_nonce(t, ACCOUNT, KEY)
    outsider = TestClient(main.app, client=("203.0.113.9", 5000))
    r = outsider.get("/ring/link", params={"nonce": nonce, "time": t})
    assert r.status_code == 403 and "pair" in r.text.lower()  # the sign-in comes first
    assert outsider.post("/ring/link", data={"nonce": nonce, "time": t}).status_code == 403

    owner = TestClient(main.app)
    page = owner.get("/ring/link", params={"nonce": nonce, "time": t})
    assert page.status_code == 200 and "Link Ring to Shopfront" in page.text
    assert not [x for x in fake.calls if x[1] == "app-integrations"]  # GET never verifies or claims
    token = link.form_token(nonce, t)
    assert token in page.text

    # a cross-site form, and one without Shopfront's own form token, are refused
    evil = owner.post("/ring/link", data={"nonce": nonce, "time": t, "form_token": token},
                      headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert owner.post("/ring/link", data={"nonce": nonce, "time": t, "form_token": "x"}).status_code == 403
    assert link.linked() is None

    done = owner.post("/ring/link", data={"nonce": nonce, "time": t, "form_token": token})
    assert done.status_code == 200 and "Linked." in done.text
    assert link.linked()["account_id"] == ACCOUNT
    assert [x[0] for x in fake.calls if x[1] == "app-integrations"] == ["POST", "PATCH"]


def test_stale_link_says_so(linked_app):
    t = str(now_ms() - 900_000)
    r = TestClient(main.app).get("/ring/link", params={"nonce": "n", "time": t})
    assert r.status_code == 400 and "more than 10 minutes old" in r.text and "Link Ring to Shopfront" not in r.text


def test_status_page_lists_the_urls_to_register(linked_app, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, public_url="https://shop.example.com"))
    r = TestClient(main.app).get("/ring/link")
    for url in ("https://shop.example.com/ring/link/token", "https://shop.example.com/ring/link",
                "https://shop.example.com/webhooks/ring", "https://shop.example.com/ring/link/home"):
        assert url in r.text
    assert TestClient(main.app).get("/ring/link/home", follow_redirects=False).headers["location"] == "/setup#ring"


def test_setup_offers_linking_next_to_the_playground_token():
    r = TestClient(main.app).get("/setup")
    assert 'href="/ring/link"' in r.text and 'href="/connect"' in r.text


def test_signed_removal_webhook_unlinks_but_a_replay_cannot(linked_app):
    from app.ring.webhooks import replay_key, sign
    link, _ = linked_app
    link.exchange("c")
    t = str(now_ms())
    link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    body = lambda rid: json.dumps({"meta": {"version": "1.1", "time": "2026-07-06T11:11:00Z", "request_id": rid,  # noqa: E731
                                            "account_id": ACCOUNT},
                                   "data": {"id": "e", "type": "app_integration_removed",
                                            "attributes": {"source": ACCOUNT, "source_type": "users",
                                                           "timestamp": 1699457230000}}}).encode()
    c = TestClient(main.app, client=("198.51.100.7", 443))
    raw = body("r1")
    assert c.post("/webhooks/ring", content=raw,
                  headers={"X-Signature": sign(raw, replay_key(KEY)), "Content-Type": "application/json"}).status_code == 200
    assert link.linked()  # a replay signed with the feeder's key can't unlink
    raw = body("r2")
    assert c.post("/webhooks/ring", content=raw,
                  headers={"X-Signature": sign(raw, KEY), "Content-Type": "application/json"}).status_code == 200
    assert link.linked() is None and not link.token_file.exists()


def test_forget_button(linked_app):
    link, _ = linked_app
    link.exchange("c")
    t = str(now_ms())
    link.claim(compute_nonce(t, ACCOUNT, KEY), t, "Shop")
    r = TestClient(main.app).post("/ring/link/forget", follow_redirects=False)
    assert r.status_code == 303 and link.linked() is None
