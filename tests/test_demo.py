"""The one-command demo: its day is never in the future, its orders import, its scan time is honest."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.orders import parse_orders
from scripts import demo

TZ = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("today", [date(2026, 10, 21) + timedelta(days=i) for i in range(7)])
def test_demo_day_is_a_past_open_day(today):
    d = demo.demo_day(today)
    assert d < today
    assert demo.open_hours(demo.SHOP_HOURS, d)


def test_monday_demo_skips_the_closed_sunday():
    assert demo.demo_day(date(2026, 10, 19)) == date(2026, 10, 17)  # Mon -> Sat


def test_sample_orders_import_on_the_demo_day():
    day = date(2026, 3, 4)
    parsed = parse_orders(demo.dated_orders(demo.SAMPLE.read_text(), day))
    assert not parsed["errors"]
    assert {r.day for r in parsed["rows"]} == {day.isoformat()}
    assert {r.order_ref for r in parsed["rows"]} >= {"PO-1041", demo.SCAN_ORDER}


def test_sample_names_no_real_carrier():
    text = demo.SAMPLE.read_text().lower()
    for mark in ("ups", "fedex", "usps", "dhl", "amazon"):
        assert mark not in text


def test_scan_time_has_activity_nearby_but_no_one_at_the_door():
    day = date(2026, 10, 20)
    at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=TZ)  # noqa: E731
    events = [at(13, 2), at(13, 50), at(14, 20), at(14, 25), at(16, 40)]
    scan = demo.pick_scan_time(day, events, (8, 18), [at(10, 12)], TZ)
    t = datetime.combine(day, datetime.strptime(scan, "%H:%M").time(), tzinfo=TZ)
    gaps = [abs((e - t).total_seconds()) / 60 for e in events]
    assert min(gaps) > 12 and sum(g <= 30 for g in gaps) >= 2
    assert 8 <= t.hour < 18
