"""Small, safe file helpers for everything under SHOPFRONT_DATA_DIR.

- `write_private`: create a file 0600 and swap it in atomically. Tokens, secrets and door frames
  (pictures of people) are never readable by other users, not even for a moment.
- `read_jsonl`: JSON-lines logs survive a line cut short by a crash; the bad line is logged and skipped.
- `prune_older_than`: the retention limit on door frames.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file only this user can read, atomically: it is created 0600, never chmod-ed after."""
    if isinstance(data, str):
        data = data.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


def prune_older_than(directory: Path, days: float, now: float | None = None, pattern: str = "*.jpg") -> int:
    """Delete files in `directory` older than `days`; returns how many went."""
    if days <= 0 or not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    gone = 0
    for p in directory.glob(pattern):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                gone += 1
        except OSError as exc:
            log.warning("could not delete %s: %s", p, exc)
    return gone
