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
                        (None, "No camera frame of the courier")]
    assert all(len(f["text"].split()) <= 12 for f in pages.verdict_facts(c))


def test_demo_people_are_never_called_rings():
    vs = [person(14, 2, "synthetic"), person(14, 12, "synthetic"), visit(14, 50, src="synthetic")]
    c = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    assert texts(c)[1] == (True, "3 door events 14:00–15:00, 2 person alerts")  # the chip says demo
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
     [(True, "Open 08:00–18:00"), (True, "1 door event 14:00–15:00"), (None, "No camera frame of the courier")]),
])
def test_closed_scan_facts_per_outcome(vs, scan, outcome, facts):
    c = rc.check_closed_scan(at(*scan), vs, OPEN, now=NOW)
    assert c["outcome"] == outcome
    assert texts(c) == facts


@pytest.mark.parametrize("vs,outcome,second", [
    ([visit(19, 30, VisitType.DELIVERY, "ring")], rc.COURIER_AT_DOOR, (True, "Courier at the door at 19:30 (Ring alert)")),
    ([visit(19, 28, by="bedrock")], rc.SOMEONE_NOT_COURIER, (False, "Not a courier: customer at 19:28 (Bedrock, Ring frame)")),
    ([visit(19, 28, VisitType.DOORBELL_PRESS, ring_event="ding")], rc.UNVERIFIED, (None, "No camera frame of the courier")),
    ([visit(9, 0), visit(19, 5)], rc.NOTHING_RECORDED, (False, "No courier seen")),
])
def test_attempt_facts_say_near_who_and_the_camera(vs, outcome, second):
    c = rc.check_attempt(at(19, 32), vs, now=NOW)
    assert c["outcome"] == outcome
    fs = texts(c)
    assert len(fs) == 3 and second in fs
    # the ticks and crosses first (the camera among them); a neutral note comes after them
    ticks = [f for f in fs if f[0] is not None]
    assert fs[:len(ticks)] == ticks
    assert any(t.startswith(("Camera", "Nothing shows the camera")) for _, t in ticks)


def test_no_courier_frame_is_a_neutral_note_not_a_cross():
    """Round 7: "✗ Courier not identified" read as "no proof anyone came". It is a note after the ticks; a frame
    Shopfront holds that nothing confirmed is said as such, and a Ring- or Bedrock-confirmed courier stays a tick."""
    held = rc.check_closed_scan(at(14, 30), BUSY, OPEN, frame_url=lambda i: f"/frames/{i}.jpg" if i == "ev-1436" else None,
                                now=NOW)
    assert texts(held)[-1] == (None, "No frame confirmed the courier")
    came = rc.check_closed_scan(at(14, 30), [visit(14, 5), visit(14, 28, VisitType.DELIVERY, "ring")], OPEN, now=NOW)
    assert texts(came)[-1] == (True, "Courier at the door at 14:28 (Ring alert)")


def test_an_empty_door_gets_no_ring_chip():
    """Round 7: a scan with no door events near it showed a RING chip on the synthetic demo."""
    assert pages.chip(None) == "" and pages.chip([]) == ""
    assert pages.chip(None, "synthetic") == "demo" and pages.chip([], "ring") == ""
    assert pages.chip(["ring"]) == "ring" and pages.chip(["synthetic"], "ring") == "demo"


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
    assert "6 door events 14:00–15:00, 4 people (Ring)" in page
    assert '<span class="na">No camera frame of the courier</span>' in page  # a note, no cross
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
    assert '<li class="na"><span class="mk" aria-hidden="true"></span><span>No camera frame of the courier</span>' in page
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    assert ".strip-words { display: block;" in css.split("@media print")[1]  # the words print under the picture
    p = client.get(f"/api/dispute?date={DAY}&at=14:30&claim=business_closed").json()
    assert p["finding"]["text"] in dispute.as_text(p)  # the zip keeps the whole reasoning


# ---- the landing fold (round 4) -----------------------------------------------------------------------------

def test_landing_leads_with_three_steps_and_the_check_then_the_deliveries(client):
    main.book.add(DAY, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042")
    page = client.get(f"/?date={DAY}").text
    steps = page.split('<section class="steps3"')[1].split("</section>")[0]
    assert [re.sub(r"<[^>]+>", "", x) for x in re.findall(r"<strong>(.*?)</strong>", steps)] == [
        "Carrier says closed", "Shopfront checks hours and the door", "You send the pack"]
    assert '<a class="primary" href="#atth">Check a carrier\'s scan</a>' in steps  # not a demo install
    assert page.index('class="steps3"') < page.index('<section class="lead">')  # the summary comes after


def test_the_date_picker_sits_under_the_title_at_every_width(client):
    """Round 7: on a phone the date picker fell between the carrier record and the headline. It sits beside the
    title on a desktop and right under it on a phone, labelled as an action next to the readable date."""
    page = client.get(f"/?date={DAY}&attempt=14:30&claim=business_closed").text
    top = page.split('<div class="desk-top">')[1]
    assert top.index("<h1>") < top.index('class="no-print day-pick"') < top.index('<section class="verdict')
    assert '<label class="field">Change day<input type="date"' in top
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    phone = css.split("@media (max-width: 560px) {")[1].split("@media print")[0]
    assert "order: 3" not in phone and ".value-quiet { display: none; }" in phone
    assert ".banner .b-long { display: none; }" in phone  # the banner is one line
    # round 6: with a verdict up, the desk's tagline gives way to it on every screen
    assert 'class="value' not in page.split("</style>")[-1] and 'class="steps3"' not in page


def test_the_answer_is_one_sentence_and_one_supporting_line(client):
    """Round 6: the demo's answer was ~90 words. The desk and the demo say one plain sentence and one line of
    facts; reconcile's full reasoning stays in the API's text and under Details."""
    c = client.get("/api/attempt", params={"at": f"{DAY}T14:30", "claim": "business_closed"}).json()
    a = pages.answer(c)
    assert a["sentence"] == pages.verdict(c) and a["sentence"].count(". ") == 0
    assert "\n" not in a["support"] and len(a["support"].split()) <= 20
    assert len((a["sentence"] + " " + a["support"]).split()) < len(c["text"].split()) // 2
    page = client.get(f"/?date={DAY}&attempt=14:30&claim=business_closed").text
    verdict = page.split('<section class="verdict')[1].split("</section>")[0]
    assert verdict.count('<p class="facts1"') == 1 and "facts3" not in verdict
    assert c["text"].split(" (")[0].replace('"', "&#34;") in verdict.split('class="tech why-verdict"')[1]
    assert pages.answer(None) == {"sentence": "", "support": ""}
