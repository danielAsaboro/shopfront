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
    assert check["outcome"] == rc.OPEN_DOOR_BUSY  # round 2: open by the hours and the busy door
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
    carrier = p["drafts"][1]  # the other draft the owner can pick: to the carrier, about its scan
    assert carrier["to"] == "Swift Parcel" and carrier["body"].startswith("Hello Swift Parcel,")
    assert "13:02" in carrier["body"] and "correct the \"business closed\" scan" in carrier["body"]
    assert "redeliver" not in carrier["body"].lower() and "no courier" not in carrier["body"].lower()


def test_late_delivery_needs_the_orders_own_courier(tmp_path):
    """A courier later that day with no order linked: Shopfront can't say whose it was. On a busy door the
    shop was still open, so there is a draft to the carrier, but it doesn't ask for a redelivery (the 13:02
    courier may have brought it) or say nobody came. On a quiet door there's nothing to send."""
    vs = pitch_day()
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["finding"]["outcome"] == rc.OPEN_DOOR_BUSY and [d["key"] for d in p["drafts"]] == ["carrier"]
    body = p["draft"]["body"]
    assert "redeliver" not in body.lower() and "no courier" not in body.lower() and "nobody" not in body.lower()
    assert "13:02" in p["finding"]["text"] and "doesn't say whose" in p["finding"]["text"]
    quiet = [visit(9, 0), visit(13, 30, "delivery", "ring", src="ring-webhook", eid="pkg")]
    p = pack(quiet, rc.check_closed_scan(at(12, 40), quiet, OPEN, now=NOW), tmp=tmp_path)
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


def test_scan_pack_and_draft_name_the_orders_unidentified_presses(tmp_path):
    """The desk says "Possible courier" for the order; the pack must say what that was, a press no frame
    identified, instead of leaving it out and reading as if the door was silent. An attempted-delivery
    draft says it too. A "business closed" draft is about being open: it states the hours and the
    activity only, and the attached door log names the press (round 2: no press offered as maybe the
    courier in the letter)."""
    vs = confirmed_lunch() + [visit(15, 36, "delivery", "rules", ring_event="ding")]
    order = Expected("exp-9", DAY.isoformat(), "Bay Mills", "15:00", "17:00", order_ref="PO-9", carrier="Swift Parcel")
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), order, tmp=tmp_path)
    assert p["order"]["door_status"] == "possible" and p["order"]["unidentified"] == "a doorbell press at 15:36"
    assert "(a doorbell press at 15:36, not identified)" in dispute.as_text(p)
    assert all("15:36" not in d["body"] for d in p["drafts"])
    p = pack(vs, rc.check_attempt(at(12, 40), vs, now=NOW), order, tmp=tmp_path)
    assert p["finding"]["outcome"] == rc.SOMEONE_NOT_COURIER
    assert "Our doorbell also recorded a doorbell press at 15:36 during the delivery window, which no camera " \
           "frame identified." in p["draft"]["body"]
    assert dispute.possible_text([{"time": "10:09", "ring_event": "ding"}, {"time": "10:53", "ring_event": "ding"}]) \
        == "doorbell presses at 10:09 and 10:53"


def test_busy_door_with_a_press_at_the_scan_is_open_and_names_the_press(tmp_path):
    """The judges' sweep: on a busy door any unidentified press near the scan used to make the answer "can't
    say", with no pack. The claim is "closed", so the hours and the busy door answer it."""
    vs = [visit(14, m) for m in (2, 11, 25, 51)] + [visit(14, 36, "doorbell_press", "rules", ring_event="ding")]
    order = Expected("exp-9", DAY.isoformat(), "Bay Mills", "13:00", "16:00", order_ref="PO-9", carrier="Swift Parcel")
    p = pack(vs, rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW), order, "Swift Parcel", tmp=tmp_path)
    f = p["finding"]
    assert f["outcome"] == rc.OPEN_DOOR_BUSY
    assert "an unidentified motion" not in f["text"]
    assert "unidentified motion at 14:25 and an unidentified doorbell press at 14:36 (no camera frame confirms " \
           "who)" in f["text"]
    assert [d["key"] for d in p["drafts"]] == ["supplier", "carrier"]
    for d in p["drafts"]:
        body = d["body"].lower()
        assert "4 motion events and 1 doorbell press in the 30 minutes either side of the scan (14:00–15:00)" in body
        assert "we were open" in body
        assert "14:36" not in body and "might have been" not in body and "possible courier" not in body
        assert "nobody" not in body and "no courier" not in body and "nothing at our door" not in body
    assert any(t["time"].startswith("14:36") for t in p["timeline"])  # the attached log names the press


def test_carrier_facts_name_kinds_and_only_confirmed_customers(tmp_path):
    vs = [visit(12, m) for m in (13, 17, 20)] + [visit(12, 25, "customer", "bedrock"), visit(12, 50, "customer", "bedrock")]
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    body = pack(vs, check, tmp=tmp_path)["draft"]["body"]
    assert "5 motion events in the 30 minutes either side of the scan (12:10–13:10), 2 of them confirmed from the camera frame as customers" in body
    assert "Our front-door camera shows otherwise." in body  # Bedrock-confirmed visits back it


def test_nothing_recorded_does_not_say_the_camera_shows_otherwise(tmp_path):
    vs = [visit(9, 0, "customer", "bedrock"), visit(17, 5, "customer", "bedrock")]  # recording at 17:05
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
    # Round 2: one short tag on the subject, one line on top of the body, the full story in the README
    assert p["draft"]["subject"].startswith("[DEMO] ") and p["draft"]["subject"].count("DEMO") == 1
    first, rest = p["draft"]["body"].split("\n\n", 1)
    assert first == "DEMO DATA, not from a camera: don't send this." and rest.startswith("Hello")
    assert "DEMO" not in rest and "demo generator" not in rest
    assert any("synthetic" in x for x in p["limits"]) and p["cited_sources"] == ["synthetic"]
    text = dispute.as_text(p)
    assert text.startswith("DEMO DATA, not from a camera")
    z = zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, FrameStore(tmp_path))))
    readme = z.read("README.txt").decode()
    assert readme.startswith("DEMO DATA, not from a camera") and dispute.DEMO_LIMIT in readme
    assert json.loads(z.read("pack.json"))["demo_data"] is True


def test_replayed_events_are_disclosed_in_limits_and_draft(tmp_path):
    vs = confirmed_lunch() + [visit(12, 55, "customer", "bedrock", src="ring-webhook-replay")]
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    assert p["replayed"] and dispute.REPLAY_LIMIT in p["limits"]
    assert p["draft"]["body"].split("\n\n")[0] == "REPLAYED RING EVENTS, not live Ring notifications: don't send this as evidence."
    assert p["draft"]["subject"].startswith("[DEMO] ")
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


# ---- tamper-evident zip ----------------------------------------------------------------------------

def _verify(blob: bytes) -> tuple[dict[str, str], str]:
    """Check the zip the way `sha256sum -c manifest.sha256` would, with hashlib; return the manifest and
    its own digest."""
    import hashlib
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = z.read("manifest.sha256").decode()
    listed = {}
    for line in text.splitlines():
        digest, name = line.split("  ", 1)  # sha256sum's text-mode separator: two spaces
        assert len(digest) == 64 and int(digest, 16) >= 0
        listed[name] = digest
    # every other file, nothing else; the signature and its how-to (app/pack_signing.py) sign the manifest itself
    assert set(listed) == set(z.namelist()) - {"manifest.sha256", "manifest.sig", "VERIFY.txt"}
    for name, digest in listed.items():
        assert hashlib.sha256(z.read(name)).hexdigest() == digest, name
    return listed, hashlib.sha256(text.encode()).hexdigest()


def test_zip_manifest_covers_every_file_and_verifies(tmp_path):
    frames = FrameStore(tmp_path)
    frames.save("pkg", b"\xff\xd8frame-pkg")
    vs = pitch_day()
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    p = pack(vs, check, FLOUR, "Swift Parcel", "SP123", frames=frames)
    blob = dispute.as_zip(p, frames)
    listed, digest = _verify(blob)
    assert {"README.txt", "pack.txt", "pack.json"} < set(listed) and any(n.startswith("ring-frame-") for n in listed)
    assert digest == dispute.manifest_digest(p, frames)
    assert "sha256sum -c manifest.sha256" in zipfile.ZipFile(io.BytesIO(blob)).read("README.txt").decode()
    assert dispute.as_zip(p, frames) == blob  # same pack, same bytes


def test_a_changed_file_fails_the_manifest(tmp_path):
    import hashlib
    vs = confirmed_lunch()
    p = pack(vs, rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW), tmp=tmp_path)
    z = zipfile.ZipFile(io.BytesIO(dispute.as_zip(p, NO_FRAMES)))
    listed = dict(reversed(line.split("  ", 1)) for line in z.read("manifest.sha256").decode().splitlines())
    edited = z.read("pack.txt").replace(b"Corner Bakery", b"Corner Bakery (edited)")
    assert hashlib.sha256(edited).hexdigest() != listed["pack.txt"]


def test_pack_page_shows_the_digest_of_the_zip_it_links_to(monkeypatch):
    from urllib.parse import unquote
    vs = confirmed_lunch()
    monkeypatch.setattr(main, "_visits", lambda d: vs)
    c = TestClient(main.app)
    page = c.get("/dispute", params={"date": DAY.isoformat(), "at": "12:40", "claim": "business_closed"}).text
    shown = page.split('id="manifest">', 1)[1].split("<", 1)[0]
    link = unquote(page.split('href="/dispute.zip?', 1)[1].split('"', 1)[0]).replace("&amp;", "&")
    assert len(shown) == 64 and "made=" in link
    import time as _time
    _time.sleep(1.1)  # the zip is made a second later; the link's "made" keeps it the same pack
    z = c.get("/dispute.zip?" + link.replace("+", "%2B"))
    assert _verify(z.content)[1] == shown == z.headers["X-Manifest-SHA256"]


def test_zip_made_time_only_from_the_last_day(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: confirmed_lunch())
    c = TestClient(main.app)
    q = {"date": DAY.isoformat(), "at": "12:40", "claim": "business_closed"}
    for made in ("2020-01-01T09:00:00-08:00", "2999-01-01T09:00:00-08:00", "nonsense", "2026-10-14T09:00:00"):
        p = json.loads(zipfile.ZipFile(io.BytesIO(c.get("/dispute.zip", params={**q, "made": made}).content))
                       .read("pack.json"))
        assert p["made_at"] != made and not p["made_at"].startswith(("2020", "2999"))


# ---- one window language, one hedge (round 3 audit) -------------------------------------------------

def test_every_window_is_named_where_it_appears_and_the_hedge_is_one_line(monkeypatch, tmp_path):
    """The check said "between 14:00 and 15:00", then "within 10 minutes", the pack "30 min either side". Each
    window now says what it is measured from, and the "doesn't prove" hedge lives once, in "What this does not
    show", not in the finding, the footer or a second limits line."""
    vs = confirmed_lunch()
    check = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW)
    assert "30 minutes either side of the scan (12:10–13:10" in check["text"]
    assert "between" not in check["text"] and "doesn't prove" not in check["text"]
    p = pack(vs, check, tmp=tmp_path)
    hedges = [x for x in p["limits"] if "door answered" in x or "can miss" in x]
    assert len(hedges) == 1 and "door answered" in hedges[0] and "can miss people" in hedges[0]
    assert "30 minutes either side of the scan (12:10–13:10)" in p["draft"]["body"]
    assert "Door log, 30 minutes either side of the scan:" in dispute.as_text(p)
    monkeypatch.setattr(main, "_visits", lambda d: vs)
    page = TestClient(main.app).get("/dispute", params={"date": DAY.isoformat(), "at": "12:40",
                                                         "claim": "business_closed"}).text
    assert "Door log, 30 minutes either side of the scan</h2>" in page
    # round 4: the window is the verdict's fact and its picture, which brackets the 10 minutes that count as near
    assert "door events 12:10–13:10" in page and "<desc id=\"pack-d\">Door from 12:10 to 13:10." in page
    assert "10 minutes either side bracketed as near the scan" in page and ">±10 min</text>" in page
    assert page.count("door answered") == 1 and page.count("can miss people") == 1


def test_the_rules_guess_line_shows_only_when_a_rules_label_is_in_the_log(tmp_path):
    guessed = pack(pitch_day(), rc.check_closed_scan(at(12, 40), pitch_day(), OPEN, now=NOW), tmp=tmp_path)
    assert dispute.RULES_LIMIT in guessed["limits"]
    seen = [visit(12, 20, "customer", "bedrock", rid="r1"), visit(12, 50, "customer", "bedrock", rid="r2")]
    clean = pack(seen, rc.check_closed_scan(at(12, 40), seen, OPEN, now=NOW), tmp=tmp_path)
    assert dispute.RULES_LIMIT not in clean["limits"]
