"""Expected deliveries, matched against the couriers the doorbell actually saw.

The owner lists what should arrive (supplier, day, optional window), by hand or by importing the
shop's purchase orders and invoices (`app/orders.py`). Each delivery-labelled visit that day is
matched to the closest expected one. A match keeps the time, who labelled it and why,
and the Ring frame when there is one: a timestamped receipt the owner can show a supplier.

Ring has no "door answered" signal, so a receipt says a courier was at the door, never that the
parcel was handed over.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.files import private_dir, write_private
from app.models import ClassifiedVisit, VisitType


@dataclass
class Expected:
    id: str
    day: str  # YYYY-MM-DD
    supplier: str
    window_start: str | None = None  # HH:MM
    window_end: str | None = None
    note: str = ""
    # From the shop's orders and invoices, when it was imported (app/orders.py)
    order_ref: str = ""
    carrier: str = ""
    tracking: str = ""
    items: str = ""
    amount: str = ""  # as written on the order, e.g. "$212.40"; never summed or converted
    origin: str = "manual"  # manual | csv:<format>
    # The optional staff tap once it's been unpacked: {"status": ok|short|damaged, "note", "at"}
    received: dict | None = None

    def window(self) -> tuple[time, time] | None:
        if self.window_start and self.window_end:
            return time.fromisoformat(self.window_start), time.fromisoformat(self.window_end)
        return None


def make_expected(day: date, supplier: str, window_start: str = "", window_end: str = "", note: str = "",
                  order_ref: str = "", carrier: str = "", tracking: str = "", items: str = "", amount: str = "",
                  origin: str = "manual") -> Expected:
    """One checked expected delivery; raises ValueError on anything the matcher couldn't use."""
    supplier = supplier.strip()
    if not supplier:
        raise ValueError("supplier is required")
    ws, we = (window_start or "").strip() or None, (window_end or "").strip() or None
    if bool(ws) != bool(we):
        raise ValueError("give both ends of the window, or neither")
    if ws:
        try:
            lo, hi = time.fromisoformat(ws), time.fromisoformat(we)
        except ValueError:
            raise ValueError("window times must be HH:MM")
        if lo >= hi:
            raise ValueError("window must start before it ends")
        ws, we = lo.strftime("%H:%M"), hi.strftime("%H:%M")
    return Expected(f"exp-{uuid.uuid4().hex[:8]}", day.isoformat(), supplier[:80], ws, we, note.strip()[:200],
                    order_ref.strip()[:60], carrier.strip()[:40], tracking.strip()[:60], items.strip()[:200],
                    amount.strip()[:20], origin)


class DeliveryBook:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()  # requests run in a thread pool; each change is read-modify-write

    def _all(self) -> list[Expected]:
        if not self.path.exists():
            return []
        return [Expected(**x) for x in json.loads(self.path.read_text())]

    def _write(self, rows: list[Expected]) -> None:
        write_private(self.path, json.dumps([asdict(r) for r in rows], indent=1))

    def for_day(self, day: date) -> list[Expected]:
        return sorted((e for e in self._all() if e.day == day.isoformat()),
                      key=lambda e: (e.window_start or "99", e.supplier.lower()))

    def add(self, day: date, supplier: str, window_start: str = "", window_end: str = "",
            note: str = "", **order) -> Expected:
        e = make_expected(day, supplier, window_start, window_end, note, **order)
        with self._lock:
            self._write(self._all() + [e])
        return e

    def remove(self, expected_id: str) -> None:
        with self._lock:
            self._write([e for e in self._all() if e.id != expected_id])

    def get(self, expected_id: str) -> Expected | None:
        return next((e for e in self._all() if e.id == expected_id), None)

    def import_rows(self, rows: list[Expected]) -> dict:
        """Add imported orders. Re-importing the same file updates rows instead of duplicating them:
        an order is the same one when its order_ref and day match, and a staff tap is kept."""
        with self._lock:
            return self._import_rows(rows)

    def _import_rows(self, rows: list[Expected]) -> dict:
        current = self._all()
        by_key = {_import_key(e): i for i, e in enumerate(current) if e.order_ref or e.origin.startswith("csv:")}
        added = updated = 0
        for r in rows:
            i = by_key.get(_import_key(r))
            if i is None:
                current.append(r)
                by_key[_import_key(r)] = len(current) - 1
                added += 1
            else:
                r.id, r.received = current[i].id, current[i].received
                current[i] = r
                updated += 1
        self._write(current)
        return {"added": added, "updated": updated}

    def mark_received(self, expected_id: str, status: str, note: str, at: datetime) -> Expected:
        if status not in RECEIVED:
            raise ValueError(f"status must be one of {', '.join(RECEIVED)}")
        with self._lock:
            rows = self._all()
            for e in rows:
                if e.id == expected_id:
                    e.received = None if status == "clear" else {
                        "status": status, "note": note.strip()[:200], "at": at.isoformat(timespec="minutes")}
                    self._write(rows)
                    return e
        raise KeyError(expected_id)


def _import_key(e: Expected) -> tuple:
    """The same order on a re-import: its order_ref and day, or, with no order number, the same line."""
    if e.order_ref:
        return ("ref", e.order_ref, e.day)
    return ("line", e.day, e.supplier.lower(), e.tracking, e.window_start, e.window_end, e.items)


RECEIVED = {"ok": "All there", "short": "Short", "damaged": "Damaged", "clear": "Undo"}


def frame_name(event_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", event_id)[:120] + ".jpg"


class FrameStore:
    """Ring frames Shopfront already holds: ones Bedrock saw, and the first frame of each live session."""

    def __init__(self, data_dir: Path):
        self.dirs = [data_dir / "frames", data_dir / "live-frames"]

    def save(self, event_id: str, jpeg: bytes) -> None:
        write_private(private_dir(self.dirs[0]) / frame_name(event_id), jpeg)  # a picture of a person: 0600

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


def _assign(cost: list[list[int]]) -> list[int]:
    """Minimum-cost assignment (Hungarian method) for n rows into m >= n columns; returns each row's column."""
    n, m = len(cost), len(cost[0]) if cost else 0
    INF = float("inf")
    u, v, p, way = [0] * (n + 1), [0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while p[j0]:
            used[j0], i0, delta, j1 = True, p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    out = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


DAY_S = 86_401  # one more than the seconds in a day


def _costs(expected: list[Expected], couriers: list[ClassifiedVisit]) -> list[list[int]]:
    """Costs in strict tiers, each worth more than everything below it put together:
    leaving a supplier unmatched > a match outside its window > seconds outside the window >
    earlier-listed suppliers get the earlier couriers. The last tier makes ties impossible, so the
    receipts never depend on the order events arrived in."""
    n = len(expected)
    lex_tier = 2 * DAY_S ** n  # above any sum of t * DAY_S ** (n - 1 - i)
    gap_tier = lex_tier * (n + 1)  # above any sum of lex terms
    outside_tier = gap_tier * DAY_S * (n + 1)  # above any sum of seconds outside
    not_seen = outside_tier * (n + 1)

    def cost(i: int, v: ClassifiedVisit, e: Expected) -> int:
        d = int(_distance(v, e).total_seconds())
        t = v.event.timestamp
        secs = t.hour * 3600 + t.minute * 60 + t.second
        return (outside_tier if d else 0) + d * gap_tier + secs * DAY_S ** (n - 1 - i)

    return [[cost(i, v, e) for v in couriers] + [not_seen] * n for i, e in enumerate(expected)]


def receipts(expected: list[Expected], visits: list[ClassifiedVisit], open_hours,
             frames: FrameStore) -> dict:
    """Match expected deliveries to delivery-labelled visits, one visit per delivery at most.

    The assignment is chosen as a whole, not supplier by supplier: first as many suppliers matched as
    there are couriers, then as many of those inside their window as possible, then the closest
    times. List the couriers nobody expected separately."""
    couriers = [v for v in visits if v.visit_type == VisitType.DELIVERY]
    pick = _assign(_costs(expected, couriers)) if expected else []
    used: set[str] = set()
    rows = []
    for e, j in zip(expected, pick):
        if j >= len(couriers):
            rows.append({**asdict(e), "status": "not_seen", "also_fits": [], "visit": None})
            continue
        best = couriers[j]
        used.add(best.event.id)
        in_window = _distance(best, e) == timedelta(0)
        status = "seen" if _is_open(best.event.timestamp, open_hours) else "seen_while_closed"
        if not in_window:
            status += "_outside_window"
        # Ring frames don't say whose courier it was; name the other suppliers this visit also fits
        also = [o.supplier for o in expected if o is not e and _distance(best, o) == timedelta(0)]
        rows.append({**asdict(e), "status": status, "also_fits": also,
                     "visit": _visit_row(best, frames, open_hours)})
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
