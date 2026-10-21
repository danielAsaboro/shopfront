"""Check a carrier's tracking scan against what the doorbell recorded.

Two claims a carrier makes about a shop, each answered in words the evidence actually supports:

- "attempted 19:32": the owner says nobody came. Outcomes from a courier in the Ring frame to no door
  data at all.
- "business closed 14:05": the scan behind a missed Black Friday delivery. Here the shop's posted
  hours and the door's own activity around the scan say whether the shop was open and operating,
  and whether a courier came to the door at all.

Only a label from something that looked at the door (Ring's package_delivery alert, or Bedrock on the
Ring frame) can say "courier" or "not a courier". A time-of-day guess near the scan makes the answer
"unverified", never "no courier came". Ring has no "door answered" signal, and motion detection can
miss someone, so "nothing recorded" is never presented as "nobody came", and posted hours alone are
never presented as "someone was there".

All gaps are measured between instants (UTC), never between wall-clock readings, so a scan and a
visit either side of a daylight-saving change are the right number of minutes apart. A scan time that
hasn't happened yet, or a local time the clocks skipped, is refused by name.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from app import provenance
from app.models import SEEN_SOURCES, ClassifiedVisit, VisitType

TOLERANCE = timedelta(minutes=10)  # carrier scans and doorbell clocks rarely agree to the minute
ACTIVITY = timedelta(minutes=30)  # how far either side of a "closed" scan counts as the shop operating

ATTEMPTED, BUSINESS_CLOSED = "attempted", "business_closed"
CLAIMS = {ATTEMPTED: "Delivery attempted", BUSINESS_CLOSED: "Business closed"}

COURIER_AT_DOOR = "courier_at_door"  # Ring or Bedrock saw a courier near that time
UNVERIFIED = "door_activity_unverified"  # something near that time, but nothing that looked at it said who
SOMEONE_NOT_COURIER = "someone_not_a_courier"  # Bedrock saw a visit near that time and it wasn't a courier
NOTHING_RECORDED = "nothing_recorded"  # door events that day, none near that time
NO_DOOR_DATA = "no_door_data"  # nothing recorded all day: can't judge either way
COURIER_LATER = "courier_came_later"  # no courier near the scan, but Ring/Bedrock saw one later that day
# "business closed" scans
OPEN_NO_COURIER = "open_no_courier"  # posted open, door busy around the scan, nothing near it could be a courier
OPEN_COURIER_CAME = "open_courier_came"  # posted open and a courier was at the door near the scan
OPEN_BY_HOURS_ONLY = "open_by_hours_only"  # posted open, but nothing at the door around the scan
POSTED_CLOSED = "posted_closed"  # the shop's own hours say closed: the scan agrees with them
# refused before looking at the door
NOT_YET = "not_happened_yet"
NO_SUCH_TIME = "no_such_local_time"
REFUSED = {NOT_YET, NO_SUCH_TIME}

TEXT = {
    COURIER_AT_DOOR: "A courier was at the door at {time}, {gap} the carrier's time ({by}).",
    UNVERIFIED: "The doorbell recorded {what} at {time}, {gap} the carrier's time. Nothing that looked at the "
                "door confirmed who it was, so Shopfront can't say it was a courier, or that it wasn't.",
    SOMEONE_NOT_COURIER: "The doorbell saw someone at {time}, {gap} the carrier's time, but Bedrock labelled them "
                         "{label}, not a courier.",
    NOTHING_RECORDED: "The doorbell recorded nothing within {tol} minutes of {attempt}. Motion detection can miss "
                      "people, so this supports the owner's account but doesn't prove it.",
    NO_DOOR_DATA: "Shopfront has no door events at all for that day, so it can't check this attempt.",
    COURIER_LATER: "No courier was seen within {tol} minutes of the carrier's time ({attempt}), but one was at the "
                   "door later that day, at {time}, {gap} it ({by}). If that was this delivery, it arrived late. "
                   "A courier label doesn't say whose parcel it was, so Shopfront can't tell whose delivery it was.",
    OPEN_NO_COURIER: "The carrier scanned \"business closed\" at {attempt}. Your posted hours had you open ({hours}), "
                     "the doorbell recorded {activity} between {lo} and {hi}{cust}, and {near}. Motion detection "
                     "can miss people, so this supports, but doesn't prove, that nobody came to the door.",
    OPEN_COURIER_CAME: "The carrier scanned \"business closed\" at {attempt}, but your posted hours had you open "
                       "({hours}) and a courier was at the door at {time}, {gap} the scan ({by}). The doorbell "
                       "recorded {activity} between {lo} and {hi}{cust}.",
    OPEN_BY_HOURS_ONLY: "Your posted hours had you open ({hours}) when the carrier scanned \"business closed\" at "
                        "{attempt}, but the doorbell recorded nothing between {lo} and {hi}. Shopfront can show your "
                        "posted hours, not that someone was there.",
    POSTED_CLOSED: "Your posted hours have the shop closed at {attempt}, so the door can't contradict the carrier's "
                   "\"business closed\" scan.",
    NOT_YET: "{attempt} on {day} hasn't happened yet (it's {now} at the shop), so there's nothing to check. Enter "
             "the time the carrier's tracking shows.",
    NO_SUCH_TIME: "{attempt} didn't exist on {day}: the clocks jumped forward an hour that night. Check the time on "
                  "the carrier's tracking.",
}
CLOSED_TEXT = {  # business-closed wording where it differs from the attempt wording
    COURIER_LATER: "The carrier scanned \"business closed\" at {attempt}, while your posted hours had you open "
                   "({hours}). No courier was seen within {tol} minutes of the scan, but one was at the door later "
                   "that day, at {time}, {gap} the scan ({by}). If that was this delivery, it arrived late, after a "
                   "scan that doesn't match your posted hours. A courier label doesn't say whose parcel it was, so "
                   "Shopfront can't tell whose delivery it was.",
    UNVERIFIED: "Your posted hours had you open ({hours}) when the carrier scanned \"business closed\" at {attempt}. "
                "The doorbell recorded {what} at {time}, {gap} the scan, and nothing that looked at the door said "
                "who it was, so Shopfront can't say whether a courier came.",
    NO_DOOR_DATA: "Shopfront has no door events at all for that day, so it can't check this scan.",
}

def _by(v: ClassifiedVisit) -> str:
    """What the courier label rests on, exactly (app/provenance.py): Ring's alert, a replay, a Ring frame or a
    frame from the owner's browser."""
    return provenance.SAID_BY[provenance.kind(v.source, v.event.source)]


AMBIGUOUS = " {attempt} happened twice that night (the clocks went back an hour); Shopfront read it as the first."


def utc(t: datetime) -> datetime:
    """The instant, for comparing times across a daylight-saving change."""
    return t.astimezone(UTC)


def shift(t: datetime, by: timedelta) -> datetime:
    """`t` moved by real elapsed time, back in t's own zone."""
    return (utc(t) + by).astimezone(t.tzinfo)


def gap(a: datetime, b: datetime) -> timedelta:
    return utc(a) - utc(b)


def nonexistent(t: datetime) -> bool:
    """A wall-clock time the clocks skipped (02:30 on a spring-forward night)."""
    return t.tzinfo is not None and shift(t, timedelta(0)).replace(tzinfo=None) != t.replace(tzinfo=None)


def ambiguous(t: datetime) -> bool:
    """A wall-clock time that happened twice (01:30 on a fall-back night)."""
    return t.tzinfo is not None and t.replace(fold=0).utcoffset() != t.replace(fold=1).utcoffset()


def confirmed(v: ClassifiedVisit) -> bool:
    """Labelled by something that looked at the door, not guessed from the clock."""
    return v.source in SEEN_SOURCES and v.visit_type != VisitType.UNKNOWN


def seen_courier(v: ClassifiedVisit) -> bool:
    return v.visit_type == VisitType.DELIVERY and v.source in SEEN_SOURCES


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _gap(visit_time: datetime, attempt: datetime) -> str:
    mins = round(gap(visit_time, attempt).total_seconds() / 60)
    if mins == 0:
        return "the same minute as"
    return f"{abs(mins)} min {'after' if mins > 0 else 'before'}"


def _visit(v: ClassifiedVisit, frame_url) -> dict:
    return {"time": _hhmm(v.event.timestamp), "at": v.event.timestamp.isoformat(timespec="seconds"),
            "event_id": v.event.id, "ring_event": v.event.kind,
            "ring_subtype": v.event.ring_subtype, "type": v.visit_type.value, "labelled_by": v.source,
            "confirmed": confirmed(v), "reason": v.reason, "bedrock_request_id": v.request_id,
            "frame_url": frame_url(v.event.id), "event_source": v.event.source}


def _hours_text(open_hours: tuple[time, time] | None) -> str:
    return f"{open_hours[0]:%H:%M}–{open_hours[1]:%H:%M}" if open_hours else "closed"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _what(v: ClassifiedVisit) -> str:
    return {"ding": "a doorbell press", "motion": "motion"}.get(v.event.kind, "live view")


def _kinds(vs: list[ClassifiedVisit]) -> str:
    """"11 motion events and 1 doorbell press": what Ring recorded, with no guess about who."""
    motion = sum(1 for v in vs if v.event.kind == "motion")
    presses = sum(1 for v in vs if v.event.kind == "ding")
    other = len(vs) - motion - presses
    parts = [f"{motion} motion event{'' if motion == 1 else 's'}" if motion else "",
             f"{presses} doorbell press{'' if presses == 1 else 'es'}" if presses else "",
             f"{other} live-view visit{'' if other == 1 else 's'}" if other else ""]
    parts = [p for p in parts if p]
    if not parts:
        return "no door events"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _base(claim: str, attempt: datetime, tolerance: timedelta) -> dict:
    return {"claim": claim, "attempt": attempt.strftime("%Y-%m-%d %H:%M"),
            "attempt_at": attempt.isoformat(timespec="minutes"), "tolerance_min": int(tolerance.total_seconds() // 60)}


def _refusal(attempt: datetime, now: datetime | None) -> str | None:
    if nonexistent(attempt):
        return NO_SUCH_TIME
    now = now or datetime.now(UTC)
    return NOT_YET if utc(attempt) > utc(now) else None


def _refused(base: dict, outcome: str, attempt: datetime, now: datetime | None) -> dict:
    now_local = (now or datetime.now(UTC)).astimezone(attempt.tzinfo)
    text = TEXT[outcome].format(attempt=_hhmm(attempt), day=attempt.date().isoformat(), now=_hhmm(now_local)
                                if now_local.date() == attempt.date() else now_local.strftime("%Y-%m-%d %H:%M"))
    return {**base, "outcome": outcome, "text": text, "visit": None, "event_source": None}


def _later(visits: list[ClassifiedVisit], attempt: datetime, tolerance: timedelta) -> ClassifiedVisit | None:
    """The first courier Ring or Bedrock saw after the tolerance, on the same shop-local day."""
    later = [v for v in visits if seen_courier(v) and gap(v.event.timestamp, attempt) > tolerance
             and v.event.timestamp.astimezone(attempt.tzinfo).date() == attempt.date()]
    return min(later, key=lambda v: utc(v.event.timestamp)) if later else None


def _note(attempt: datetime) -> str:
    return AMBIGUOUS.format(attempt=_hhmm(attempt)) if ambiguous(attempt) else ""


def check_closed_scan(scan: datetime, visits: list[ClassifiedVisit], open_hours: tuple[time, time] | None,
                      frame_url=lambda event_id: None, tolerance: timedelta = TOLERANCE,
                      now: datetime | None = None) -> dict:
    """A "business closed" scan at `scan` (aware, shop local time) against posted hours and the door."""
    tol = int(tolerance.total_seconds() // 60)
    base = {**_base(BUSINESS_CLOSED, scan, tolerance), "posted_hours": _hours_text(open_hours)}
    refused = _refusal(scan, now)
    if refused:
        return {**_refused(base, refused, scan, now), "activity": None}
    lo, hi = shift(scan, -ACTIVITY), shift(scan, ACTIVITY)
    t = scan.timetz().replace(tzinfo=None)
    posted_open = open_hours is not None and open_hours[0] <= t < open_hours[1]
    around = [v for v in visits if abs(gap(v.event.timestamp, scan)) <= ACTIVITY]
    near = [v for v in visits if abs(gap(v.event.timestamp, scan)) <= tolerance]
    customers = sum(1 for v in around if v.visit_type == VisitType.CUSTOMER and confirmed(v))
    activity = {"from": _hhmm(lo), "to": _hhmm(hi), "events": len(around),
                "motion": sum(1 for v in around if v.event.kind == "motion"),
                "presses": sum(1 for v in around if v.event.kind == "ding"),
                "customers": customers,  # confirmed by Ring or Bedrock only
                "guessed_customers": sum(1 for v in around if v.visit_type == VisitType.CUSTOMER and not confirmed(v)),
                "staff": sum(1 for v in around if v.visit_type == VisitType.STAFF and confirmed(v)),
                "seen": sum(1 for v in around if confirmed(v)),
                "near": len(near)}
    fmt = {"attempt": _hhmm(scan), "hours": base["posted_hours"], "lo": activity["from"],
           "hi": activity["to"], "tol": tol, "activity": _kinds(around),
           "cust": f" ({customers} of them confirmed from the camera frame as "
                   f"{'a customer' if customers == 1 else 'customers'})" if customers else "",
           "near": f"no courier within {tol} minutes of the scan: "
                   + ("nothing was recorded that close" if not near else
                      f"{'the visit' if len(near) == 1 else 'all ' + str(len(near)) + ' visits'} recorded that close "
                      f"{'was' if len(near) == 1 else 'were'} labelled from the frame as not a courier")}
    couriers = [v for v in near if seen_courier(v)]
    unconfirmed = [v for v in near if not confirmed(v)]
    later = _later(visits, scan, tolerance)

    def nearest(vs: list[ClassifiedVisit]) -> ClassifiedVisit:
        return min(vs, key=lambda v: (abs(gap(v.event.timestamp, scan)), utc(v.event.timestamp)))

    cited = None
    if not posted_open:
        outcome = POSTED_CLOSED
    elif not visits:
        outcome = NO_DOOR_DATA
    elif couriers:
        outcome, cited = OPEN_COURIER_CAME, nearest(couriers)
    elif later:
        outcome, cited = COURIER_LATER, later
    elif unconfirmed:  # anything nothing looked at could have been the courier
        outcome, cited = UNVERIFIED, nearest(unconfirmed)
    elif around:
        outcome = OPEN_NO_COURIER
    else:
        outcome = OPEN_BY_HOURS_ONLY
    if cited:
        fmt.update(time=_hhmm(cited.event.timestamp), gap=_gap(cited.event.timestamp, scan),
                   by=_by(cited), what=_what(cited))
    text = CLOSED_TEXT.get(outcome, TEXT[outcome]).format(**fmt) + _note(scan)
    return {**base, "outcome": outcome, "text": text, "activity": activity,
            "visit": _visit(cited, frame_url) if cited else None,
            "event_source": sorted({v.event.source for v in around + ([cited] if cited else [])}) or None}


def check_attempt(attempt: datetime, visits: list[ClassifiedVisit], frame_url=lambda event_id: None,
                  tolerance: timedelta = TOLERANCE, now: datetime | None = None) -> dict:
    """`attempt` is the carrier's time (aware, shop local time); `visits` are that day's classified visits."""
    base = _base(ATTEMPTED, attempt, tolerance)
    refused = _refusal(attempt, now)
    if refused:
        return _refused(base, refused, attempt, now)
    if not visits:
        return {**base, "outcome": NO_DOOR_DATA, "text": TEXT[NO_DOOR_DATA], "visit": None, "event_source": None}
    near = [v for v in visits if abs(gap(v.event.timestamp, attempt)) <= tolerance]
    fmt = {"tol": base["tolerance_min"], "attempt": _hhmm(attempt)}

    def nearest(vs: list[ClassifiedVisit]) -> ClassifiedVisit:
        return min(vs, key=lambda x: (abs(gap(x.event.timestamp, attempt)), utc(x.event.timestamp)))

    couriers = [x for x in near if seen_courier(x)]
    unconfirmed = [x for x in near if not confirmed(x)]
    later = _later(visits, attempt, tolerance)
    v = None
    if couriers:
        outcome, v = COURIER_AT_DOOR, nearest(couriers)
    elif later:
        outcome, v = COURIER_LATER, later
    elif unconfirmed:  # anything nothing looked at could have been the courier
        outcome, v = UNVERIFIED, nearest(unconfirmed)
    elif near:  # everything that close was looked at and wasn't a courier
        outcome, v = SOMEONE_NOT_COURIER, nearest(near)
    else:
        outcome = NOTHING_RECORDED
    if v:
        t = v.event.timestamp
        fmt.update(time=_hhmm(t), gap=_gap(t, attempt), what=_what(v), label=v.visit_type.value.replace("_", " "),
                   by=_by(v))
    text = TEXT[outcome].format(**fmt) + _note(attempt)
    sources = [v.event.source] if v else sorted({x.event.source for x in visits})
    return {**base, "outcome": outcome, "text": text, "visit": _visit(v, frame_url) if v else None,
            "event_source": sources}
