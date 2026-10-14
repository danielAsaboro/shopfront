"""Per-event classification cache, so a Ring event is sent to Bedrock once."""

from __future__ import annotations

import json
from pathlib import Path


class ClassificationCache:
    def __init__(self, path: Path | None):
        self.path = path
        self._data: dict[str, dict] = {}
        if path and path.exists():
            self._data = json.loads(path.read_text())

    def get(self, event_id: str, model: str) -> dict | None:
        row = self._data.get(event_id)
        return row if row and row.get("model") == model else None

    def put(self, event_id: str, row: dict) -> None:
        self._data[event_id] = row
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1))
            tmp.replace(self.path)
