from datetime import date
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.briefing import build_briefing
from app.classify.stub import StubClassifier
from app.main import app
from app.ring.mock import MockRingClient

TZ = ZoneInfo("America/Los_Angeles")


def test_mock_is_deterministic():
    ring = MockRingClient(TZ)
    a = [e.timestamp for e in ring.events_for_day(date(2026, 10, 7))]
    b = [e.timestamp for e in ring.events_for_day(date(2026, 10, 7))]
    assert a == b and a


def test_weekday_briefing_has_traffic_and_hours():
    b = build_briefing(MockRingClient(TZ), StubClassifier(), date(2026, 10, 7), "Test Shop")
    assert b.customers > 0 and b.customers_last_week > 0
    assert b.busiest_hour is not None
    assert b.opened_at and b.opened_at < "08:00"
    assert sum(b.hourly_customers) == b.customers
    assert b.headline


def test_sunday_is_closed():
    b = build_briefing(MockRingClient(TZ), StubClassifier(), date(2026, 10, 11), "Test Shop")
    assert b.customers == 0 and b.opened_at is None
    assert len(b.missed_deliveries) == 1


def test_dashboard_and_api_render():
    c = TestClient(app)
    assert "daily briefing" in c.get("/?date=2026-10-07").text
    assert c.get("/api/briefing?date=2026-10-07").json()["customers"] > 0
    assert c.get("/api/events?date=2026-10-07").json()
    assert c.get("/?date=nope").status_code == 400
