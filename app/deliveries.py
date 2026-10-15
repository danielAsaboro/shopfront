"""Expected deliveries, matched against the couriers the doorbell actually saw.

The owner lists what should arrive (supplier, day, optional window). Each delivery-labelled visit
that day is matched to the closest expected one. A match keeps the time, who labelled it and why,
and the Ring frame when there is one: a timestamped receipt the owner can show a supplier.

Ring has no "door answered" signal, so a receipt says a courier was at the door, never that the
parcel was handed over.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.models import ClassifiedVisit, VisitType


@dataclass
class Expected:
    id: str
    day: str  # YYYY-MM-DD
    supplier: str
    window_start: str | None = None  # HH:MM
    window_end: str | None = None
    note: str = ""

    def window(self) -> tuple[time, time] | None:
        if self.window_start and self.window_end:
            return time.fromisoformat(self.window_start), time.fromisoformat(self.window_end)
        return None


class DeliveryBook:
    def __init__(self, path: Path):
        self.path = path

    def _all(self) -> list[Expected]:
        if not self.path.exists():
            return []
        return [Expected(**x) for x in json.loads(self.path.read_text())]

    def _write(self, rows: list[Expected]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in rows], indent=1))
        tmp.replace(self.path)

    def for_day(self, day: date) -> list[Expected]:
        return sorted((e for e in self._all() if e.day == day.isoformat()),
                      key=lambda e: (e.window_start or "99", e.supplier.lower()))

    def add(self, day: date, supplier: str, window_start: str = "", window_end: str = "",
            note: str = "") -> Expected:
        supplier = supplier.strip()
        if not supplier:
            raise ValueError("supplier is required")
        ws, we = (window_start or "").strip() or None, (window_end or "").strip() or None
        if bool(ws) != bool(we):
            raise ValueError("give both ends of the window, or neither")
        if ws and time.fromisoformat(ws) >= time.fromisoformat(we):
            raise ValueError("window must start before it ends")
        e = Expected(f"exp-{uuid.uuid4().hex[:8]}", day.isoformat(), supplier[:80], ws, we, note.strip()[:200])
        self._write(self._all() + [e])
        return e

    def remove(self, expected_id: str) -> None:
        self._write([e for e in self._all() if e.id != expected_id])


def frame_name(event_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", event_id)[:120] + ".jpg"


class FrameStore:
    """Ring frames Shopfront already holds: ones Bedrock saw, and the first frame of each live session."""

    def __init__(self, data_dir: Path):
        self.dirs = [data_dir / "frames", data_dir / "live-frames"]

    def save(self, event_id: str, jpeg: bytes) -> None:
        self.dirs[0].mkdir(parents=True, exist_ok=True)
        (self.dirs[0] / frame_name(event_id)).write_bytes(jpeg)

    def path(self, event_id: str) -> Path | None:
        for d in self.dirs:
            p = d / frame_name(event_id)
            if p.exists():
                return p
        return None

    def url(self, event_id: str) -> str | None:
        return f"/frames/{frame_name(event_id)}" if self.path(event_id) else None


def _is_open(t: datetime, open_hours: tuple[time, time] | None) -> bool:
    tt = t.timetz().replace(tzinfo=None)
    return bool(open_hours) and open_hours[0] <= tt < open_hours[1]


def _distance(v: ClassifiedVisit, e: Expected) -> timedelta:
    w = e.window()
    if not w:
        return timedelta(0)
    t = v.event.timestamp
    lo, hi = (datetime.combine(t.date(), x, tzinfo=t.tzinfo) for x in w)
    return max(lo - t, t - hi, timedelta(0))


def _visit_row(v: ClassifiedVisit, frames: FrameStore, open_hours) -> dict:
    return {"time": v.event.timestamp.strftime("%H:%M"), "event_id": v.event.id,
            "event_source": v.event.source, "labelled_by": v.source, "confidence": v.confidence,
            "reason": v.reason, "bedrock_request_id": v.request_id, "frame_url": frames.url(v.event.id),
            "shop_open": _is_open(v.event.timestamp, open_hours)}


def receipts(expected: list[Expected], visits: list[ClassifiedVisit], open_hours,
             frames: FrameStore) -> dict:
    """Match each expected delivery to one delivery-labelled visit; list the rest as unexpected."""
    couriers = [v for v in visits if v.visit_type == VisitType.DELIVERY]
    used: set[str] = set()
    rows = []
    for e in expected:
        pool = [v for v in couriers if v.event.id not in used]
        best = min(pool, key=lambda v: (_distance(v, e), v.event.timestamp), default=None)
        if best is None:
            rows.append({**asdict(e), "status": "not_seen", "visit": None})
            continue
        used.add(best.event.id)
        in_window = _distance(best, e) == timedelta(0)
        open_ = _is_open(best.event.timestamp, open_hours)
        status = "seen" if open_ else "seen_while_closed"
        if not in_window:
            status += "_outside_window"
        rows.append({**asdict(e), "status": status, "visit": _visit_row(best, frames, open_hours)})
    unexpected = [_visit_row(v, frames, open_hours) for v in couriers if v.event.id not in used]
    return {"expected": rows, "unexpected": unexpected,
            "seen": sum(1 for r in rows if r["status"] != "not_seen"), "total": len(rows)}


STATUS_TEXT = {
    "seen": "Courier at the door",
    "seen_outside_window": "Courier at the door, outside the window",
    "seen_while_closed": "Courier rang while you were closed",
    "seen_while_closed_outside_window": "Courier rang while closed, outside the window",
    "not_seen": "No courier seen",
}


def headline(r: dict) -> list[str]:
    """Lines for the top of the briefing, most actionable first."""
    lines = []
    missing = [x["supplier"] for x in r["expected"] if x["status"] == "not_seen"]
    closed = [x for x in r["expected"] if x["status"].startswith("seen_while_closed")]
    if missing:
        lines.append(f"No courier seen for {', '.join(missing)}.")
    for x in closed:
        lines.append(f"{x['supplier']}'s courier rang at {x['visit']['time']}, while the shop was closed.")
    if r["total"] and not missing and not closed:
        lines.append(f"All {r['total']} expected deliveries reached the door.")
    return lines


def money_line(closed_attempts: int, missing: int, cost: float | None, currency: str) -> str | None:
    """The owner's own redelivery cost times what the door saw. No cost set means no line."""
    n = closed_attempts + missing
    if cost is None or n == 0:
        return None
    parts = []
    if closed_attempts:
        parts.append(f"{closed_attempts} courier{'s' if closed_attempts != 1 else ''} rang while you were closed")
    if missing:
        parts.append(f"{missing} expected deliver{'ies' if missing != 1 else 'y'} never reached the door")
    amount = f"{currency}{cost * n:,.2f}".replace(".00", "")
    return f"Last 7 days: {' and '.join(parts)}. At your {currency}{cost:g} per redelivery, that's {amount}."
