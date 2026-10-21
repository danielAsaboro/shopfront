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
    scan = demo.pick_scan_time(day, events, (8, 18), [at(10, 12)], TZ, demo.SCAN_WINDOW)
    assert scan is not None
    t = datetime.combine(day, datetime.strptime(scan, "%H:%M").time(), tzinfo=TZ)
    gaps = [abs((e - t).total_seconds()) / 60 for e in events]
    assert min(gaps) > 12 and sum(g <= 30 for g in gaps) >= 2
    assert 13 <= t.hour < 16 and t > at(10, 52)  # in the dairy window, well after Ring's 10:12 courier


def test_scan_time_is_none_when_the_door_never_goes_quiet():
    at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=TZ)  # noqa: E731
    busy = [at(h, m) for h in range(8, 18) for m in range(0, 60, 10)]
    assert demo.pick_scan_time(date(2026, 10, 20), busy, (8, 18), [], TZ) is None


def test_scan_never_comes_before_a_confirmed_courier():
    """A scan before Ring's courier reads "a courier came later", not the open-and-no-courier case."""
    at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=TZ)  # noqa: E731
    events = [at(9, 0), at(16, 30)]
    scan = demo.pick_scan_time(date(2026, 10, 20), events, (8, 18), [at(16, 0)], TZ)
    assert scan is not None and scan > "16:40"


# --- the demo's story against the app: what it prints is what the pages show --------------------------------
DEMO_DAYS = sorted({demo.demo_day(date(2026, 10, 21) - timedelta(days=i), usable=lambda d: demo.has_scan_slot(d, TZ))
                    for i in range(28)})


@pytest.mark.parametrize("day", DEMO_DAYS, ids=str)
def test_demo_story_matches_the_receiving_desk_and_yields_a_pack(day, monkeypatch):
    """The audit found the demo saying "flour courier at 10:12" while the desk gave the 10:12 alert to the
    windowless paper-goods invoice: the matcher's tie-break favoured the last-listed order when there
    were fewer couriers than orders. Run the demo's own data through the app and its matcher."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app import main
    from app.reconcile import OPEN_NO_COURIER

    key = "demo-key"
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=key))
    text = demo.dated_orders(demo.SAMPLE.read_text(), day)
    main.book.import_rows(parse_orders(text)["rows"])
    client = TestClient(main.app)
    demo.feed_package_alert(client, day, TZ, key)

    story = demo.walk(client, day, TZ, text)

    by_ref = {x["order_ref"]: x for x in client.get("/api/deliveries", params={"date": day.isoformat()}).json()["expected"]}
    flour = by_ref["PO-1041"]
    assert flour["supplier"] == "Hillside Mill" and flour["status"] == "seen"
    assert flour["visit"]["time"] == demo.PACKAGE_AT and flour["visit"]["labelled_by"] == "ring"
    assert by_ref["INV-2208"]["visit"] is None  # the windowless invoice doesn't take the flour courier
    assert "Pinecrest Paper Goods" in flour["also_fits"]  # but the desk says the alert could be its courier
    # the scan case: open by the posted hours, busy door, nothing near the scan; a draft to the supplier
    assert story["check"]["outcome"] == OPEN_NO_COURIER
    pack = story["pack"]
    assert pack["draft"]["to"] == "Valley Creamery" and pack["order"]["order_ref"] == demo.SCAN_ORDER
    assert pack["demo_data"] and pack["draft"]["subject"].startswith("[DEMO DATA")
    if pack["order"]["possible"]:  # the desk's "Possible courier" for the dairy order is in the draft too
        assert pack["order"]["unidentified"] in pack["draft"]["body"]
    page = client.get("/", params={"date": day.isoformat()}).text
    assert "Hillside Mill" in page and demo.PACKAGE_AT in page


def test_demo_stops_when_the_desk_disagrees(monkeypatch):
    """No package alert fed: the demo must refuse to print its 10:12 story."""
    from fastapi.testclient import TestClient

    from app import main

    day = DEMO_DAYS[-1]
    text = demo.dated_orders(demo.SAMPLE.read_text(), day)
    main.book.import_rows(parse_orders(text)["rows"])
    with pytest.raises(demo.DemoMismatch, match="PO-1041"):
        demo.walk(TestClient(main.app), day, TZ, text)
