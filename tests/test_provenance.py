"""Every label says exactly what it rests on: a Ring alert with no frame never "shows" anything."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import dispute, main, pages, provenance
from app.deliveries import FrameStore, receipts
from app.models import ClassifiedVisit, DoorEvent, VisitType
from tests.test_dispute_pack import LA, at
from tests.test_dispute_pack import visit as dvisit

CASES = [  # labelled_by, event_source, has_frame -> kind, how_known, not_whose
    ("ring", "ring-webhook", False, provenance.RING_ALERT, "Ring's package_delivery alert, no frame",
     "Ring's package_delivery alert says a courier came, not whose order it was"),
    ("ring", "ring", True, provenance.RING_ALERT, "Ring's package_delivery alert; Shopfront holds its Ring frame",
     "Ring's package_delivery alert says a courier came, not whose order it was"),
    ("bedrock", "ring", True, provenance.BEDROCK_RING, "Bedrock read the Ring frame",
     "the Ring frame shows a courier, not whose order it was"),
    ("bedrock", "ring-live-browser", True, provenance.BEDROCK_LIVE,
     "Bedrock read a frame captured in your browser from Ring live view",
     "the frame captured in your browser from Ring live view shows a courier, not whose order it was"),
    ("rules", "ring", False, provenance.RULES, "Guess from the time of day",
     "the time-of-day rules only guessed a courier, and can't say whose order it was"),
    ("ring", "ring-webhook-replay", False, provenance.REPLAY,
     "Replayed Ring package_delivery alert from Shopfront's feeder, not a Ring notification, no frame",
     "the replayed package_delivery alert says a courier came, not whose order it was"),
    ("rules", "synthetic", False, provenance.SYNTHETIC,
     "Synthetic demo event, no camera recorded it; guess from the time of day",
     "it's a synthetic demo event: no camera saw a courier, let alone whose order it was"),
]


@pytest.mark.parametrize("by,src,frame,kind,known,whose", CASES)
def test_each_source_has_its_own_exact_wording(by, src, frame, kind, known, whose):
    row = {"labelled_by": by, "event_source": src}
    assert provenance.kind(by, src) == kind
    assert provenance.how_known(by, src, frame) == known
    assert provenance.not_whose(row) == whose
    if not frame:  # nothing without a frame may talk about one as if it existed
        assert "frame shows" not in whose and "in the Ring frame" not in provenance.said_by(row)


def test_page_reason_drops_what_the_prefix_already_says():
    assert pages.how("ring", "Ring Smart Alert: package_delivery", "ring-webhook") == \
        "Ring's package_delivery alert, no frame"
    assert pages.how("rules", "doorbell press during open hours (synthetic event has no frame)", "synthetic") == \
        "Synthetic demo event, no camera recorded it; guess from the time of day: doorbell press during open hours"
    assert pages.how("none", "no frames labelled", "ring-live-browser") == "none: no frames labelled"


DAY = date(2026, 10, 14)


def _ring_alert(eid="pkg", source="ring-webhook"):
    e = DoorEvent(eid, "d", at(10, 12), "motion", source, ring_subtype="package_delivery")
    return ClassifiedVisit(e, VisitType.DELIVERY, 0.8, "Ring Smart Alert: package_delivery", "ring")


def test_dashboard_never_says_the_frame_shows_for_a_ring_alert_without_one(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: [_ring_alert()])
    main.book.add(DAY, "Hillside Mill", "10:00", "11:00")
    main.book.add(DAY, "Pinecrest Paper Goods")
    html = TestClient(main.app).get("/", params={"date": DAY.isoformat()}).text
    assert "Could also be Pinecrest Paper Goods's courier: Ring&#39;s package_delivery alert says a courier " \
           "came, not whose order it was." in html
    assert "the frame shows" not in html and "Ring&#39;s package_delivery alert, no frame" in html


def test_late_note_and_finding_say_what_the_label_rests_on(tmp_path):
    from app import reconcile as rc
    from app.deliveries import Expected
    vs = [dvisit(9, 50), dvisit(9, 55)] + [_ring_alert(source="ring-webhook-replay")]
    flour = Expected("exp-f", DAY.isoformat(), "Hillside Mill", "10:00", "11:00", order_ref="PO-1041")
    paper = Expected("exp-p", DAY.isoformat(), "Pinecrest Paper Goods")
    receipt = receipts([flour, paper], vs, (at(8, 0).time(), at(18, 0).time()), FrameStore(tmp_path))["expected"][0]
    assert receipt["also_fits"] == ["Pinecrest Paper Goods"]
    check = rc.check_closed_scan(at(9, 40), vs, (at(8, 0).time(), at(18, 0).time()), now=at(23, 0))
    p = dispute.build_pack("Corner Bakery", DAY, vs, FrameStore(tmp_path), "08:00–18:00", check, flour, receipt,
                           "Northline Parcel", tz=LA)
    assert "a replayed Ring package_delivery alert, not a Ring notification" in p["finding"]["text"]
    assert "the replayed package_delivery alert says a courier came, not whose order it was" in p["finding"]["text"]
    body = p["draft"]["body"]
    assert "(The replayed package_delivery alert says a courier came, not whose order it was: it could also " \
           "have been for Pinecrest Paper Goods.)" in body
    assert "frame" not in body.replace("no camera frame", "")
