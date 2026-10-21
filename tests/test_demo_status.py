"""The demo's made-up status checks: the camera-coverage line works in the demo, and says it was made up."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import coverage, main
from app.hours import parse_hours
from app.ring.synthetic import STATUS_EVERY_S, SyntheticRingClient

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)  # a Wednesday, open 8-18 by default


def test_synthetic_status_samples_cover_the_open_day_and_are_marked_demo():
    ring = SyntheticRingClient(TZ, parse_hours(""))
    s = ring.status_samples(DAY)
    assert s[0]["at"] == datetime(2026, 10, 7, 7, 0, tzinfo=TZ) and s[-1]["at"] == datetime(2026, 10, 7, 19, 0, tzinfo=TZ)
    assert all(x["online"] is True and x["demo"] and x["sampled"] and x["every_s"] == STATUS_EVERY_S for x in s)
    assert all((b["at"] - a["at"]).total_seconds() == STATUS_EVERY_S for a, b in zip(s, s[1:]))
    closed = SyntheticRingClient(TZ, parse_hours("8-18,8-18,closed,8-18,8-18,8-18,closed"))
    assert closed.status_samples(DAY) == []
    today = datetime.now(TZ).date()
    assert all(x["at"] <= datetime.now(TZ) for x in ring.status_samples(today))  # nothing from the future


def test_coverage_says_demo_checks_were_made_up_and_real_ones_are_rings():
    ring = SyntheticRingClient(TZ, parse_hours(""))
    scan = datetime(2026, 10, 7, 14, 30, tzinfo=TZ)
    demo = coverage.check(scan, [], ring.status_samples(DAY), timedelta(minutes=10))
    assert demo["state"] == coverage.ONLINE_BY_STATUS and demo["demo"] and demo["status_covered"]
    assert demo["text"].startswith("Demo status checks, made up by Shopfront's demo and not asked of Ring, said "
                                   "online every 5 minutes from 14:20 to 14:40 (10 minutes either side of the 14:30 "
                                   "scan).")
    real = [{**x, "demo": False} for x in ring.status_samples(DAY)]
    live = coverage.check(scan, [], real, timedelta(minutes=10))
    assert "demo" not in live and live["text"].startswith("Shopfront asked Ring every 5 minutes")


def test_a_synthetic_scan_check_uses_the_demo_status_checks():
    r = TestClient(main.app).get("/api/attempt", params={"at": "2026-10-07T14:30", "claim": "business_closed"}).json()
    assert r["coverage"]["state"] == coverage.ONLINE_BY_STATUS and r["coverage"]["demo"]
