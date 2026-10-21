"""Frames a Playground token can actually get: whole-second at_timestamp values, and nothing from before the
token was issued. Local only (MockTransport). The two traps are not in the API reference; another Playground
integration logged them: a fractional timestamp answered 403 REQUEST_FORBIDDEN ("invalid request body"), and a
time before the token's issue answered 403 TIME_RANGE_NOT_AUTHORIZED."""

import base64
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.briefing import no_ring_frame
from app.config import Settings
from app.models import DoorEvent
from app.ring import RingSource
from app.ring.api import RingApiClient, RingAuth, RingError, RingStatus, jwt_issued_at, whole_second

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
ISSUED = datetime(2026, 10, 7, 14, 0, 0, 400_000, tzinfo=UTC).timestamp()  # 14:00:00.4 UTC


def ring(bodies, **auth):
    def handler(req: httpx.Request):
        if req.url.host != "api.amazonvision.com":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        bodies.append(json.loads(req.content))
        return httpx.Response(303, headers={"Location": "https://download.test/v1/download?x=1"})
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    a = RingAuth(**{"access_token": "t", "issued_at": ISSUED, **auth}, http=http)
    return RingApiClient(auth=a, tz=TZ, device_id=DEV, http=http)


def event(at: datetime, kind="motion", ends=None):
    return DoorEvent("e1", DEV, at.astimezone(TZ), kind, "ring", ends=ends)


def jwt(claims: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{part}.sig"


def test_at_timestamp_is_sent_in_whole_seconds():
    bodies: list = []
    at = datetime(2026, 10, 7, 14, 5, 12, 734_000, tzinfo=UTC)  # Ring's history start has milliseconds
    f = ring(bodies).frame(event(at))
    [b] = bodies
    assert b == {"type": "at_timestamp", "timestamp": int(datetime(2026, 10, 7, 14, 5, 13, tzinfo=UTC).timestamp()) * 1000}
    assert isinstance(b["timestamp"], int) and b["timestamp"] % 1000 == 0 and f.requested_ms == b["timestamp"]
    assert whole_second(1_791_396_000_499.9) == 1_791_396_000_000 and whole_second(1_791_396_000_500) == 1_791_396_001_000


def test_an_event_before_the_playground_token_is_never_asked_for():
    bodies: list = []
    early = event(datetime(2026, 10, 7, 13, 59, 30, tzinfo=UTC))
    with pytest.raises(RingError) as e:
        ring(bodies).frame(early)
    assert bodies == []  # no call: Ring would answer 403, which reads like a permissions problem
    assert e.value.status == RingStatus.BEFORE_TOKEN and e.value.http_status is None
    assert "after it was issued (07:00:01, when it was pasted)" in e.value.detail and "06:59:30" in e.value.detail
    assert no_ring_frame(e.value) == "no Ring frame: the event is from before the Playground token was issued"


def test_a_live_view_running_when_the_token_was_issued_is_asked_at_the_first_second_after():
    bodies: list = []
    start = datetime(2026, 10, 7, 13, 59, 50, tzinfo=UTC)
    ring(bodies).frame(event(start, "on_demand", ends=start + timedelta(seconds=40)))
    assert bodies[0]["timestamp"] == int(datetime(2026, 10, 7, 14, 0, 1, tzinfo=UTC).timestamp()) * 1000


def test_a_jwt_iat_is_the_issue_time_and_a_refresh_token_has_no_floor():
    iat = int(ISSUED) - 600
    a = RingAuth(access_token=jwt({"iat": iat, "exp": iat + 1800}), issued_at=ISSUED)
    assert a.issued_at == iat and a.issued_from == "the token's iat" and a.media_floor_ms() == iat * 1000
    assert jwt_issued_at("not-a-jwt") is None and jwt_issued_at(jwt({"iat": "x"})) is None
    assert jwt_issued_at(jwt({"iat": 4_102_444_800})) is None  # year 2100: not a real issue time
    loaded = RingAuth(access_token="opaque")
    assert loaded.issued_from == "when Shopfront loaded it" and loaded.media_floor_ms() > 0
    refresh = RingAuth(refresh_token="r", client_id="c", client_secret="s")
    assert refresh.media_floor_ms() == 0 and refresh.issued_at is None
    bodies: list = []
    long_ago = event(datetime(2026, 1, 1, tzinfo=UTC))
    ring(bodies, access_token="", refresh_token="r", client_id="c", client_secret="s",
         issued_at=None)._media_time(long_ago, 1_767_225_600_000)  # no floor, no refusal


def test_pasting_a_token_records_when_and_the_client_uses_it(tmp_path, monkeypatch):
    import app.ring as ring_mod
    monkeypatch.setattr(ring_mod.time, "time", lambda: ISSUED)
    src = RingSource(Settings(data_dir=str(tmp_path)), tmp_path / "ring-access-token")
    src.save_token("opaque-token")
    assert src.pasted_at() == ISSUED
    client = src.get()
    assert client.auth.issued_at == ISSUED and client.auth.issued_from == "when it was pasted"
    src.forget_token()
    assert not list(tmp_path.glob("ring-access-token*"))
