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


def test_the_scan_is_a_fixed_afternoon_minute_not_a_quiet_one():
    """Round 2: the demo used to hunt for a minute with nothing near it. It now takes 14:30 in the dairy
    window, and only asks that the door was in use 10 to 30 minutes either side."""
    assert demo.SCAN_WINDOW[0] <= demo.SCAN_AT < demo.SCAN_WINDOW[1]
    at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=TZ)  # noqa: E731
    assert demo.door_in_use([at(14, 2), at(14, 31)], at(14, 30))  # 14:02 is 28 min away
    assert not demo.door_in_use([at(14, 25), at(14, 36)], at(14, 30))  # only events near enough to be the courier
    assert not demo.door_in_use([at(13, 50)], at(14, 30))


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

    from app import dispute, main
    from app.dispute import OPEN_FINDINGS

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
    # the scan case: "You were open" by the posted hours and a busy door; drafts to the supplier and the carrier
    assert story["check"]["outcome"] in OPEN_FINDINGS and story["scan"] == demo.SCAN_AT
    pack = story["pack"]
    assert [d["to"] for d in pack["drafts"]] == ["Valley Creamery"]
    assert pack["order"]["order_ref"] == demo.SCAN_ORDER
    assert pack["demo_data"] and all(d["subject"].startswith("[DEMO] ") for d in pack["drafts"])
    if pack["order"]["possible"]:  # the desk's "Possible courier" for the dairy order is named in the pack
        assert pack["order"]["unidentified"] in dispute.as_text(pack)
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


SWEEP_DAY = date(2026, 10, 20)  # the day the round-2 judges swept: 27 of 40 scans came back "can't say"


def test_business_closed_sweep_through_open_hours_yields_a_pack(monkeypatch):
    """A "business closed" scan every 15 minutes through the demo day's open hours. The rule: a pack with
    a draft whenever a Ring/Bedrock courier was within 10 minutes of the scan, or the door was in use
    within 30 minutes by something other than the unidentified events within 10 (any of which could have
    been the courier). Checked minute by minute against the door, and it must be the clear majority."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key="demo-key"))
    text = demo.dated_orders(demo.SAMPLE.read_text(), SWEEP_DAY)
    main.book.import_rows(parse_orders(text)["rows"])
    client = TestClient(main.app)
    demo.feed_package_alert(client, SWEEP_DAY, TZ, "demo-key")
    events = [(datetime.fromisoformat(e["time"]), e["classifier"] in ("ring", "bedrock") and e["type"] != "unknown",
               e["type"]) for e in client.get("/api/events", params={"date": SWEEP_DAY.isoformat()}).json()]
    opens, closes = demo.open_hours(demo.SHOP_HOURS, SWEEP_DAY)
    packs, rows = 0, []
    for m in range(opens * 60, closes * 60, 15):
        t = datetime(2026, 10, 20, m // 60, m % 60, tzinfo=TZ)
        mins = [(abs((at - t).total_seconds()) / 60, seen, kind) for at, seen, kind in events]
        courier_near = any(g <= 10 and seen and kind == "delivery" for g, seen, kind in mins)
        in_use = any(g <= 30 and (g > 10 or seen) for g, seen, _ in mins)
        p = client.get("/api/dispute", params={"date": SWEEP_DAY.isoformat(), "at": f"{t:%H:%M}",
                                               "claim": "business_closed", "carrier": demo.CARRIER,
                                               "tracking": "NP4471002958"}).json()
        rows.append((f"{t:%H:%M}", p["finding"]["outcome"]))
        assert bool(p["drafts"]) == (courier_near or in_use), (f"{t:%H:%M}", p["finding"]["outcome"])
        for d in p["drafts"]:
            assert "nobody came" not in d["body"].lower()
        packs += bool(p["drafts"])
    assert packs >= 0.9 * len(rows), rows
