"""Check a carrier's tracking scan against what the doorbell recorded.

Two claims a carrier makes about a shop, each answered in words the evidence actually supports:

- "attempted 19:32": the owner says nobody came. Five outcomes, from a courier in the Ring frame to
  no door data at all.
- "business closed 14:05": the scan behind a missed Black Friday delivery. Here the shop's posted
  hours and the door's own activity around the scan say whether the shop was open and operating,
  and whether a courier came to the door at all.

Ring has no "door answered" signal, and motion detection can miss someone, so "nothing recorded" is
never presented as "nobody came", and posted hours alone are never presented as "someone was there".
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.models import SEEN_SOURCES, ClassifiedVisit, VisitType

TOLERANCE = timedelta(minutes=10)  # carrier scans and doorbell clocks rarely agree to the minute
ACTIVITY = timedelta(minutes=30)  # how far either side of a "closed" scan counts as the shop operating

ATTEMPTED, BUSINESS_CLOSED = "attempted", "business_closed"
CLAIMS = {ATTEMPTED: "Delivery attempted", BUSINESS_CLOSED: "Business closed"}

COURIER_AT_DOOR = "courier_at_door"  # Bedrock saw a courier in the Ring frame near that time
UNVERIFIED = "door_activity_unverified"  # something near that time, but no frame confirmed who
SOMEONE_NOT_COURIER = "someone_not_a_courier"  # Bedrock saw a visit near that time and it wasn't a courier
NOTHING_RECORDED = "nothing_recorded"  # door events that day, none near that time
NO_DOOR_DATA = "no_door_data"  # nothing recorded all day: can't judge either way
# "business closed" scans
OPEN_NO_COURIER = "open_no_courier"  # posted open, door busy around the scan, no courier near it
OPEN_COURIER_CAME = "open_courier_came"  # posted open and a courier was at the door near the scan
OPEN_BY_HOURS_ONLY = "open_by_hours_only"  # posted open, but nothing at the door around the scan
POSTED_CLOSED = "posted_closed"  # the shop's own hours say closed: the scan agrees with them

TEXT = {
    COURIER_AT_DOOR: "A courier was at the door at {time}, {gap} the carrier's time ({by}).",
    UNVERIFIED: "The doorbell recorded {what} at {time}, {gap} the carrier's time. No Ring frame confirmed who "
                "it was, so Shopfront can't say it was a courier.",
    SOMEONE_NOT_COURIER: "The doorbell saw someone at {time}, {gap} the carrier's time, but Bedrock labelled them "
                         "{label}, not a courier.",
    NOTHING_RECORDED: "The doorbell recorded nothing within {tol} minutes of {attempt}. Motion detection can miss "
                      "people, so this supports the owner's account but doesn't prove it.",
    NO_DOOR_DATA: "Shopfront has no door events at all for that day, so it can't check this attempt.",
    OPEN_NO_COURIER: "The carrier scanned \"business closed\" at {attempt}. Your posted hours had you open ({hours}), "
                     "the doorbell recorded {activity} between {lo} and {hi}{cust}, and no courier was seen within {tol} "
                     "minutes of the scan. Motion detection can miss people, so this supports, but doesn't prove, "
                     "that nobody came to the door.",
    OPEN_COURIER_CAME: "The carrier scanned \"business closed\" at {attempt}, but your posted hours had you open "
                       "({hours}) and a courier was at the door at {time}, {gap} the scan ({by}). The doorbell "
                       "recorded {activity} between {lo} and {hi}{cust}.",
    OPEN_BY_HOURS_ONLY: "Your posted hours had you open ({hours}) when the carrier scanned \"business closed\" at "
                        "{attempt}, but the doorbell recorded nothing between {lo} and {hi}. Shopfront can show your "
                        "posted hours, not that someone was there.",
    POSTED_CLOSED: "Your posted hours have the shop closed at {attempt}, so the door can't contradict the carrier's "
                   "\"business closed\" scan.",
}

BY = {"bedrock": "Bedrock saw them in the Ring frame", "ring": "Ring's package_delivery alert"}


def _seen_courier(v: ClassifiedVisit) -> bool:
    return v.visit_type == VisitType.DELIVERY and v.source in SEEN_SOURCES


def _gap(visit_time: datetime, attempt: datetime) -> str:
    mins = round((visit_time - attempt).total_seconds() / 60)
    if mins == 0:
        return "the same minute as"
    return f"{abs(mins)} min {'after' if mins > 0 else 'before'}"


def _visit(v: ClassifiedVisit, frame_url) -> dict:
    return {"time": v.event.timestamp.strftime("%H:%M"), "event_id": v.event.id, "ring_event": v.event.kind,
            "ring_subtype": v.event.ring_subtype, "type": v.visit_type.value, "labelled_by": v.source,
            "reason": v.reason, "bedrock_request_id": v.request_id, "frame_url": frame_url(v.event.id),
            "event_source": v.event.source}


def _hours_text(open_hours: tuple[time, time] | None) -> str:
    return f"{open_hours[0]:%H:%M}–{open_hours[1]:%H:%M}" if open_hours else "closed"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def check_closed_scan(scan: datetime, visits: list[ClassifiedVisit], open_hours: tuple[time, time] | None,
                      frame_url=lambda event_id: None, tolerance: timedelta = TOLERANCE) -> dict:
    """A "business closed" scan at `scan` (shop local time) against posted hours and the door around it."""
    tol = int(tolerance.total_seconds() // 60)
    lo, hi = scan - ACTIVITY, scan + ACTIVITY
    base = {"claim": BUSINESS_CLOSED, "attempt": scan.strftime("%Y-%m-%d %H:%M"), "tolerance_min": tol,
            "posted_hours": _hours_text(open_hours)}
    t = scan.timetz().replace(tzinfo=None)
    posted_open = bool(open_hours) and open_hours[0] <= t < open_hours[1]
    around = [v for v in visits if lo <= v.event.timestamp <= hi]
    customers = sum(1 for v in around if v.visit_type == VisitType.CUSTOMER)
    activity = {"from": lo.strftime("%H:%M"), "to": hi.strftime("%H:%M"), "events": len(around),
                "customers": customers, "staff": sum(1 for v in around if v.visit_type == VisitType.STAFF),
                "seen": sum(1 for v in around if v.source in SEEN_SOURCES)}
    fmt = {"attempt": scan.strftime("%H:%M"), "hours": base["posted_hours"], "lo": activity["from"],
           "hi": activity["to"], "tol": tol,
           "activity": _plural(len(around), "door event"),
           "cust": f" ({_plural(customers, 'labelled customer')})" if customers else ""}
    near = [v for v in visits if abs(v.event.timestamp - scan) <= tolerance and _seen_courier(v)]
    courier = min(near, key=lambda v: (abs(v.event.timestamp - scan), v.event.timestamp)) if near else None
    if not posted_open:
        outcome = POSTED_CLOSED
    elif not visits:
        outcome = NO_DOOR_DATA
    elif courier:
        outcome = OPEN_COURIER_CAME
        fmt.update(time=courier.event.timestamp.strftime("%H:%M"), gap=_gap(courier.event.timestamp, scan),
                   by=BY[courier.source])
    elif around:
        outcome = OPEN_NO_COURIER
    else:
        outcome = OPEN_BY_HOURS_ONLY
    text = TEXT[outcome].replace("check this attempt", "check this scan").format(**fmt)
    return {**base, "outcome": outcome, "text": text, "activity": activity,
            "visit": _visit(courier, frame_url) if courier else None,
            "event_source": sorted({v.event.source for v in around}) or None}


def check_attempt(attempt: datetime, visits: list[ClassifiedVisit], frame_url=lambda event_id: None,
                  tolerance: timedelta = TOLERANCE) -> dict:
    """`attempt` is the carrier's time in shop local time; `visits` are that day's classified door visits."""
    base = {"claim": ATTEMPTED, "attempt": attempt.strftime("%Y-%m-%d %H:%M"),
            "tolerance_min": int(tolerance.total_seconds() // 60)}
    if not visits:
        return {**base, "outcome": NO_DOOR_DATA, "text": TEXT[NO_DOOR_DATA], "visit": None, "event_source": None}
    near = [v for v in visits if abs(v.event.timestamp - attempt) <= tolerance]
    sources = sorted({v.event.source for v in visits})
    if not near:
        text = TEXT[NOTHING_RECORDED].format(tol=base["tolerance_min"], attempt=attempt.strftime("%H:%M"))
        return {**base, "outcome": NOTHING_RECORDED, "text": text, "visit": None, "event_source": sources}

    def rank(v: ClassifiedVisit):
        return (not _seen_courier(v), abs(v.event.timestamp - attempt), v.event.timestamp)

    v = min(near, key=rank)
    if v.source not in SEEN_SOURCES or v.visit_type == VisitType.UNKNOWN:
        outcome = UNVERIFIED
    elif v.visit_type == VisitType.DELIVERY:
        outcome = COURIER_AT_DOOR
    else:
        outcome = SOMEONE_NOT_COURIER
    t = v.event.timestamp
    what = {"ding": "a doorbell press", "motion": "motion"}.get(v.event.kind, "live view")
    text = TEXT[outcome].format(time=t.strftime("%H:%M"), gap=_gap(t, attempt), what=what,
                                label=v.visit_type.value.replace("_", " "), by=BY.get(v.source, ""),
                                tol=base["tolerance_min"], attempt=attempt.strftime("%H:%M"))
    return {**base, "outcome": outcome, "text": text, "visit": _visit(v, frame_url), "event_source": [v.event.source]}
