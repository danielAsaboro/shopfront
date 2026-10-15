"""Check a carrier's "delivery attempted" time against what the doorbell recorded.

The dispute this answers: the carrier's tracking says "attempted 19:32", the owner says nobody came.
Shopfront lines that time up with the door timeline and says which of five things it can show,
each worded for what the evidence actually supports. Ring has no "door answered" signal, and
motion detection can miss someone, so "nothing recorded" is never presented as "nobody came".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models import ClassifiedVisit, VisitType

TOLERANCE = timedelta(minutes=10)  # carrier scans and doorbell clocks rarely agree to the minute

COURIER_AT_DOOR = "courier_at_door"  # Bedrock saw a courier in the Ring frame near that time
UNVERIFIED = "door_activity_unverified"  # something near that time, but no frame confirmed who
SOMEONE_NOT_COURIER = "someone_not_a_courier"  # Bedrock saw a visit near that time and it wasn't a courier
NOTHING_RECORDED = "nothing_recorded"  # door events that day, none near that time
NO_DOOR_DATA = "no_door_data"  # nothing recorded all day: can't judge either way

TEXT = {
    COURIER_AT_DOOR: "A courier was at the door at {time}, {gap} the carrier's time. Ring frame attached.",
    UNVERIFIED: "The doorbell recorded {what} at {time}, {gap} the carrier's time. No Ring frame confirmed who "
                "it was, so Shopfront can't say it was a courier.",
    SOMEONE_NOT_COURIER: "The doorbell saw someone at {time}, {gap} the carrier's time, but Bedrock labelled them "
                         "{label}, not a courier.",
    NOTHING_RECORDED: "The doorbell recorded nothing within {tol} minutes of {attempt}. Motion detection can miss "
                      "people, so this supports the owner's account but doesn't prove it.",
    NO_DOOR_DATA: "Shopfront has no door events at all for that day, so it can't check this attempt.",
}


def _gap(visit_time: datetime, attempt: datetime) -> str:
    mins = round((visit_time - attempt).total_seconds() / 60)
    if mins == 0:
        return "the same minute as"
    return f"{abs(mins)} min {'after' if mins > 0 else 'before'}"


def check_attempt(attempt: datetime, visits: list[ClassifiedVisit], frame_url=lambda event_id: None,
                  tolerance: timedelta = TOLERANCE) -> dict:
    """`attempt` is the carrier's time in shop local time; `visits` are that day's classified door visits."""
    base = {"attempt": attempt.strftime("%Y-%m-%d %H:%M"), "tolerance_min": int(tolerance.total_seconds() // 60)}
    if not visits:
        return {**base, "outcome": NO_DOOR_DATA, "text": TEXT[NO_DOOR_DATA], "visit": None, "event_source": None}
    near = [v for v in visits if abs(v.event.timestamp - attempt) <= tolerance]
    sources = sorted({v.event.source for v in visits})
    if not near:
        text = TEXT[NOTHING_RECORDED].format(tol=base["tolerance_min"], attempt=attempt.strftime("%H:%M"))
        return {**base, "outcome": NOTHING_RECORDED, "text": text, "visit": None, "event_source": sources}

    def rank(v: ClassifiedVisit):
        seen_courier = v.visit_type == VisitType.DELIVERY and v.source == "bedrock"
        return (not seen_courier, abs(v.event.timestamp - attempt), v.event.timestamp)

    v = min(near, key=rank)
    if v.source != "bedrock" or v.visit_type == VisitType.UNKNOWN:
        outcome = UNVERIFIED
    elif v.visit_type == VisitType.DELIVERY:
        outcome = COURIER_AT_DOOR
    else:
        outcome = SOMEONE_NOT_COURIER
    t = v.event.timestamp
    what = {"ding": "a doorbell press", "motion": "motion"}.get(v.event.kind, "live view")
    text = TEXT[outcome].format(time=t.strftime("%H:%M"), gap=_gap(t, attempt), what=what,
                                label=v.visit_type.value.replace("_", " "),
                                tol=base["tolerance_min"], attempt=attempt.strftime("%H:%M"))
    visit = {"time": t.strftime("%H:%M"), "event_id": v.event.id, "ring_event": v.event.kind,
             "type": v.visit_type.value, "labelled_by": v.source, "reason": v.reason,
             "bedrock_request_id": v.request_id, "frame_url": frame_url(v.event.id)}
    return {**base, "outcome": outcome, "text": text, "visit": visit, "event_source": [v.event.source]}
