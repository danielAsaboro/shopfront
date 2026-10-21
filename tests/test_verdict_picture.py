"""The scan's verdict: one line, three short facts, one provenance chip, a picture of the door, Details for the rest.

Round 4: the check page and the pack led with a ~90-word paragraph. The line stays; the paragraph moves under
Details (every caveat in it is still on the page), and three facts with a tick or a cross say what decided it.
The door around the scan is drawn as an inline SVG strip (role=img, with the same picture in words).
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import dispute, pages
from app import reconcile as rc
from app.classify.rules import RulesClassifier
from app.deliveries import DeliveryBook, FrameStore
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.ring import RingSource
from app.ring.webhooks import WebhookLog
from app.shop import ShopProfile

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
OPEN = (time(8), time(18))
NOW = datetime(2026, 10, 14, 20, 0, tzinfo=TZ)


def at(hh, mm):
    return datetime.combine(DAY, time(hh, mm), tzinfo=TZ)


def visit(hh, mm, kind=VisitType.CUSTOMER, by="rules", src="ring", ring_event="motion", sub=None):
    e = DoorEvent(f"ev-{hh:02d}{mm:02d}", "d", at(hh, mm), ring_event, src, ring_subtype=sub)
    return ClassifiedVisit(e, kind, 0.9, "why", by, "rid" if by == "bedrock" else None)


def person(hh, mm, src="ring"):
    return visit(hh, mm, VisitType.PERSON, "ring", src, sub="human")


BUSY = [person(14, 2), person(14, 12), person(14, 25), visit(14, 36, VisitType.DOORBELL_PRESS, ring_event="ding"),
        person(14, 55), visit(14, 59)]


# ---- three facts ----------------------------------------------------------------------------------------------

def texts(c):
    return [(f["ok"], f["text"]) for f in pages.verdict_facts(c)]


def test_busy_door_is_three_short_facts():
    c = rc.check_closed_scan(at(14, 30), BUSY, OPEN, now=NOW)
    assert c["outcome"] == rc.OPEN_DOOR_BUSY
    assert texts(c) == [(True, "Open 08:00–18:00"), (True, "6 door events 14:00–15:00, 4 people (Ring)"),
                        (False, "Courier not identified")]
    assert all(len(f["text"].split()) <= 12 for f in pages.verdict_facts(c))


def test_demo_people_are_never_called_rings():
    vs = [person(14, 2, "synthetic"), person(14, 12, "synthetic"), visit(14, 50, src="synthetic")]
    c = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    assert texts(c)[1] == (True, "3 door events 14:00–15:00, 2 people (demo alerts)")
    assert pages.chip(c["event_source"]) == "demo"


@pytest.mark.parametrize("vs,scan,outcome,facts", [
    ([visit(14, 5), visit(14, 28, VisitType.DELIVERY, "ring")], (14, 30), rc.OPEN_COURIER_CAME,
     [(True, "Open 08:00–18:00"), (True, "2 door events 14:00–15:00"), (True, "Courier at the door at 14:28 (Ring alert)")]),
    ([visit(14, 5), visit(14, 27, by="bedrock"), visit(14, 50)], (14, 30), rc.OPEN_NO_COURIER,
     [(True, "Open 08:00–18:00"), (True, "3 door events 14:00–15:00"),
      (False, "No courier within 10 min of the scan (checked from the frame)")]),
    ([visit(19, 10)], (19, 30), rc.POSTED_CLOSED,
     [(False, "Closed by your posted hours (08:00–18:00)"), (True, "1 door event 19:00–20:00")]),
    ([visit(9, 0)], (14, 30), rc.NO_COVERAGE,
     [(True, "Open 08:00–18:00"), (False, "No door events 14:00–15:00"),
      (False, "Nothing shows the camera was watching at 14:30")]),
    ([visit(14, 28, VisitType.DOORBELL_PRESS, ring_event="ding")], (14, 30), rc.UNVERIFIED,
     [(True, "Open 08:00–18:00"), (True, "1 door event 14:00–15:00"), (False, "Courier not identified")]),
])
def test_closed_scan_facts_per_outcome(vs, scan, outcome, facts):
    c = rc.check_closed_scan(at(*scan), vs, OPEN, now=NOW)
    assert c["outcome"] == outcome
    assert texts(c) == facts


@pytest.mark.parametrize("vs,outcome,second", [
    ([visit(19, 30, VisitType.DELIVERY, "ring")], rc.COURIER_AT_DOOR, (True, "Courier at the door at 19:30 (Ring alert)")),
    ([visit(19, 28, by="bedrock")], rc.SOMEONE_NOT_COURIER, (False, "Not a courier: customer at 19:28 (Bedrock, Ring frame)")),
    ([visit(19, 28, VisitType.DOORBELL_PRESS, ring_event="ding")], rc.UNVERIFIED, (False, "Courier not identified")),
    ([visit(9, 0), visit(19, 5)], rc.NOTHING_RECORDED, (False, "No courier seen")),
])
def test_attempt_facts_say_near_who_and_the_camera(vs, outcome, second):
    c = rc.check_attempt(at(19, 32), vs, now=NOW)
    assert c["outcome"] == outcome
    fs = texts(c)
    assert len(fs) == 3 and fs[1] == second
    assert fs[2][1].startswith(("Camera", "Nothing shows the camera"))


def test_refused_scans_have_no_facts():
    c = rc.check_closed_scan(at(21, 0), BUSY, OPEN, now=at(20, 0))
    assert c["outcome"] == rc.NOT_YET and pages.verdict_facts(c) == []


def test_no_fact_states_a_guess_as_a_courier():
    """A rules-labelled 'delivery' near the scan is a guess: never 'Courier at the door'."""
    vs = [visit(14, 5), visit(14, 28, VisitType.DELIVERY, "rules", ring_event="ding"), visit(14, 50)]
    c = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    assert not any("Courier at" in t for _, t in texts(c))


# ---- the picture ----------------------------------------------------------------------------------------------

def test_strip_marks_every_event_in_the_hour_and_the_scan_in_the_middle():
    c = rc.check_closed_scan(at(14, 30), BUSY + [visit(13, 50), visit(15, 10)], OPEN, now=NOW)
    s = pages.door_strip(c, "08:00–18:00", {"order_ref": "PO-1", "window_start": "13:00", "window_end": "16:00"})
    assert s["scan"] == 50.0 and s["scan_label"] == "Scan 14:30"
    assert [d["x"] for d in s["dots"]] == [3.33, 20.0, 41.67, 60.0, 91.67, 98.33]  # 13:50 and 15:10 are outside
    assert [d["kind"] for d in s["dots"]] == ["person", "person", "person", "guess", "person", "guess"]
    assert s["band"] == {"x": 0.0, "w": 100.0, "label": "Open 08:00–18:00"}
    assert s["near"]["x"] == pytest.approx(33.33) and s["near"]["w"] == pytest.approx(33.34, abs=0.02)
    assert s["window"]["label"] == "PO-1 due 13:00–16:00"
    assert [t["label"] for t in s["ticks"]] == ["14:00", "14:15", "14:30", "14:45", "15:00"]
    assert s["text"].startswith("Door from 14:00 to 15:00. Posted hours 08:00–18:00 cover all of it. Carrier's scan at "
                                "14:30;")
    assert "14:36 doorbell press, not identified" in s["text"] and "14:02 person (Ring alert)" in s["text"]


def test_strip_shades_only_the_open_part_and_names_a_confirmed_courier():
    vs = [visit(17, 50, VisitType.DELIVERY, "ring", "ring-webhook-replay")]
    c = rc.check_attempt(at(17, 45), vs, now=NOW)
    s = pages.door_strip(c, "08:00–18:00")
    assert s["band"]["x"] == 0.0 and s["band"]["w"] == pytest.approx(75.0)  # 17:15 to 18:00 of 17:15–18:15
    assert "cover the shaded part" in s["text"]
    assert s["dots"] == [{"x": pytest.approx(58.33), "kind": "courier", "label": "17:50 courier (replayed alert)"}]
    closed = pages.door_strip(rc.check_closed_scan(at(19, 30), [visit(19, 10)], OPEN, now=NOW), "08:00–18:00")
    assert closed["band"] is None and "All of it is outside the posted hours (08:00–18:00)." in closed["text"]


def test_strip_without_events_says_so():
    s = pages.door_strip(rc.check_attempt(at(12, 0), [visit(9, 0)], now=NOW), "closed")
    assert s["dots"] == [] and s["text"].endswith("No door events in this hour.")
    assert pages.door_strip(None) is None


# ---- on the pages --------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "profile", ShopProfile(tmp_path / "shop.json", "Corner Bakery", ""))
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "expected.json"))
    monkeypatch.setattr(main, "frames", FrameStore(tmp_path))
    monkeypatch.setattr(main, "webhook_log", WebhookLog(tmp_path / "hooks.jsonl"))
    monkeypatch.setattr(main, "_aws_creds_available", lambda region: False)
    monkeypatch.setattr(main, "_visits", lambda day: list(BUSY) if day == DAY else [])
    return TestClient(main.app)


def visible(html: str) -> str:
    return re.sub(r"<details.*?</details>", "", html, flags=re.S)


def test_check_page_has_the_line_facts_chip_picture_and_details(client):
    page = client.get(f"/?date={DAY}&attempt=14:30&claim=business_closed&carrier=Northline").text
    check = rc.check_closed_scan(at(14, 30), BUSY, OPEN, now=NOW)
    assert "You were open: your posted hours and a busy door say so." in page
    assert "6 door events 14:00–15:00, 4 people (Ring)" in page and "Courier not identified" in page
    assert '<span class="badge b-ring"' in page
    svg = re.search(r'<svg class="strip-svg"[^>]*role="img"[^>]*aria-labelledby="chk-t chk-d"', page)
    assert svg, "the strip is an image with a title and a description"
    assert "<desc id=\"chk-d\">Door from 14:00 to 15:00." in page
    assert len(re.findall(r'<circle class="s-dot s-\w+" cx="[\d.]+%"', page)) == 6  # one per event, legend aside
    # the long reasoning is still on the page, under Details, with its caveats; not in the visible verdict
    assert check["text"] in html.unescape(page)
    assert "can&#39;t say whether a courier came to the door" in page
    assert "can&#39;t say whether a courier came to the door" not in visible(page)


def test_pack_page_has_the_same_picture_and_prints_its_words(client):
    page = client.get(f"/dispute?date={DAY}&at=14:30&claim=business_closed").text
    assert '<svg class="strip-svg"' in page and 'aria-labelledby="pack-t pack-d"' in page
    assert '<p class="strip-words">Door from 14:00 to 15:00.' in page
    assert "Courier not identified" in page
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    assert ".strip-words { position: static;" in css  # the words print under the picture
    p = client.get(f"/api/dispute?date={DAY}&at=14:30&claim=business_closed").json()
    assert p["finding"]["text"] in dispute.as_text(p)  # the zip keeps the whole reasoning
