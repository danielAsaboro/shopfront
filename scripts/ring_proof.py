"""Make real Ring (and optionally Bedrock) calls and record what came back, for the proof table.

    RING_ACCESS_TOKEN=... .venv/bin/python -m scripts.ring_proof [--date YYYY-MM-DD] [--bedrock]

Without RING_ACCESS_TOKEN it uses the token pasted on /connect (.data/ring-access-token).

Writes evidence/ring-proof-<utc>.json: endpoints called, HTTP status, X-Request-ID, event counts
and times, and the snapshot's sha256/size/headers. Never writes the token. Device and account IDs
are stored as short hashes. The frame itself goes to evidence/raw/ (git-ignored) for the demo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.ring.api import API_BASE, RingApiClient, RingAuth, RingError

OUT = Path("evidence")


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def redact(url: str) -> str:
    """Hash Ring device/account IDs and drop pre-signed query strings."""
    url = re.sub(r"ava1\.ring\.(device|account)\.[A-Z0-9]+", lambda m: f"<{m.group(1)}:{h(m.group(0))}>", url)
    return url.split("?security_token")[0].split("?location=")[0]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="shop-local day to read (default: today)")
    p.add_argument("--bedrock", action="store_true", help="also classify the frame with Bedrock (costs a call)")
    p.add_argument("--latest-frame", action="store_true",
                   help="also ask for the latest image in the last 24 h, even with no history events")
    args = p.parse_args()
    day = date.fromisoformat(args.date) if args.date else datetime.now(settings.tz).date()

    log: list[dict] = []

    def hook(resp: httpx.Response) -> None:
        req = resp.request
        log.append({"method": req.method, "url": redact(str(req.url).replace(API_BASE, "")),
                    "status": resp.status_code,
                    "x_request_id": resp.headers.get("X-Request-ID"),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    http = httpx.Client(timeout=30, follow_redirects=True, event_hooks={"response": [hook]})
    pasted = Path(settings.data_dir) / "ring-access-token"
    access = settings.ring_access_token or (pasted.read_text().strip() if pasted.exists() else "")
    auth = RingAuth(access_token=access, refresh_token="" if access else settings.ring_refresh_token,
                    client_id=settings.ring_client_id, client_secret=settings.ring_client_secret, http=http)
    ring = RingApiClient(auth=auth, tz=settings.tz, device_id=settings.ring_device_id,
                         event_types=settings.ring_event_types, http=http)
    record: dict = {"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "auth_mode": auth.mode, "day": day.isoformat(), "calls": log}
    try:
        devices = ring.list_devices()
        record["devices"] = [{"id_hash": h(d["id"]), "type": d.get("type"),
                              "attributes_keys": sorted((d.get("attributes") or {}).keys())} for d in devices]
        events = ring.events_for_day(day)
        record["device_id_hash"] = h(ring.device_id)
        record["events"] = [{"kind": e.kind, "local_time": e.timestamp.isoformat()} for e in events]
        if events:
            e = events[-1]
            frame = ring.snapshot(e)
            raw = OUT / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / f"{day}-{e.timestamp:%H%M%S}.jpg").write_bytes(frame)
            record["snapshot"] = {"event_local_time": e.timestamp.isoformat(), "bytes": len(frame),
                                  "sha256": hashlib.sha256(frame).hexdigest()}
            if args.bedrock:
                from app.classify.bedrock import BedrockClassifier
                from app.hours import parse_hours, posted_hours

                e.snapshot = frame
                v = BedrockClassifier(settings.aws_region, settings.bedrock_model_id).classify(
                    e, posted_hours(parse_hours(settings.shop_hours), day))
                record["bedrock"] = {"model": settings.bedrock_model_id, "label": v.visit_type.value,
                                     "source": v.source, "reason": v.reason, "request_id": v.request_id}
        if args.latest_frame:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            try:
                resp = ring._request("POST", f"/v1/devices/{ring.device_id}/media/image/download",
                                     json={"type": "latest_in_range", "start_timestamp": now_ms - 86_400_000,
                                           "end_timestamp": now_ms, "image_options": {"format": "jpeg"}})
                frame = resp.content
                record["latest_frame"] = {"bytes": len(frame), "sha256": hashlib.sha256(frame).hexdigest(),
                                          "content_type": resp.headers.get("content-type"),
                                          "x_media_timestamp": resp.headers.get("X-Media-Timestamp"),
                                          "x_media_origin": resp.headers.get("X-Media-Origin")}
                raw = OUT / "raw"
                raw.mkdir(parents=True, exist_ok=True)
                (raw / f"latest-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jpg").write_bytes(frame)
            except RingError as exc:
                record["latest_frame"] = {"error": exc.status.value, "http_status": exc.http_status,
                                          "request_id": exc.request_id}
    except RingError as exc:
        record["error"] = {"status": exc.status.value, "http_status": exc.http_status,
                           "request_id": exc.request_id}
    OUT.mkdir(exist_ok=True)
    path = OUT / f"ring-proof-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {path}")
    print(json.dumps({k: record[k] for k in record if k not in ("calls", "events")}, indent=2))
    return 1 if "error" in record else 0


if __name__ == "__main__":
    sys.exit(main())
