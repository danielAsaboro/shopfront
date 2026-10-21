"""Replay signed Ring webhooks into a running Shopfront, for demos and tests.

The Developer Playground sends no webhooks (organisers, forum 45511; a self-built signed feeder is
allowed). This builds payloads in the shape of Ring's v1.1 notifications (API reference, Notifications;
`sub_type` from the 2026-10-05 release notes) and marks them as replays where nobody can strip the
mark: signed with a replay-only key derived from RING_HMAC_KEY, with `meta.shopfront_replay: true`
inside the signed body (app/ring/webhooks.py). Shopfront logs them as replayed, never as Ring's own.

    RING_HMAC_KEY=demo-key .venv/bin/python -m scripts.feed_webhook --sub-type package_delivery --at 10:15
    .venv/bin/python -m scripts.feed_webhook --type button_press --at now --port 8001
    .venv/bin/python -m scripts.feed_webhook --at 2026-10-21T10:15 --url https://shop.example/webhooks/ring

With live Ring connected, Shopfront only counts events from the shop's own doorbell: pass its ID with --device.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.ring.webhooks import REPLAY_MARKER, replay_key, sign

SUB_TYPES = ["motion", "human", "vehicle", "package_delivery", "other_motion"]
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8000  # README: uvicorn app.main:app --port 8000


def payload(kind: str, at: datetime, device: str, sub_type: str | None) -> dict:
    ms = int(at.timestamp() * 1000)
    attrs = {"source": device, "source_type": "devices", "timestamp": ms}
    if kind == "motion_detected" and sub_type:
        attrs["sub_type"] = sub_type
    return {"meta": {"version": "1.1", "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "request_id": f"replay-{uuid.uuid4().hex[:12]}", "account_id": "replay",
                     REPLAY_MARKER: True},
            "data": {"id": f"{device}_{kind}_{ms}", "type": kind, "attributes": attrs}}


def signed(body: dict, key: str) -> tuple[bytes, str]:
    """Raw bytes and X-Signature for a replay: signed with the replay-only key, never Ring's own."""
    raw = json.dumps(body).encode()
    return raw, sign(raw, replay_key(key))


def parse_at(value: str, now: datetime | None = None) -> datetime:
    """'now', 'HH:MM' (today) or 'YYYY-MM-DDTHH:MM', in shop local time. ValueError in plain words."""
    now = now or datetime.now(settings.tz)
    value = value.strip()
    if value == "now":
        return now
    try:
        if len(value) <= 5 and ":" in value:
            t = datetime.strptime(value, "%H:%M").time()
            at = datetime.combine(now.date(), t, tzinfo=settings.tz)
        else:
            at = datetime.fromisoformat(value)
            at = at.replace(tzinfo=settings.tz) if at.tzinfo is None else at.astimezone(settings.tz)
    except ValueError:
        raise ValueError(f"--at {value!r}: use now, HH:MM (today) or YYYY-MM-DDTHH:MM, shop local time") from None
    if at > now + timedelta(minutes=1):
        raise ValueError(f"--at {value!r} hasn't happened yet (it's {now:%Y-%m-%d %H:%M} at the shop)")
    return at


def send(url: str, kind: str, at: datetime, device: str, sub_type: str | None, key: str,
         transport: httpx.BaseTransport | None = None) -> httpx.Response:
    raw, signature = signed(payload(kind, at, device, sub_type), key)
    local = urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")
    with httpx.Client(transport=transport, trust_env=not local, timeout=10) as c:  # no proxy for localhost
        return c.post(url, content=raw, headers={"X-Signature": signature, "Content-Type": "application/json"})


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--type", default="motion_detected", choices=["motion_detected", "button_press", "device_online", "device_offline"])
    ap.add_argument("--sub-type", choices=SUB_TYPES, help="motion only; leave out to mimic Smart Alerts off")
    ap.add_argument("--at", default="now", help="now, HH:MM (today) or YYYY-MM-DDTHH:MM, shop local time")
    ap.add_argument("--device", default="replay-doorbell", help="the doorbell's Ring device ID (live mode needs the real one)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="the port Shopfront runs on (README: 8000)")
    ap.add_argument("--url", help="full webhook URL; overrides --host and --port")
    a = ap.parse_args(argv)
    key = os.getenv("RING_HMAC_KEY") or settings.ring_hmac_key
    if not key:
        ap.error("set RING_HMAC_KEY (the same value the server runs with, e.g. demo-key)")
    try:
        at = parse_at(a.at)
    except ValueError as exc:
        ap.error(str(exc))
    url = a.url or f"http://{a.host}:{a.port}/webhooks/ring"
    try:
        r = send(url, a.type, at, a.device, a.sub_type, key, transport)
    except httpx.HTTPError as exc:
        print(f"Couldn't reach Shopfront at {url} ({type(exc).__name__}). Is it running? "
              f"Start it with: .venv/bin/uvicorn app.main:app --port {a.port}", file=sys.stderr)
        return 1
    print(r.status_code, r.text)
    if r.status_code == 503:
        print("The server has no RING_HMAC_KEY; start it with the same key, e.g. RING_HMAC_KEY=demo-key.",
              file=sys.stderr)
    elif r.status_code == 401:
        print("Signature refused: the server runs with a different RING_HMAC_KEY.", file=sys.stderr)
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
