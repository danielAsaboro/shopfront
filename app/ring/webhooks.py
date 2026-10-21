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

Whose door: Ring sends every event for the account, from every device, with the Ring user's Account ID
in `meta.account_id` ("allowing partners to associate events with the correct Ring user", API reference,
Notifications > Webhook v1.1 Payload Structure, and Users API > Key Uses of Account ID) and the device
in `data.attributes.source`. When the shop's device and account are known (RING_DEVICE_ID /
RING_ACCOUNT_ID, or learned from the live Ring client), events from anything else are logged but never
reach the door timeline. Ring retries 5xx and may disable an endpoint after repeated 4xx (Notifications >
Webhook Response Handling), so those are still answered 200.

Replays: the Developer Playground sends no webhooks (organisers, forum 45511), so
`scripts/feed_webhook.py` replays payloads in Ring's documented shape. A replay is signed with a
replay-only key derived from RING_HMAC_KEY (`replay_key`), and its body carries
`meta.shopfront_replay: true` (our own field, not Ring's) under that signature. Either one makes it
`replayed: true`, and its door events say `ring-webhook-replay`, never `ring`; removing a header or the
marker can't turn a replay into Ring's own. Ring's real payloads, signed with RING_HMAC_KEY and
without the marker, verify exactly as Ring documents. The old `X-Shopfront-Replay: 1` header can
still mark a request as a replay, never the other way round.

Every log line is JSON; a line cut short by a crash is skipped with a warning instead of stopping the app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import ClassifiedVisit, DoorEvent, VisitType

log = logging.getLogger(__name__)

KINDS = {"button_press": "ding", "motion_detected": "motion"}  # webhook data.type -> Event History kind
SKIP_AT_STAGE0 = {"vehicle", "other_motion"}
MAX_BODY = 64 * 1024  # Ring's v1.1 notifications are a few hundred bytes; refuse anything huge before HMAC
REPLAY_MARKER = "shopfront_replay"


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


def replay_key(key: str) -> str:
    """The key Shopfront's own feeder signs with. Ring never uses it, so nothing it signs passes as Ring's."""
    return hmac.new(key.encode(), b"shopfront-replay-v1", hashlib.sha256).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSON-lines file, skipping (and logging) any line that isn't a JSON object."""
    if not path.exists():
        return []
    rows, bad = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        log.warning("%s: skipped %d unreadable line%s", path, bad, "" if bad == 1 else "s")
    return rows


class WebhookLog:
    def __init__(self, path: Path, device_id: str = "", account_id: str = ""):
        self.path = path
        self.device_id = device_id  # the shop's doorbell; empty = accept any device
        self.account_id = account_id  # the shop owner's Ring Account ID; empty = accept any account
        self._lock = threading.Lock()
        self._seen: set[str] = {r.get("request_id", "") for r in read_jsonl(path)}

    def handle(self, raw: bytes, signature: str | None, key: str, replayed: bool = False) -> WebhookStatus:
        if not key:
            return WebhookStatus.NOT_CONFIGURED
        if verify(raw, signature, key):
            pass
        elif verify(raw, signature, replay_key(key)):
            replayed = True
        else:
            return WebhookStatus.BAD_SIGNATURE
        try:
            body = json.loads(raw)
            meta, data = body["meta"], body["data"]
            request_id = meta["request_id"]
            attrs = data.get("attributes") or {}
            row = {"request_id": request_id, "type": data["type"], "id": data.get("id"),
                   "device": attrs.get("source"), "account": meta.get("account_id"),
                   "timestamp": attrs.get("timestamp"), "sub_type": attrs.get("sub_type"), "sent": meta.get("time")}
            if replayed or meta.get(REPLAY_MARKER) is True:
                row["replayed"] = True
        except (ValueError, KeyError, TypeError, AttributeError):
            return WebhookStatus.MALFORMED
        with self._lock:
            if request_id in self._seen:
                return WebhookStatus.DUPLICATE
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            self._seen.add(request_id)
        return WebhookStatus.ACCEPTED

    def _ours(self, r: dict) -> bool:
        return ((not self.device_id or r.get("device") == self.device_id)
                and (not self.account_id or r.get("account") == self.account_id))

    def recent(self, n: int = 20) -> list[dict]:
        return read_jsonl(self.path)[-n:][::-1]

    def _rows(self) -> list[dict]:
        return read_jsonl(self.path)

    @staticmethod
    def _when(r: dict, tz: ZoneInfo) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=tz)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None

    def ignored(self, day: date, tz: ZoneInfo) -> int:
        """Door events that day from another device or account: logged, never put on the shop's door."""
        return sum(1 for r in self._rows() if KINDS.get(r.get("type")) and not self._ours(r)
                   and (ts := self._when(r, tz)) is not None and ts.date() == day)

    def day(self, day: date, tz: ZoneInfo) -> dict:
        """Stage 0 for one day: door events that go on, Ring-labelled couriers, and what was skipped."""
        events, ring_labelled, skipped = [], [], []
        seen = set()
        for r in self._rows():
            kind = KINDS.get(r.get("type"))
            ts = self._when(r, tz)
            key = r.get("id") or r.get("request_id")  # Ring's data.id, else the delivery's own request_id
            if not kind or ts is None or ts.date() != day or key in seen or not self._ours(r):
                continue
            seen.add(key)
            sub = r.get("sub_type")
            e = DoorEvent(id=str(key), device_id=str(r.get("device") or ""),
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
