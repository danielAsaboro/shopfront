"""Was the camera watching? Ring's device_online/device_offline notices and the door's own events decide
whether "nothing recorded" can stand as evidence (app/coverage.py)."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app import coverage, dispute, main
from app import reconcile as rc
from tests.test_dispute_pack import LA, NO_FRAMES, NOW, OPEN, at, confirmed_lunch, visit

DAY = date(2026, 10, 14)
KEY = "k"


def n(hh, mm, online, replayed=False):
    return {"at": at(hh, mm), "online": online, "replayed": replayed}


def test_offline_notice_across_the_scan_turns_no_courier_into_camera_offline():
    vs = confirmed_lunch()  # busy lunch, no courier near 12:40
    status = [n(7, 55, True), n(12, 35, False), n(13, 5, True)]
    r = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW, status=status)
    assert r["outcome"] == rc.CAMERA_OFFLINE and r["coverage"]["state"] == coverage.CAMERA_DOWN
    assert "Ring reported the doorbell offline from 12:35 until 13:05 (device_offline)" in r["text"]
    p = dispute.build_pack("Corner Bakery", DAY, vs, NO_FRAMES, "08:00–18:00", r, tz=LA)
    assert p["draft"] is None and "offline" in p["no_draft_reason"]
    assert "Camera around the scan: Ring reported the doorbell offline" in dispute.as_text(p)


def test_offline_before_the_scan_with_no_online_after_covers_it():
    r = rc.check_attempt(at(19, 32), [visit(19, 10)], now=NOW, status=[n(18, 0, False)])
    assert r["outcome"] == rc.CAMERA_OFFLINE and "with no device_online notice after it" in r["text"]


def test_an_outage_that_ended_before_the_tolerance_doesnt_count():
    r = rc.check_attempt(at(19, 32), [visit(19, 10)], now=NOW, status=[n(18, 0, False), n(18, 30, True)])
    assert r["outcome"] == rc.NOTHING_RECORDED and r["coverage"]["state"] == coverage.WATCHING


def test_door_events_around_the_scan_show_it_was_recording():
    r = rc.check_closed_scan(at(12, 40), confirmed_lunch(), OPEN, now=NOW)
    assert r["outcome"] == rc.OPEN_NO_COURIER and r["coverage"]["state"] == coverage.WATCHING
    assert "so it was recording around then" in r["coverage"]["text"]
    assert "no Ring device_online/device_offline notices" in r["coverage"]["text"]  # and says what it can't see
    p = dispute.build_pack("Corner Bakery", DAY, confirmed_lunch(), NO_FRAMES, "08:00–18:00",
                           r, tz=LA)
    assert p["draft"] and dispute.NO_STATUS_LIMIT in p["limits"]


def test_silent_door_is_cant_tell_unless_ring_said_online():
    silent = rc.check_closed_scan(at(14, 5), [visit(9, 0)], OPEN, now=NOW)
    assert silent["outcome"] == rc.NO_COVERAGE and silent["text"].startswith("Can't tell:")
    online = rc.check_closed_scan(at(14, 5), [visit(9, 0)], OPEN, now=NOW, status=[n(7, 55, True)])
    assert online["outcome"] == rc.OPEN_BY_HOURS_ONLY and "device_online at 07:55" in online["coverage"]["text"]


def test_a_courier_seen_near_the_scan_isnt_overridden_by_status():
    vs = confirmed_lunch() + [visit(12, 44, "delivery", "bedrock", eid="c")]
    r = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW, status=[n(12, 30, False)])
    assert r["outcome"] == rc.OPEN_COURIER_CAME


def test_replayed_status_notices_say_so():
    r = rc.check_attempt(at(19, 32), [visit(19, 10)], now=NOW, status=[n(19, 0, False, replayed=True)])
    assert r["outcome"] == rc.CAMERA_OFFLINE and "A replayed status notice (Shopfront's feeder, not Ring)" in r["text"]


def test_notices_come_from_the_shops_doorbell_only():
    ms = lambda hh, mm: int(at(hh, mm).timestamp() * 1000)  # noqa: E731
    rows = [{"type": "device_offline", "device": "door", "account": "acct", "timestamp": ms(12, 0)},
            {"type": "device_offline", "device": "backyard", "account": "acct", "timestamp": ms(12, 1)},
            {"type": "device_online", "device": "door", "account": "other", "timestamp": ms(12, 2)},
            {"type": "motion_detected", "device": "door", "account": "acct", "timestamp": ms(12, 3)},
            {"type": "device_online", "device": "door", "account": "acct", "timestamp": "bad"},
            {"type": "device_online", "device": "door", "account": "acct", "timestamp": ms(12, 30), "replayed": True}]
    got = coverage.notices(rows, LA, "door", "acct")
    assert [(x["at"].strftime("%H:%M"), x["online"], x["replayed"]) for x in got] == \
        [("12:00", False, False), ("12:30", True, True)]


def test_a_signed_device_offline_notice_reaches_the_scan_check(monkeypatch):
    """End to end: Ring's documented device_offline payload (Notifications > Device Offline), signed, posted
    to the webhook; the attempt check then says the camera may not have been watching."""
    from app.ring.webhooks import sign
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=KEY))
    when = datetime(2026, 10, 7, 3, 0, tzinfo=LA)
    body = {"meta": {"version": "1.1", "time": "2026-10-07T10:00:00Z", "request_id": "off-1",
                     "account_id": "ava1.ring.account.X"},
            "data": {"id": "dev_device_offline_1", "type": "device_offline",
                     "attributes": {"source": "dev", "source_type": "devices",
                                    "timestamp": int(when.timestamp() * 1000)}}}
    raw = json.dumps(body).encode()
    c = TestClient(main.app)
    assert c.post("/webhooks/ring", content=raw, headers={"X-Signature": sign(raw, KEY)}).status_code == 200
    r = c.get("/api/attempt", params={"at": "2026-10-07T03:17"}).json()
    assert r["outcome"] == rc.CAMERA_OFFLINE and r["coverage"]["offline_from"].startswith("2026-10-07T03:00")
    page = c.get("/dispute", params={"date": "2026-10-07", "at": "03:17", "claim": "attempted"}).text
    assert "Camera around the scan" in page and "may not have been watching" in page
    assert (when + timedelta(minutes=17)).strftime("%H:%M") in page
