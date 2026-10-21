"""Camera coverage from Shopfront's own status checks (app/ring/status.py, app/coverage.py).

API reference, Device Discovery > Status: GET /v1/devices/{id}/status answers data.attributes.online, "whether
the device is currently online". Only the time between two close "online" answers counts as watching.
Fake clocks and MockTransport throughout: nothing here touches Ring.
"""

import asyncio
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import coverage, dispute, main
from app import reconcile as rc
from app.ring.api import RingApiClient, RingAuth
from app.ring.status import StatusLog, close, sampler
from tests.test_dispute_pack import LA, NO_FRAMES, NOW, OPEN, at, confirmed_lunch, visit

DEV = "ava1.ring.device.TEST"
TOL = timedelta(minutes=10)


def s(hh, mm, online=True, every=300):
    return {"at": at(hh, mm), "online": online, "every_s": every, "request_id": None, "sampled": True}


def every_5_min(start, end, online=True):
    out, t = [], at(*start)
    while t <= at(*end):
        out.append({**s(0, 0, online), "at": t})
        t += timedelta(minutes=5)
    return out


# ---- the store ------------------------------------------------------------------------------------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_status_log_keeps_samples_private_reads_them_back_and_drops_old_ones(tmp_path):
    clock = Clock(at(12, 0).timestamp())
    log = StatusLog(tmp_path / "ring-status.jsonl", retention_days=1, clock=clock)
    log.add(DEV, True, 300, "rid-1")
    log.add("ava1.ring.device.OTHER", False, 300)
    clock.t += 300
    log.add(DEV, None, 300, error="TOKEN_REJECTED")
    assert stat.S_IMODE(os.stat(tmp_path / "ring-status.jsonl").st_mode) == 0o600
    got = log.samples(LA, DEV)
    assert [(x["at"].strftime("%H:%M"), x["online"]) for x in got] == [("12:00", True), ("12:05", None)]
    assert got[0]["request_id"] == "rid-1" and all(x["sampled"] for x in got)
    assert len(log.samples(LA)) == 3  # device not learned yet: every sample
    clock.t += 2 * 86400  # two days on; the next write prunes what's past retention
    log.add(DEV, True, 300)
    assert [x["at"].date() for x in log.samples(LA)] == [(at(12, 0) + timedelta(days=2)).date()]


def test_close_means_no_missed_check():
    assert close(s(12, 0), s(12, 5)) and close(s(12, 0), s(12, 7))
    assert not close(s(12, 0), s(12, 10))  # one check missing in between
    assert close(s(12, 0, every=1200), s(12, 20, every=1200))


# ---- what coverage says ---------------------------------------------------------------------------------
def test_close_online_samples_all_through_the_window_mean_watching_and_say_the_window():
    vs = confirmed_lunch()
    r = rc.check_closed_scan(at(12, 40), vs, OPEN, now=NOW, status=every_5_min((12, 25), (12, 55)))
    cov = r["coverage"]
    assert cov["state"] == coverage.ONLINE_BY_STATUS and cov["status_covered"] is True
    assert "every 5 minutes" in cov["text"]
    assert "watching from 12:30 to 12:50 (10 minutes either side of the 12:40 scan)" in cov["text"]
    p = dispute.build_pack("Corner Bakery", at(12, 40).date(), vs, NO_FRAMES, "08:00–18:00", r, tz=LA)
    assert dispute.NO_STATUS_LIMIT not in p["limits"]
    assert "Camera around the scan: Shopfront asked Ring every 5 minutes" in dispute.as_text(p)


def test_one_online_answer_says_nothing_about_the_minutes_after_it():
    r = rc.check_attempt(at(14, 5), [visit(9, 0)], now=NOW, status=[s(13, 58)])
    assert r["coverage"]["state"] == coverage.UNKNOWN and r["outcome"] == rc.NO_COVERAGE
    assert "don't cover 13:55 to 14:15" in r["coverage"]["text"]


def test_samples_too_far_apart_leave_a_gap_the_pack_names():
    samples = every_5_min((12, 0), (12, 30)) + every_5_min((12, 45), (13, 30))  # 12:30 -> 12:45 missed checks
    r = rc.check_closed_scan(at(12, 40), confirmed_lunch(), OPEN, now=NOW, status=samples)
    cov = r["coverage"]
    assert cov["state"] == coverage.WATCHING and cov["status_covered"] is False  # door events still say recording
    assert "don't cover 12:30 to 12:45, so an outage then wouldn't show" in cov["text"]
    assert cov["status_gaps"] == [["2026-10-14T12:30-07:00", "2026-10-14T12:45-07:00"]]


def test_a_failed_check_is_a_gap_not_an_online():
    samples = every_5_min((13, 50), (14, 30))
    samples[4] = {**samples[4], "online": None}  # 14:10: Ring didn't answer
    r = rc.check_attempt(at(14, 5), [visit(9, 0)], now=NOW, status=samples)
    assert r["coverage"]["state"] == coverage.UNKNOWN and "don't cover 14:05 to 14:15" in r["coverage"]["text"]


def test_an_offline_answer_near_the_scan_means_the_camera_may_not_have_been_watching():
    samples = every_5_min((13, 50), (14, 30))
    samples[3] = {**samples[3], "online": False}  # 14:05
    r = rc.check_attempt(at(14, 5), [visit(9, 0)], now=NOW, status=samples)
    assert r["outcome"] == rc.CAMERA_OFFLINE and r["coverage"]["state"] == coverage.CAMERA_DOWN
    assert "Ring's status check said the doorbell was offline at 14:05 and online again at 14:10" in r["text"]


def test_offline_webhooks_still_count_over_online_samples():
    status = every_5_min((12, 25), (12, 55)) + [{"at": at(12, 35), "online": False, "replayed": False}]
    r = rc.check_closed_scan(at(12, 40), confirmed_lunch(), OPEN, now=NOW, status=status)
    assert r["outcome"] == rc.CAMERA_OFFLINE and "(device_offline)" in r["text"]


# ---- sampling ------------------------------------------------------------------------------------------
def live_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RingApiClient(auth=RingAuth(access_token="t", http=http), tz=LA, device_id=DEV, http=http)


class LiveSource:
    def __init__(self, ring):
        self.ring = ring

    def get(self):
        return self.ring


def test_a_sample_asks_ring_and_keeps_the_answer_with_its_request_id(monkeypatch):
    def handler(req):
        assert req.url.path == f"/v1/devices/{DEV}/status"
        return httpx.Response(200, headers={"X-Request-ID": "rid-s"},
                              json={"data": {"type": "device-status", "id": "x", "attributes": {"online": True}}})
    monkeypatch.setattr(main, "source", LiveSource(live_client(handler)))
    row = main.sample_status()
    assert row["online"] is True and row["request_id"] == "rid-s" and row["device"] == DEV
    assert row["every_s"] == main.settings.status_minutes * 60
    assert [x["online"] for x in main.status_log.samples(LA, DEV)] == [True]


def test_a_check_ring_refuses_is_kept_as_a_gap(monkeypatch):
    monkeypatch.setattr(main, "source", LiveSource(live_client(
        lambda r: httpx.Response(401, headers={"X-Request-ID": "rid-401"}))))
    row = main.sample_status()
    assert row["online"] is None and row["error"] == "TOKEN_REJECTED" and row["request_id"] == "rid-401"


def test_synthetic_mode_samples_nothing():
    assert main.sample_status() is None and main.status_log.samples(LA) == []


def test_the_sampler_runs_every_interval_and_survives_a_bad_sample():
    calls, waits = [], []

    def sample():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")

    async def fake_sleep(d):
        waits.append(d)
        if len(waits) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sampler(sample, 300, sleep=fake_sleep))
    assert len(calls) == 3 and waits == [300, 300, 300]


def test_the_app_lifespan_starts_and_stops_the_sampler(monkeypatch):
    started, stopped = [], []

    async def fake_sampler(sample, every_s):
        started.append(every_s)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    monkeypatch.setattr(main, "sampler", fake_sampler)
    monkeypatch.setattr(main, "settings", replace(main.settings, status_minutes=2))
    with TestClient(main.app):
        pass
    assert started == [120] and stopped == [True]
    monkeypatch.setattr(main, "settings", replace(main.settings, status_minutes=0))
    started.clear()
    with TestClient(main.app):
        pass
    assert started == []


def test_samples_reach_the_scan_check(monkeypatch):
    day = datetime(2026, 10, 7, tzinfo=LA)
    t = day.replace(hour=11, minute=40)
    for i in range(10):
        main.status_log.add(DEV, True, 300, at_ms=int((t + timedelta(minutes=5 * i)).timestamp() * 1000))
    r = TestClient(main.app).get("/api/attempt", params={"at": "2026-10-07T12:00"}).json()
    assert r["coverage"]["state"] == coverage.ONLINE_BY_STATUS
    assert "watching from 11:50 to 12:10" in r["coverage"]["text"]
