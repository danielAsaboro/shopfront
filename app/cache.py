"""Per-event classification cache, so a Ring event is sent to Bedrock once.

Thread-safe (requests run in a thread pool), written atomically, and a file damaged by a crash is
set aside as `<name>.corrupt-<time>` instead of stopping the app.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.files import write_private

log = logging.getLogger(__name__)


class ClassificationCache:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                self._data = data
            except ValueError as exc:
                aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                path.replace(aside)
                log.warning("%s was unreadable (%s); set aside as %s and starting empty", path, exc, aside.name)

    def get(self, event_id: str, model: str) -> dict | None:
        row = self._data.get(event_id)
        return row if row and row.get("model") == model else None

    def put(self, event_id: str, row: dict) -> None:
        with self._lock:
            self._data[event_id] = row
            if self.path:
                write_private(self.path, json.dumps(self._data, indent=1))
