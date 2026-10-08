"""Ring Partner API client (OAuth 2.0). Skeleton until sandbox credentials are available.

TODO once the Ring developer account / sandbox is set up:
  * confirm the token URL, grant type and scopes from the Ring Partner API docs
  * confirm the event-history and snapshot endpoint paths and response shapes
  * map the response fields into DoorEvent in `_to_event`
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.models import DoorEvent
from app.ring.base import RingClient


class PartnerRingClient(RingClient):
    name = "partner"

    def __init__(self, *, api_base: str, token_url: str, client_id: str, client_secret: str,
                 device_id: str, tz: ZoneInfo):
        if not (api_base and token_url and client_id and client_secret and device_id):
            raise ValueError("Ring partner mode needs RING_API_BASE, RING_TOKEN_URL, "
                             "RING_CLIENT_ID, RING_CLIENT_SECRET and RING_DEVICE_ID")
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_id = device_id
        self.tz = tz
        self._token: str | None = None
        self._token_expiry = 0.0
        self._http = httpx.Client(timeout=20)

    def _access_token(self) -> str:
        if self._token and _time.time() < self._token_expiry - 60:
            return self._token
        resp = self._http.post(self.token_url, data={
            "grant_type": "client_credentials",  # TODO: confirm grant type with Ring docs
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = _time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, **params) -> httpx.Response:
        resp = self._http.get(f"{self.api_base}{path}", params=params,
                              headers={"Authorization": f"Bearer {self._access_token()}"})
        resp.raise_for_status()
        return resp

    def events_for_day(self, day: date) -> list[DoorEvent]:
        start = datetime.combine(day, time.min, tzinfo=self.tz)
        end = start + timedelta(days=1)
        # TODO: real endpoint path and query params from the Ring Partner API docs.
        body = self._get(f"/devices/{self.device_id}/events",
                         start=start.isoformat(), end=end.isoformat()).json()
        events = [self._to_event(item) for item in body.get("events", [])]
        return sorted(events, key=lambda e: e.timestamp)

    def _to_event(self, item: dict) -> DoorEvent:
        # TODO: field names are placeholders until we see a real sandbox payload.
        return DoorEvent(
            id=str(item["id"]),
            device_id=self.device_id,
            timestamp=datetime.fromisoformat(item["created_at"]).astimezone(self.tz),
            kind=item.get("kind", "motion"),
            answered=bool(item.get("answered", False)),
            snapshot_url=item.get("snapshot_url"),
        )
