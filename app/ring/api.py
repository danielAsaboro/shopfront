"""Ring Partner API client (https://developer.amazon.com/docs/ring/api-documentation.html).

Calls, all against https://api.amazonvision.com:
  GET  /v1/devices?include=capabilities              find the doorbell when RING_DEVICE_ID is empty
  GET  /v1/users/me                                  the Ring Account ID, to match webhooks' meta.account_id
  GET  /v1/history/devices/{id}/events               the day's dings, motion and live views (on_demand), paged
                                                     newest first, and again with event_types=
                                                     motion.package_delivery to learn which motion events were
                                                     Ring's courier alerts
  POST /v1/devices/{id}/media/image/download         the frame at an event's time (303 -> pre-signed GET)
  POST /v1/devices/{id}/media/streaming/whep/sessions live view (the Developer Playground's simulator), + DELETE
Tokens come from POST https://oauth.ring.com/oauth/token (refresh_token grant), or are a
Developer Playground access token used as-is.

Every failure raises RingError with a named RingStatus. Nothing here falls back to synthetic data.

Calls are kept down (API reference, Rate Limiting: limits are per partner client_id; "Cache device
information when appropriate"): a past day's Event History doesn't change, so it is fetched once per
client; today's is reused for `TODAY_TTL`. A frame Ring said is corrupt (422), doesn't exist (416, once the
event is older than Ring's 2-minute media window) or is forbidden (403) isn't asked for again: a 403 for
that one time only, unless Ring's error code says the client isn't authorised for the device at all
(`REQUEST_FORBIDDEN`), and then for every frame of that device. A 429 is RATE_LIMITED with Ring's
Retry-After; when that is a second or two, the call waits and tries once more ("respect the Retry-After
header").
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time as _time
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.files import write_private
from app.models import DoorEvent
from app.ring.base import RingClient

log = logging.getLogger(__name__)

API_BASE = "https://api.amazonvision.com"
TOKEN_URL = "https://oauth.ring.com/oauth/token"
MAX_PAGES = 50
MAX_CACHED_DAYS = 120
TODAY_TTL = 60.0  # seconds today's Event History is reused for
MAX_RETRY_WAIT = 2  # seconds: a longer Retry-After is reported, not waited out in a page load
REMEMBERED_FRAME_ERRORS = {403, 416, 422}
TAGGED_SUBTYPES = ("package_delivery",)  # motion subtypes asked for by composite filter, for stage 0
# API reference, Event History > Event types: motion, on_demand ("User or partner initiated a live view
# session") and ding. A Developer Playground scene files an on_demand event, never motion or ding.
KINDS = ("ding", "motion", "on_demand")
# API reference, Media Clips > Download video after a webhook event: 416 MEDIA_NOT_FOUND and 425
# RECORDING_NOT_READY on a recent event mean "not processed yet": retry the unchanged request with capped
# backoff and jitter (5, 10, 20, 30 s), and stop 2 minutes after the requested time.
MEDIA_RETRY_STATUS = (416, 425)
MEDIA_RETRY_SCHEDULE = (5, 10, 20, 30)
MEDIA_WINDOW = 120.0  # seconds after the requested time that a 416 can still turn into a frame
# API reference, Image Snapshots > Error Codes: the one documented 403 code, "Client is not authorized for the
# device" (No access to device). Any other 403 (no code, or one the reference doesn't list, such as the
# TIME_RANGE_NOT_AUTHORIZED other Playground integrations logged) refuses that request, not the token.
DEVICE_FORBIDDEN_CODE = "REQUEST_FORBIDDEN"
ON_DEMAND_OFFSET_MS = 5_000  # a live view's frame is asked for this far into the session


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
                 request_id: str | None = None, retry_after: int | None = None, code: str | None = None):
        super().__init__(f"{status.value}: {detail}" if detail else status.value)
        self.status = status
        self.detail = detail
        self.http_status = http_status
        self.request_id = request_id
        self.retry_after = retry_after  # seconds, from Ring's Retry-After on a 429
        self.code = code  # Ring's own error code (errors[0].code), e.g. REQUEST_FORBIDDEN, when it sent one


def error_code(resp: httpx.Response) -> str | None:
    """`errors[0].code` from Ring's error body (API reference, Error Handling and Image Snapshots >
    Error Handling: {"errors": [{"status", "code", "detail", ...}]}), or None."""
    try:
        errs = resp.json().get("errors")
        code = errs[0].get("code") if isinstance(errs, list) and errs and isinstance(errs[0], dict) else None
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and code else None


@dataclass
class Frame:
    """One downloaded image and what Ring said about it (API reference, Image Snapshots > Image Response
    Headers)."""

    content: bytes
    content_type: str
    requested_ms: int
    media_timestamp: str | None  # X-Media-Timestamp: when the frame was captured (epoch ms)
    media_origin: str | None  # X-Media-Origin: "recording" or "snapshot"
    request_id: str | None
    attempts: int


def retry_after_seconds(value: str | None) -> int | None:
    """Retry-After as seconds: either delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0, round(when.timestamp() - _time.time()))
    except (TypeError, ValueError):
        return None


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
        if self._file:  # created 0600 and swapped in whole: never readable by others, never half-written
            write_private(self._file, json.dumps({"refresh_token": self._refresh}))


class RingApiClient(RingClient):
    name = "ring"

    def __init__(self, *, auth: RingAuth, tz: ZoneInfo, device_id: str = "",
                 event_types: str = "ding,motion,on_demand", http: httpx.Client | None = None):
        self.auth = auth
        self.tz = tz
        self._device_id = device_id
        self.event_types = event_types
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)
        self.last_request_id: str | None = None
        self._account_id: str | None = None
        self._account_asked = False
        self._history: dict[tuple, tuple[float, list[DoorEvent]]] = {}  # (day, event_types) -> (fetched, events)
        self._no_frame: dict[tuple, RingError] = {}  # (device, ms or "*") -> the error Ring gave for that frame
        self._refused_filters: set[str] = set()  # composite subtypes Ring answered 400 to
        self._lock = threading.Lock()
        self.sleep = _time.sleep  # tests swap these three for a fake clock
        self.clock = _time.time
        self.jitter = random.random

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        extra = kw.pop("headers", {})
        refreshed = waited = False
        while True:
            headers = {"Authorization": f"Bearer {self.auth.token(force_refresh=refreshed)}", **extra}
            try:
                resp = self._http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise RingError(RingStatus.NETWORK_ERROR, f"{method} {path}: {exc}") from exc
            self.last_request_id = resp.headers.get("X-Request-ID") or self.last_request_id
            # One retry after a refresh, only when we hold a refresh token.
            if resp.status_code == 401 and not refreshed and self.auth.mode == "refresh":
                refreshed = True
                continue
            wait = retry_after_seconds(resp.headers.get("Retry-After"))
            if resp.status_code == 429 and not waited and wait is not None and wait <= MAX_RETRY_WAIT:
                waited = True
                self.sleep(wait or 1)
                continue
            break
        if resp.status_code >= 400:
            raise RingError(status_for_http(resp.status_code), f"{method} {path}: {resp.text[:200]}",
                            resp.status_code, resp.headers.get("X-Request-ID"),
                            retry_after_seconds(resp.headers.get("Retry-After")) if resp.status_code == 429 else None,
                            error_code(resp))
        return resp

    def _json(self, resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError as exc:
            raise RingError(RingStatus.MALFORMED_RESPONSE, str(resp.url)) from exc

    def list_devices(self) -> list[dict]:
        return self._json(self._request("GET", "/v1/devices")).get("data", [])

    def online(self, device_id: str | None = None) -> bool:
        """Whether the doorbell is online right now: GET /v1/devices/{id}/status, `data.attributes.online`
        (API reference, Device Discovery > Status: "Whether the device is currently online"). Raises RingError;
        a body without a boolean `online` is MALFORMED_RESPONSE, never a guess."""
        body = self._json(self._request("GET", f"/v1/devices/{device_id or self.device_id}/status"))
        value = ((body.get("data") or {}).get("attributes") or {}).get("online") if isinstance(body, dict) else None
        if not isinstance(value, bool):
            raise RingError(RingStatus.MALFORMED_RESPONSE, "device status without a boolean `online`")
        return value

    def _cameras(self) -> list[str]:
        """IDs of devices with a camera, in Ring's order.

        API reference, Device Discovery: GET /v1/devices returns sensors and chimes alongside cameras and
        doorbells ("Filter your device collection before calling a media endpoint"); the device resource
        has no type field, and ?include=capabilities side-loads each device's capabilities into `included`,
        matched by the type and id in its relationships. A chime's `video` capability is always null, and no
        sensor has a camera, so a camera is a device whose capabilities carry a `video` object.
        """
        body = self._json(self._request("GET", "/v1/devices", params={"include": "capabilities"}))
        devices = body.get("data") or []
        if not devices:
            raise RingError(RingStatus.NO_DEVICES, "the token can't see any Ring devices")
        caps = {(x.get("type"), x.get("id")): x.get("attributes") or {} for x in body.get("included") or []}
        if not caps:  # capabilities weren't side-loaded: keep Ring's order, as before
            return [d["id"] for d in devices]
        out = []
        for d in devices:
            ref = ((d.get("relationships") or {}).get("capabilities") or {}).get("data") or {}
            if (caps.get((ref.get("type"), ref.get("id"))) or {}).get("video"):
                out.append(d["id"])
        if not out:
            raise RingError(RingStatus.NO_DEVICES, "the token sees Ring devices, but no camera or doorbell")
        return out

    @property
    def device_id(self) -> str:
        if not self._device_id:
            self._device_id = self._cameras()[0]
        return self._device_id

    def account_id(self) -> str | None:
        """The Ring user's Account ID (API reference, Users API: GET /v1/users/me, data.id), which webhooks
        carry as meta.account_id. None when Ring won't say; asked once per client."""
        if not self._account_asked:
            self._account_asked = True
            try:
                data = self._json(self._request("GET", "/v1/users/me")).get("data")
                self._account_id = data.get("id") if isinstance(data, dict) and data.get("type") == "users" else None
            except (RingError, AttributeError):
                self._account_id = None
        return self._account_id

    def events_for_day(self, day: date) -> list[DoorEvent]:
        key = (day, self.event_types)
        today = datetime.now(self.tz).date()
        with self._lock:
            hit = self._history.get(key)
        if hit and (day < today or _time.monotonic() - hit[0] < TODAY_TTL):
            return [replace(e) for e in hit[1]]  # copies: callers attach and drop frames on them
        events = self._fetch_day(day)
        if day <= today:
            with self._lock:
                if len(self._history) >= MAX_CACHED_DAYS:
                    self._history.pop(next(iter(self._history)))
                self._history[key] = (_time.monotonic(), events)
        return [replace(e) for e in events]

    def _fetch_day(self, day: date) -> list[DoorEvent]:
        """The day's events, each motion event carrying the Smart Alert subtype Ring filtered it by.

        API reference, Event History > Event type filtering: an Event History item has no sub_type field,
        but `event_types` takes dot-delimited composite values ("motion.vehicle filters by event type AND
        detection subtype", combined with OR). So the subtype is read from the filter that returned the
        event: when the only motion filter is one composite value, every motion event has that subtype;
        otherwise motion events are asked for again as `motion.<subtype>` for each subtype stage 0 acts on
        (`TAGGED_SUBTYPES`), and the events in that answer, by Ring's event ID, get the subtype.
        `package_delivery` is a detection subtype in the API reference (Notifications > Motion Detection,
        Smart Alerts enabled); the Event History table lists human, vehicle, animal and other_motion.
        If Ring refuses a composite filter (400, "malformed query parameters"), it isn't asked again and
        those events go to the classifier untagged, as with Smart Alerts off.
        """
        events = [e for e in self._fetch_filtered(day, self.event_types) if e.kind in KINDS]
        motion = [f.strip() for f in (self.event_types or "ding,motion").split(",") if f.strip().startswith("motion")]
        if len(motion) == 1 and "." in motion[0]:
            only = motion[0].split(".", 1)[1]
            return [replace(e, ring_subtype=only) if e.kind == "motion" else e for e in events]
        if not any(e.kind == "motion" for e in events):
            return events
        for sub in TAGGED_SUBTYPES:
            if sub in self._refused_filters:
                continue
            try:
                ids = {e.id for e in self._fetch_filtered(day, f"motion.{sub}")}
            except RingError as exc:
                if exc.http_status != 400:
                    raise
                log.warning("Ring Event History refused event_types=motion.%s; those events go untagged", sub)
                self._refused_filters.add(sub)
                continue
            events = [replace(e, ring_subtype=sub) if e.kind == "motion" and e.id in ids and not e.ring_subtype
                      else e for e in events]
        return events

    def _fetch_filtered(self, day: date, event_types: str) -> list[DoorEvent]:
        start = datetime.combine(day, time.min, tzinfo=self.tz)
        end = start + timedelta(days=1)
        return self._fetch_range(int(start.timestamp() * 1000), int(end.timestamp() * 1000), event_types)

    def recent_events(self, since: datetime) -> list[DoorEvent]:
        """Every event from `since` to now, oldest first, of any kind, never cached: for watching Event History
        for something new (scripts/ring_proof.py waits on it for the Playground's live-view event)."""
        now_ms = int(self.clock() * 1000) + 1
        return self._fetch_range(int(since.timestamp() * 1000), now_ms, self.event_types)

    def _fetch_range(self, start_ms: int, end_ms: int, event_types: str) -> list[DoorEvent]:
        """Events with start_ms <= start < end_ms, oldest first, every event_type Ring returned."""
        path: str | None = f"/v1/history/devices/{self.device_id}/events"
        params: dict | None = {"event_types": event_types} if event_types else None
        events: list[DoorEvent] = []
        for _ in range(MAX_PAGES):
            if path is None:
                break
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
                if ms >= end_ms:
                    continue
                try:
                    ends = datetime.fromtimestamp(int(attrs["end"]) / 1000, tz=self.tz) if attrs.get("end") else None
                except (TypeError, ValueError, OverflowError, OSError):
                    ends = None  # `end` only places a live view's frame; a bad one isn't worth failing the day
                events.append(DoorEvent(
                    id=str(item.get("id")), device_id=self.device_id,
                    timestamp=datetime.fromtimestamp(ms / 1000, tz=self.tz),
                    kind=kind, source="ring", ends=ends))
            nxt = (body.get("links") or {}).get("next")
            if older or not items or not nxt:
                break
            path, params = nxt, None  # links.next already carries the filters and cursor
        return sorted(events, key=lambda e: e.timestamp)

    def frame_ms(self, event: DoorEvent) -> int:
        """The time to ask Ring for an event's frame, epoch ms.

        A ding or motion event: its start, the moment the press or the motion was recorded. A live view
        (on_demand): `ON_DEMAND_OFFSET` into it, kept inside its `end`, because the first instant of a
        session can come before any video does (other Playground integrations ask a few seconds in)."""
        ms = int(event.timestamp.timestamp() * 1000)
        if event.kind == "on_demand":
            end = int(event.ends.timestamp() * 1000) if event.ends else None
            ms += ON_DEMAND_OFFSET_MS if end is None else max(0, min(ON_DEMAND_OFFSET_MS, end - ms))
        return ms

    def snapshot(self, event: DoorEvent, retry_for: float = 0.0) -> bytes:
        """JPEG frame for the event (see `frame`). Raises RingError (e.g. MEDIA_NOT_FOUND)."""
        return self.frame(event, retry_for=retry_for).content

    def frame(self, event: DoorEvent, retry_for: float = 0.0, at_ms: int | None = None) -> Frame:
        """The image Ring holds at `at_ms` (default `frame_ms(event)`), with Ring's media headers.

        API reference, Image Snapshots: POST {"type": "at_timestamp", "timestamp": ms}; `image_options` is
        optional (JPEG unless asked otherwise), so it isn't sent. Ring answers 303 to a pre-signed URL, which
        the client follows (httpx drops the Bearer header on that cross-origin hop).

        Media for a recent event can lag: Ring answers 416 or 425 until it has processed the recording. With
        `retry_for` > 0 the unchanged request is retried on those two, on Ring's schedule (5, 10, 20, 30 s
        plus jitter), until `retry_for` seconds have passed or 2 minutes after the requested time, whichever
        comes first. A page load passes 0 and never waits; a recent miss isn't remembered, so the next load
        asks again."""
        ms = self.frame_ms(event) if at_ms is None else at_ms
        known = self._no_frame.get((event.device_id, "*")) or self._no_frame.get((event.device_id, ms))
        if known:  # the token may not download this device's images at all, or Ring said no for this time
            raise known
        started, attempts = self.clock(), 0
        while True:
            attempts += 1
            try:
                resp = self._request("POST", f"/v1/devices/{event.device_id}/media/image/download",
                                     json={"type": "at_timestamp", "timestamp": ms})
                break
            except RingError as exc:
                now = self.clock()
                recent = now - ms / 1000 <= MEDIA_WINDOW
                if exc.http_status in MEDIA_RETRY_STATUS and recent:
                    step = MEDIA_RETRY_SCHEDULE[min(attempts - 1, len(MEDIA_RETRY_SCHEDULE) - 1)]
                    delay = step * (1 + 0.1 * self.jitter())
                    stop = min(started + retry_for, ms / 1000 + MEDIA_WINDOW)
                    if now + delay <= stop:
                        log.info("Ring frame not ready (%s); asking again in %.0f s", exc.status.value, delay)
                        self.sleep(delay)
                        continue
                    raise  # not ready yet; not remembered, so a later call asks again
                if exc.http_status in REMEMBERED_FRAME_ERRORS:
                    whole_device = exc.http_status == 403 and exc.code == DEVICE_FORBIDDEN_CODE
                    if whole_device:
                        log.warning("Ring: this token isn't authorised for device frames (%s); not asking again",
                                    exc.code)
                    self._no_frame[(event.device_id, "*" if whole_device else ms)] = exc
                raise
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise RingError(RingStatus.MALFORMED_RESPONSE, f"snapshot content-type {ctype or 'missing'}")
        return Frame(content=resp.content, content_type=ctype, requested_ms=ms,
                     media_timestamp=resp.headers.get("X-Media-Timestamp"),
                     media_origin=resp.headers.get("X-Media-Origin"),
                     request_id=resp.headers.get("X-Request-ID"), attempts=attempts)

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
