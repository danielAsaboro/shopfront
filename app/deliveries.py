"""Expected deliveries, matched against the couriers the doorbell actually saw.

The owner lists what should arrive (supplier, day, optional window), by hand or by importing the
shop's purchase orders and invoices (`app/orders.py`). Each courier that something looked at (Ring's
package_delivery alert, or Bedrock on the Ring frame) is matched to the closest expected delivery. A
match keeps the time, who labelled it and why, and the Ring frame when there is one: a timestamped
receipt the owner can show a supplier.

A doorbell press nothing looked at (the rules label it `doorbell_press`, never "delivery") is a guess: it is listed as a *possible*
courier, never a receipt, never counted as seen and never priced in the money line.

Ring has no "door answered" signal, so a receipt says a courier was at the door, never that the
parcel was handed over.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app import provenance
from app.files import private_dir, write_private
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
    # The owner marked it perishable (fresh dairy, bread, flowers): a missed delivery can mean the stock itself is
    # lost, so the desk shows the order's value at risk next to the redelivery cost
    perishable: bool = False

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

    def for_scan(self, day: date, tracking: str = "", order_id: str = "") -> Expected | None:
        """The order a carrier's scan was for, on the scan's day: the one its tracking number names, else the one the
        owner picked ("Carrier says closed?" on an order with no tracking number). A kept check stores it, so the
        record ties the scan to the order."""
        rows = self.for_day(day)
        return (next((e for e in rows if tracking and e.tracking == tracking), None)
                or next((e for e in rows if order_id and e.id == order_id), None))

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
                r.perishable = r.perishable or current[i].perishable  # the owner's mark survives a re-import
                current[i] = r
                updated += 1
        self._write(current)
        return {"added": added, "updated": updated}

    def set_perishable(self, expected_id: str, on: bool) -> Expected:
        with self._lock:
            rows = self._all()
            e = next((x for x in rows if x.id == expected_id), None)
            if e is None:
                raise KeyError(expected_id)
            e.perishable = bool(on)
            self._write(rows)
            return e

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
    return open_hours is not None and open_hours[0] <= tt < open_hours[1]


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


def is_possible_courier(v: ClassifiedVisit) -> bool:
    """A door event that could have been a courier but nothing looked at: a doorbell press the rules only
    timed, or a "delivery" label from something that isn't Ring or Bedrock."""
    return not is_seen_courier(v) and v.visit_type in (VisitType.DOORBELL_PRESS, VisitType.DELIVERY)


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
    a match to an order with no window > earlier-listed suppliers get the earlier couriers.

    The fourth tier means a courier inside one order's window goes to that order before one that
    could have come any time: the window is the stronger evidence. In the last tier an unmatched row
    counts as later than any courier, so with fewer couriers than orders the earlier-listed orders are
    matched first (before, a row left unmatched cost nothing in this tier, so a 10:12 courier went to
    a windowless invoice listed last instead of the 10:00-11:00 order listed first). That tier also
    makes ties impossible, so the receipts never depend on the order events arrived in."""
    n = len(expected)
    lex_tier = 2 * DAY_S ** n  # above any sum of t * DAY_S ** (n - 1 - i), t < DAY_S
    open_tier = lex_tier  # a match to an order with no window
    gap_tier = open_tier * (n + 1)  # above n open-tier terms plus the lex sum
    outside_tier = gap_tier * DAY_S * (n + 1)  # above any sum of seconds outside
    not_seen = outside_tier * (n + 1)
    unmatched_t = DAY_S - 1  # later than any second of the day, for the lex tier

    def cost(i: int, v: ClassifiedVisit, e: Expected) -> int:
        d = int(_distance(v, e).total_seconds())
        t = v.event.timestamp
        secs = t.hour * 3600 + t.minute * 60 + t.second
        return ((outside_tier if d else 0) + d * gap_tier + (0 if e.window() else open_tier)
                + secs * DAY_S ** (n - 1 - i))

    return [[cost(i, v, e) for v in couriers] + [not_seen + unmatched_t * DAY_S ** (n - 1 - i)] * n
            for i, e in enumerate(expected)]


def _due_by(e: Expected, tz) -> datetime:
    """When an expected delivery stops being "on its way": its window's end, or the end of its day."""
    d = date.fromisoformat(e.day)
    if e.window_end:
        return datetime.combine(d, time.fromisoformat(e.window_end), tzinfo=tz)
    return datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)


def _fits(v: ClassifiedVisit, e: Expected, open_hours) -> bool:
    """Whether an unidentified door event could be this order's courier: inside the order's window, or, for an
    order with no window, during posted hours. A press before opening or after closing named no order's time,
    so it stays a doorbell press while closed and is never offered to an order that could have come any time."""
    if e.window():
        return _distance(v, e) == timedelta(0)
    return _is_open(v.event.timestamp, open_hours)


def _possible_owners(orders: list[Expected], guessed: list[ClassifiedVisit], open_hours) -> dict[str, Expected]:
    """Each unidentified door event goes to at most one unmatched order: the best fit among those it fits.
    An order with a window beats one without, a narrower window beats a wider one, then the order listed
    first. The same doorbell press is never offered as the possible courier of two orders at once."""
    def fit(e: Expected) -> tuple:
        w = e.window()
        width = (w[1].hour * 60 + w[1].minute) - (w[0].hour * 60 + w[0].minute) if w else 24 * 60 + 1
        return (width, orders.index(e))
    out = {}
    for v in guessed:
        fits = [o for o in orders if _fits(v, o, open_hours)]
        if fits:
            out[v.event.id] = min(fits, key=fit)
    return out


def receipts(expected: list[Expected], visits: list[ClassifiedVisit], open_hours,
             frames: FrameStore, now: datetime | None = None) -> dict:
    """Match expected deliveries to couriers Ring or Bedrock saw, one visit per delivery at most.

    The assignment is chosen as a whole, not supplier by supplier: first as many suppliers matched as
    there are couriers, then as many of those inside their window as possible, then the closest
    times. Couriers nobody expected are listed separately. Rule-guessed couriers are only ever
    "possible". Pass `now` (aware) so an order whose window hasn't ended yet is "expected", not
    "not seen"."""
    couriers = [v for v in visits if is_seen_courier(v)]
    guessed = [v for v in visits if is_possible_courier(v)]
    pick = _assign(_costs(expected, couriers)) if expected else []
    unmatched = [e for e, j in zip(expected, pick) if j >= len(couriers)]
    owner = _possible_owners(unmatched, guessed, open_hours)
    used: set[str] = set()
    rows = []
    for e, j in zip(expected, pick):
        if j >= len(couriers):
            possible, elsewhere = [], []
            for v in guessed:
                fits = [o for o in unmatched if _fits(v, o, open_hours)]
                if e not in fits:
                    continue
                best = owner[v.event.id]
                if best is e:  # listed here only; the other orders it fits are named, not offered it too
                    possible.append({**_visit_row(v, frames, open_hours, visits),
                                     "also_fits": [o.supplier for o in fits if o is not e]})
                else:
                    elsewhere.append({"time": v.event.timestamp.strftime("%H:%M"), "ring_event": v.event.kind,
                                      "supplier": best.supplier})
            if possible:
                status = "possible"
            elif now is not None and _utc(now) < _utc(_due_by(e, now.tzinfo)):
                status = "expected"
            else:
                status = "not_seen"
            row = {**asdict(e), "status": status, "also_fits": [], "visit": None, "possible": possible,
                   "possible_elsewhere": elsewhere}
            rows.append({**row, "elsewhere_text": elsewhere_text(row) + "." if elsewhere and not possible else ""})
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
                     "visit": _visit_row(best, frames, open_hours, visits), "possible": [], "possible_elsewhere": [],
                     "elsewhere_text": ""})
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


# Who put the courier at the door, for the lead: said exactly for its source (app/provenance.py)
SAW = {
    provenance.RING_ALERT: "Ring's package alert saw the courier at {t}",
    provenance.REPLAY: "a replayed Ring package alert puts the courier at the door at {t}",
    provenance.BEDROCK_RING: "Bedrock saw the courier in the Ring frame at {t}",
    provenance.BEDROCK_LIVE: "Bedrock saw the courier in a frame from Ring live view at {t}",
}


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _seen_line(x: dict) -> str:
    v = x["visit"]
    name = x["supplier"] + (f" ({x['order_ref']})" if x.get("order_ref") else "")
    saw = SAW.get(provenance.of(v), "the courier was at the door at {t}").format(t=v["time"])
    tail = ""
    if x["status"].startswith("seen_while_closed"):
        tail = ", while the shop was closed"
    if x["status"].endswith("outside_window"):
        tail += f", outside its {x['window_start']}–{x['window_end']} window"
    return f"{name}: {saw}{tail}."


def _possible_line(x: dict) -> str:
    ps = x["possible"]
    presses = all(p.get("ring_event") == "ding" for p in ps)
    what = ("a doorbell press" if presses else "a door event") if len(ps) == 1 else \
        ("doorbell presses" if presses else "door events")
    return (f"Possible courier for {x['supplier']}: {what} at {_and([p['time'] for p in ps])}, "
            f"not confirmed by a camera frame.")


def headline(r: dict, scan: str | None = None, scan_first: bool = False) -> list[str]:
    """Lines for the top of the receiving desk: what Ring or Bedrock confirmed first, then the carrier's scan
    when one was checked (`scan`, its one-line answer), then what's missing, then the guesses. A scan the
    door disputes (`scan_first`) is the page's headline, above everything."""
    rows = r["expected"]
    seen = sorted((x for x in rows if x["visit"]), key=lambda x: x["visit"]["time"])
    closed = [x for x in seen if x["status"].startswith("seen_while_closed")]
    confirmed = [_seen_line(x) for x in seen]
    if r["total"] > 1 and len(seen) == r["total"] and not closed:
        confirmed.insert(0, f"All {r['total']} expected deliveries reached the door.")
    missing = [x["supplier"] for x in rows if x["status"] == "not_seen" and not x.get("possible_elsewhere")]
    lines = ([scan] if scan and scan_first else []) + confirmed + ([scan] if scan and not scan_first else [])
    if missing:
        lines.append(f"No courier seen for {_and(missing)}.")
    due = [x["supplier"] + (f" ({x['window_start']}–{x['window_end']})" if x.get("window_start") else "")
           for x in rows if x["status"] == "expected"]
    if due:
        lines.append(f"Still to come: {_and(due)}.")
    lines += [_possible_line(x) for x in rows if x["status"] == "possible" and x["possible"]]
    lines += [elsewhere_text(x) + "." for x in rows if x["status"] == "not_seen" and x.get("possible_elsewhere")]
    return lines


def elsewhere_text(x: dict) -> str:
    """"No courier confirmed for Pinecrest Paper Goods; the doorbell press at 14:36 that fits it is listed under
    Valley Creamery, the closer fit": an order whose only candidate went to another order."""
    ps = x["possible_elsewhere"]
    what = "the doorbell press" if all(p["ring_event"] == "ding" for p in ps) and len(ps) == 1 else \
        ("the door event" if len(ps) == 1 else "the door events")
    under = _and(sorted({p["supplier"] for p in ps}))
    return (f"No courier confirmed for {x['supplier']}; {what} at {_and([p['time'] for p in ps])} that "
            f"{'fits' if len(ps) == 1 else 'fit'} it {'is' if len(ps) == 1 else 'are'} listed under {under}, "
            f"the closer fit")


# What a failed delivery costs, until the owner says what it costs them: Loqate's "Fixing Failed Deliveries
# 2021" report (survey by Censuswide, December 2020, of 304 retail executives in the US, UK and Germany): "In
# the US, 8% of first-time deliveries fail, at an average cost of $17.20. In Germany, 7% ... costing on
# average €14.69 apiece. In the UK, it's a 6% failure rate at an average cost of £11.60 each." It is the
# sender's cost per failed parcel, not the shop's lost trade, so every page that uses it says so.
COST_DEFAULTS = {"$": 17.20, "£": 11.60, "€": 14.69}
COST_SOURCE = {
    "name": "Loqate, Fixing Failed Deliveries 2021",
    "what": "the average cost of one failed first-time delivery, from a December 2020 survey of 304 retail "
            "executives in the US, UK and Germany",
    "url": "https://info.loqate.com/hubfs/Loqate%202021/Fixing%20Failed%20Deliveries/"
           "Fixing%20Failed%20Deliveries%20-%20Final.pdf",
}


def default_cost(currency: str) -> float | None:
    """The survey's figure for this currency, or None when the survey has none for it."""
    return COST_DEFAULTS.get((currency or "").strip())


def money_line(closed_attempts: int, missing: int, cost: float | None, currency: str,
               default: bool = False, sourced_above: bool = False) -> str | None:
    """A redelivery cost times what the door saw: the owner's own number, or with `default` the survey
    average above, said as such. No cost means no line. Both counts must come from couriers Ring or
    Bedrock saw (`receipts()["closed_couriers"]`, "not_seen" rows). With `sourced_above` the page has already
    said what the survey average is and where it comes from, so the line only points back to it."""
    n = closed_attempts + missing
    if cost is None or n == 0:
        return None
    parts = []
    if closed_attempts:
        parts.append(f"{closed_attempts} courier{'s' if closed_attempts != 1 else ''} came while you were closed")
    if missing:
        parts.append(f"{missing} expected deliver{'ies' if missing != 1 else 'y'} with no courier seen at the door")
    amount = f"{currency}{cost * n:,.2f}".replace(".00", "")
    if default and sourced_above:
        return f"Last 7 days: {' and '.join(parts)}. At {currency}{cost:.2f} each (the survey average above), that's about {amount}."
    if default:
        return (f"Last 7 days: {' and '.join(parts)}. At {currency}{cost:.2f} each, the survey average for a failed "
                f"delivery (not your own number yet), that's about {amount}.")
    return f"Last 7 days: {' and '.join(parts)}. At your {currency}{cost:g} per redelivery, that's {amount}."
