"""Expected deliveries, receipts, the owner's profile and the Ask agent. Local tests only: synthetic
events and a fake agent, never Ring or Bedrock."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import agent as shop_agent
from app.classify.rules import RulesClassifier
from app.deliveries import DeliveryBook, FrameStore, headline, money_line, receipts
from app.hours import parse_hours, posted_hours
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.ring import RingSource
from app.shop import Profile, ShopProfile, hours_from_form

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)  # a Wednesday, open 8-18
OPEN = posted_hours(parse_hours(""), DAY)


def courier(hh, mm, source="bedrock", eid=None):
    e = DoorEvent(id=eid or f"ev-{hh}{mm}", device_id="d", timestamp=datetime(2026, 10, 7, hh, mm, tzinfo=TZ),
                  kind="ding", source="ring")
    return ClassifiedVisit(e, VisitType.DELIVERY, 0.9, "courier holding a parcel", source, "rid-1")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "profile", ShopProfile(tmp_path / "shop.json", "Test Shop", ""))
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "expected.json"))
    monkeypatch.setattr(main, "frames", FrameStore(tmp_path))
    monkeypatch.setattr(main, "cache", main.ClassificationCache(None))
    return tmp_path


def test_expected_delivery_matched_inside_its_window(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    book.add(DAY, "Bay Mills flour", "09:00", "11:00")
    frames = FrameStore(tmp_path)
    frames.save("ev-1015", b"\xff\xd8jpeg")
    r = receipts(book.for_day(DAY), [courier(7, 5), courier(10, 15)], OPEN, frames)
    x = r["expected"][0]
    assert x["status"] == "seen" and x["visit"]["time"] == "10:15"
    assert x["visit"]["frame_url"] == "/frames/ev-1015.jpg" and x["visit"]["bedrock_request_id"] == "rid-1"
    assert [u["time"] for u in r["unexpected"]] == ["07:05"] and r["seen"] == r["total"] == 1


def test_courier_while_closed_and_missing_supplier_lead_the_headline(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    book.add(DAY, "Milk")
    book.add(DAY, "Coffee beans", "08:00", "09:00")
    r = receipts(book.for_day(DAY), [courier(19, 30)], OPEN, FrameStore(tmp_path))
    by = {x["supplier"]: x for x in r["expected"]}
    # Milk has no window, so the 19:30 courier fits it; Coffee beans was due 08:00-09:00
    assert by["Milk"]["status"] == "seen_while_closed" and by["Milk"]["also_fits"] == []
    assert by["Coffee beans"]["status"] == "not_seen"
    lines = headline(r)
    assert lines[0] == "No courier seen for Coffee beans." and "Milk's courier rang at 19:30" in lines[1]


def test_receipt_names_other_suppliers_the_courier_also_fits(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    book.add(DAY, "Bread", "09:00", "12:00")
    book.add(DAY, "Milk")
    r = receipts(book.for_day(DAY), [courier(10, 0)], OPEN, FrameStore(tmp_path))
    matched = [x for x in r["expected"] if x["visit"]]
    assert len(matched) == 1 and matched[0]["also_fits"] == ["Milk" if matched[0]["supplier"] == "Bread" else "Bread"]


def test_no_window_without_both_ends(tmp_path):
    with pytest.raises(ValueError):
        DeliveryBook(tmp_path / "e.json").add(DAY, "Milk", "09:00", "")
    with pytest.raises(ValueError):
        DeliveryBook(tmp_path / "e.json").add(DAY, "Milk", "11:00", "09:00")


def test_money_uses_only_the_owners_number():
    assert money_line(3, 1, None, "$") is None
    assert money_line(0, 0, 15, "$") is None
    line = money_line(3, 1, 12.5, "£")
    assert line.endswith("At your £12.5 per redelivery, that's £50.") and "3 couriers rang" in line


def test_hours_form_round_trip():
    assert hours_from_form(["8-18"] * 4 + ["8-19", "9-16", ""]) == "8-18,8-18,8-18,8-18,8-19,9-16,closed"
    with pytest.raises(ValueError):
        hours_from_form(["8-18"] * 6 + ["19-8"])


def test_setup_saves_profile_and_expected_delivery(isolated):
    c = TestClient(main.app)
    form = {"name": "Corner Bakery", "currency": "$", "redelivery_cost": "15",
            **{f"h{i}": "7-15" for i in range(6)}, "h6": ""}
    assert c.post("/setup", data=form, follow_redirects=False).status_code == 303
    p = main.profile.get()
    assert p.saved and p.hours.endswith("7-15,closed") and p.redelivery_cost == 15
    assert c.post("/deliveries", data={"day": "2026-10-07", "supplier": "Flour", "window_start": "06:00",
                                       "window_end": "08:00"}, follow_redirects=False).status_code == 303
    page = c.get("/setup").text
    assert "Flour" in page and "Corner Bakery" in page
    bad = c.post("/setup", data={**form, "h0": "18-8"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]


def test_dashboard_shows_receipt_and_money_line(isolated):
    main.profile.save(Profile("Test Shop", "8-18,8-18,8-18,8-18,8-19,9-16,closed", 10, "$"))
    main.book.add(date(2026, 10, 11), "Sunday bread")  # a closed day; synthetic courier rings at 11:xx
    c = TestClient(main.app)
    page = c.get("/?date=2026-10-11").text
    assert "Sunday bread" in page and "Courier rang while you were closed" in page
    assert "At your $10 per redelivery" in page
    assert c.get("/api/deliveries?date=2026-10-11").json()["seen"] == 1


def test_frames_are_local_only_and_name_checked(isolated):
    main.frames.save("live-abc", b"\xff\xd8jpeg")
    assert TestClient(main.app).get("/frames/live-abc.jpg").content == b"\xff\xd8jpeg"
    assert TestClient(main.app).get("/frames/..%2Fshop.json").status_code == 404
    assert TestClient(main.app, client=("203.0.113.9", 5000)).get("/frames/live-abc.jpg").status_code == 403


def test_agent_tools_answer_from_shop_data(isolated):
    t = main._agent_tools(date(2026, 10, 12))
    y = t.door_visits("yesterday")
    assert y["date"] == "2026-10-11" and y["posted_hours"] == "closed"
    assert any(v["type"] == "delivery" and not v["shop_open"] for v in y["visits"])
    assert len(t.week("2026-10-11")) == 7
    with pytest.raises(ValueError):
        t.door_visits("2026-12-01")


def test_ask_returns_answer_with_its_trace(isolated, monkeypatch):
    def fake_factory(tools, trace, shop, region, model_id):
        def run(question):
            trace.tools.append({"tool": "deliveries", "args": {"day": "yesterday"}})
            trace.bedrock_request_ids.append("rid-agent")
            return f"Yes, a courier rang at 11:20 while {shop} was closed.\nFrame: /frames/ev-1.jpg"
        return run
    monkeypatch.setattr(main, "agent_factory", fake_factory)
    d = TestClient(main.app).post("/api/ask", json={"question": "did bread come?"}).json()
    assert d["answer"].startswith("Yes") and "Frame:" not in d["answer"] and d["frame_url"] == "/frames/ev-1.jpg"
    assert d["tools"][0]["tool"] == "deliveries" and d["bedrock_request_ids"] == ["rid-agent"]


def test_ask_names_the_failure_instead_of_answering(isolated, monkeypatch):
    def broken(*a):
        def run(q):
            raise RuntimeError("NoCredentialsError")
        return run
    monkeypatch.setattr(main, "agent_factory", broken)
    r = TestClient(main.app).post("/api/ask", json={"question": "anything?"})
    assert r.status_code == 503 and r.json()["error"] == "AGENT_UNAVAILABLE"
    assert TestClient(main.app).post("/api/ask", json={"question": " "}).status_code == 400


def test_real_strands_agent_builds_with_its_tools(isolated):
    pytest.importorskip("strands")
    trace = shop_agent.Trace()
    a = shop_agent.build_agent(main._agent_tools(DAY), trace, "Test Shop", "us-east-1",
                               "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert sorted(a.tool_names) == ["check_attempt", "deliveries", "door_visits", "week"]
