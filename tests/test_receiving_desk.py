"""The receiving desk: orders and invoices in, Ring's package_delivery funnel, the "business closed"
check, the staff tap and the dispute pack. Local only: synthetic events, fakes, no Ring or Bedrock."""

import io
import json
import zipfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from app import dispute, main
from app import reconcile as rc
from app.briefing import classify_day
from app.classify.rules import RulesClassifier
from app.deliveries import DeliveryBook, Expected, FrameStore, receipts
from app.hours import parse_hours
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.orders import parse_orders
from app.reconcile import (
    COURIER_LATER,
    NO_DOOR_DATA,
    OPEN_BY_HOURS_ONLY,
    OPEN_COURIER_CAME,
    OPEN_NO_COURIER,
    POSTED_CLOSED,
    UNVERIFIED,
    check_attempt,
    check_closed_scan,
)
from app.ring import RingSource
from app.ring.synthetic import SyntheticRingClient
from app.ring.webhooks import WebhookLog, sign
from app.shop import ShopProfile

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)  # a Wednesday, open 8-18 by default
OPEN = (time(8), time(18))
KEY = "test-hmac-key"


def visit(hh, mm, kind=VisitType.CUSTOMER, source="rules", eid=None, ring_event="motion"):
    e = DoorEvent(eid or f"ev-{hh:02d}{mm:02d}", "d", datetime.combine(DAY, time(hh, mm), tzinfo=TZ), ring_event, "ring")
    return ClassifiedVisit(e, kind, 0.9, "why", source, "rid" if source == "bedrock" else None)


def at(hh, mm):
    return datetime.combine(DAY, time(hh, mm), tzinfo=TZ)


# ---- orders in -------------------------------------------------------------------------------

SHOPFRONT_CSV = """day,supplier,window_start,window_end,order_ref,carrier,tracking,items,amount
2026-10-07,Bay Mills,09:00,11:00,PO-1042,UPS,1Z999AA10123456784,"Flour 25kg x4",$212.40
2026-10-07,Dairy Co,,,PO-1043,,,Milk,
not-a-date,Broken,,,PO-1,,,,
2026-10-07,Half window,09:00,,PO-2,,,,
"""

AMAZON_CSV = """Order Date,Order ID,Seller Name,Title,Item Subtotal,Carrier Name & Tracking Number,Expected Delivery Date
10/05/2026,111-222,Acme Paper,Till rolls,$18.00,UPS(1Z111),10/07/2026
10/05/2026,111-222,Acme Paper,Receipt printer ink,$32.00,UPS(1Z111),10/07/2026
10/06/2026,111-333,Box Ltd,Cake boxes,$40.00,AMZL_US(TBA999),10/08/2026
"""


def test_shopfront_csv_imports_and_reports_bad_rows_by_line():
    out = parse_orders(SHOPFRONT_CSV)
    assert out["format"] == "shopfront" and [r.order_ref for r in out["rows"]] == ["PO-1042", "PO-1043"]
    flour = out["rows"][0]
    assert (flour.window_start, flour.carrier, flour.tracking, flour.amount) == ("09:00", "UPS", "1Z999AA10123456784", "$212.40")
    assert flour.origin == "csv:shopfront"
    assert len(out["errors"]) == 2 and out["errors"][0].startswith("line 4:") and "line 5:" in out["errors"][1]


def test_amazon_business_style_export_folds_items_and_splits_carrier():
    out = parse_orders(AMAZON_CSV)
    assert out["format"] == "amazon-business" and len(out["rows"]) == 2
    paper = out["rows"][0]
    assert (paper.day, paper.supplier, paper.order_ref) == ("2026-10-07", "Acme Paper", "111-222")
    assert (paper.carrier, paper.tracking) == ("UPS", "1Z111")
    assert paper.items == "Till rolls; Receipt printer ink"
    assert paper.amount == ""  # two item amounts are not a total; don't pick one


def test_import_needs_day_and_supplier_columns_and_reads_day_first():
    with pytest.raises(ValueError, match="no day"):
        parse_orders("supplier,amount\nX,1\n")
    with pytest.raises(ValueError, match="empty"):
        parse_orders("")
    assert parse_orders("Due date,Vendor\n07/10/2026,X\n", day_first=True)["rows"][0].day == "2026-10-07"


def test_reimport_updates_instead_of_duplicating_and_keeps_the_staff_tap(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    first = book.import_rows(parse_orders(SHOPFRONT_CSV)["rows"])
    flour = next(e for e in book.for_day(DAY) if e.order_ref == "PO-1042")
    book.mark_received(flour.id, "short", "one sack missing", at(10, 40))
    again = book.import_rows(parse_orders(SHOPFRONT_CSV.replace("Flour 25kg x4", "Flour 25kg x5"))["rows"])
    assert first == {"added": 2, "updated": 0} and again == {"added": 0, "updated": 2}
    rows = book.for_day(DAY)
    assert len(rows) == 2
    kept = next(e for e in rows if e.order_ref == "PO-1042")
    assert kept.id == flour.id and kept.items == "Flour 25kg x5" and kept.received["status"] == "short"


def test_staff_tap_validates_and_can_be_undone(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    e = book.add(DAY, "Milk")
    with pytest.raises(ValueError):
        book.mark_received(e.id, "lost", "", at(9, 0))
    with pytest.raises(KeyError):
        book.mark_received("exp-nope", "ok", "", at(9, 0))
    assert book.mark_received(e.id, "damaged", "crushed crate", at(9, 5)).received == {
        "status": "damaged", "note": "crushed crate", "at": "2026-10-07T09:05-07:00"}
    assert book.mark_received(e.id, "clear", "", at(9, 6)).received is None


def test_old_delivery_files_still_load(tmp_path):
    (tmp_path / "e.json").write_text(json.dumps([{"id": "exp-1", "day": DAY.isoformat(), "supplier": "Milk",
                                                  "window_start": None, "window_end": None, "note": ""}]))
    e = DeliveryBook(tmp_path / "e.json").for_day(DAY)[0]
    assert e.order_ref == "" and e.received is None and e.origin == "manual"


# ---- stage 0: Ring's package_delivery sub_type ----------------------------------------------

def hook(log, kind, hh, mm, sub_type=None, rid=None, replayed=False, when=None):
    ms = int((when or at(hh, mm)).timestamp() * 1000)
    attrs = {"source": "dev", "source_type": "devices", "timestamp": ms}
    if sub_type:
        attrs["sub_type"] = sub_type
    raw = json.dumps({"meta": {"version": "1.1", "time": "2026-10-07T16:00:00Z", "request_id": rid or f"r-{hh}{mm}{sub_type}"},
                      "data": {"id": f"dev_{kind}_{ms}", "type": kind, "attributes": attrs}}).encode()
    return log.handle(raw, sign(raw, KEY), KEY, replayed=replayed)


def test_stage0_routes_by_sub_type(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    hook(log, "motion_detected", 10, 15, "package_delivery")
    hook(log, "motion_detected", 10, 20, "vehicle")
    hook(log, "motion_detected", 10, 25, "other_motion")
    hook(log, "motion_detected", 10, 30, "human")
    hook(log, "motion_detected", 10, 35)  # Smart Alerts off: no sub_type
    hook(log, "button_press", 10, 40)
    hook(log, "motion_detected", 3, 0, "package_delivery", replayed=True)
    d = log.day(DAY, TZ)
    assert [v.event.timestamp.strftime("%H:%M") for v in d["ring_labelled"]] == ["10:15", "03:00"]
    assert all(v.source == "ring" and v.visit_type == VisitType.DELIVERY for v in d["ring_labelled"])
    assert d["ring_labelled"][1].event.source == "ring-webhook-replay"
    assert d["ring_labelled"][0].event.source == "ring-webhook"
    assert sorted(e.ring_subtype or "-" for e in d["skipped"]) == ["other_motion", "vehicle"]
    assert [(e.kind, e.ring_subtype) for e in d["events"]] == [("motion", "human"), ("motion", None), ("ding", None)]
    assert log.day(DAY + timedelta(days=1), TZ) == {"events": [], "ring_labelled": [], "skipped": []}


def test_funnel_counts_and_dedupes_against_history(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    ring = SyntheticRingClient(TZ, parse_hours(""))
    history = ring.events_for_day(DAY)
    ding = next(e for e in history if e.kind == "ding")
    # Ring's webhook for the same doorbell press the history already has, and a skipped vehicle
    hook(log, "button_press", 0, 0, when=ding.timestamp + timedelta(seconds=2), rid="same-press")
    hook(log, "motion_detected", 4, 10, "vehicle")
    hook(log, "motion_detected", 13, 5, "package_delivery")
    stats: dict = {}
    vs = classify_day(ring, RulesClassifier(), DAY, parse_hours(""), webhooks=log, stats=stats)
    same = [v for v in vs if v.event.kind == "ding" and abs(v.event.timestamp - ding.timestamp) <= timedelta(seconds=5)]
    assert len(same) == 1 and same[0].event.source == "ring-webhook"  # one press, not two; the webhook copy wins
    assert stats["skipped_stage0"] == 1 and stats["labelled_by_ring"] - stats["ring_people"] == 1 and stats["reached_bedrock"] == 0
    assert stats["events"] == len(vs) + 1 and stats["webhook_events"] >= 2 and not stats["replayed"]
    assert any(v.source == "ring" and v.event.timestamp.strftime("%H:%M") == "13:05" for v in vs)


def test_signed_replay_is_marked_through_the_api(tmp_path, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=KEY))
    log = WebhookLog(tmp_path / "w.jsonl")
    monkeypatch.setattr(main, "webhook_log", log)
    from scripts.feed_webhook import payload
    raw = json.dumps(payload("motion_detected", at(9, 30), "dev", "package_delivery")).encode()
    r = TestClient(main.app).post("/webhooks/ring", content=raw,
                                  headers={"X-Signature": sign(raw, KEY), "X-Shopfront-Replay": "1"})
    assert r.status_code == 200 and log.recent()[0]["replayed"] is True
    assert log.day(DAY, TZ)["ring_labelled"][0].event.source == "ring-webhook-replay"


# ---- "business closed" scans ----------------------------------------------------------------

def test_closed_scan_while_open_and_busy_with_no_courier():
    # the one visit within 10 minutes of the scan was looked at (Bedrock) and wasn't a courier
    vs = [visit(13, 40), visit(13, 55, VisitType.CUSTOMER, "bedrock"), visit(14, 20), visit(14, 30, VisitType.STAFF),
          visit(9, 0, VisitType.DELIVERY, "bedrock")]
    r = check_closed_scan(at(14, 5), vs, OPEN)
    assert r["outcome"] == OPEN_NO_COURIER and r["claim"] == "business_closed"
    assert r["activity"] == {"from": "13:35", "to": "14:35", "events": 4, "motion": 4, "presses": 0, "customers": 1,
                             "guessed_customers": 2, "staff": 0, "seen": 1, "people": 0, "people_source": [],
                             "near": 1, "before": "13:40", "after": "14:20"}  # door use beyond the 10 min near it
    assert "posted hours had you open (08:00–18:00)" in r["text"]
    assert ("4 motion events in the 30 minutes either side of the scan (13:35–14:35; 1 of them confirmed from the "
            "camera frame as a customer)") in r["text"]
    # each window is named where it appears; the hedge lives once, in the pack's "What this does not show"
    assert "within 10 minutes of the scan" in r["text"] and "between" not in r["text"]
    assert "labelled from the frame as not a courier" in r["text"] and "doesn't prove" not in r["text"]


def test_a_busy_door_says_which_side_of_the_scan_it_was_used_on(tmp_path):
    """Round 8: the demo's door was used only after the 14:30 scan (a press at 14:35, people 14:45-14:51), yet the
    draft said "our door was in use around the time of the scan". The finding, the verdict facts and the draft
    now name the last door event before the scan and the first after it, beyond the 10 minutes near it."""
    from app import pages
    after_only = [visit(14, 35, VisitType.DOORBELL_PRESS, ring_event="ding"), visit(14, 45), visit(14, 51)]
    r = check_closed_scan(at(14, 30), after_only, OPEN)
    assert r["outcome"] == rc.OPEN_DOOR_BUSY and (r["activity"]["before"], r["activity"]["after"]) == (None, "14:45")
    assert ("(none in the 30 minutes before the scan beyond the 10 near it; the first after it at 14:45)"
            in r["text"])
    assert pages.verdict_facts(r)[1]["text"] == "3 door events 14:00–15:00 (all after the scan)"
    body = dispute.build_pack("Corner Bakery", DAY, after_only, frames_with(tmp_path), "08:00–18:00", r)["draft"]["body"]
    assert ("Outside the 10 minutes either side of the scan, nothing was recorded at our door in the 30 minutes "
            "before it; the first event after it was at 14:45.") in body
    assert "our door was in use after the scan." in body and "around the time of the scan" not in body
    assert "14:35" not in body  # the press near the scan isn't named in the draft: it could be the courier
    both = [visit(14, 12)] + after_only
    r = check_closed_scan(at(14, 30), both, OPEN)
    assert "(beyond the 10 minutes near the scan, the last before it at 14:12 and the first after it at 14:45)" \
        in r["text"]
    assert pages.verdict_facts(r)[1]["text"] == "4 door events 14:00–15:00 (1 before the scan, 3 after)"
    body = dispute.build_pack("Corner Bakery", DAY, both, frames_with(tmp_path), "08:00–18:00", r)["draft"]["body"]
    assert "the last door event before it was at 14:12 and the first after it at 14:45." in body
    assert "our door was in use around the time of the scan." in body


def test_closed_scan_with_a_courier_seen_by_ring_or_bedrock():
    for src, by in (("ring", "Ring's package_delivery alert"), ("bedrock", "Bedrock saw them in the Ring frame")):
        r = check_closed_scan(at(14, 5), [visit(13, 50), visit(14, 9, VisitType.DELIVERY, src, eid="c")], OPEN,
                              frame_url=lambda i: f"/frames/{i}.jpg")
        assert r["outcome"] == OPEN_COURIER_CAME and r["visit"]["event_id"] == "c" and by in r["text"]
        assert "4 min after the scan" in r["text"] and r["visit"]["frame_url"] == "/frames/c.jpg"


def test_closed_scan_rules_courier_is_neither_a_sighting_nor_proof_nobody_came():
    """This test used to assert open_no_courier, which let a draft tell the carrier "no courier at our door"
    while an unlabelled doorbell press sat one minute after the scan. Nothing looked at that press, so
    the honest answer is "unverified", with no draft."""
    r = check_closed_scan(at(14, 5), [visit(14, 6, VisitType.DELIVERY, "rules", ring_event="ding")], OPEN)
    assert r["outcome"] == UNVERIFIED and r["visit"]["labelled_by"] == "rules" and not r["visit"]["confirmed"]


def test_closed_scan_other_outcomes():
    assert check_closed_scan(at(19, 32), [visit(13, 0)], OPEN)["outcome"] == POSTED_CLOSED
    assert check_closed_scan(at(14, 5), [visit(13, 0, VisitType.DELIVERY, "bedrock")], None)["outcome"] == POSTED_CLOSED
    assert check_closed_scan(at(14, 5), [], OPEN)["outcome"] == NO_DOOR_DATA
    r = check_closed_scan(at(14, 5), [visit(9, 0)], OPEN)  # nothing within 30 min, no Ring status: can't tell
    assert r["outcome"] == rc.NO_COVERAGE and "may not have been watching" in r["text"]
    online = [{"at": at(7, 55), "online": True, "replayed": False}]
    r = check_closed_scan(at(14, 5), [visit(9, 0)], OPEN, status=online)  # Ring said online: hours only
    assert r["outcome"] == OPEN_BY_HOURS_ONLY and "not that someone was there" in r["text"]


minute = st.integers(0, 24 * 60 - 1)
# Ring's person alert comes from Ring's sub_type, never from a classifier's label: tested by example elsewhere
LABELS = [t for t in VisitType if t is not VisitType.PERSON]


@settings(max_examples=300, deadline=None)
@given(minute, st.lists(st.tuples(minute, st.sampled_from(LABELS), st.sampled_from(["bedrock", "rules", "ring"])),
                        max_size=6), st.booleans())
def test_closed_scan_outcome_matches_the_evidence(scan_min, raw, has_hours):
    vs = [visit(m // 60, m % 60, k, s, eid=f"ev-{i}") for i, (m, k, s) in enumerate(raw)]
    scan = at(scan_min // 60, scan_min % 60)
    r = check_closed_scan(scan, vs, OPEN if has_hours else None)
    posted_open = has_hours and OPEN[0] <= scan.time() < OPEN[1]
    mins = [(i, m, k, s) for i, (m, k, s) in enumerate(raw)]
    around = [x for x in mins if abs(x[1] - scan_min) <= 30]
    near = [x for x in mins if abs(x[1] - scan_min) <= 10]
    couriers = [x for x in near if x[2] == VisitType.DELIVERY and x[3] != "rules"]
    later = [x for x in mins if x[1] - scan_min > 10 and x[2] == VisitType.DELIVERY and x[3] != "rules"]
    guessed = [x for x in near if x[3] == "rules" or x[2] == VisitType.UNKNOWN]
    others = [x for x in around if x not in guessed]
    if not posted_open:
        assert r["outcome"] == POSTED_CLOSED  # never claim "open" against the shop's own hours
    elif not vs:
        assert r["outcome"] == NO_DOOR_DATA
    elif couriers:
        assert r["outcome"] == OPEN_COURIER_CAME and r["visit"]["confirmed"]
    elif guessed and others:  # open and in use; the guess near the scan is named, never "no courier"
        assert r["outcome"] == rc.OPEN_DOOR_BUSY and not r["visit"]["confirmed"]
        assert "no courier" not in r["text"].lower()
    elif around and not guessed:
        assert r["outcome"] == OPEN_NO_COURIER and r["activity"]["events"] == len(around)
    elif later:
        assert r["outcome"] == COURIER_LATER and r["visit"]["time"] == "{:02d}:{:02d}".format(*divmod(min(x[1] for x in later), 60))
    elif guessed:
        assert r["outcome"] == UNVERIFIED  # only a guess near the scan: never "no courier", never "open"
    else:  # nothing within 30 minutes and no Ring status notice: the camera may not have been watching
        assert r["outcome"] == rc.NO_COVERAGE


# ---- the dispute pack -----------------------------------------------------------------------

def frames_with(tmp_path, *ids):
    f = FrameStore(tmp_path)
    for i in ids:
        f.save(i, b"\xff\xd8frame-" + i.encode())
    return f


def test_closed_scan_pack_has_order_frame_hash_and_a_draft_that_traces_to_the_facts(tmp_path):
    vs = [visit(13, 40), visit(13, 55, VisitType.CUSTOMER, "bedrock"), visit(14, 20),
          visit(9, 30, VisitType.DELIVERY, "bedrock", eid="c1")]
    order = Expected("exp-1", DAY.isoformat(), "Bay Mills", "13:00", "16:00", order_ref="PO-1042",
                     carrier="Swift Parcel", tracking="SP999", items="Flour 25kg x4")
    check = check_closed_scan(at(14, 5), vs, OPEN)
    p = dispute.build_pack("Corner Bakery", DAY, vs, frames_with(tmp_path), "08:00–18:00", check, order)
    assert p["claim"]["label"] == "Business closed" and p["claim"]["carrier"] == "Swift Parcel"
    assert p["claim"]["tracking"] == "SP999"
    assert [t["time"] for t in p["timeline"]] == ["13:40:00", "13:55:00", "14:20:00"]
    d = p["draft"]
    # an order of the shop's: the supplier shipped it, so the draft goes to the supplier (was: to the carrier)
    assert d["to"] == "Bay Mills" and d["subject"] == "Business closed scan, 2026-10-07 14:05, SP999"
    assert ("Swift Parcel scanned it \"business closed\" at 14:05 on 2026-10-07 (tracking SP999). Our front-door "
            "camera shows otherwise.") in d["body"]
    assert ("Our posted hours that day were 08:00–18:00, and our doorbell camera recorded 3 motion events in the 30 "
            "minutes either side of the scan (13:35–14:35), 1 of them confirmed from the camera frame as a customer. Outside the 10 minutes "
            "either side of the scan, the last door event before it was at 13:40 and the first after it at 14:20. "
            "The visit it recorded within 10 minutes of the scan was not a courier, judged from the camera frame.") \
        in d["body"]
    assert "Your posted hours" not in d["body"]  # written by the shop, not to the owner
    assert "order PO-1042 from Bay Mills (Flour 25kg x4)" in d["body"]
    assert d["body"].endswith("Corner Bakery") and "sent" not in d["body"].lower()
    assert p["frame"] is None and "attached the door log" in d["body"]  # no frame was cited: don't say there is one


def test_pack_with_a_frame_carries_its_hash(tmp_path):
    import hashlib
    vs = [visit(19, 0), visit(19, 34, VisitType.CUSTOMER, "bedrock", eid="c2")]
    f = frames_with(tmp_path, "c2")
    check = check_attempt(at(19, 32), vs, f.url)
    p = dispute.build_pack("Shop", DAY, vs, f, "08:00–18:00", check)
    assert p["finding"]["outcome"] == "someone_not_a_courier"
    assert "nearest that time, at 19:34, was not a courier" in p["draft"]["body"]
    assert p["frame"]["sha256"] == hashlib.sha256(b"\xff\xd8frame-c2").hexdigest()
    z = zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, f)))
    assert sorted(z.namelist()) == ["README.txt", "manifest.sha256", "pack.json", "pack.txt", "ring-frame-1934.jpg"]
    assert p["frame"]["sha256"] in z.read("pack.txt").decode()
    assert json.loads(z.read("pack.json"))["draft"]["subject"].startswith("Missed delivery scan")


def test_no_draft_when_the_door_agrees_with_the_carrier(tmp_path):
    vs = [visit(19, 30, VisitType.DELIVERY, "bedrock", eid="c3")]
    p = dispute.build_pack("Shop", DAY, vs, FrameStore(tmp_path), "08:00–18:00", check_attempt(at(19, 32), vs))
    assert p["draft"] is None and "agrees with the carrier" in p["no_draft_reason"]
    p = dispute.build_pack("Shop", DAY, vs, FrameStore(tmp_path), "08:00–18:00", check_closed_scan(at(20, 0), vs, OPEN))
    assert p["draft"] is None and "posted hours say the shop was closed" in p["no_draft_reason"]


def test_order_packs_for_not_seen_and_short(tmp_path):
    vs = [visit(10, 0), visit(10, 30, VisitType.DELIVERY, "bedrock", eid="c4")]
    f = frames_with(tmp_path, "c4")
    missing = Expected("exp-m", DAY.isoformat(), "Dairy Co", "06:00", "07:00", order_ref="PO-7")
    short = Expected("exp-s", DAY.isoformat(), "Bay Mills", "10:00", "11:00", order_ref="PO-8",
                     received={"status": "short", "note": "one sack missing", "at": "2026-10-07T10:45-07:00"})
    r = receipts([missing, short], vs, OPEN, f)
    rows = {x["id"]: x for x in r["expected"]}
    pm = dispute.build_pack("Shop", DAY, vs, f, "08:00–18:00", order=missing, receipt=rows["exp-m"])
    # nothing in Dairy Co's 06:00-07:00 window at all (was: "2 door events that day and no courier", which
    # counted a rules-labelled 10:00 visit as looked at)
    assert pm["draft"]["to"] == "Dairy Co"
    assert "recorded nothing at our door between 06:00 and 07:00" in pm["draft"]["body"]
    ps = dispute.build_pack("Shop", DAY, vs, f, "08:00–18:00", order=short, receipt=rows["exp-s"])
    assert "it was short: one sack missing." in ps["draft"]["body"] and "courier at 10:30" in ps["draft"]["body"]
    assert ps["frame"]["event_id"] == "c4" and ps["timeline"]
    ok = Expected("exp-o", DAY.isoformat(), "Bay Mills", "10:00", "11:00")
    po = dispute.build_pack("Shop", DAY, vs, f, "08:00–18:00", order=ok, receipt=receipts([ok], vs, OPEN, f)["expected"][0])
    assert po["draft"] is None
    with pytest.raises(ValueError):
        dispute.build_pack("Shop", DAY, vs, f, "08:00–18:00")


def test_replayed_events_are_named_in_the_limits(tmp_path):
    v = visit(14, 0, VisitType.DELIVERY, "ring")
    v.event.source = "ring-webhook-replay"
    p = dispute.build_pack("Shop", DAY, [v], FrameStore(tmp_path), "08:00–18:00", check_closed_scan(at(14, 5), [v], OPEN))
    assert dispute.REPLAY_LIMIT in p["limits"]


# ---- through the app ------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "profile", ShopProfile(tmp_path / "shop.json", "Test Shop", ""))
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "expected.json"))
    monkeypatch.setattr(main, "frames", FrameStore(tmp_path))
    monkeypatch.setattr(main, "cache", main.ClassificationCache(None))
    return tmp_path


def add_ring_courier(monkeypatch, day, hh, mm, eid="ring-pkg"):
    """The synthetic day plus one courier Ring's package_delivery alert named. Rule-guessed presses are only
    "possible" couriers, so a receipt needs one of these (or a Bedrock label)."""
    synthetic = main._visits

    def visits(d):
        out = synthetic(d)
        if d == day:
            e = DoorEvent(eid, "d", datetime.combine(d, time(hh, mm), tzinfo=main.settings.tz), "motion",
                          "ring-webhook", "package_delivery")
            out = sorted(out + [ClassifiedVisit(e, VisitType.DELIVERY, 1.0, "Ring Smart Alert: package_delivery", "ring")],
                         key=lambda v: v.event.timestamp)
        return out
    monkeypatch.setattr(main, "_visits", visits)


def test_import_page_flow_and_staff_tap(isolated, monkeypatch):
    add_ring_courier(monkeypatch, date(2026, 10, 11), 11, 20)
    c = TestClient(main.app)
    r = c.post("/orders/import", files={"file": ("orders.csv", SHOPFRONT_CSV.replace("2026-10-07", "2026-10-11"), "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303 and "imported=Imported%202%20new" in r.headers["location"]
    assert "Skipped 2" in c.get(r.headers["location"]).text
    bad = c.post("/orders/import", data={"csv": "nothing,useful\n1,2\n"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    # Oct 11 is a closed Sunday; Ring's courier at 11:20 lands on the window-less order
    page = c.get("/?date=2026-10-11").text
    assert "PO-1043" in page and "Flour 25kg x4" in page and 'name="status" value="short"' in page
    e = next(x for x in main.book.for_day(date(2026, 10, 11)) if x.order_ref == "PO-1043")
    assert c.post(f"/deliveries/{e.id}/received", data={"status": "short", "note": "2 crates"},
                  follow_redirects=False).status_code == 303
    page = c.get("/?date=2026-10-11").text
    assert "Staff marked short" in page and f"/dispute?date=2026-10-11&amp;order={e.id}" in page
    assert c.post(f"/deliveries/{e.id}/received", data={"status": "bogus"}).status_code == 400
    assert c.post("/deliveries/exp-nope/received", data={"status": "ok"}).status_code == 404
    pack = c.get(f"/dispute?date=2026-10-11&order={e.id}").text
    assert "Short delivery, PO-1043" in pack and "2 crates" in pack and "Shopfront doesn't send this" in pack
    z = c.get(f"/dispute.zip?date=2026-10-11&order={e.id}")
    assert z.headers["content-type"] == "application/zip" and "pack.txt" in zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert TestClient(main.app, client=("203.0.113.9", 5000)).get(f"/dispute.zip?date=2026-10-11&order={e.id}").status_code == 403
    assert c.get("/dispute?date=2026-10-11&order=exp-nope").status_code == 404
    assert c.get("/dispute?date=2026-10-11").status_code == 400


def test_a_press_is_listed_as_a_possible_courier_or_as_pressed_while_closed_never_both(isolated, monkeypatch):
    """Round 8: the 18:46 press after an 18:00 close was the windowless order's possible courier and also
    "Doorbell pressed while closed". Now a windowless order is offered presses in posted hours only, and a press in
    a window set before opening is listed under that order, not again as a press while closed."""
    from app import briefing
    tz, rules, hours = main.settings.tz, RulesClassifier(), (time(8), time(18))

    def press(hh, mm):
        return rules.classify(DoorEvent(f"ding-{hh}{mm}", "d", datetime.combine(DAY, time(hh, mm), tzinfo=tz),
                                        "ding", "ring"), hours)
    day = [press(6, 30), press(10, 14), press(18, 46)]
    monkeypatch.setattr(main, "_visits", lambda d: list(day) if d == DAY else [])
    monkeypatch.setattr(briefing, "classify_day", lambda ring, cl, d, *a, **k: list(day) if d == DAY else [])
    main.book.add(DAY, "Dairy Co", "06:00", "07:00")
    main.book.add(DAY, "Pinecrest Paper Goods", order_ref="INV-2208")
    page = TestClient(main.app).get("/", params={"date": DAY.isoformat()}).text
    assert "Possible courier for Dairy Co: a doorbell press at 06:30, not confirmed by a camera frame." in page
    assert ("Possible courier for Pinecrest Paper Goods: a doorbell press at 10:14, not confirmed by a camera frame."
            in page)
    assert "Doorbell pressed while closed, at 18:46; no camera frame confirmed who it was." in page
    assert "Doorbell pressed while closed, at 06:30" not in page and "06:30, 18:46" not in page
    assert "A doorbell press at 10:14 came during posted hours" in page


def lunchtime(d):
    """A busy Wednesday lunchtime that Bedrock looked at: customers, no courier near 12:30."""
    tz = main.settings.tz
    return [ClassifiedVisit(DoorEvent(f"lunch-{m}", "d", datetime.combine(d, time(12, m), tzinfo=tz), "motion", "ring"),
                            VisitType.CUSTOMER, 0.9, "customer at the counter", "bedrock", f"rid-{m}")
            for m in (5, 15, 25, 35, 50)] if d == DAY else []


def test_business_closed_check_on_the_dashboard_and_api(isolated, monkeypatch):
    monkeypatch.setattr(main, "_visits", lunchtime)
    c = TestClient(main.app)
    page = c.get("/", params={"date": "2026-10-07", "claim": "business_closed", "attempt": "12:30"}).text
    assert "The carrier scanned &#34;business closed&#34; at 12:30" in page or 'scanned "business closed" at 12:30' in page
    j = c.get("/api/attempt", params={"at": "2026-10-07T12:30", "claim": "business_closed"}).json()
    assert j["claim"] == "business_closed" and j["outcome"] == OPEN_NO_COURIER
    assert j["activity"]["events"] == 5 and j["activity"]["customers"] == 5
    if j["outcome"] == OPEN_NO_COURIER:
        assert "Build the dispute pack" in page
        # the disputed scan is the page's one verdict, above the orders, and holds the pack button (round 3: it
        # was said twice, once as the headline and again under the form)
        card = page.split('<section class="verdict act"')[1].split("</section>")[0]
        assert "Business closed scan at 12:30" in card.split('<p class="first')[0]
        first = card.split('<p class="first')[1].split("</p>")[0]
        assert "You were open, and the doorbell saw no courier near the scan." in first and ' warn">' in first
        assert "Build the dispute pack" in card and page.count("Build the dispute pack") == 1
        assert page.count("You were open, and the doorbell saw no courier near the scan.") == 1
        assert page.index('class="verdict act"') < page.index('id="dlv"')
        page = c.get("/", params={"date": "2026-10-07", "claim": "business_closed", "attempt": "12:30",
                                  "carrier": "UPS", "tracking": "1Z 9"}).text
        assert "claim=business_closed&amp;carrier=UPS&amp;tracking=1Z%209" in page
        assert "Business closed scan at 12:30 · UPS · 1Z 9" in page
        pack = c.get("/api/dispute", params={"date": "2026-10-07", "at": "12:30", "claim": "business_closed"}).json()
        assert pack["draft"]["subject"].startswith("Business closed scan, 2026-10-07 12:30")
        main.book.add(DAY, "Bay Mills", order_ref="PO-5", tracking="1Z5")
        linked = c.get("/api/dispute", params={"date": "2026-10-07", "at": "12:30", "claim": "business_closed",
                                               "tracking": "1Z5"}).json()
        assert linked["order"]["order_ref"] == "PO-5" and "order PO-5 from Bay Mills" in linked["draft"]["body"]
    assert c.get("/", params={"date": "2026-10-07", "claim": "lost", "attempt": "12:30"}).status_code == 400
    assert c.get("/api/attempt", params={"at": "2026-10-07T21:00", "claim": "business_closed"}).json()["outcome"] == POSTED_CLOSED


def test_agent_tools_check_scans_and_build_packs(isolated):
    main.book.add(DAY, "Dairy Co", "05:00", "05:30", order_ref="PO-9")
    t = main._agent_tools(DAY + timedelta(days=1))
    assert t.check_attempt("yesterday", "21:00", "business closed")["outcome"] == POSTED_CLOSED
    p = t.dispute_pack("yesterday", order_ref="po-9")
    assert p["pack_url"].startswith("/dispute?date=2026-10-07") and "order=exp-" in p["pack_url"]
    assert p["order"]["order_ref"] == "PO-9"
    with pytest.raises(ValueError):
        t.dispute_pack("yesterday")
    with pytest.raises(ValueError):
        t.dispute_pack("yesterday", order_ref="PO-404")
    with pytest.raises(ValueError):
        t.check_attempt("yesterday", "12:00", "lost")
