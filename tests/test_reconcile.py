"""Carrier "attempted delivery" times checked against the door timeline. Local only."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from app import main
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.reconcile import (
    COURIER_AT_DOOR,
    COURIER_LATER,
    NO_DOOR_DATA,
    NOTHING_RECORDED,
    SOMEONE_NOT_COURIER,
    TOLERANCE,
    UNVERIFIED,
    check_attempt,
)

TZ = ZoneInfo("Africa/Lagos")
DAY = date(2026, 10, 7)


def visit(hh, mm, kind=VisitType.DELIVERY, source="bedrock", ring_event="ding", eid=None, tz=TZ):
    e = DoorEvent(eid or f"ev-{hh:02d}{mm:02d}", "d", datetime.combine(DAY, time(hh, mm), tzinfo=tz), ring_event, "ring")
    return ClassifiedVisit(e, kind, 0.9, "why", source, "rid" if source == "bedrock" else None)


def at(hh, mm):
    return datetime.combine(DAY, time(hh, mm), tzinfo=TZ)


def test_courier_seen_by_bedrock_near_the_attempt():
    r = check_attempt(at(19, 32), [visit(9, 0), visit(19, 30)], frame_url=lambda i: f"/frames/{i}.jpg")
    assert r["outcome"] == COURIER_AT_DOOR and r["visit"]["time"] == "19:30"
    assert r["visit"]["frame_url"] == "/frames/ev-1930.jpg" and "2 min before" in r["text"]


def test_bedrock_courier_beats_a_closer_unverified_press():
    r = check_attempt(at(19, 32), [visit(19, 32, source="rules"), visit(19, 38)])
    assert r["outcome"] == COURIER_AT_DOOR and r["visit"]["time"] == "19:38"


def test_rules_label_is_never_reported_as_a_courier():
    r = check_attempt(at(19, 32), [visit(19, 31, source="rules")])
    assert r["outcome"] == UNVERIFIED and "can't say it was a courier" in r["text"]


def test_someone_else_at_the_door():
    r = check_attempt(at(12, 0), [visit(12, 4, kind=VisitType.CUSTOMER, ring_event="motion")])
    assert r["outcome"] == SOMEONE_NOT_COURIER and "labelled them customer" in r["text"]


def test_nothing_near_the_attempt_is_not_called_proof():
    r = check_attempt(at(19, 32), [visit(9, 0)])
    assert r["outcome"] == NOTHING_RECORDED and "doesn't prove it" in r["text"]


def test_no_door_data_means_no_judgement():
    assert check_attempt(at(19, 32), [])["outcome"] == NO_DOOR_DATA


minute = st.integers(0, 24 * 60 - 1)


@settings(max_examples=300, deadline=None)
@given(minute, st.lists(st.tuples(minute, st.sampled_from(list(VisitType)), st.sampled_from(["bedrock", "rules", "ring"])),
                        max_size=6))
def test_outcome_matches_the_evidence(attempt_min, raw):
    vs = [visit(m // 60, m % 60, k, s, eid=f"ev-{i}") for i, (m, k, s) in enumerate(raw)]
    a = at(attempt_min // 60, attempt_min % 60)
    r = check_attempt(a, vs)
    near = [v for v in vs if abs(v.event.timestamp - a) <= TOLERANCE]
    seen_courier = [v for v in near if v.visit_type == VisitType.DELIVERY and v.source in ("bedrock", "ring")]
    later = [v for v in vs if v.event.timestamp - a > TOLERANCE and v.visit_type == VisitType.DELIVERY
             and v.source in ("bedrock", "ring")]
    guessed = [v for v in near if v.source == "rules" or v.visit_type == VisitType.UNKNOWN]
    if not vs:
        assert r["outcome"] == NO_DOOR_DATA
    elif seen_courier:
        assert r["outcome"] == COURIER_AT_DOOR
    elif later:
        assert r["outcome"] == COURIER_LATER
    elif not near:
        assert r["outcome"] == NOTHING_RECORDED
    elif guessed:
        assert r["outcome"] == UNVERIFIED
    else:
        assert r["outcome"] == SOMEONE_NOT_COURIER
    if r["outcome"] == COURIER_AT_DOOR:  # the cited visit really is a Bedrock courier inside the tolerance
        cited = next(v for v in vs if v.event.id == r["visit"]["event_id"])
        assert cited in seen_courier


def test_api_attempt(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: [visit(19, 30, tz=main.settings.tz)])
    c = TestClient(main.app)
    r = c.get("/api/attempt", params={"at": "2026-10-07T19:32"})
    assert r.status_code == 200 and r.json()["outcome"] == COURIER_AT_DOOR
    assert c.get("/api/attempt", params={"at": "nope"}).status_code == 400
    future = (datetime.now(main.settings.tz) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    assert c.get("/api/attempt", params={"at": future}).status_code == 400


def test_dashboard_checks_an_attempt_time(monkeypatch, tmp_path):
    from app.ring import RingSource
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    c = TestClient(main.app)
    page = c.get("/", params={"date": "2026-10-07", "attempt": "03:17"}).text
    assert "The carrier's tracking says" in page and "recorded nothing within 10 minutes of 03:17" in page
    assert c.get("/", params={"date": "2026-10-07", "attempt": "late"}).status_code == 400
