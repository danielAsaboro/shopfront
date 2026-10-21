"""Test isolation: no network, no .env, nothing written outside pytest's tmp dirs.

The environment is pinned *before* `app` is imported, because `app.config.settings` and the
module-level stores in `app.main` are built at import time. Without this, a developer's `.env`
(CLASSIFIER=auto + AWS credentials) made the import build a live Bedrock classifier and tests
wrote `.data/bedrock-calls.jsonl` into the repo.
"""

import ipaddress
import os
import socket
import tempfile

_SCRATCH = tempfile.mkdtemp(prefix="shopfront-tests-")
os.environ.update({
    "SHOPFRONT_DATA_DIR": os.path.join(_SCRATCH, "data"),
    "CLASSIFIER": "rules",
    "RING_MODE": "synthetic",
    "RING_ACCESS_TOKEN": "",
    "RING_REFRESH_TOKEN": "",
    "RING_DEVICE_ID": "",
    "RING_ACCOUNT_ID": "",
    "RING_HMAC_KEY": "",
    "SHOPFRONT_ALLOWED_HOSTS": "",
    "SHOPFRONT_WEBHOOK_STORE": "",
    "SHOP_HOURS": "8-18,8-18,8-18,8-18,8-19,9-16,closed",
    "SHOP_NAME": "Corner Bakery",
    "SHOP_TIMEZONE": "America/Los_Angeles",
    "HYPOTHESIS_STORAGE_DIRECTORY": os.path.join(_SCRATCH, "hypothesis"),
})

import pytest  # noqa: E402
from hypothesis import settings as hypothesis_settings  # noqa: E402

import app.main as main  # noqa: E402
from app.bedrock_budget import LabelRequests  # noqa: E402
from app.cache import ClassificationCache  # noqa: E402
from app.checks import CheckBook  # noqa: E402
from app.classify.rules import RulesClassifier  # noqa: E402
from app.deliveries import DeliveryBook, FrameStore  # noqa: E402
from app.live import LiveLog  # noqa: E402
from app.owner import OwnerGate  # noqa: E402
from app.pack_signing import LocalSigner  # noqa: E402
from app.ring import RingSource  # noqa: E402
from app.ring.link import RingLink  # noqa: E402
from app.ring.status import StatusLog  # noqa: E402
from app.ring.webhooks import WebhookLog  # noqa: E402
from app.shop import ShopProfile  # noqa: E402

hypothesis_settings.register_profile("isolated", database=None)  # no .hypothesis/ in the repo
hypothesis_settings.load_profile("isolated")

# --- no outbound network: only loopback and unix sockets ------------------------------------------------
_real_connect, _real_connect_ex, _real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


class NetworkBlocked(RuntimeError):
    pass


def _is_local(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _check(sock, address):
    if sock.family == getattr(socket, "AF_UNIX", None):
        return
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local(host):
        raise NetworkBlocked(f"tests may not open network connections (tried {address!r})")


def _connect(self, address):
    _check(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(self, address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *args, **kw):
    if not _is_local(host.decode() if isinstance(host, bytes) else host):
        raise NetworkBlocked(f"tests may not resolve {host!r}")
    return _real_getaddrinfo(host, *args, **kw)


socket.socket.connect = _connect  # type: ignore[method-assign]
socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
socket.getaddrinfo = _getaddrinfo


@pytest.fixture(autouse=True)
def no_local_data(tmp_path, monkeypatch):
    """Keep every test off this machine's .data: a pasted Ring token there switches the app to live
    Ring, and a saved profile, delivery list or webhook log would change what the tests see."""
    d = tmp_path / "data"
    monkeypatch.setattr(main, "DATA", d)
    link = RingLink(d, client_id="", client_secret="", hmac_key="")
    monkeypatch.setattr(main, "ring_link", link)
    monkeypatch.setattr(main, "source", RingSource(main.settings, d / "ring-access-token", link))
    monkeypatch.setattr(main, "profile", ShopProfile(d / "shop.json", main.settings.shop_name, main.settings.shop_hours))
    monkeypatch.setattr(main, "book", DeliveryBook(d / "expected-deliveries.json"))
    monkeypatch.setattr(main, "checks", CheckBook(d / "checks.json"))
    monkeypatch.setattr(main, "frames", FrameStore(d))
    monkeypatch.setattr(main, "cache", ClassificationCache(d / "classifications.json"))
    monkeypatch.setattr(main, "webhook_log", WebhookLog(d / "ring-webhooks.jsonl"))
    monkeypatch.setattr(main, "status_log", StatusLog(d / "ring-status.jsonl"))
    monkeypatch.setattr(main, "live", LiveLog(d, main.settings.tz))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "pack_signer", LocalSigner(d / "pack-signing-key.pem"))
    monkeypatch.setattr(main, "label_requests", LabelRequests(d / "bedrock-requests.json"))
    # Starlette's TestClient connects as client "testclient" to host "testserver". Production never
    # treats either as this machine, so only the tests add them.
    monkeypatch.setattr(main, "gate", OwnerGate(d / "owner-secret", hosts={"testserver"},
                                               local_clients={"testclient"}))
