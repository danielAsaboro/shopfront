"""Who may use Shopfront: the shop owner, and nobody else except Ring's webhook.

- A request from this computer (loopback, not relayed by a proxy) is the owner. Running the app on
  localhost needs no sign-in, so the README demo works as is.
- Another device (the owner's phone on the shop Wi-Fi) pairs once: /setup on the shop computer shows
  a six-digit code, the phone enters it on /pair, and gets a signed HttpOnly, SameSite=Strict cookie.
  "Forget paired devices" on /setup rotates the signing secret, which signs every device out.
- Everyone else gets the pairing page (HTML) or 403 OWNER_ONLY (API). POST /webhooks/ring (HMAC), Ring's
  account-linking Token Exchange URL POST /ring/link/token (app/ring/link.py), the dispute packs' public
  signing key (/.well-known/shopfront-pack-key) and /healthz are the only public routes. The Account Link URL (/ring/link) is owner-only: the pairing page is its sign-in.
- SHOPFRONT_PUBLIC_DEMO=1 (the hosted demo, app/public_demo.py) uses PublicDemoGate instead: nobody is
  the owner, nobody can pair, and a request gets through only after the demo's own allowlist admitted it
  as a read of the made-up shop.

Browser-borne attacks on a localhost app are handled here too:
- DNS rebinding: the Host header must be localhost, an IP address, or a name in
  SHOPFRONT_ALLOWED_HOSTS. A rebinding page always arrives with its own hostname, never an IP.
- CSRF: a state-changing request whose Origin (or Referer, or Sec-Fetch-Site) shows another site is
  refused, and /api/* POSTs must carry a non-form content type (JSON, or the SDP/JPEG the live view
  sends), which a cross-site form or no-cors fetch can't send.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from app.files import write_private

COOKIE = "shopfront_owner"
COOKIE_DAYS = 180
CODE_MINUTES = 10
MAX_TRIES = 5  # wrong codes before the code is replaced

LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
LOCAL_HOSTS = frozenset({"localhost"})
# Ring's two server-to-server calls (HMAC-signed webhooks; the account-linking Token Exchange URL, whose code is
# worthless without our client secret) are public, and so is the dispute packs' public signing key, which a
# recipient checks a pack against; everything else is the owner's.
RING_SERVER_PATHS = frozenset({"/webhooks/ring", "/ring/link/token"})
PACK_KEY_PATH = "/.well-known/shopfront-pack-key"
PUBLIC_PATHS = RING_SERVER_PATHS | {"/healthz", "/pair", PACK_KEY_PATH}
API_BODY_TYPES = frozenset({"application/json", "application/sdp", "image/jpeg"})
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORWARDED = ("forwarded", "x-forwarded-for", "x-real-ip")


def host_name(host_header: str | None) -> str:
    """'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1'."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class OwnerGate:
    public_demo = False
    public_paths = PUBLIC_PATHS  # answered without the owner check

    def __init__(self, secret_file: Path, hosts=(), local_clients=()):
        self.secret_file = secret_file
        self.hosts = LOCAL_HOSTS | {h.strip().lower() for h in hosts if h.strip()}
        self.local_clients = LOOPBACK_CLIENTS | set(local_clients)
        self._lock = threading.Lock()
        self._code: str | None = None
        self._code_expires = 0.0
        self._tries = 0

    # --- requests ---------------------------------------------------------------------------------------
    def host_ok(self, host_header: str | None) -> bool:
        name = host_name(host_header)
        return bool(name) and (name in self.hosts or _is_ip(name))

    def is_local(self, client_host: str | None, headers) -> bool:
        # A proxy or tunnel on this machine relays other people's requests from loopback.
        return client_host in self.local_clients and not any(headers.get(h) for h in FORWARDED)

    def is_owner(self, client_host: str | None, headers, cookies) -> bool:
        return self.is_local(client_host, headers) or self.check_cookie(cookies.get(COOKIE))

    @staticmethod
    def cross_site(method: str, host_header: str | None, headers) -> str | None:
        """Why a state-changing request looks cross-site, or None."""
        if method not in UNSAFE:
            return None
        if headers.get("sec-fetch-site") == "cross-site":
            return "sent from another site"
        host = (host_header or "").lower()
        origin = headers.get("origin")
        if origin is not None:
            if origin == "null" or urlsplit(origin).netloc.lower() != host:
                return "sent from another site"
            return None
        referer = headers.get("referer")
        if referer and urlsplit(referer).netloc.lower() != host:
            return "sent from another site"
        return None  # no Origin and no Referer: not a browser form, so not CSRF

    @staticmethod
    def api_body_ok(content_type: str | None) -> bool:
        return (content_type or "").split(";")[0].strip().lower() in API_BODY_TYPES

    # --- pairing ----------------------------------------------------------------------------------------
    def _secret(self) -> bytes:
        try:
            return self.secret_file.read_bytes()
        except FileNotFoundError:
            pass
        with self._lock:
            if not self.secret_file.exists():
                write_private(self.secret_file, secrets.token_hex(32).encode())
        return self.secret_file.read_bytes()

    def forget_devices(self) -> None:
        """Rotate the signing secret: every paired device is signed out."""
        write_private(self.secret_file, secrets.token_hex(32).encode())

    def pairing_code(self, now: float | None = None) -> tuple[str, float]:
        now = time.time() if now is None else now
        with self._lock:
            if not self._code or now >= self._code_expires:
                self._new_code(now)
            return self._code, self._code_expires  # type: ignore[return-value]

    def _new_code(self, now: float) -> None:
        self._code = f"{secrets.randbelow(10**6):06d}"
        self._code_expires = now + CODE_MINUTES * 60
        self._tries = 0

    def pair(self, code: str, now: float | None = None) -> str | None:
        """A signed cookie value for a right, unexpired code; None otherwise. A code works once."""
        now = time.time() if now is None else now
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        with self._lock:
            if not self._code or now >= self._code_expires:
                return None
            if not hmac.compare_digest(code, self._code):
                self._tries += 1
                if self._tries >= MAX_TRIES:
                    self._code = None  # too many guesses: the owner gets a fresh code on /setup
                return None
            self._code = None
        return self._sign(secrets.token_hex(8), int(now))

    def _sign(self, device: str, issued: int) -> str:
        msg = f"{device}.{issued}"
        return f"{msg}.{hmac.new(self._secret(), msg.encode(), hashlib.sha256).hexdigest()}"

    def check_cookie(self, value: str | None, now: float | None = None) -> bool:
        try:
            device, issued, sig = (value or "").split(".")
            age = (time.time() if now is None else now) - int(issued)
        except ValueError:
            return False
        if not 0 <= age <= COOKIE_DAYS * 86400:
            return False
        return hmac.compare_digest(self._sign(device, int(issued)).rsplit(".", 1)[1], sig)


# --- the hosted public demo ---------------------------------------------------------------------------------
_admitted: ContextVar[bool] = ContextVar("public_demo_admitted", default=False)


@contextmanager
def admit_visitor():
    """The public demo's allowlist (app/public_demo.py) wraps one admitted read in this. Nothing a visitor
    sends can set it: it is a context variable, not a header or cookie."""
    token = _admitted.set(True)
    try:
        yield
    finally:
        _admitted.reset(token)


class PublicDemoGate(OwnerGate):
    """The gate when SHOPFRONT_PUBLIC_DEMO=1. There is no owner: no request is local, no cookie is valid,
    no code pairs, and nothing is signed, so there is no secret to keep. A request passes only inside
    admit_visitor(), which the demo's allowlist uses for reads of the made-up shop and nothing else;
    without that wrapper (app.main:app started with the flag set) every gated route is refused.

    The demo is served from a URL nobody knows in advance (a Lambda Function URL), and holds nothing
    private, so any Host is accepted; the cross-site check still applies to anything state-changing."""

    public_demo = True
    public_paths: frozenset[str] = frozenset()  # not even the webhook or /healthz: only what admit_visitor() lets in

    def __init__(self, hosts=()):
        super().__init__(Path("/nonexistent/public-demo-has-no-owner-secret"), hosts=hosts)
        self.local_clients = frozenset()

    def host_ok(self, host_header: str | None) -> bool:
        return bool(host_name(host_header))

    def is_local(self, client_host: str | None, headers) -> bool:
        return False

    def is_owner(self, client_host: str | None, headers, cookies) -> bool:
        return _admitted.get()

    def check_cookie(self, value: str | None, now: float | None = None) -> bool:
        return False

    def pair(self, code: str, now: float | None = None) -> str | None:
        return None

    def pairing_code(self, now: float | None = None) -> tuple[str, float]:
        raise PermissionError("the public demo has no owner to pair with")

    def forget_devices(self) -> None:
        raise PermissionError("the public demo has no paired devices")

    def _secret(self) -> bytes:
        raise PermissionError("the public demo signs nothing")
