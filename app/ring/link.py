"""Ring one-way account linking, as the API reference describes it
(https://developer.amazon.com/docs/ring/api-documentation.html, Authentication > One-Way Account Linking,
Users API, App Integrations, and Getting Started sections 1.3, 4.1, 5 and 6).

The flow, Ring-driven from start to finish:

1. Token Exchange URL (`POST /ring/link/token`, public). After the owner installs Shopfront from the Ring
   AppStore, Ring sends an authorization code to this URL, backend to backend (section 5.1). The code lives
   60 seconds (section 5.3), so it is redeemed at once: `POST https://oauth.ring.com/oauth/token` with
   `grant_type=authorization_code`, `code`, `client_id`, `client_secret` (API reference, Authentication
   Endpoints > Exchange Authorization Code). Then `GET /v1/users/me` gives the Account ID (`data.id`), and the
   token pair is parked as "unclaimed" under it (Users API > Store Account ID at Token Receipt).
2. Account Link URL (`/ring/link?nonce=...&time=...`). Ring redirects the owner's browser here (One-Way Account
   Linking, Step 1). The page is a sign-in, never an automatic verification (Step 2, "Important"): in
   Shopfront the sign-in is the owner gate (app/owner.py), the shop computer itself or a device paired with
   its code, and the owner then presses "Link". `time` must be no older than 600 seconds and not in the
   future (Step 2; Nonce Technical Reference: "Validation window 600 seconds").
3. Matching (Step 4): for each unclaimed token, nonce' = URL-safe Base64 without padding of
   HMAC-SHA256(RING_HMAC_KEY, "<time>:<account_id>"), compared in constant time. The same key signs webhooks
   in hex; the two encodings are kept apart (Nonce Technical Reference, "Same key, different encoding").
4. Claim (Steps 5 and 7, App Integrations): `POST /v1/accounts/me/app-integrations` with the matched token,
   `{"account_identifier": <masked>, "nonce": <nonce>}` (status -> awaiting), then the mandatory
   `PATCH /v1/accounts/me/app-integrations` `{"status": "completed"}`.
5. The claimed refresh token is written, 0600 and atomically, to the file RingAuth already reads and rotates
   (`.data/ring-refresh-token.json`, app/ring/api.py): every later refresh saves Ring's new refresh token
   there (Refresh Tokens: "they are updated with each exchange").
6. App Homepage URL (`/ring/link/home`): the "custom app configuration page for post-linking setup"
   (section 4.1). It is /setup's Ring step.

Unlinking: one-way apps get 403 on `DELETE /v1/accounts/me/app-integrations` ("their users remove the
integration from Ring AppStore"), so Shopfront never calls it. "Forget" deletes the tokens here, and a signed
`app_integration_removed` webhook for the linked account does the same (Notifications > App Integration
Removed: "Clean up immediately ... remove cached tokens").

What the docs don't say, and so isn't verified: the shape of Ring's request to the Token Exchange URL (the
docs say only that Ring "POSTs the auth code"). Shopfront reads `code` from a JSON body, a form body or the
query string, and answers 200 once the code is redeemed (section 7.1, Step 4: "Partner Backend returns 200
response"). No real Ring AppStore install has been run against it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

import httpx

from app.files import write_private
from app.ring.api import API_BASE, TOKEN_URL

WINDOW_SECONDS = 600  # One-Way Account Linking > Nonce Technical Reference: validation window
UNCLAIMED_SECONDS = 3600  # an unclaimed token nobody linked within the hour is dropped
MAX_UNCLAIMED = 20  # the Token Exchange URL is public: never let it fill the disk
INTEGRATIONS = "/v1/accounts/me/app-integrations"


class LinkError(Exception):
    """A step of the link failed; `step` names which, `detail` is safe to show the owner (no tokens)."""

    def __init__(self, step: str, detail: str, http_status: int | None = None, request_id: str | None = None):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail
        self.http_status = http_status
        self.request_id = request_id


def compute_nonce(time_param: str, account_id: str, hmac_key: str) -> str:
    """One-Way Account Linking, Step 4: URL-safe Base64 (no '=') of HMAC-SHA256(key, "<time>:<account_id>")."""
    mac = hmac.new(hmac_key.encode(), f"{time_param}:{account_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def freshness(time_param: str | None, now_ms: int | None = None) -> str | None:
    """Why a redirect's `time` is unusable (Step 2's freshness check), or None when it is fresh."""
    try:
        sent = int(str(time_param))
    except (TypeError, ValueError):
        return "the link from Ring has no valid time"
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    age = (now_ms - sent) / 1000
    if age < 0:
        return "the link from Ring is dated in the future"
    if age > WINDOW_SECONDS:
        return "the link from Ring is more than 10 minutes old; start again from the Ring app"
    return None


def mask(identifier: str) -> str:
    """The masked partner identifier Ring shows the Ring user (App Integrations > POST, `account_identifier`:
    "Obfuscated partner account identifier ... e.g. masked email u***r@partner.example.com")."""
    s = " ".join((identifier or "").split())
    if not s:
        return ""
    if "@" in s:
        user, _, domain = s.partition("@")
        return f"{user[:1]}***{user[-1:] if len(user) > 1 else ''}@{domain}"
    return f"{s[:1]}***{s[-1:] if len(s) > 1 else ''}"


class RingLink:
    """The unclaimed-token pool and the one linked Ring account, kept in the data folder (all files 0600)."""

    def __init__(self, data_dir: Path, *, client_id: str, client_secret: str, hmac_key: str,
                 http: httpx.Client | None = None, api_base: str = API_BASE, token_url: str = TOKEN_URL):
        self.unclaimed_file = data_dir / "ring-link-unclaimed.json"
        self.link_file = data_dir / "ring-link.json"
        self.token_file = data_dir / "ring-refresh-token.json"  # the file RingAuth reads and rotates
        self.client_id, self.client_secret, self.hmac_key = client_id, client_secret, hmac_key
        self.http = http or httpx.Client(timeout=20)
        self.api_base, self.token_url = api_base, token_url
        self._lock = threading.RLock()
        self._form_secret = secrets.token_bytes(32)

    # --- state ------------------------------------------------------------------------------------------
    def configured(self) -> str | None:
        """What's missing for linking (the three credentials Ring issues, Getting Started 1.2), or None."""
        missing = [n for n, v in (("RING_CLIENT_ID", self.client_id), ("RING_CLIENT_SECRET", self.client_secret),
                                  ("RING_HMAC_KEY", self.hmac_key)) if not v]
        return ", ".join(missing) or None

    def linked(self) -> dict | None:
        """{account_id, linked_at, status, account_identifier, ...} of the linked account, or None."""
        try:
            row = json.loads(self.link_file.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(row, dict) or not row.get("account_id") or not self.token_file.exists():
            return None
        return row

    def refresh_token(self) -> str:
        try:
            return json.loads(self.token_file.read_text()).get("refresh_token") or ""
        except (OSError, ValueError, AttributeError):
            return ""

    def _unclaimed(self, now: float | None = None) -> dict[str, dict]:
        try:
            rows = json.loads(self.unclaimed_file.read_text())
        except (OSError, ValueError):
            return {}
        now = time.time() if now is None else now
        return {k: v for k, v in rows.items() if isinstance(v, dict) and now - v.get("received_at", 0) < UNCLAIMED_SECONDS} \
            if isinstance(rows, dict) else {}

    def unclaimed_count(self) -> int:
        return len(self._unclaimed())

    # --- 1. Token Exchange URL --------------------------------------------------------------------------
    def exchange(self, code: str) -> str:
        """Redeem Ring's authorization code, learn the Account ID, park the tokens unclaimed. Returns the
        Account ID. Raises LinkError."""
        if self.configured():
            raise LinkError("config", f"set {self.configured()}")
        code = (code or "").strip()
        if not code or len(code) > 2048:
            raise LinkError("token_exchange", "no authorization code")
        try:
            resp = self.http.post(self.token_url, data={
                "grant_type": "authorization_code", "client_id": self.client_id, "code": code,
                "client_secret": self.client_secret})
        except httpx.HTTPError as exc:
            raise LinkError("token_exchange", f"oauth.ring.com unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise LinkError("token_exchange", "oauth.ring.com refused the code", resp.status_code)
        try:
            body = resp.json()
            access, refresh = body["access_token"], body["refresh_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LinkError("token_exchange", "token response without access_token and refresh_token") from exc
        account_id = self._account_id(access)
        with self._lock:
            pool = self._unclaimed()
            pool[account_id] = {"access_token": access, "refresh_token": refresh, "received_at": time.time(),
                                "expires_in": body.get("expires_in"), "scope": body.get("scope")}
            if len(pool) > MAX_UNCLAIMED:  # keep the newest
                pool = dict(sorted(pool.items(), key=lambda kv: kv[1]["received_at"])[-MAX_UNCLAIMED:])
            write_private(self.unclaimed_file, json.dumps(pool))
        return account_id

    def _account_id(self, access: str) -> str:
        """Users API > GET /v1/users/me: `data.id` is the Account ID."""
        try:
            resp = self.http.get(f"{self.api_base}/v1/users/me", headers={"Authorization": f"Bearer {access}"})
        except httpx.HTTPError as exc:
            raise LinkError("users_me", f"api.amazonvision.com unreachable: {type(exc).__name__}") from exc
        rid = resp.headers.get("x-request-id")
        if resp.status_code != 200:
            raise LinkError("users_me", "Ring didn't return the account", resp.status_code, rid)
        try:
            account_id = resp.json()["data"]["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LinkError("users_me", "users/me without data.id", 200, rid) from exc
        if not isinstance(account_id, str) or not account_id:
            raise LinkError("users_me", "users/me without data.id", 200, rid)
        return account_id

    # --- 2./3. Account Link URL ------------------------------------------------------------------------
    def form_token(self, nonce: str, time_param: str) -> str:
        """Ties the confirm form to this redirect and this running app (on top of the owner gate's
        same-site check), so a link can't be confirmed by a form another site posts."""
        return hmac.new(self._form_secret, f"{nonce}|{time_param}".encode(), hashlib.sha256).hexdigest()

    def form_ok(self, nonce: str, time_param: str, token: str) -> bool:
        return hmac.compare_digest(self.form_token(nonce, time_param), token or "")

    def match(self, nonce: str, time_param: str) -> tuple[str, dict] | None:
        """Step 4: the unclaimed token whose Account ID recomputes to this nonce, or None."""
        for account_id, rec in self._unclaimed().items():
            if hmac.compare_digest(compute_nonce(time_param, account_id, self.hmac_key), nonce or ""):
                return account_id, rec
        return None

    # --- 4./5. claim -----------------------------------------------------------------------------------
    def claim(self, nonce: str, time_param: str, identifier: str, now_ms: int | None = None) -> dict:
        """Steps 2 to 7 after the owner signed in and pressed Link. Returns the link record. Raises LinkError."""
        if self.configured():
            raise LinkError("config", f"set {self.configured()}")
        stale = freshness(time_param, now_ms)
        if stale:
            raise LinkError("nonce", stale)
        with self._lock:
            hit = self.match(nonce, time_param)
            if not hit:
                raise LinkError("nonce", "this link doesn't match a Ring sign-in Shopfront received; "
                                         "start again from the Ring app")
            account_id, rec = hit
            masked = mask(identifier)
            headers = {"Authorization": f"Bearer {rec['access_token']}", "Content-Type": "application/json"}
            post = self._integrations("POST", headers, {"account_identifier": masked, "nonce": nonce})
            patch = self._integrations("PATCH", headers, {"status": "completed"})
            try:
                status = patch.json()["data"]["attributes"]["status"]
            except (ValueError, KeyError, TypeError):
                status = "completed"
            write_private(self.token_file, json.dumps({"refresh_token": rec["refresh_token"]}))
            row = {"account_id": account_id, "account_identifier": masked, "status": status,
                   "linked_at": time.time(), "post_request_id": post.headers.get("x-request-id"),
                   "patch_request_id": patch.headers.get("x-request-id")}
            write_private(self.link_file, json.dumps(row))
            pool = self._unclaimed()
            pool.pop(account_id, None)
            write_private(self.unclaimed_file, json.dumps(pool))
            return row

    def _integrations(self, method: str, headers: dict, body: dict) -> httpx.Response:
        step = "app_integrations_" + method.lower()
        try:
            resp = self.http.request(method, f"{self.api_base}{INTEGRATIONS}", headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LinkError(step, f"api.amazonvision.com unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            title = ""
            try:
                errs = resp.json().get("errors")
                if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                    title = errs[0].get("title") or errs[0].get("detail") or ""
            except (ValueError, AttributeError):
                pass
            raise LinkError(step, f"Ring answered {resp.status_code}{': ' + str(title)[:80] if title else ''}",
                            resp.status_code, resp.headers.get("x-request-id"))
        return resp

    # --- unlinking ---------------------------------------------------------------------------------------
    def forget(self) -> None:
        """Delete the linked account's tokens here. Ring's side is removed from the Ring AppStore."""
        with self._lock:
            self.link_file.unlink(missing_ok=True)
            self.token_file.unlink(missing_ok=True)

    def on_webhook(self, kind: str, account_id: str | None) -> bool:
        """app_integration_added / app_integration_removed (Notifications) for the linked account. Returns True
        when it changed something."""
        row = self.linked()
        if not row or not account_id or account_id != row["account_id"]:
            return False
        if kind == "app_integration_removed":
            self.forget()
            return True
        if kind == "app_integration_added":
            row["ring_confirmed_at"] = time.time()
            write_private(self.link_file, json.dumps(row))
            return True
        return False
