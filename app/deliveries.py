"""Expected deliveries, matched against the couriers the doorbell actually saw.

The owner lists what should arrive (supplier, day, optional window), by hand or by importing the
shop's purchase orders and invoices (`app/orders.py`). Each courier that something looked at (Ring's
package_delivery alert, or Bedrock on the Ring frame) is matched to the closest expected delivery. A
match keeps the time, who labelled it and why, and the Ring frame when there is one: a timestamped
receipt the owner can show a supplier.

A doorbell press the time-of-day rules call "delivery" is a guess: it is listed as a *possible*
courier, never a receipt, never counted as seen and never priced in the money line.

Ring has no "door answered" signal, so a receipt says a courier was at the door, never that the
parcel was handed over.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.models import SEEN_SOURCES, ClassifiedVisit, VisitType


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
    if ws and we:
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
            note: str = "", **order) -> Expected:
        e = make_expected(day, supplier, window_start, window_end, note, **order)
        self._write(self._all() + [e])
        return e

    def remove(self, expected_id: str) -> None:
        self._write([e for e in self._all() if e.id != expected_id])

    def get(self, expected_id: str) -> Expected | None:
        return next((e for e in self._all() if e.id == expected_id), None)

    def import_rows(self, rows: list[Expected]) -> dict:
        """Add imported orders. Re-importing the same file updates rows instead of duplicating them:
        an order is the same one when its order_ref and day match, and a staff tap is kept."""
        current = self._all()
        by_key = {(e.order_ref, e.day): i for i, e in enumerate(current) if e.order_ref}
        added = updated = 0
        for r in rows:
            i = by_key.get((r.order_ref, r.day)) if r.order_ref else None
            if i is None:
                current.append(r)
                if r.order_ref:
                    by_key[(r.order_ref, r.day)] = len(current) - 1
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
        rows = self._all()
        for e in rows:
            if e.id == expected_id:
                e.received = None if status == "clear" else {
                    "status": status, "note": note.strip()[:200], "at": at.isoformat(timespec="minutes")}
                self._write(rows)
                return e
        raise KeyError(expected_id)


RECEIVED = {"ok": "All there", "short": "Short", "damaged": "Damaged", "clear": "Undo"}


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


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)


def _distance(v: ClassifiedVisit, e: Expected) -> timedelta:
    """Real time outside the window (zero inside it), measured between instants so a window on a
    daylight-saving day is the right length."""
    w = e.window()
    if not w:
        return timedelta(0)
    t = v.event.timestamp
    lo, hi = (_utc(datetime.combine(t.date(), x, tzinfo=t.tzinfo)) for x in w)
    return max(lo - _utc(t), _utc(t) - hi, timedelta(0))


def is_seen_courier(v: ClassifiedVisit) -> bool:
    """A courier something looked at: Ring's package_delivery alert or Bedrock on the frame."""
    return v.visit_type == VisitType.DELIVERY and v.source in SEEN_SOURCES


def staff_in(t: datetime, visits: list[ClassifiedVisit], open_hours) -> bool:
    """Staff were at the door by `t` and still around: a staff visit at or before it, and either another at
    or after it, or `t` before opening (they came in early to open up)."""
    staff = [_utc(v.event.timestamp) for v in visits if v.visit_type == VisitType.STAFF]
    u = _utc(t)
    if not any(s <= u for s in staff):
        return False
    before_opening = open_hours is not None and t.timetz().replace(tzinfo=None) < open_hours[0]
    return before_opening or any(s >= u for s in staff)


def _visit_row(v: ClassifiedVisit, frames: FrameStore, open_hours, visits: list[ClassifiedVisit] | None = None) -> dict:
    return {"time": v.event.timestamp.strftime("%H:%M"), "at": v.event.timestamp.isoformat(timespec="seconds"),
            "event_id": v.event.id, "ring_event": v.event.kind,
            "event_source": v.event.source, "labelled_by": v.source, "confirmed": v.source in SEEN_SOURCES,
            "confidence": v.confidence,
            "reason": v.reason, "bedrock_request_id": v.request_id, "frame_url": frames.url(v.event.id),
            "shop_open": _is_open(v.event.timestamp, open_hours),
            "staff_in": staff_in(v.event.timestamp, visits or [], open_hours)}


def _assign(cost: list[list[int]]) -> list[int]:
    """Minimum-cost assignment (Hungarian method) for n rows into m >= n columns; returns each row's column."""
    n, m = len(cost), len(cost[0]) if cost else 0
    INF = float("inf")
    u: list[float] = [0] * (n + 1)
    v: list[float] = [0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
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


def _due_by(e: Expected, tz) -> datetime:
    """When an expected delivery stops being "on its way": its window's end, or the end of its day."""
    d = date.fromisoformat(e.day)
    if e.window_end:
        return datetime.combine(d, time.fromisoformat(e.window_end), tzinfo=tz)
    return datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)


def receipts(expected: list[Expected], visits: list[ClassifiedVisit], open_hours,
             frames: FrameStore, now: datetime | None = None) -> dict:
    """Match expected deliveries to couriers Ring or Bedrock saw, one visit per delivery at most.

    The assignment is chosen as a whole, not supplier by supplier: first as many suppliers matched as
    there are couriers, then as many of those inside their window as possible, then the closest
    times. Couriers nobody expected are listed separately. Rule-guessed couriers are only ever
    "possible". Pass `now` (aware) so an order whose window hasn't ended yet is "expected", not
    "not seen"."""
    couriers = [v for v in visits if is_seen_courier(v)]
    guessed = [v for v in visits if v.visit_type == VisitType.DELIVERY and not is_seen_courier(v)]
    pick = _assign(_costs(expected, couriers)) if expected else []
    used: set[str] = set()
    rows = []
    for e, j in zip(expected, pick):
        if j >= len(couriers):
            possible = [_visit_row(v, frames, open_hours, visits) for v in guessed if _distance(v, e) == timedelta(0)]
            if possible:
                status = "possible"
            elif now is not None and _utc(now) < _utc(_due_by(e, now.tzinfo)):
                status = "expected"
            else:
                status = "not_seen"
            rows.append({**asdict(e), "status": status, "also_fits": [], "visit": None, "possible": possible})
            continue
        best = couriers[j]
        used.add(best.event.id)
        in_window = _distance(best, e) == timedelta(0)
        t = best.event.timestamp
        status = "seen" if _is_open(t, open_hours) or staff_in(t, visits, open_hours) else "seen_while_closed"
        if not in_window:
            status += "_outside_window"
        # Ring frames don't say whose courier it was; name the other suppliers this visit also fits
        also = [o.supplier for o in expected if o is not e and _distance(best, o) == timedelta(0)]
        rows.append({**asdict(e), "status": status, "also_fits": also,
                     "visit": _visit_row(best, frames, open_hours, visits), "possible": []})
    unexpected = [_visit_row(v, frames, open_hours, visits) for v in couriers if v.event.id not in used]
    closed = sum(1 for v in couriers if not _is_open(v.event.timestamp, open_hours)
                 and not staff_in(v.event.timestamp, visits, open_hours))
    return {"expected": rows, "unexpected": unexpected,
            "unconfirmed": [_visit_row(v, frames, open_hours, visits) for v in guessed],
            "closed_couriers": closed,
            "seen": sum(1 for r in rows if r["visit"]), "total": len(rows)}


STATUS_TEXT = {
    "seen": "Courier at the door",
    "seen_outside_window": "Courier at the door, outside the window",
    "seen_while_closed": "Courier came while you were closed",
    "seen_while_closed_outside_window": "Courier came while closed, outside the window",
    "possible": "Possible courier, not confirmed by a camera frame",
    "expected": "Not due yet",
    "not_seen": "No courier seen",
}


def _what(row: dict) -> str:
    return "a doorbell press" if row.get("ring_event") == "ding" else "a door event"


def headline(r: dict) -> list[str]:
    """Lines for the top of the briefing, most actionable first."""
    lines = []
    missing = [x["supplier"] for x in r["expected"] if x["status"] == "not_seen"]
    closed = [x for x in r["expected"] if x["status"].startswith("seen_while_closed")]
    if missing:
        lines.append(f"No courier seen for {', '.join(missing)}.")
    for x in r["expected"]:
        if x["status"] == "possible":
            what = " and ".join(f"{_what(p)} at {p['time']}" for p in x["possible"])
            lines.append(f"Possible courier for {x['supplier']}: {what}, not confirmed by a camera frame.")
    for x in closed:
        lines.append(f"{x['supplier']}'s courier was at the door at {x['visit']['time']}, while the shop was closed.")
    if r["total"] and all(x["visit"] for x in r["expected"]) and not closed:
        lines.append(f"All {r['total']} expected deliveries reached the door.")
    return lines


def money_line(closed_attempts: int, missing: int, cost: float | None, currency: str) -> str | None:
    """The owner's own redelivery cost times what the door saw. No cost set means no line. Both counts
    must come from couriers Ring or Bedrock saw (`receipts()["closed_couriers"]`, "not_seen" rows)."""
    n = closed_attempts + missing
    if cost is None or n == 0:
        return None
    parts = []
    if closed_attempts:
        parts.append(f"{closed_attempts} courier{'s' if closed_attempts != 1 else ''} came while you were closed")
    if missing:
        parts.append(f"{missing} expected deliver{'ies' if missing != 1 else 'y'} with no courier seen at the door")
    amount = f"{currency}{cost * n:,.2f}".replace(".00", "")
    return f"Last 7 days: {' and '.join(parts)}. At your {currency}{cost:g} per redelivery, that's {amount}."
