from app.config import Settings
from app.ring.base import RingClient
from app.ring.mock import MockRingClient
from app.ring.partner import PartnerRingClient


def make_ring_client(s: Settings) -> RingClient:
    if s.ring_mode == "partner":
        return PartnerRingClient(api_base=s.ring_api_base, token_url=s.ring_token_url,
                                 client_id=s.ring_client_id, client_secret=s.ring_client_secret,
                                 device_id=s.ring_device_id, tz=s.tz)
    return MockRingClient(tz=s.tz)
