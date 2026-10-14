from pathlib import Path

from app.config import Settings
from app.hours import parse_hours
from app.ring.api import RingApiClient, RingAuth
from app.ring.base import RingClient
from app.ring.synthetic import SyntheticRingClient


def make_ring_client(s: Settings) -> RingClient:
    if s.ring_mode == "live":
        auth = RingAuth(access_token=s.ring_access_token, refresh_token=s.ring_refresh_token,
                        client_id=s.ring_client_id, client_secret=s.ring_client_secret,
                        token_file=Path(s.data_dir) / "ring-refresh-token.json")
        return RingApiClient(auth=auth, tz=s.tz, device_id=s.ring_device_id, event_types=s.ring_event_types)
    if s.ring_mode != "synthetic":
        raise ValueError(f"RING_MODE must be 'synthetic' or 'live', got {s.ring_mode!r}")
    return SyntheticRingClient(tz=s.tz, hours=parse_hours(s.shop_hours))
