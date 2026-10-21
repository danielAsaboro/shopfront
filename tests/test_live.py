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
from tests.bedrock_fakes import label

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
JPEG = b"\xff\xd8\xff\xe0fakejpeg"
JPEG_TYPE = {"Content-Type": "image/jpeg"}
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
        conf = 0.9 if self.calls == 2 else 0.6
        return label("delivery", conf, f"parcel {self.calls}", f"br-{self.calls}")


def test_session_is_labelled_by_its_most_confident_frame_and_caps_bedrock_calls(tmp_path):
    fake = FakeBedrock()
    clf = BedrockClassifier("us-east-1", "m", client=fake)
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    for _ in range(MAX_FRAMES_CLASSIFIED + 3):
        log.frame(s.id, JPEG, clf, None, key=s.key)
    assert fake.calls == MAX_FRAMES_CLASSIFIED
    row = log.stop(s.id)
    assert row["label"]["reason"] == "parcel 2" and row["label"]["source"] == "bedrock"
    assert row["bedrock_request_ids"][0] == "br-1" and row["frames_seen"] == MAX_FRAMES_CLASSIFIED + 3
    assert (tmp_path / "live-frames" / f"{s.id}.jpg").read_bytes() == JPEG
    [v] = log.visits_for_day(datetime.now(TZ).date())
    assert v.event.kind == "live_view" and v.event.source == "ring-live-browser" and v.source == "bedrock"


def test_rules_never_pretend_to_read_a_frame(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    out = log.frame(s.id, JPEG, RulesClassifier(), None, key=s.key)
    assert out["label"]["type"] == "unknown" and "need Bedrock" in out["label"]["reason"]


def test_bad_frames_and_unknown_sessions_are_named(tmp_path):
    log = LiveLog(tmp_path, TZ)
    with pytest.raises(LiveError) as e1:
        log.frame("nope", JPEG, RulesClassifier(), None)
    s = log.start(DEV, SESSION_URL)
    with pytest.raises(LiveError) as e2:
        log.frame(s.id, b"GIF89a", RulesClassifier(), None, key=s.key)
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
    frame = c.post(f"/api/live/{start['session']}/frame", content=JPEG,
                   headers={**JPEG_TYPE, "X-Live-Key": start["key"]}).json()
    assert frame["frames_seen"] == 1
    stop = c.post(f"/api/live/{start['session']}/stop", json={}).json()
    assert stop["ring_stop_error"] is None and calls[-1].method == "DELETE"
    assert c.get("/api/live/sessions").json()[0]["id"] == start["session"]
    assert c.post(f"/api/live/{start['session']}/frame", content=JPEG,
                  headers={**JPEG_TYPE, "X-Live-Key": start["key"]}).json()["error"] == "UNKNOWN_SESSION"


def test_live_view_needs_ring_connected(monkeypatch, tmp_path):
    from app.ring import RingSource

    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    c = TestClient(main.app)
    assert c.post("/api/live/start", content="v=0", headers={"Content-Type": "application/sdp"}).status_code == 409
    assert "Connect Ring" in c.get("/live").text


def test_demo_mode_never_links_to_live_and_live_says_one_thing(monkeypatch, tmp_path):
    """Round 3 audit: "Live door" was a dead end in the demo. Without Ring nothing links to /live, and /live
    itself is one sentence and a Connect button: no player, no empty table, no Details."""
    from app.ring import RingSource

    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    assert main.source.get().name == "synthetic"
    c = TestClient(main.app)
    for path in ("/", "/setup", "/connect", "/?attempt=14:30&claim=business_closed"):
        assert 'href="/live"' not in c.get(path).text, path
    page = c.get("/live").text
    main_part = page.split("<main", 1)[1].split("</main>", 1)[0]
    assert main_part.count("<p") == 1 and "needs Ring connected" in main_part
    assert 'class="primary" href="/connect">Connect Ring</a>' in main_part
    for gone in ("Start live view", "<video", "Recent live visits", "<details"):
        assert gone not in main_part


def test_session_without_video_is_logged_but_not_a_visit(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    assert log.stop(s.id)["frames_seen"] == 0
    assert log.visits_for_day(datetime.now(TZ).date()) == []


# ---- whose frames, how long they live -------------------------------------------------------------------
from app.live import BROWSER_FRAME, SESSION_TTL, frame_provenance  # noqa: E402


def test_frames_need_the_session_key_from_start(monkeypatch, tmp_path):
    ring = ring_client(whep_handler([]))
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring})())
    monkeypatch.setattr(main, "live", LiveLog(tmp_path, TZ))
    c = TestClient(main.app)
    start = c.post("/api/live/start", content="v=0\r\no=- offer\r\n", headers={"Content-Type": "application/sdp"}).json()
    sid, key = start["session"], start["key"]
    assert len(key) >= 16
    for headers in (JPEG_TYPE, {**JPEG_TYPE, "X-Live-Key": "guess"}):
        r = c.post(f"/api/live/{sid}/frame", content=JPEG, headers=headers)
        assert r.status_code == 403 and r.json()["error"] == "BAD_KEY"
    assert c.post(f"/api/live/{sid}/frame", content=JPEG, headers={**JPEG_TYPE, "X-Live-Key": key}).json()["frames_seen"] == 1


def test_browser_frames_say_where_they_came_from(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    log.frame(s.id, JPEG, RulesClassifier(), None, key=s.key)
    row = log.stop(s.id)
    assert row["frame_provenance"] == BROWSER_FRAME == "captured in the owner's browser from a Ring live-view session"
    [v] = log.visits_for_day(datetime.now(TZ).date())
    assert v.event.source == "ring-live-browser"
    assert frame_provenance(s.id) == BROWSER_FRAME and frame_provenance("evt-123") is None


def test_frames_are_private(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    log.frame(s.id, JPEG, RulesClassifier(), None, key=s.key)
    files = list((tmp_path / "live-frames").glob("*.jpg"))
    assert files and all(oct(f.stat().st_mode & 0o777) == "0o600" for f in files)
    assert oct((tmp_path / "live-frames").stat().st_mode & 0o777) == "0o700"


def test_abandoned_sessions_expire(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    log.frame(s.id, JPEG, RulesClassifier(), None, key=s.key)
    s.started = s.started - SESSION_TTL - SESSION_TTL / 10
    with pytest.raises(LiveError) as err:
        log.frame(s.id, JPEG, RulesClassifier(), None, key=s.key)
    assert err.value.code == "UNKNOWN_SESSION" and s.id not in log.active
    [row] = [r for r in log.rows() if r["id"] == s.id]
    assert row["expired"] is True and row["frames_seen"] == 1  # still a visit; it just wasn't stopped


def test_a_truncated_session_log_line_is_skipped(tmp_path):
    log = LiveLog(tmp_path, TZ)
    s = log.start(DEV, SESSION_URL)
    log.stop(s.id)
    with (tmp_path / "live-sessions.jsonl").open("a") as f:
        f.write('{"id": "live-cut')
    assert [r["id"] for r in LiveLog(tmp_path, TZ).rows()] == [s.id]
    assert LiveLog(tmp_path, TZ).visits_for_day(datetime.now(TZ).date()) == []
