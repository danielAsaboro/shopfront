"""Frames from Ring's image download, and live views (on_demand) in Event History.

MockTransport shaped like the API reference (Image Snapshots; Media Clips > Download video after a webhook
event; Event History > Event types). Local tests of our handling: they prove nothing about the live API.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.models import DoorEvent
from app.ring.api import MEDIA_WINDOW, RingApiClient, RingAuth, RingError, RingStatus

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
DAY = date(2026, 10, 7)


class Clock:
    def __init__(self, t: float):
        self.t = t
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.slept.append(d)
        self.t += d


def make(handler, clock: Clock | None = None):
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    # a Playground token issued before the test's events: it may download their frames (test_ring_token_floor.py)
    ring = RingApiClient(auth=RingAuth(access_token="t", http=http, issued_at=1_767_225_600.0), tz=TZ, device_id=DEV,
                         http=http)
    if clock:
        ring.clock, ring.sleep, ring.jitter = clock.time, clock.sleep, lambda: 0.0
    return ring


def event(at: datetime, kind="ding", ends: datetime | None = None) -> DoorEvent:
    return DoorEvent(id="e1", device_id=DEV, timestamp=at, kind=kind, source="ring", ends=ends)


def image_after(n_not_ready: int, status=425, bodies=None):
    """Ring's 425 (or 416) n times, then 303 -> the pre-signed GET -> a JPEG."""
    asks = {"n": 0}

    def handler(req: httpx.Request):
        if req.url.host != "api.amazonvision.com":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={
                "content-type": "image/jpeg", "X-Media-Timestamp": "1", "X-Media-Origin": "recording",
                "X-Request-ID": "rid-get"})
        if bodies is not None:
            bodies.append(json.loads(req.content))
        asks["n"] += 1
        if asks["n"] <= n_not_ready:
            code = {425: "RECORDING_NOT_READY", 416: "MEDIA_NOT_FOUND"}[status]
            return httpx.Response(status, json={"errors": [{"status": str(status), "code": code}]})
        return httpx.Response(303, headers={"Location": "https://download-us.example.test/v1/download?security_token=s"})
    return handler, asks


def test_image_request_is_at_timestamp_without_image_options():
    # API reference, Image Snapshots > Request Body: image_options is optional; rivals that got Playground
    # frames don't send it either.
    bodies: list = []
    handler, _ = image_after(0, bodies=bodies)
    at = datetime(2026, 10, 7, 9, 0, tzinfo=TZ)
    assert make(handler).snapshot(event(at)) == b"\xff\xd8jpeg"
    assert bodies == [{"type": "at_timestamp", "timestamp": int(at.timestamp() * 1000)}]


def test_a_live_views_frame_is_asked_for_a_few_seconds_in_kept_inside_the_event():
    ring = make(lambda r: httpx.Response(500))
    at = datetime(2026, 10, 7, 9, 0, tzinfo=TZ)
    start = int(at.timestamp() * 1000)
    assert ring.frame_ms(event(at, "on_demand", at + timedelta(seconds=31))) == start + 5000
    assert ring.frame_ms(event(at, "on_demand", at + timedelta(seconds=2))) == start + 2000  # never past its end
    assert ring.frame_ms(event(at, "on_demand")) == start + 5000
    assert ring.frame_ms(event(at, "ding")) == start  # a press or motion: the moment it was recorded


def test_not_ready_frames_are_retried_on_rings_schedule_until_they_arrive():
    at = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)
    clock = Clock(at.timestamp() + 40)  # 40 s after the event: inside Ring's 2-minute media window
    handler, asks = image_after(2)
    f = make(handler, clock).frame(event(at), retry_for=120)
    assert f.content == b"\xff\xd8jpeg" and f.attempts == 3 and asks["n"] == 3
    assert clock.slept == [5, 10]  # API reference: 5, 10, 20, 30 s
    assert f.media_origin == "recording" and f.request_id == "rid-get"


def test_frame_retries_stop_two_minutes_after_the_requested_time():
    at = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)
    clock = Clock(at.timestamp() + 60)
    handler, asks = image_after(99, status=416)
    ring = make(handler, clock)
    with pytest.raises(RingError) as err:
        ring.frame(event(at), retry_for=600)
    assert err.value.status == RingStatus.MEDIA_NOT_FOUND
    assert clock.t <= at.timestamp() + MEDIA_WINDOW and sum(clock.slept) >= 30  # 5 + 10 + 20, then out of time
    # a recent 416 isn't remembered: Ring may still be processing, so the next call asks again
    before = asks["n"]
    with pytest.raises(RingError):
        ring.frame(event(at))
    assert asks["n"] == before + 1


def test_a_page_load_never_waits_for_a_frame():
    at = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)
    clock = Clock(at.timestamp() + 10)
    handler, asks = image_after(5)
    with pytest.raises(RingError) as err:
        make(handler, clock).snapshot(event(at))
    assert err.value.status == RingStatus.RECORDING_NOT_READY and clock.slept == [] and asks["n"] == 1
    assert err.value.code == "RECORDING_NOT_READY"  # Ring's own code is kept on the error


def test_an_old_416_is_terminal_and_remembered():
    at = datetime(2026, 10, 7, 9, 0, tzinfo=TZ)
    clock = Clock(at.timestamp() + 86_400)
    handler, asks = image_after(99, status=416)
    ring = make(handler, clock)
    for _ in range(2):
        with pytest.raises(RingError):
            ring.frame(event(at), retry_for=120)
    assert asks["n"] == 1 and clock.slept == []


def hist(event_type, start, length=31_742):
    return {"type": "history-events", "id": f"evt-{event_type}-{start}",
            "attributes": {"event_type": event_type, "is_third_party_reviewed": False,
                           "start": start, "end": start + length},
            "relationships": {"source": {"data": {"type": "devices", "id": DEV}}}}


def test_live_views_are_kept_as_on_demand_and_never_become_visitors():
    from app.briefing import classify_day
    from app.classify.rules import RulesClassifier
    from app.hours import parse_hours
    from app.pages import labelled_by_text

    noon = int(datetime(2026, 10, 7, 12, 0, tzinfo=TZ).timestamp() * 1000)
    items = [hist("on_demand", noon + 60_000), hist("ding", noon)]

    def handler(req: httpx.Request):
        return httpx.Response(200, json={"data": items, "links": {}})

    ring = make(handler)
    assert [e.kind for e in ring.events_for_day(DAY)] == ["ding", "on_demand"]
    stats: dict = {}
    visits = classify_day(ring, RulesClassifier(), DAY, parse_hours(""), stats=stats)
    assert [v.event.kind for v in visits] == ["ding"]  # the live view is not a customer, courier or press
    assert stats["ring_live_views"] == 1 and stats["events"] == 2
    assert "1 Ring live view (on_demand: the camera was opened), not visitors" in labelled_by_text(stats, "rules")


def test_recent_events_reads_every_kind_from_a_moment_until_now_uncached():
    t0 = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)
    ms0 = int(t0.timestamp() * 1000)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"data": [hist("on_demand", ms0 + 30_000), hist("ding", ms0 - 60_000)],
                                         "links": {}})

    clock = Clock(t0.timestamp() + 90)
    ring = make(handler, clock)
    assert [e.kind for e in ring.recent_events(t0)] == ["on_demand"]
    ring.recent_events(t0)
    assert len(calls) == 2 and calls[0].url.params["event_types"] == "ding,motion,on_demand"


def test_device_status_reads_online_and_refuses_to_guess():
    def handler(req: httpx.Request):
        assert req.url.path == f"/v1/devices/{DEV}/status"
        return httpx.Response(200, json={"data": {"type": "device-status", "id": "s", "attributes": {"online": False}}})
    assert make(handler).online() is False
    bad = make(lambda r: httpx.Response(200, json={"data": {"attributes": {}}}))
    with pytest.raises(RingError) as err:
        bad.online()
    assert err.value.status == RingStatus.MALFORMED_RESPONSE


# ---- a 403 refuses one request unless Ring's code says the token can't see the device -----------------
def forbid_first(code: str | None):
    """403 on the first image request (with Ring's error `code`), frames after that."""
    asks: list = []

    def handler(req: httpx.Request):
        if req.url.host != "api.amazonvision.com":
            return httpx.Response(200, content=b"\xff\xd8ok", headers={"content-type": "image/jpeg"})
        asks.append((req.url.path, json.loads(req.content)["timestamp"]))
        if len(asks) == 1:
            err = {"status": "403", **({"code": code} if code else {})}
            return httpx.Response(403, json={"errors": [err]})
        return httpx.Response(303, headers={"Location": "https://download-us.example.test/v1/download?security_token=s"})
    return handler, asks


@pytest.mark.parametrize("code", [None, "TIME_RANGE_NOT_AUTHORIZED"])
def test_one_refused_frame_doesnt_stop_every_other_frame(code):
    handler, asks = forbid_first(code)
    ring = make(handler)
    first = event(datetime(2026, 10, 7, 9, 0, tzinfo=TZ))
    with pytest.raises(RingError) as err:
        ring.snapshot(first)
    assert err.value.status == RingStatus.FORBIDDEN and err.value.code == code
    with pytest.raises(RingError):
        ring.snapshot(first)  # that one time is remembered ...
    assert len(asks) == 1
    assert ring.snapshot(event(datetime(2026, 10, 7, 9, 5, tzinfo=TZ))) == b"\xff\xd8ok"  # ... no other
    assert len(asks) == 2


def test_request_forbidden_stops_that_devices_frames_only():
    # API reference, Image Snapshots > Error Codes: 403 REQUEST_FORBIDDEN, "Client is not authorized for the device"
    handler, asks = forbid_first("REQUEST_FORBIDDEN")
    ring = make(handler)
    with pytest.raises(RingError):
        ring.snapshot(event(datetime(2026, 10, 7, 9, 0, tzinfo=TZ)))
    with pytest.raises(RingError) as err:
        ring.snapshot(event(datetime(2026, 10, 7, 9, 5, tzinfo=TZ)))  # no second request for this device
    assert err.value.code == "REQUEST_FORBIDDEN" and len(asks) == 1
    other = DoorEvent(id="e2", device_id="ava1.ring.device.OTHER", timestamp=datetime(2026, 10, 7, 9, 5, tzinfo=TZ),
                      kind="ding", source="ring")
    assert ring.snapshot(other) == b"\xff\xd8ok" and len(asks) == 2
