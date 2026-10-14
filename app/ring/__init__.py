from dataclasses import replace
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


class RingSource:
    """The Ring client for the current request.

    A Playground token pasted on /connect (saved to `token_file`) wins over .env and switches the
    app to live mode until it is removed. The client is rebuilt only when the token changes.
    """

    def __init__(self, settings: Settings, token_file: Path):
        self.settings = settings
        self.token_file = token_file
        self._key: tuple | None = None
        self._client: RingClient | None = None

    def pasted_token(self) -> str:
        return self.token_file.read_text().strip() if self.token_file.exists() else ""

    def save_token(self, token: str) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token.strip())
        self.token_file.chmod(0o600)

    def forget_token(self) -> None:
        self.token_file.unlink(missing_ok=True)

    def effective_settings(self) -> Settings:
        pasted = self.pasted_token()
        if pasted:
            return replace(self.settings, ring_mode="live", ring_access_token=pasted, ring_refresh_token="")
        return self.settings

    def get(self) -> RingClient:
        s = self.effective_settings()
        key = (s.ring_mode, s.ring_access_token, s.ring_refresh_token)
        if key != self._key or self._client is None:
            self._client, self._key = None, None
            self._client = make_ring_client(s)  # may raise RingError(NOT_CONFIGURED)
            self._key = key
        return self._client
