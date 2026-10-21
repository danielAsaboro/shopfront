"""The draft says "Our front-door camera shows otherwise" only when a real frame or a real Ring alert backs it.

Round 4 found the demo's pack saying the camera showed otherwise while the same page said "No camera frame for
this scan" and every person label came from the demo generator. Demo (`synthetic`) and replayed
(`ring-webhook-replay`) events are never "the camera"; the draft says "Our door log shows otherwise" for them.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import dispute
from app import reconcile as rc
from app.deliveries import Expected, FrameStore
from app.models import ClassifiedVisit, DoorEvent, VisitType

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
OPEN = (datetime(2026, 10, 14, 8).time(), datetime(2026, 10, 14, 18).time())
NOW = datetime(2026, 10, 14, 20, 0, tzinfo=TZ)
REAL = ("ring", "ring-webhook", "ring-live-browser")
NOT_A_CAMERA = ("synthetic", "ring-webhook-replay")
CAMERA = "Our front-door camera shows otherwise."
DOOR_LOG = "Our door log shows otherwise."


def at(hh, mm):
    return datetime(2026, 10, 14, hh, mm, tzinfo=TZ)


def visit(hh, mm, kind=VisitType.CUSTOMER, by="rules", src="ring", ring_event="motion", sub=None):
    e = DoorEvent(f"ev-{hh:02d}{mm:02d}", "d", at(hh, mm), ring_event, src, ring_subtype=sub)
    return ClassifiedVisit(e, kind, 0.9, "why", by, "rid" if by == "bedrock" else None)


def person(hh, mm, src):
    return visit(hh, mm, VisitType.PERSON, "ring", src, sub="human")


def bodies(vs, check, frames=None, order=None):
    p = dispute.build_pack("Corner Bakery", DAY, vs, frames or _store(), "08:00–18:00", check, order,
                           carrier="Northline Parcel")
    assert p["drafts"], p["no_draft_reason"]
    return p, [d["body"] for d in p["drafts"]]


_TMP: list = []


def _store():  # an empty frame store in this test's tmp_path
    return FrameStore(_TMP[0])


@pytest.fixture(autouse=True)
def _frames_dir(tmp_path):
    _TMP[:] = [tmp_path]
    yield


ORDER = Expected("exp-1", DAY.isoformat(), "Valley Creamery", "13:00", "16:00", order_ref="PO-1042",
                 carrier="Northline Parcel", tracking="NP1")


def cases(src):
    """One door per outcome where the draft says the door disagrees with the scan, every event from `src`."""
    closed = lambda vs: rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)  # noqa: E731
    return {
        rc.OPEN_COURIER_CAME: (lambda vs: closed(vs), [visit(14, 5, src=src), visit(14, 28, VisitType.DELIVERY,
                                                                                         "ring", src)]),
        rc.OPEN_NO_COURIER: (lambda vs: closed(vs), [visit(14, 5, src=src), visit(14, 27, by="bedrock", src=src),
                                                       visit(14, 50, src=src)]),
        rc.OPEN_DOOR_BUSY: (lambda vs: closed(vs), [person(14, 2, src), person(14, 10, src), person(14, 50, src),
                                                      visit(14, 36, VisitType.DOORBELL_PRESS, src=src, ring_event="ding")]),
        rc.SOMEONE_NOT_COURIER: (lambda vs: rc.check_attempt(at(14, 30), vs, now=NOW),
                                 [visit(14, 27, by="bedrock", src=src)]),
    }


@pytest.mark.parametrize("src", REAL)
@pytest.mark.parametrize("outcome", [rc.OPEN_COURIER_CAME, rc.OPEN_NO_COURIER, rc.OPEN_DOOR_BUSY,
                                     rc.SOMEONE_NOT_COURIER])
def test_real_ring_events_let_the_draft_say_the_camera_shows_it(outcome, src):
    check_of, vs = cases(src)[outcome]
    check = check_of(vs)
    assert check["outcome"] == outcome
    for with_order in (None, ORDER):
        _, bs = bodies(vs, check, order=with_order)
        for body in bs:
            assert CAMERA in body and DOOR_LOG not in body
            assert "our door log" not in body.lower()


@pytest.mark.parametrize("src", NOT_A_CAMERA)
@pytest.mark.parametrize("outcome", [rc.OPEN_COURIER_CAME, rc.OPEN_NO_COURIER, rc.OPEN_DOOR_BUSY,
                                     rc.SOMEONE_NOT_COURIER])
def test_demo_or_replayed_events_never_say_the_camera(outcome, src):
    check_of, vs = cases(src)[outcome]
    check = check_of(vs)
    assert check["outcome"] == outcome
    for with_order in (None, ORDER):
        p, bs = bodies(vs, check, order=with_order)
        assert p["frame"] is None
        for body in bs:
            assert "camera shows" not in body and DOOR_LOG in body
            assert "our doorbell camera" not in body.lower()


def test_only_motion_counts_say_the_door_log_not_the_camera():
    """Real Ring motion nothing looked at: the door log disagrees with "closed", but no frame or alert says who."""
    vs = [visit(14, 2), visit(14, 12), visit(14, 50)]
    check = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    assert check["outcome"] == rc.OPEN_NO_COURIER
    _, bs = bodies(vs, check)
    assert all(DOOR_LOG in b and "camera shows" not in b for b in bs)
    assert all("our doorbell camera recorded 3 motion events" in b for b in bs)  # Ring's camera did record them


def test_a_real_ring_frame_backs_the_camera_line(tmp_path):
    frames = FrameStore(tmp_path)
    vs = [visit(14, 2), visit(14, 12), visit(14, 26, VisitType.DOORBELL_PRESS, ring_event="ding"), visit(14, 50)]
    frames.save("ev-1426", b"\xff\xd8frame")
    check = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    assert check["outcome"] == rc.OPEN_DOOR_BUSY
    p, bs = bodies(vs, check, frames)
    assert p["frame"] and all(CAMERA in b for b in bs)


def test_mixed_demo_and_real_person_alerts_need_one_real_one():
    vs = [person(14, 2, "synthetic"), person(14, 10, "ring"), person(14, 50, "synthetic")]
    check = rc.check_closed_scan(at(14, 30), vs, OPEN, now=NOW)
    _, bs = bodies(vs, check)
    assert all(CAMERA in b for b in bs)
    assert all("our door log recorded" in b for b in bs)  # the counts include demo events: not all camera


@pytest.mark.parametrize("src", REAL + NOT_A_CAMERA)
def test_nothing_recorded_never_says_shows_otherwise(src):
    vs = [visit(9, 0, by="bedrock", src=src), visit(17, 5, by="bedrock", src=src)]
    check = rc.check_attempt(at(17, 30), vs, now=NOW)
    assert check["outcome"] == rc.NOTHING_RECORDED
    _, bs = bodies(vs, check)
    for b in bs:
        assert "shows otherwise" not in b
        assert ("Our doorbell camera recorded nothing" if src in REAL else "Our door log recorded nothing") in b


@pytest.mark.parametrize("src", REAL + NOT_A_CAMERA)
def test_no_draft_outcomes_have_no_draft_at_all(src):
    vs = [visit(14, 28, VisitType.DELIVERY, "ring", src)]
    check = rc.check_attempt(at(14, 30), vs, now=NOW)
    assert check["outcome"] == rc.COURIER_AT_DOOR
    p = dispute.build_pack("Shop", DAY, vs, _store(), "08:00–18:00", check)
    assert p["drafts"] == []


def test_the_demo_pack_says_door_log(monkeypatch):
    """The demo itself: synthetic door, a replayed alert, no frame. The draft says what backs it."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app import main
    from app.orders import parse_orders
    from scripts import demo

    day = demo.demo_day(date(2026, 10, 21), usable=lambda d: demo.has_scan_slot(d, TZ))
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key="demo-key"))
    text = demo.dated_orders(demo.SAMPLE.read_text(), day)
    main.book.import_rows(parse_orders(text)["rows"])
    client = TestClient(main.app)
    demo.feed_package_alert(client, day, TZ, "demo-key")
    pack = demo.walk(client, day, TZ, text)["pack"]
    assert pack["frame"] is None
    for d in pack["drafts"]:
        assert "camera shows" not in d["body"] and "doorbell camera" not in d["body"]
        assert DOOR_LOG in d["body"]
