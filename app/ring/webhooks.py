"""Ring webhook receiver helpers: HMAC-SHA256 check and an idempotent append-only log.

Ring signs the raw body with the partner HMAC key and sends `X-Signature: sha256=<hex>`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import Enum
from pathlib import Path


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

    def handle(self, raw: bytes, signature: str | None, key: str) -> WebhookStatus:
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
