"""Ring webhook receiver helpers: HMAC-SHA256 check and an idempotent append-only log.

Ring signs the raw body with the partner HMAC key and sends `X-Signature: sha256=<hex>`.

Logged notifications also feed the door timeline, as stage 0 of the funnel Ring's Computer Vision
Guidelines recommend (https://developer.amazon.com/docs/ring/computer-vision-guidelines.html):
the motion `sub_type` Ring added on 2026-10-05 (release notes,
https://developer.amazon.com/docs/ring/release-notes.html) decides, for free, what goes further.

- `package_delivery`: Ring's own vision saw a delivery. It becomes a courier visit labelled "ring",
  with no Bedrock call.
- `human`, plain `motion` (Smart Alerts off, so no sub_type) and doorbell presses go on to the classifier.
- `vehicle` and `other_motion` are logged and counted, and never reach the classifier.

The Developer Playground sends no webhooks (organisers, forum 45511), so `scripts/feed_webhook.py`
replays signed payloads in Ring's documented shape. Those carry `X-Shopfront-Replay: 1`, are logged
with `replayed: true`, and their door events say `ring-webhook-replay`, never `ring`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import ClassifiedVisit, DoorEvent, VisitType

KINDS = {"button_press": "ding", "motion_detected": "motion"}  # webhook data.type -> Event History kind
SKIP_AT_STAGE0 = {"vehicle", "other_motion"}


class WebhookStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"            # same meta.request_id seen before: 200, not processed again
    NOT_CONFIGURED = "NOT_CONFIGURED"  # no RING_HMAC_KEY: refuse everything
    BAD_SIGNATURE = "BAD_SIGNATURE"    # missing or wrong X-Signature
    MALFORMED = "MALFORMED"            # signature ok, body not a Ring v1.1 payload


def sign(raw: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, header: str | None, key: str) -> bool:
    return bool(header) and hmac.compare_digest(sign(raw, key), header.strip())


class WebhookLog:
    def __init__(self, path: Path):
        self.path = path
        self._seen: set[str] = set()
        if path.exists():
            for line in path.read_text().splitlines():
                self._seen.add(json.loads(line).get("request_id", ""))

    def handle(self, raw: bytes, signature: str | None, key: str, replayed: bool = False) -> WebhookStatus:
        if not key:
            return WebhookStatus.NOT_CONFIGURED
        if not verify(raw, signature, key):
            return WebhookStatus.BAD_SIGNATURE
        try:
            body = json.loads(raw)
            meta, data = body["meta"], body["data"]
            request_id = meta["request_id"]
            row = {"request_id": request_id, "type": data["type"], "id": data.get("id"),
                   "device": data.get("attributes", {}).get("source"),
                   "timestamp": data.get("attributes", {}).get("timestamp"),
                   "sub_type": data.get("attributes", {}).get("sub_type"), "sent": meta.get("time")}
            if replayed:
                row["replayed"] = True
        except (ValueError, KeyError, TypeError):
            return WebhookStatus.MALFORMED
        if request_id in self._seen:
            return WebhookStatus.DUPLICATE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        self._seen.add(request_id)
        return WebhookStatus.ACCEPTED

    def recent(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines()[-n:]][::-1]

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def day(self, day: date, tz: ZoneInfo) -> dict:
        """Stage 0 for one day: door events that go on, Ring-labelled couriers, and what was skipped."""
        events, ring_labelled, skipped = [], [], []
        seen = set()
        for r in self._rows():
            kind = KINDS.get(r.get("type"))
            try:
                ts = datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=tz)
            except (KeyError, TypeError, ValueError):
                continue
            if not kind or ts.date() != day or r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            sub = r.get("sub_type")
            e = DoorEvent(id=str(r.get("id") or r["request_id"]), device_id=str(r.get("device") or ""),
                          timestamp=ts, kind=kind, ring_subtype=sub,
                          source="ring-webhook-replay" if r.get("replayed") else "ring-webhook")
            if kind == "motion" and sub in SKIP_AT_STAGE0:
                skipped.append(e)
            elif kind == "motion" and sub == "package_delivery":
                ring_labelled.append(ClassifiedVisit(e, VisitType.DELIVERY, 0.8,
                                                     "Ring Smart Alert: package_delivery", "ring"))
            else:
                events.append(e)
        return {"events": events, "ring_labelled": ring_labelled, "skipped": skipped}
