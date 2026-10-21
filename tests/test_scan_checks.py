"""Carrier scan checks worked out by hand: each case below states the door, the scan and the answer a
person reading the door log would give, independently of how app/reconcile.py computes it. Also the
cases the judge audit found: rule guesses treated as couriers, times that haven't happened, clocks
changing. Local only."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import reconcile as rc
from app.classify.rules import RulesClassifier
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.reconcile import check_attempt, check_closed_scan

LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)  # a Wednesday
OPEN = (time(8), time(18))
NOW = datetime(2027, 1, 1, 12, 0, tzinfo=LA)  # after every case below


def visit(hh, mm, kind=VisitType.CUSTOMER, source="rules", eid=None, ring_event="motion", day=DAY, fold=0):
    e = DoorEvent(eid or f"ev-{hh:02d}{mm:02d}", "d", datetime(day.year, day.month, day.day, hh, mm, tzinfo=LA,
                                                                fold=fold), ring_event, "ring")
    return ClassifiedVisit(e, kind, 0.9, "why", source, "rid" if source == "bedrock" else None)


def at(hh, mm, day=DAY, fold=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=LA, fold=fold)


# ---- one worked day, every business-closed outcome by hand --------------------------------------

# 07:50 staff (Bedrock) · 09:00 customer (Bedrock) · 11:00 courier (Ring alert) · 12:30 customer (rules)
# 13:00 customer (Bedrock) · 14:06 doorbell press (rules) · 16:00 customer (Bedrock)
WORKED_DAY = [
    visit(7, 50, VisitType.STAFF, "bedrock"), visit(9, 0, VisitType.CUSTOMER, "bedrock"),
    visit(11, 0, VisitType.DELIVERY, "ring", eid="pkg"), visit(12, 30),
    visit(13, 0, VisitType.CUSTOMER, "bedrock"),
    visit(14, 6, VisitType.DELIVERY, "rules", ring_event="ding"), visit(16, 0, VisitType.CUSTOMER, "bedrock"),
]


@pytest.mark.parametrize("scan, outcome", [
    ((7, 0), rc.POSTED_CLOSED),         # before opening: the shop's own hours agree with the scan
    ((19, 0), rc.POSTED_CLOSED),        # after closing
    ((11, 5), rc.OPEN_COURIER_CAME),    # Ring's courier 5 min before the scan
    ((10, 0), rc.COURIER_LATER),        # no courier near 10:00, Ring's courier came at 11:00
    ((12, 35), rc.UNVERIFIED),          # a rules-guessed "customer" at 12:30 could have been the courier
    ((14, 5), rc.UNVERIFIED),           # a doorbell press 1 min later, no frame: can't say no courier came
    ((13, 2), rc.OPEN_NO_COURIER),      # only a Bedrock-confirmed customer within 10 min, busy around it
    ((15, 0), rc.NO_COVERAGE),          # open, nothing within 30 min, no Ring status: can't tell
])
def test_worked_day_business_closed(scan, outcome):
    assert check_closed_scan(at(*scan), WORKED_DAY, OPEN, now=NOW)["outcome"] == outcome


@pytest.mark.parametrize("attempt, outcome", [
    ((11, 3), rc.COURIER_AT_DOOR),       # Ring's courier 3 min before
    ((14, 0), rc.UNVERIFIED),            # a press 6 min later, no frame
    ((9, 4), rc.COURIER_LATER),          # a Bedrock customer near 09:04, Ring's courier at 11:00
    ((15, 55), rc.SOMEONE_NOT_COURIER),  # Bedrock saw a customer at 16:00 and no courier came later
    ((20, 0), rc.NO_COVERAGE),           # nothing within 30 min, nothing after, no Ring status: can't tell
    ((16, 20), rc.NOTHING_RECORDED),     # nothing within 10 min, but the 16:00 customer shows it was recording
])
def test_worked_day_attempts(attempt, outcome):
    assert check_attempt(at(*attempt), WORKED_DAY, now=NOW)["outcome"] == outcome


def test_no_door_data_for_either_claim():
    assert check_attempt(at(12, 0), [], now=NOW)["outcome"] == rc.NO_DOOR_DATA
    assert check_closed_scan(at(12, 0), [], OPEN, now=NOW)["outcome"] == rc.NO_DOOR_DATA


# ---- rule guesses are not couriers, and not "no courier" either ----------------------------------

def test_rules_doorbell_press_after_a_closed_scan_is_unverified_not_no_courier():
    rules = RulesClassifier()
    press = rules.classify(DoorEvent("p", "d", at(14, 6), "ding", "ring"), OPEN)
    motion = rules.classify(DoorEvent("m", "d", at(13, 50), "motion", "ring"), OPEN)
    r = check_closed_scan(at(14, 5), [press, motion], OPEN, now=NOW)
    assert r["outcome"] == rc.UNVERIFIED and r["visit"]["event_id"] == "p"
    assert "no courier" not in r["text"].lower() and "can't say" in r["text"]


def test_closed_scan_counts_only_confirmed_customers():
    vs = [visit(13, 40), visit(13, 45), visit(13, 50, VisitType.CUSTOMER, "bedrock"),
          visit(14, 20, VisitType.CUSTOMER, "bedrock"), visit(14, 25, ring_event="ding", kind=VisitType.DELIVERY)]
    r = check_closed_scan(at(14, 5), vs, OPEN, now=NOW)
    a = r["activity"]
    assert (a["events"], a["motion"], a["presses"], a["customers"], a["guessed_customers"]) == (5, 4, 1, 2, 2)
    assert "4 motion events and 1 doorbell press" in r["text"] and "2 of them confirmed from the camera frame as customers" in r["text"]


# ---- the pitch case: scanned "business closed", the courier came later -------------------------

def test_courier_later_the_same_day_is_not_no_courier():
    vs = [visit(12, 13), visit(12, 39), visit(12, 50, VisitType.CUSTOMER, "bedrock"),
          visit(13, 2, VisitType.DELIVERY, "ring", eid="pkg")]
    r = check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    assert r["outcome"] == rc.COURIER_LATER and r["visit"]["event_id"] == "pkg" and r["visit"]["time"] == "13:02"
    assert "13:02" in r["text"] and "22 min after" in r["text"] and "can't tell whose" in r["text"]


def test_a_courier_the_next_day_is_not_later_that_day():
    nxt = DAY + timedelta(days=1)
    r = check_attempt(at(20, 0), [visit(9, 0, VisitType.DELIVERY, "bedrock", day=nxt)], now=NOW)
    assert r["outcome"] == rc.NO_COVERAGE  # not a courier later; and nothing around 20:00 at all


# ---- times that haven't happened, or never existed ------------------------------------------------

def test_future_times_are_refused():
    now = datetime(2026, 10, 7, 12, 5, tzinfo=LA)
    for check in (lambda t: check_attempt(t, WORKED_DAY, now=now),
                  lambda t: check_closed_scan(t, WORKED_DAY, OPEN, now=now)):
        r = check(at(17, 30))
        assert r["outcome"] == rc.NOT_YET and "hasn't happened yet" in r["text"] and r["visit"] is None
        assert check(at(17, 30, day=DAY + timedelta(days=3)))["outcome"] == rc.NOT_YET
        assert check(at(12, 0))["outcome"] != rc.NOT_YET


def test_future_check_defaults_to_the_real_clock():
    tomorrow = datetime.now(UTC).astimezone(LA) + timedelta(days=1)
    assert check_attempt(tomorrow, WORKED_DAY)["outcome"] == rc.NOT_YET


def test_spring_forward_gap_is_refused_by_name():
    spring = date(2026, 3, 8)  # 02:00 PST jumps to 03:00 PDT
    vs = [visit(3, 31, VisitType.DELIVERY, "bedrock", day=spring)]
    for r in (check_attempt(at(2, 30, day=spring), vs, now=NOW),
              check_closed_scan(at(2, 30, day=spring), vs, OPEN, now=NOW)):
        assert r["outcome"] == rc.NO_SUCH_TIME and "02:30" in r["text"] and "clocks" in r["text"]


# ---- clocks going back: compare instants, not wall-clock readings --------------------------------

FALL = date(2026, 11, 1)  # 02:00 PDT goes back to 01:00 PST; 01:xx happens twice


def test_fall_back_gap_is_measured_in_real_minutes():
    courier = visit(1, 58, VisitType.DELIVERY, "bedrock", day=FALL, fold=0)  # 01:58 PDT = 08:58 UTC
    scan = at(1, 3, day=FALL, fold=1)  # 01:03 PST = 09:03 UTC, five real minutes later
    r = check_attempt(scan, [courier], now=NOW)
    assert r["outcome"] == rc.COURIER_AT_DOOR and "5 min before" in r["text"]
    # and the reverse: 01:03 PDT and 01:58 PST are 115 real minutes apart, not 55
    far = visit(1, 58, VisitType.DELIVERY, "bedrock", day=FALL, fold=1)
    assert check_attempt(at(1, 3, day=FALL, fold=0), [far], now=NOW)["outcome"] == rc.COURIER_LATER


def test_ambiguous_scan_time_says_which_reading_it_used():
    r = check_attempt(at(1, 30, day=FALL), [visit(1, 31, VisitType.DELIVERY, "bedrock", day=FALL)], now=NOW)
    assert "happened twice" in r["text"]


instants = st.integers(0, 26 * 3600 - 1)  # seconds from 00:00 PDT on the fall-back day (25 real hours)
FALL_START = datetime(2026, 11, 1, 0, 0, tzinfo=LA).astimezone(UTC)


def _la(sec: int) -> datetime:
    return (FALL_START + timedelta(seconds=sec)).astimezone(LA)


@settings(max_examples=300, deadline=None)
@given(instants, st.lists(st.tuples(instants, st.sampled_from([VisitType.DELIVERY, VisitType.CUSTOMER]),
                                    st.sampled_from(["bedrock", "ring", "rules"])), max_size=6))
def test_attempt_tolerance_is_real_time_across_fall_back(attempt_s, raw):
    """The oracle works in UTC seconds; the check gets shop-local datetimes with fold set by zoneinfo."""
    a = _la(attempt_s)
    vs = [ClassifiedVisit(DoorEvent(f"ev-{i}", "d", _la(s), "motion", "ring"), k, 0.9, "", src)
          for i, (s, k, src) in enumerate(raw)]
    r = check_attempt(a, vs, now=NOW)
    near_confirmed = [s for s, k, src in raw if abs(s - attempt_s) <= 600 and k == VisitType.DELIVERY
                      and src != "rules"]
    if near_confirmed:
        assert r["outcome"] == rc.COURIER_AT_DOOR
    else:
        assert r["outcome"] != rc.COURIER_AT_DOOR
