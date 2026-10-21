"""Was the doorbell online? Shopfront asks Ring every few minutes and keeps the answers.

API reference, Device Discovery > Status: `GET /v1/devices/{device_id}/status` answers
`data.attributes.online`, "whether the device is currently online". It only speaks for the moment it is
asked, so a scan in the past can be checked against it only if someone asked at the time. While Ring is
live, the app's lifespan task (`sampler`) asks every `SHOPFRONT_STATUS_MINUTES` minutes (default 5) and
appends the answer to `.data/ring-status.jsonl`:

    {"at": epoch ms, "device": Ring device ID, "online": true | false | null, "every_s": 300,
     "request_id": X-Request-ID, "error": RingStatus when Ring didn't answer}

`online: null` is a check that failed (token expired, network down). It counts as a gap, never as online.
Rows older than `RETENTION_DAYS` are dropped. Reads and writes share one lock; the file is created 0600.

app/coverage.py counts the doorbell as watching only between two consecutive samples that both say online
and are close together (`close`): one online answer says nothing about the minutes after it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.files import read_jsonl, write_private

log = logging.getLogger(__name__)

RETENTION_DAYS = 90  # a carrier dispute can come weeks after the scan
DEFAULT_MINUTES = 5.0
CLOSE_FACTOR = 1.5  # two samples are "close" when no more than 1.5 sampling intervals apart: no missed check


def close(a: dict, b: dict) -> bool:
    """Were two consecutive samples taken close enough together to say nothing was missed between them?"""
    every = max(float(a.get("every_s") or 0), float(b.get("every_s") or 0)) or DEFAULT_MINUTES * 60
    return 0 <= (b["at"] - a["at"]).total_seconds() <= every * CLOSE_FACTOR


class StatusLog:
    def __init__(self, path: Path, retention_days: float = RETENTION_DAYS, clock: Callable[[], float] = _time.time):
        self.path = path
        self.retention_days = retention_days
        self.clock = clock
        self._lock = threading.Lock()
        self._pruned = 0.0

    def add(self, device_id: str, online: bool | None, every_s: float, request_id: str | None = None,
            error: str | None = None, at_ms: int | None = None) -> dict:
        row = {"at": int(self.clock() * 1000) if at_ms is None else at_ms, "device": device_id, "online": online,
               "every_s": every_s, "request_id": request_id, "error": error}
        line = json.dumps(row) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line)
            if self.clock() - self._pruned >= 3600:
                self._prune()
        return row

    def _prune(self) -> None:
        self._pruned = self.clock()
        cutoff = (self.clock() - self.retention_days * 86400) * 1000
        rows = read_jsonl(self.path)
        keep = [r for r in rows if isinstance(r.get("at"), int) and r["at"] >= cutoff]
        if len(keep) != len(rows):
            write_private(self.path, "".join(json.dumps(r) + "\n" for r in keep))

    def samples(self, tz: ZoneInfo, device_id: str = "") -> list[dict]:
        """The doorbell's samples, oldest first: {"at": aware datetime, "online": bool | None, "every_s",
        "request_id", "sampled": True}. With no device_id (not learned yet), every sample counts."""
        with self._lock:
            rows = read_jsonl(self.path)
        out = []
        for r in rows:
            if device_id and r.get("device") != device_id:
                continue
            try:
                at = datetime.fromtimestamp(int(r["at"]) / 1000, tz=tz)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            online = r.get("online")
            out.append({"at": at, "online": online if isinstance(online, bool) else None,
                        "every_s": r.get("every_s"), "request_id": r.get("request_id"), "sampled": True})
        return sorted(out, key=lambda s: s["at"])


async def sampler(sample: Callable[[], object], every_s: float,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Call `sample` (blocking: it runs in a worker thread) every `every_s` seconds, for ever. One failed
    sample is logged and the loop goes on; cancelling the task stops it."""
    while True:
        try:
            await asyncio.to_thread(sample)
        except Exception as exc:  # a sampling bug must never take the app down
            log.warning("Ring status sample failed: %s", exc)
        await sleep(every_s)
