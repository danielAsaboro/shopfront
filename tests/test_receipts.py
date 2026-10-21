"""Receipts only from couriers something looked at: Ring's package_delivery alert or Bedrock on the
frame. A doorbell press the time-of-day rules call "delivery" is a possible courier, never a receipt,
never "seen" and never money. Local only."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.classify.rules import RulesClassifier
from app.deliveries import (
    STATUS_TEXT,
    Expected,
    FrameStore,
    headline,
    money_line,
    receipts,
)
from app.models import ClassifiedVisit, DoorEvent, VisitType

LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)
OPEN = (time(8), time(18))
NO_FRAMES = FrameStore.__new__(FrameStore)
NO_FRAMES.dirs = []
RULES = RulesClassifier()


def ev(hh, mm, kind="ding", eid=None):
    return DoorEvent(eid or f"ev-{hh:02d}{mm:02d}", "d", datetime(2026, 10, 7, hh, mm, tzinfo=LA), kind, "ring")


def seen(hh, mm, source="ring", eid=None):
    return ClassifiedVisit(ev(hh, mm, "motion", eid), VisitType.DELIVERY, 0.95, "courier with a parcel", source)


def staff(hh, mm, source="bedrock"):
    return ClassifiedVisit(ev(hh, mm, "motion"), VisitType.STAFF, 0.9, "staff unlocking", source)


def test_a_customer_ringing_is_not_a_receipt():
    """The audit's case: rules used to label every press a delivery, so a customer at 10:15 became Bay Mills'
    receipt. Now the rules say only what Ring recorded (a doorbell press), and the matcher lists it as possible."""
    press = RULES.classify(ev(10, 15), OPEN)
    assert press.visit_type == VisitType.DOORBELL_PRESS and press.source == "rules"
    r = receipts([Expected("x1", DAY.isoformat(), "Bay Mills", "09:00", "11:00")], [press], OPEN, NO_FRAMES)
    row = r["expected"][0]
    assert row["status"] == "possible" and row["visit"] is None and r["seen"] == 0
    assert [p["time"] for p in row["possible"]] == ["10:15"] and row["possible"][0]["labelled_by"] == "rules"
    assert "not confirmed" in STATUS_TEXT["possible"]
    assert r["unexpected"] == [] and [u["time"] for u in r["unconfirmed"]] == ["10:15"]
    assert headline(r) == ["Possible courier for Bay Mills: a doorbell press at 10:15, not confirmed by a camera frame."]


def test_a_confirmed_courier_beats_a_guessed_one():
    press = RULES.classify(ev(9, 30), OPEN)
    r = receipts([Expected("x1", DAY.isoformat(), "Bay Mills", "09:00", "11:00")], [press, seen(10, 40)], OPEN, NO_FRAMES)
    row = r["expected"][0]
    assert row["status"] == "seen" and row["visit"]["time"] == "10:40" and row["visit"]["labelled_by"] == "ring"
    assert r["seen"] == 1 and headline(r) == ["All 1 expected deliveries reached the door."]


def test_nothing_in_the_window_is_not_seen_and_an_order_not_due_yet_is_expected():
    rows = [Expected("a", DAY.isoformat(), "Dairy Co", "06:00", "07:00"),
            Expected("b", DAY.isoformat(), "Bay Mills", "15:00", "17:00")]
    now = datetime(2026, 10, 7, 12, 5, tzinfo=LA)
    r = receipts(rows, [RULES.classify(ev(10, 15), OPEN)], OPEN, NO_FRAMES, now=now)
    by = {x["supplier"]: x["status"] for x in r["expected"]}
    assert by == {"Dairy Co": "not_seen", "Bay Mills": "expected"}
    assert headline(r) == ["No courier seen for Dairy Co."]


def test_staff_at_the_door_means_a_courier_before_opening_was_not_missed():
    """The bakery: staff in from 07:45, a courier at 07:47, opening at 08:00."""
    rows = [Expected("a", DAY.isoformat(), "Bay Mills", "07:00", "08:30")]
    r = receipts(rows, [staff(7, 45), seen(7, 47)], OPEN, NO_FRAMES)
    x = r["expected"][0]
    assert x["status"] == "seen" and x["visit"]["staff_in"] and not x["visit"]["shop_open"]
    assert r["closed_couriers"] == 0 and headline(r) == ["All 1 expected deliveries reached the door."]
    # without anyone in, the same courier came while the shop was closed
    r = receipts(rows, [seen(7, 47)], OPEN, NO_FRAMES)
    assert r["expected"][0]["status"] == "seen_while_closed" and r["closed_couriers"] == 1
    assert headline(r) == ["Bay Mills's courier was at the door at 07:47, while the shop was closed."]


def test_closed_couriers_count_only_confirmed_ones_matched_or_not():
    vs = [seen(19, 30), seen(20, 0, "bedrock"), RULES.classify(ev(21, 0), OPEN)]
    r = receipts([], vs, OPEN, NO_FRAMES)
    assert r["closed_couriers"] == 2 and len(r["unexpected"]) == 2 and len(r["unconfirmed"]) == 1


def test_money_line_names_what_the_door_saw():
    line = money_line(2, 1, 15, "$")
    assert line == ("Last 7 days: 2 couriers came while you were closed and 1 expected delivery with no courier "
                    "seen at the door. At your $15 per redelivery, that's $45.")


def test_rules_never_call_a_press_a_delivery():
    """Round 2: the rules can't see who pressed the bell, so no press, open or closed, is labelled "delivery"."""
    for hh, hours in ((10, OPEN), (6, OPEN), (21, OPEN), (10, None)):
        v = RULES.classify(ev(hh, 0), hours)
        assert v.visit_type == VisitType.DOORBELL_PRESS and v.visit_type != VisitType.DELIVERY
        assert v.visit_type.value.replace("_", " ") == "doorbell press"
