"""Ring Partner API client (https://developer.amazon.com/docs/ring/api-documentation.html).

Calls, all against https://api.amazonvision.com:
  GET  /v1/devices                                   find the doorbell when RING_DEVICE_ID is empty
  GET  /v1/history/devices/{id}/events               the day's dings and motion events (paged, newest first)
  POST /v1/devices/{id}/media/image/download         the frame at an event's start time (303 -> pre-signed GET)
  POST /v1/devices/{id}/media/streaming/whep/sessions live view (the Developer Playground's simulator), + DELETE
Tokens come from POST https://oauth.ring.com/oauth/token (refresh_token grant), or are a
Developer Playground access token used as-is.

Every failure raises RingError with a named RingStatus. Nothing here falls back to synthetic data.
"""

from __future__ import annotations

import json
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.models import DoorEvent
from app.ring.base import RingClient

log = logging.getLogger(__name__)

API_BASE = "https://api.amazonvision.com"
TOKEN_URL = "https://oauth.ring.com/oauth/token"
MAX_PAGES = 50


class RingStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"        # no token in config
    TOKEN_REJECTED = "TOKEN_REJECTED"        # 401: expired or invalid access token
    REFRESH_FAILED = "REFRESH_FAILED"        # oauth.ring.com refused the refresh token
    FORBIDDEN = "FORBIDDEN"                  # 403: token not authorised for this device
    NO_DEVICES = "NO_DEVICES"                # token sees no devices
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"    # 404
    RATE_LIMITED = "RATE_LIMITED"            # 429
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"      # 416: nothing recorded at that time
    RECORDING_NOT_READY = "RECORDING_NOT_READY"  # 425
    CORRUPT_RECORDING = "CORRUPT_RECORDING"  # 422
    RING_UNAVAILABLE = "RING_UNAVAILABLE"    # 5xx, including 503 device offline / server busy
    NETWORK_ERROR = "NETWORK_ERROR"          # no response at all (DNS, TLS, timeout)
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"  # 2xx with a body we can't read
    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"  # any other HTTP status


_BY_HTTP = {401: RingStatus.TOKEN_REJECTED, 403: RingStatus.FORBIDDEN, 404: RingStatus.DEVICE_NOT_FOUND,
            416: RingStatus.MEDIA_NOT_FOUND, 422: RingStatus.CORRUPT_RECORDING,
            425: RingStatus.RECORDING_NOT_READY, 429: RingStatus.RATE_LIMITED}


class RingError(Exception):
    def __init__(self, status: RingStatus, detail: str = "", http_status: int | None = None,
                 request_id: str | None = None):
        super().__init__(f"{status.value}: {detail}" if detail else status.value)
        self.status = status
        self.detail = detail
        self.http_status = http_status
        self.request_id = request_id


def status_for_http(code: int) -> RingStatus:
    if code in _BY_HTTP:
        return _BY_HTTP[code]
    return RingStatus.RING_UNAVAILABLE if code >= 500 else RingStatus.UNEXPECTED_STATUS


class RingAuth:
    """Access token for Ring API calls.

    Playground mode: a fixed access token (about 30 minutes), never refreshed.
    Refresh mode: refresh token + client credentials. Ring rotates the refresh token on every
    refresh, so the newest one is saved to `token_file` and preferred over the configured one.
    """

    def __init__(self, *, access_token: str = "", refresh_token: str = "", client_id: str = "",
                 client_secret: str = "", token_file: Path | None = None, http: httpx.Client | None = None):
        if access_token and refresh_token:
            raise RingError(RingStatus.NOT_CONFIGURED, "set RING_ACCESS_TOKEN or RING_REFRESH_TOKEN, not both")
        if refresh_token and not (client_id and client_secret):
            raise RingError(RingStatus.NOT_CONFIGURED, "RING_REFRESH_TOKEN needs RING_CLIENT_ID and RING_CLIENT_SECRET")
        if not (access_token or refresh_token):
            raise RingError(RingStatus.NOT_CONFIGURED, "set RING_ACCESS_TOKEN or RING_REFRESH_TOKEN")
        self.mode = "playground" if access_token else "refresh"
        self._access = access_token
        self._expiry = float("inf") if access_token else 0.0
        self._refresh = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._file = token_file
        self._http = http or httpx.Client(timeout=20)
        if self.mode == "refresh" and token_file and token_file.exists():
            self._refresh = json.loads(token_file.read_text()).get("refresh_token") or refresh_token

    def token(self, force_refresh: bool = False) -> str:
        if self.mode == "playground":
            return self._access
        if force_refresh or _time.time() > self._expiry - 60:
            self._do_refresh()
        return self._access

    def _do_refresh(self) -> None:
        try:
            resp = self._http.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": self._refresh,
                "client_id": self._client_id, "client_secret": self._client_secret})
        except httpx.HTTPError as exc:
            raise RingError(RingStatus.NETWORK_ERROR, f"token refresh: {exc}") from exc
        if resp.status_code != 200:
            raise RingError(RingStatus.REFRESH_FAILED, resp.text[:200], resp.status_code)
        try:
            body = resp.json()
            self._access = body["access_token"]
            self._refresh = body.get("refresh_token") or self._refresh
            self._expiry = _time.time() + int(body.get("expires_in", 14400))
        except (ValueError, KeyError) as exc:
            raise RingError(RingStatus.MALFORMED_RESPONSE, "token response") from exc
        if self._file:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"refresh_token": self._refresh}))
            self._file.chmod(0o600)


class RingApiClient(RingClient):
    name = "ring"

    def __init__(self, *, auth: RingAuth, tz: ZoneInfo, device_id: str = "",
                 event_types: str = "ding,motion", http: httpx.Client | None = None):
        self.auth = auth
        self.tz = tz
        self._device_id = device_id
        self.event_types = event_types
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)
        self.last_request_id: str | None = None

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        extra = kw.pop("headers", {})
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.auth.token(force_refresh=attempt == 1)}", **extra}
            try:
                resp = self._http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise RingError(RingStatus.NETWORK_ERROR, f"{method} {path}: {exc}") from exc
            self.last_request_id = resp.headers.get("X-Request-ID") or self.last_request_id
            # One retry after a refresh, only when we hold a refresh token.
            if resp.status_code == 401 and attempt == 0 and self.auth.mode == "refresh":
                continue
            break
        if resp.status_code >= 400:
            raise RingError(status_for_http(resp.status_code), f"{method} {path}: {resp.text[:200]}",
                            resp.status_code, resp.headers.get("X-Request-ID"))
        return resp

    def _json(self, resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError as exc:
            raise RingError(RingStatus.MALFORMED_RESPONSE, str(resp.url)) from exc

    def list_devices(self) -> list[dict]:
        return self._json(self._request("GET", "/v1/devices")).get("data", [])

    @property
    def device_id(self) -> str:
        if not self._device_id:
            devices = self.list_devices()
            if not devices:
                raise RingError(RingStatus.NO_DEVICES, "the token can't see any Ring devices")
            self._device_id = devices[0]["id"]
        return self._device_id

    def events_for_day(self, day: date) -> list[DoorEvent]:
        start = datetime.combine(day, time.min, tzinfo=self.tz)
        end = start + timedelta(days=1)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        path: str | None = f"/v1/history/devices/{self.device_id}/events"
        params: dict | None = {"event_types": self.event_types} if self.event_types else None
        events: list[DoorEvent] = []
        for _ in range(MAX_PAGES):
            body = self._json(self._request("GET", path, params=params))
            items = body.get("data", [])
            older = False
            for item in items:
                try:
                    attrs = item["attributes"]
                    ms = int(attrs["start"])
                    kind = attrs["event_type"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise RingError(RingStatus.MALFORMED_RESPONSE, f"history item {item!r:.120}") from exc
                if ms < start_ms:
                    older = True  # newest first, so everything after this is older too
                    break
                if ms >= end_ms or kind not in ("ding", "motion"):
                    continue
                events.append(DoorEvent(
                    id=str(item.get("id")), device_id=self.device_id,
                    timestamp=datetime.fromtimestamp(ms / 1000, tz=self.tz),
                    kind=kind, source="ring"))
            nxt = (body.get("links") or {}).get("next")
            if older or not items or not nxt:
                break
            path, params = nxt, None  # links.next already carries the filters and cursor
        return sorted(events, key=lambda e: e.timestamp)

    def snapshot(self, event: DoorEvent) -> bytes:
        """JPEG frame at the event's start time. Raises RingError (e.g. MEDIA_NOT_FOUND)."""
        resp = self._request("POST", f"/v1/devices/{event.device_id}/media/image/download",
                             json={"type": "at_timestamp", "timestamp": int(event.timestamp.timestamp() * 1000),
                                   "image_options": {"format": "jpeg"}})
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise RingError(RingStatus.MALFORMED_RESPONSE, f"snapshot content-type {resp.headers.get('content-type')}")
        return resp.content

    # Live view (WHEP). The browser builds the SDP offer; the Bearer token stays on the server.
    def whep_start(self, sdp_offer: str) -> tuple[str, str]:
        """POST /v1/devices/{id}/media/streaming/whep/sessions → (SDP answer, session URL)."""
        resp = self._request("POST", f"/v1/devices/{self.device_id}/media/streaming/whep/sessions",
                             content=sdp_offer.encode(),
                             headers={"Content-Type": "application/sdp"})
        location = resp.headers.get("Location")
        if resp.status_code != 201 or not location or "v=0" not in resp.text:
            raise RingError(RingStatus.MALFORMED_RESPONSE,
                            f"WHEP answer: HTTP {resp.status_code}, Location {'set' if location else 'missing'}")
        return resp.text, location

    def whep_stop(self, session_url: str) -> None:
        """DELETE the WHEP session. A 404 means Ring already ended it, which is fine."""
        try:
            self._request("DELETE", session_url)
        except RingError as exc:
            if exc.status != RingStatus.DEVICE_NOT_FOUND:
                raise
