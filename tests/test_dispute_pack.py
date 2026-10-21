"""The dispute pack's draft is sent by the owner to a carrier or supplier, so every sentence in it must
stand on camera-confirmed evidence, and anything else must be labelled: a guess, a replay, demo data.
These are the judge audit's cases. Local only."""

import io
import json
import zipfile
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import dispute, main
from app import reconcile as rc
from app.deliveries import Expected, FrameStore, receipts

LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
OPEN = (time(8), time(18))
HOURS = "08:00–18:00"
NOW = datetime(2026, 10, 21, 12, 5, tzinfo=LA)
NO_FRAMES = FrameStore.__new__(FrameStore)
NO_FRAMES.dirs = []


def visit(hh, mm, kind="customer", labelled_by="rules", src="ring", eid=None, ring_event="motion", rid=None):
    from app.models import ClassifiedVisit, DoorEvent, VisitType
    e = DoorEvent(eid or f"ev-{hh:02d}{mm:02d}", "d", datetime(2026, 10, 14, hh, mm, tzinfo=LA), ring_event, src)
    return ClassifiedVisit(e, VisitType(kind), 0.9, "why", labelled_by, rid)


def at(hh, mm):
    return datetime(2026, 10, 14, hh, mm, tzinfo=LA)


FLOUR = Expected("exp-1042", DAY.isoformat(), "Northside Flour", "12:00", "14:00", order_ref="PO-1042",
                 carrier="Swift Parcel", tracking="SP123", items="10 x 25kg flour", amount="412.50")


def pitch_day():
    """Busy lunchtime (motion the rules call customers), Ring's package_delivery alert at 13:02."""
    vs = [visit(12, m) for m in (13, 17, 20, 24, 25, 37, 39, 46, 59)]
    return vs + [visit(13, 2, "delivery", "ring", src="ring-webhook", eid="pkg")]


def pack(vs, check=None, order=None, carrier="", tracking="", tmp=None, frames=None):
    frames = frames or FrameStore(tmp)
    receipt = None
    if order:
        receipt = next(x for x in receipts([order], vs, OPEN, frames)["expected"])
    return dispute.build_pack("Corner Bakery", DAY, vs, frames, HOURS, check, order, receipt, carrier, tracking, tz=LA)


# ---- the pitch opener ------------------------------------------------------------------------------

def test_scanned_closed_then_delivered_later_is_a_late_delivery_note_to_the_supplier(tmp_path):
    vs = pitch_day()
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    assert check["outcome"] == rc.COURIER_LATER
    p = pack(vs, check, FLOUR, "Swift Parcel", "SP123", tmp_path)
    assert p["finding"]["outcome"] == dispute.DELIVERED_AFTER_SCAN
    assert "scanned \"business closed\" at 12:40, then the courier was at the door at 13:02" in p["finding"]["text"]
    d = p["draft"]
    assert d["to"] == "Northside Flour" and d["subject"] == "Late delivery after a \"business closed\" scan, PO-1042"
    body = d["body"]
    assert body.startswith("Hello Northside Flour,")
    assert "13:02" in body and "22 minutes after" in body and "Swift Parcel" in body and "SP123" in body
    assert "redeliver" not in body.lower() and "no courier" not in body.lower()
    assert "customers" not in body  # the lunchtime "customers" are rule guesses
    assert "the carrier," not in body


def test_late_delivery_needs_the_orders_own_courier(tmp_path):
    """A courier later that day with no order linked: Shopfront can't say whose it was, so no draft."""
    vs = pitch_day()
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["draft"] is None and "can't tell whose" in p["no_draft_reason"]


# ---- who the draft goes to -------------------------------------------------------------------------

def confirmed_lunch():
    """Bedrock looked at every lunchtime visit: customers, no courier near 12:40."""
    return [visit(12, m, "customer", "bedrock", rid=f"rid-{m}") for m in (13, 20, 25, 50, 59)]


def test_carrier_draft_uses_the_typed_name_or_a_neutral_greeting(tmp_path):
    vs = confirmed_lunch()
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    assert check["outcome"] == rc.OPEN_NO_COURIER
    named = pack(vs, check, carrier="Swift Parcel", tmp=tmp_path)["draft"]
    assert named["to"] == "Swift Parcel" and named["body"].startswith("Hello Swift Parcel,")
    plain = pack(vs, check, tmp=tmp_path)["draft"]
    assert plain["to"] == "" and plain["body"].startswith("Hello,\n") and "the carrier," not in plain["body"]


def test_an_order_linked_scan_dispute_goes_to_the_supplier(tmp_path):
    vs = confirmed_lunch()
    order = Expected("exp-9", DAY.isoformat(), "Bay Mills", "15:00", "17:00", order_ref="PO-9", carrier="Swift Parcel")
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), order, tmp=tmp_path)
    d = p["draft"]
    assert d["to"] == "Bay Mills" and d["body"].startswith("Hello Bay Mills,")
    assert "Swift Parcel scanned it \"business closed\" at 12:40" in d["body"]
    assert "please ask swift parcel to correct the scan" in d["body"].lower()


def test_carrier_facts_name_kinds_and_only_confirmed_customers(tmp_path):
    vs = [visit(12, m) for m in (13, 17, 20)] + [visit(12, 25, "customer", "bedrock"), visit(12, 50, "customer", "bedrock")]
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    body = pack(vs, check, tmp=tmp_path)["draft"]["body"]
    assert "5 motion events between 12:10 and 13:10, 2 of them confirmed from the camera frame as customers" in body
    assert "Our front-door camera shows otherwise." in body  # Bedrock-confirmed visits back it


def test_nothing_recorded_does_not_say_the_camera_shows_otherwise(tmp_path):
    vs = [visit(9, 0, "customer", "bedrock")]
    p = pack(vs, rc.check_attempt(at(17, 30), vs, now=NOW), tmp=tmp_path)
    assert p["finding"]["outcome"] == rc.NOTHING_RECORDED
    assert "shows otherwise" not in p["draft"]["body"] and "recorded nothing at our door within 10 minutes" in p["draft"]["body"]


def test_no_draft_when_a_guess_sits_near_the_scan(tmp_path):
    vs = [visit(12, 41, "delivery", "rules", ring_event="ding")]
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["draft"] is None and "no camera frame confirmed who" in p["no_draft_reason"]


# ---- orders without a scan -------------------------------------------------------------------------

def test_staff_tap_time_is_shop_local(tmp_path):
    vs = [visit(10, 30, "delivery", "bedrock", eid="c4")]
    short = Expected("exp-s", DAY.isoformat(), "Bay Mills", "10:00", "11:00", order_ref="PO-8",
                     received={"status": "short", "note": "one sack missing", "at": "2026-10-14T10:45-07:00"})
    body = pack(vs, order=short, tmp=tmp_path)["draft"]["body"]
    assert "When we unpacked it at 10:45, it was short: one sack missing." in body and "07:00" not in body


def test_not_seen_order_needs_every_door_event_in_its_window_looked_at(tmp_path):
    milk = Expected("exp-m", DAY.isoformat(), "Dairy Co", "06:00", "07:00", order_ref="PO-7")
    guessed = [visit(6, 30)]  # motion nothing looked at, inside the window
    p = pack(guessed, order=milk, tmp=tmp_path)
    assert p["draft"] is None and "06:30" in p["no_draft_reason"]
    quiet = [visit(10, 0)]  # nothing in the window at all
    d = pack(quiet, order=milk, tmp=tmp_path)["draft"]
    assert d["to"] == "Dairy Co" and "recorded nothing at our door between 06:00 and 07:00" in d["body"]


# ---- where the evidence came from ------------------------------------------------------------------

def test_synthetic_events_watermark_the_pack_draft_and_zip(tmp_path):
    vs = [visit(12, m, "customer", "bedrock", src="synthetic") for m in (13, 20, 50)]
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["demo_data"] and p["watermark"].startswith("DEMO DATA, not from a camera")
    assert p["draft"]["subject"].startswith("[DEMO DATA, not from a camera]")
    assert p["draft"]["body"].startswith("DEMO DATA, not from a camera")
    assert any("synthetic" in x for x in p["limits"]) and p["cited_sources"] == ["synthetic"]
    text = dispute.as_text(p)
    assert text.startswith("DEMO DATA, not from a camera")
    z = zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, FrameStore(tmp_path))))
    assert z.read("README.txt").decode().startswith("DEMO DATA, not from a camera")
    assert json.loads(z.read("pack.json"))["demo_data"] is True


def test_replayed_events_are_disclosed_in_limits_and_draft(tmp_path):
    vs = confirmed_lunch() + [visit(12, 55, "customer", "bedrock", src="ring-webhook-replay")]
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["replayed"] and dispute.REPLAY_LIMIT in p["limits"]
    assert "REPLAYED RING EVENTS" in p["draft"]["body"] and "REPLAYED" in p["draft"]["subject"]
    real = pack(confirmed_lunch(), rc.check_closed_scan(at(12, 40), confirmed_lunch(), OPEN, now=NOW), tmp=tmp_path)
    assert not real["replayed"] and not real["demo_data"] and real["watermark"] is None
    assert real["draft"]["body"].startswith("Hello,")


def test_frame_provenance_is_named(tmp_path):
    f = FrameStore(tmp_path)
    f.save("live-1", b"\xff\xd8live")
    vs = [visit(17, 31, "customer", "bedrock", src="ring-live", eid="live-1")]
    p = pack(vs, rc.check_attempt(at(17, 30), vs, f.url, now=NOW), frames=f)
    assert p["frame"]["provenance"] == "captured in the owner's browser from a Ring live-view session"
    assert "captured in the owner's browser" in dispute.as_text(p)
    names = zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, f))).namelist()
    assert "live-view-frame-1731.jpg" in names


def test_made_at_is_shop_time():
    p = dispute.build_pack("S", DAY, [visit(9, 0)], NO_FRAMES, HOURS,
                           rc.check_attempt(at(9, 3), [visit(9, 0)], now=NOW), tz=ZoneInfo("Pacific/Auckland"))
    assert p["made_at"][-6:] in ("+13:00", "+12:00")


def test_a_check_that_was_refused_makes_no_pack():
    future = rc.check_attempt(at(17, 30), [], now=datetime(2026, 10, 14, 12, 5, tzinfo=LA))
    with pytest.raises(ValueError, match="hasn't happened yet"):
        dispute.build_pack("S", DAY, [], NO_FRAMES, HOURS, future, tz=LA)


def test_future_time_through_the_app_is_refused(monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(main, "_visits", lambda d: [])
    tomorrow = (datetime.now(main.settings.tz) + timedelta(days=1)).date().isoformat()
    c = TestClient(main.app)
    r = c.get("/api/dispute", params={"date": tomorrow, "at": "09:00", "claim": "attempted"})
    assert r.status_code == 400 and "hasn't happened yet" in r.text
    page = c.get("/", params={"date": tomorrow, "attempt": "09:00"}).text
    assert "hasn&#39;t happened yet" in page or "hasn't happened yet" in page


def test_live_frames_are_never_called_ring_frames(tmp_path):
    # app/live.py names live-view events ring-live-browser: the bytes came from the owner's browser
    f = FrameStore(tmp_path)
    f.save("live-abc", b"\xff\xd8live")
    vs = [visit(17, 31, "delivery", "bedrock", src="ring-live-browser", eid="live-abc")]
    p = pack(vs, rc.check_attempt(at(17, 30), vs, f.url, now=NOW), frames=f)
    assert p["frame"]["label"] == "Live-view frame"
    assert p["frame"]["provenance"] == "captured in the owner's browser from a Ring live-view session"
    text = dispute.as_text(p)
    assert "Ring frame" not in text and "captured in the owner's browser from a Ring live-view session" in text
    if p["draft"]:
        assert "captured in the owner's browser from a Ring live-view session" in p["draft"]["body"]
        assert "doorbell frame" not in p["draft"]["body"]
    assert any(n.startswith("live-view-frame-") for n in zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, f))).namelist())
