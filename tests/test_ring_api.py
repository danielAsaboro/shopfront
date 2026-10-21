"""RingApiClient against an httpx MockTransport shaped like Ring's documented responses.

These are local tests of our request/response handling. They prove nothing about the live API.
"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.models import DoorEvent
from app.ring.api import RingApiClient, RingAuth, RingError, RingStatus

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
DAY = date(2026, 10, 7)


def ms(h, m=0, d=DAY):
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp() * 1000)


def hist(event_type, start):
    return {"type": "history-events", "id": f"evt-{event_type}-{start}",
            "attributes": {"event_type": event_type, "is_third_party_reviewed": False,
                           "start": start, "end": start + 20000},
            "relationships": {"source": {"data": {"type": "devices", "id": DEV}}}}


def client(handler, **auth):
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    a = RingAuth(**(auth or {"access_token": "playground-token"}), http=http)
    return RingApiClient(auth=a, tz=TZ, device_id=DEV, http=http), http


def test_history_pages_until_older_than_the_day_and_maps_events():
    calls = []
    page1 = {"data": [hist("ding", ms(23, 30, date(2026, 10, 8))),  # next day: skipped
                      hist("motion", ms(17)), hist("on_demand", ms(12)), hist("ding", ms(11))],
             "links": {"next": f"/v1/history/devices/{DEV}/events?event_types=ding,motion&page[key]=x"}}
    page2 = {"data": [hist("motion", ms(7, 40)), hist("motion", ms(22, 0, date(2026, 10, 6)))],
             "links": {"next": f"/v1/history/devices/{DEV}/events?page[key]=y"}}

    def handler(req: httpx.Request):
        calls.append(req)
        assert req.headers["Authorization"] == "Bearer playground-token"
        return httpx.Response(200, json=page2 if "page%5Bkey%5D=x" in str(req.url) or "page[key]=x" in str(req.url) else page1)

    ring, _ = client(handler)
    events = ring.events_for_day(DAY)
    assert [(e.kind, e.timestamp.hour) for e in events] == [("motion", 7), ("ding", 11), ("motion", 17)]
    assert all(e.source == "ring" for e in events)
    assert len(calls) == 2  # stopped at the first event older than the day
    assert calls[0].url.params["event_types"] == "ding,motion"


def test_snapshot_follows_303_without_leaking_the_bearer_token():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.host == "api.amazonvision.com":
            body = json.loads(req.content)
            assert body["type"] == "at_timestamp" and body["timestamp"] == ms(9)
            return httpx.Response(303, headers={"Location": "https://download-us.example.test/v1/download?security_token=s"})
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    ring, _ = client(handler)
    e = DoorEvent(id="e1", device_id=DEV, timestamp=datetime.fromtimestamp(ms(9) / 1000, TZ), kind="ding")
    assert ring.snapshot(e) == b"\xff\xd8jpeg"
    assert seen["auth"] is None


@pytest.mark.parametrize("code,status", [(401, RingStatus.TOKEN_REJECTED), (403, RingStatus.FORBIDDEN),
                                          (404, RingStatus.DEVICE_NOT_FOUND), (416, RingStatus.MEDIA_NOT_FOUND),
                                          (425, RingStatus.RECORDING_NOT_READY), (422, RingStatus.CORRUPT_RECORDING), (429, RingStatus.RATE_LIMITED),
                                          (503, RingStatus.RING_UNAVAILABLE), (418, RingStatus.UNEXPECTED_STATUS)])
def test_http_errors_fail_closed_to_named_states(code, status):
    ring, _ = client(lambda req: httpx.Response(code, text="nope", headers={"X-Request-ID": "rid-1"}))
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == status and err.value.request_id == "rid-1"


def test_network_failure_is_its_own_state():
    def handler(req):
        raise httpx.ConnectError("dns")

    ring, _ = client(handler)
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == RingStatus.NETWORK_ERROR


def test_malformed_history_item_is_not_guessed():
    ring, _ = client(lambda req: httpx.Response(200, json={"data": [{"type": "history-events", "attributes": {}}]}))
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == RingStatus.MALFORMED_RESPONSE


def test_refresh_mode_refreshes_on_401_and_persists_the_rotated_token(tmp_path):
    state = {"api": 0}

    def handler(req: httpx.Request):
        if req.url.host == "oauth.ring.com":
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token"
            return httpx.Response(200, json={"access_token": f"at{state['api']}", "refresh_token": "rt-new",
                                             "expires_in": 14400, "token_type": "Bearer"})
        state["api"] += 1
        if state["api"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [], "links": {}})

    token_file = tmp_path / "rt.json"
    ring, _ = client(handler, refresh_token="rt-old", client_id="c", client_secret="s", token_file=token_file)
    assert ring.events_for_day(DAY) == []
    assert json.loads(token_file.read_text()) == {"refresh_token": "rt-new"}


def test_refresh_rejected_is_named():
    ring, _ = client(lambda req: httpx.Response(400, text="invalid_grant"),
                     refresh_token="rt", client_id="c", client_secret="s")
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == RingStatus.REFRESH_FAILED


@pytest.mark.parametrize("kw", [{}, {"access_token": "a", "refresh_token": "r"}, {"refresh_token": "r"}])
def test_bad_auth_config_is_not_configured(kw):
    with pytest.raises(RingError) as err:
        RingAuth(**kw)
    assert err.value.status == RingStatus.NOT_CONFIGURED


def test_no_devices_is_named():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})))
    ring = RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, http=http)
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == RingStatus.NO_DEVICES


def test_missing_frame_labels_that_event_by_rules_and_says_why():
    from app.briefing import classify_day
    from app.classify.bedrock import BedrockClassifier
    from app.hours import parse_hours

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [hist("ding", ms(9))], "links": {}})
        return httpx.Response(416, json={"errors": [{"code": "MEDIA_NOT_FOUND"}]})

    class NoCall:
        def converse(self, **kw):
            raise AssertionError("no frame, no Bedrock call")

    ring, _ = client(handler)
    [v] = classify_day(ring, BedrockClassifier("us-east-1", "m", client=NoCall()), DAY, parse_hours(""))
    assert v.source == "rules" and "no Ring frame: MEDIA_NOT_FOUND" in v.reason


# ---- which device is the doorbell ----------------------------------------------------------------------
def _device(dev_id, caps_id):
    return {"type": "devices", "id": dev_id, "attributes": {"name": dev_id},
            "relationships": {"capabilities": {"data": {"type": "device-capabilities", "id": caps_id},
                                               "links": {"related": f"/v1/devices/{dev_id}/capabilities"}}}}


def _caps(caps_id, video):
    return {"type": "device-capabilities", "id": caps_id,
            "attributes": {"video": video, "motion_detection": {"configurations": ["enabled"]},
                           "image_enhancements": None}}


def test_device_pick_skips_sensors_and_chimes():
    # API reference, Device Discovery: GET /v1/devices returns sensors and chimes alongside cameras; a chime's
    # video capability is null, and no sensor has a camera ("Filter your device collection before calling a
    # media endpoint"). Capabilities are side-loaded with ?include=capabilities.
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={
            "data": [_device("ava1.ring.device.CONTACT", "c1"), _device("ava1.ring.device.CHIME", "c2"),
                     _device("ava1.ring.device.DOORBELL", "c3")],
            "included": [_caps("c1", None), _caps("c2", None),
                         _caps("c3", {"codecs": ["AVC"], "max_resolution": 1080})]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    ring = RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, http=http)
    assert ring.device_id == "ava1.ring.device.DOORBELL" and seen["params"]["include"] == "capabilities"


def test_only_sensors_means_no_camera():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "data": [_device("ava1.ring.device.CONTACT", "c1")], "included": [_caps("c1", None)]})))
    ring = RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, http=http)
    with pytest.raises(RingError) as err:
        _ = ring.device_id
    assert err.value.status == RingStatus.NO_DEVICES and "camera" in err.value.detail


def test_account_id_comes_from_users_me_and_failures_are_not_fatal():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"data": {"type": "users", "id": "ava1.ring.account.SHOP",
                                                  "attributes": {"first_name": "A"}}})

    ring, _ = client(handler)
    assert ring.account_id() == "ava1.ring.account.SHOP" and ring.account_id() == "ava1.ring.account.SHOP"
    assert calls == ["/v1/users/me"]  # asked once
    broken, _ = client(lambda r: httpx.Response(403))
    assert broken.account_id() is None


# ---- fewer Ring calls -----------------------------------------------------------------------------------
from collections import Counter  # noqa: E402
from datetime import timedelta  # noqa: E402


def counting(calls, events_per_day=0, frame_status=416):
    def handler(req: httpx.Request):
        path = req.url.path
        calls[(req.method, path.split("/")[2] if path.startswith("/v1/") else path)] += 1
        if path == "/v1/users/me":
            return httpx.Response(200, json={"data": {"type": "users", "id": "ava1.ring.account.SHOP"}})
        if path.endswith("/image/download"):
            return httpx.Response(frame_status, json={"errors": [{"code": "x"}]})
        return httpx.Response(200, json={"data": [hist("ding", ms(9, i)) for i in range(events_per_day)], "links": {}})
    return handler


def test_past_days_are_fetched_once_and_today_briefly():
    calls = Counter()
    ring, _ = client(counting(calls))
    ring.events_for_day(DAY)
    ring.events_for_day(DAY)
    assert calls[("GET", "history")] == 1
    today = datetime.now(TZ).date()
    ring.events_for_day(today)
    ring.events_for_day(today)
    assert calls[("GET", "history")] == 2  # within the short TTL
    ring._history[(today, ring.event_types)] = (0.0, [])  # pretend the TTL ran out
    ring.events_for_day(today)
    assert calls[("GET", "history")] == 3


def test_missing_and_forbidden_frames_are_remembered():
    for status in (416, 403):
        calls = Counter()
        ring, _ = client(counting(calls, frame_status=status))
        e = DoorEvent(id="e1", device_id=DEV, timestamp=datetime.fromtimestamp(ms(9) / 1000, TZ), kind="ding")
        for _ in range(3):
            with pytest.raises(RingError):
                ring.snapshot(e)
        assert calls[("POST", "devices")] == 1, status


def test_not_ready_frames_are_asked_again():
    calls = Counter()
    ring, _ = client(counting(calls, frame_status=425))
    e = DoorEvent(id="e1", device_id=DEV, timestamp=datetime.fromtimestamp(ms(9) / 1000, TZ), kind="ding")
    for _ in range(2):
        with pytest.raises(RingError):
            ring.snapshot(e)
    assert calls[("POST", "devices")] == 2


def test_rate_limit_is_named_with_retry_after_and_retried_once_when_short():
    ring, _ = client(lambda r: httpx.Response(429, headers={"Retry-After": "30", "X-Request-ID": "rid-429"}))
    with pytest.raises(RingError) as err:
        ring.events_for_day(DAY)
    assert err.value.status == RingStatus.RATE_LIMITED and err.value.retry_after == 30
    n = {"calls": 0}

    def once(req):
        n["calls"] += 1
        return httpx.Response(429, headers={"Retry-After": "1"}) if n["calls"] == 1 else \
            httpx.Response(200, json={"data": [], "links": {}})
    ring, _ = client(once)
    slept = []
    ring.sleep = slept.append
    assert ring.events_for_day(DAY) == [] and slept == [1] and n["calls"] == 2


def test_one_dashboard_load_asks_ring_once_per_day_and_a_reload_asks_nothing(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    from app.classify.bedrock import BedrockClassifier
    from app.shop import Profile

    class NoCall:
        def converse(self, **kw):
            raise AssertionError("no frame, no Bedrock call")

    calls = Counter()
    ring, _ = client(counting(calls, events_per_day=3))
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring, "pasted_token": lambda self: "t"})())
    monkeypatch.setattr(main, "classifier", BedrockClassifier("us-east-1", "m", client=NoCall()))
    main.profile.save(Profile("Shop", "", 15.0, "$"))
    c = TestClient(main.app)
    assert c.get(f"/?date={DAY.isoformat()}&attempt=12:00").status_code == 200
    days = {DAY - timedelta(days=i) for i in range(8)}  # the day, the week for the money line, last week
    assert calls[("GET", "history")] == len(days)
    first_frames = calls[("POST", "devices")]
    calls.clear()
    assert c.get(f"/?date={DAY.isoformat()}&attempt=12:00").status_code == 200
    assert calls[("GET", "history")] == 0 and calls[("POST", "devices")] == 0 and first_frames > 0


def test_a_dashboard_load_labels_each_day_once(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    from app.shop import Profile

    seen = Counter()
    real = main.classify_day

    def counted(ring, classifier, day, *a, **kw):
        seen[day] += 1
        return real(ring, classifier, day, *a, **kw)
    monkeypatch.setattr(main, "classify_day", counted)
    main.profile.save(Profile("Shop", "", 15.0, "$"))
    assert TestClient(main.app).get(f"/?date={DAY.isoformat()}&attempt=12:00").status_code == 200
    assert seen[DAY] == 1 and len(seen) == 7


def test_rate_limit_reaches_the_api_caller_with_retry_after(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    ring, _ = client(lambda r: httpx.Response(429, headers={"Retry-After": "30", "X-Request-ID": "rid-429"}))
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring, "pasted_token": lambda self: "t"})())
    r = TestClient(main.app).get(f"/api/events?date={DAY.isoformat()}")
    assert r.status_code == 429 and r.json()["error"] == "RATE_LIMITED" and r.json()["retry_after"] == 30
    assert r.headers["retry-after"] == "30"


def test_rate_limit_page_says_when_to_try_again(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    ring, _ = client(lambda r: httpx.Response(429, headers={"Retry-After": "30"}))
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring, "pasted_token": lambda self: "t"})())
    c = TestClient(main.app)
    r = c.get(f"/?date={DAY.isoformat()}")  # the receiving desk degrades and says when to reload
    assert r.status_code == 200 and "slow down" in r.text and "reload in 30 seconds" in r.text
    page = c.get(f"/dispute?date={DAY.isoformat()}&at=12:00")
    assert page.status_code == 429 and "Try again in 30 seconds" in page.text and page.headers["retry-after"] == "30"
