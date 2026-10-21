"""Replay signed Ring webhooks into a running Shopfront, for demos and tests.

The Developer Playground sends no webhooks (organisers, forum 45511; a self-built signed feeder is
allowed). This builds payloads in the shape of Ring's v1.1 notifications (API reference, Webhooks;
`sub_type` from the 2026-10-05 release notes), signs them with RING_HMAC_KEY, and posts them with
`X-Shopfront-Replay: 1`, so Shopfront logs them as replayed and never as Ring's own.

    RING_HMAC_KEY=... .venv/bin/python -m scripts.feed_webhook --sub-type package_delivery --at 2026-10-21T10:15
    .venv/bin/python -m scripts.feed_webhook --type button_press --at now
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.ring.webhooks import REPLAY_MARKER, replay_key, sign

SUB_TYPES = ["motion", "human", "vehicle", "package_delivery", "other_motion"]


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--type", default="motion_detected", choices=["motion_detected", "button_press"])
    ap.add_argument("--sub-type", choices=SUB_TYPES, help="motion only; leave out to mimic Smart Alerts off")
    ap.add_argument("--at", default="now", help="shop local time, YYYY-MM-DDTHH:MM, or 'now'")
    ap.add_argument("--device", default="replay-doorbell")
    ap.add_argument("--url", default="http://127.0.0.1:8000/webhooks/ring")
    a = ap.parse_args()
    key = os.getenv("RING_HMAC_KEY") or settings.ring_hmac_key
    if not key:
        raise SystemExit("set RING_HMAC_KEY (the same value the server runs with)")
    at = datetime.now(settings.tz) if a.at == "now" else datetime.fromisoformat(a.at).replace(tzinfo=settings.tz)
    raw, signature = signed(payload(a.type, at, a.device, a.sub_type), key)
    r = httpx.post(a.url, content=raw, headers={"X-Signature": signature, "X-Shopfront-Replay": "1",
                                                "Content-Type": "application/json"})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
