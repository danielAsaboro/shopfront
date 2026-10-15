"""Live-view (WHEP) path against MockTransport and a fake Bedrock. Local tests only."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.classify.bedrock import BedrockClassifier
from app.classify.rules import RulesClassifier
from app.live import MAX_FRAMES_CLASSIFIED, LiveError, LiveLog
from app.ring.api import RingApiClient, RingAuth, RingError, RingStatus

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
JPEG = b"\xff\xd8\xff\xe0fakejpeg"
SESSION_URL = f"https://api.amazonvision.com/v1/devices/{DEV}/media/streaming/whep/sessions/abc?location=x"


def whep_handler(calls):
    def handler(req: httpx.Request):
        calls.append(req)
        if req.method == "POST" and req.url.path.endswith("/whep/sessions"):
            assert req.headers["Content-Type"] == "application/sdp" and req.content.startswith(b"v=0")
            return httpx.Response(201, text="v=0\r\no=- answer\r\n",
                                  headers={"Location": SESSION_URL, "Content-Type": "application/sdp",
                                           "X-Request-ID": "rid-whep"})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"data": [], "links": {}})
    return handler


def ring_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, device_id=DEV, http=http)


def test_whep_start_returns_answer_and_session_url_then_deletes_it():
    calls = []
    ring = ring_client(whep_handler(calls))
    answer, url = ring.whep_start("v=0\r\no=- offer\r\n")
    assert answer.startswith("v=0") and url == SESSION_URL and ring.last_request_id == "rid-whep"
    ring.whep_stop(url)
    assert calls[-1].method == "DELETE" and str(calls[-1].url) == SESSION_URL


def test_whep_without_location_is_malformed():
    ring = ring_client(lambda r: httpx.Response(201, text="v=0"))
    with pytest.raises(RingError) as err:
        ring.whep_start("v=0")
    assert err.value.status == RingStatus.MALFORMED_RESPONSE


def test_whep_offline_device_is_named():
    ring = ring_client(lambda r: httpx.Response(503, text="device offline"))
    with pytest.raises(RingError) as err:
        ring.whep_start("v=0")
    assert err.value.status == RingStatus.RING_UNAVAILABLE


class FakeBedrock:
    def __init__(self):
        self.calls = 0

    def converse(self, **kw):
        self.calls += 1
        conf = "0.9" if self.calls == 2 else "0.6"
        return {"output": {"message": {"content": [{"text": f'{{"type": "delivery", "confidence": {conf}, "reason": "parcel {self.calls}"}}'}]}},
                "ResponseMetadata": {"RequestId": f"br-{self.calls}"}}


def test_session_is_labelled_by_its_most_confident_frame_and_caps_bedrock_calls(tmp_path):
    fake = FakeBedrock()
    clf = BedrockClassifier("us-east-1", "m", client=fake)
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    for _ in range(MAX_FRAMES_CLASSIFIED + 3):
        log.frame(s.id, JPEG, clf, None)
    assert fake.calls == MAX_FRAMES_CLASSIFIED
    row = log.stop(s.id)
    assert row["label"]["reason"] == "parcel 2" and row["label"]["source"] == "bedrock"
    assert row["bedrock_request_ids"][0] == "br-1" and row["frames_seen"] == MAX_FRAMES_CLASSIFIED + 3
    assert (tmp_path / "live-frames" / f"{s.id}.jpg").read_bytes() == JPEG
    [v] = log.visits_for_day(datetime.now(TZ).date())
    assert v.event.kind == "live_view" and v.event.source == "ring-live" and v.source == "bedrock"


def test_rules_never_pretend_to_read_a_frame(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    out = log.frame(s.id, JPEG, RulesClassifier(), None)
    assert out["label"]["type"] == "unknown" and "need Bedrock" in out["label"]["reason"]


def test_bad_frames_and_unknown_sessions_are_named(tmp_path):
    log = LiveLog(tmp_path, TZ)
    with pytest.raises(LiveError) as e1:
        log.frame("nope", JPEG, RulesClassifier(), None)
    s = log.start(DEV, SESSION_URL)
    with pytest.raises(LiveError) as e2:
        log.frame(s.id, b"GIF89a", RulesClassifier(), None)
    assert (e1.value.code, e2.value.code) == ("UNKNOWN_SESSION", "BAD_FRAME")


def test_live_endpoints_end_to_end(monkeypatch, tmp_path):
    calls = []
    ring = ring_client(whep_handler(calls))

    class Src:
        def get(self):
            return ring

    monkeypatch.setattr(main, "source", Src())
    monkeypatch.setattr(main, "live", LiveLog(tmp_path, TZ))
    c = TestClient(main.app)
    assert "Start live view" in c.get("/live").text
    start = c.post("/api/live/start", content="v=0\r\no=- offer\r\n", headers={"Content-Type": "application/sdp"}).json()
    assert start["answer"].startswith("v=0") and start["ring_request_id"] == "rid-whep"
    frame = c.post(f"/api/live/{start['session']}/frame", content=JPEG).json()
    assert frame["frames_seen"] == 1
    stop = c.post(f"/api/live/{start['session']}/stop").json()
    assert stop["ring_stop_error"] is None and calls[-1].method == "DELETE"
    assert c.get("/api/live/sessions").json()[0]["id"] == start["session"]
    assert c.post(f"/api/live/{start['session']}/frame", content=JPEG).json()["error"] == "UNKNOWN_SESSION"


def test_live_view_needs_ring_connected(monkeypatch, tmp_path):
    from app.ring import RingSource

    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    c = TestClient(main.app)
    assert c.post("/api/live/start", content="v=0").status_code == 409
    assert "Connect Ring" in c.get("/live").text
