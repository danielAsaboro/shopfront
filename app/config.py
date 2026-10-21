"""Settings loaded from environment / .env. Every external dependency is optional."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app.hours import DEFAULT as DEFAULT_HOURS

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def frame_days(raw: str) -> float:
    """SHOPFRONT_FRAME_DAYS: empty means 30 days; 0 keeps frames forever."""
    return float(raw) if raw else 30.0


@dataclass(frozen=True)
class Settings:
    shop_name: str = _env("SHOP_NAME", "Corner Bakery")
    timezone: str = _env("SHOP_TIMEZONE", "America/Los_Angeles")
    shop_hours: str = _env("SHOP_HOURS") or DEFAULT_HOURS

    # Ring: "synthetic" (default, generated demo data) or "live" (Ring Partner API).
    ring_mode: str = _env("RING_MODE", "synthetic")
    # Live auth, one of: a Developer Playground access token, or a refresh token + client credentials.
    ring_access_token: str = _env("RING_ACCESS_TOKEN")
    ring_refresh_token: str = _env("RING_REFRESH_TOKEN")
    ring_client_id: str = _env("RING_CLIENT_ID")
    ring_client_secret: str = _env("RING_CLIENT_SECRET")
    ring_device_id: str = _env("RING_DEVICE_ID")  # empty = first camera or doorbell the token can see
    # The shop owner's Ring Account ID (GET /v1/users/me data.id); empty = learned from the live token.
    # Webhooks from any other account are ignored.
    ring_account_id: str = _env("RING_ACCOUNT_ID")
    # API reference, Event History > Event types. on_demand (a live view) is kept, never counted as a visitor:
    # it is the only event a Developer Playground scene files, and the moment a Playground frame exists.
    ring_event_types: str = _env("RING_EVENT_TYPES", "ding,motion,on_demand")
    ring_hmac_key: str = _env("RING_HMAC_KEY")  # webhook signature key from partner onboarding

    # Classifier: auto (Bedrock if AWS credentials resolve, else rules) | rules | bedrock
    classifier: str = _env("CLASSIFIER", "auto")
    aws_region: str = _env("AWS_REGION", "us-east-1")
    bedrock_model_id: str = _env("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    data_dir: str = _env("SHOPFRONT_DATA_DIR", ".data")
    # Door frames (pictures of people) are deleted after this many days; 0 keeps them.
    frame_days: float = frame_days(_env("SHOPFRONT_FRAME_DAYS"))
    # Extra host names this app may be reached by (e.g. "shop-mac.local"), comma-separated. localhost and
    # IP addresses always work; anything else is refused, which stops DNS-rebinding pages.
    allowed_hosts: str = _env("SHOPFRONT_ALLOWED_HOSTS")

    # Ring webhooks: "file" (.data/ring-webhooks.jsonl, the app's own POST /webhooks/ring) or "dynamodb" (the
    # table the webhook Lambda in infra/template.yaml writes; docs/aws.md). SHOPFRONT_SHOP_ID matches the stack's.
    webhook_store: str = _env("SHOPFRONT_WEBHOOK_STORE") or "file"
    webhook_table: str = _env("SHOPFRONT_WEBHOOK_TABLE")
    webhook_region: str = _env("SHOPFRONT_WEBHOOK_REGION")  # empty = AWS_REGION
    shop_id: str = _env("SHOPFRONT_SHOP_ID", "shop")
    # Live Ring only: ask GET /v1/devices/{id}/status this often (minutes) for camera coverage
    # (app/ring/status.py). 0 turns it off.
    status_minutes: float = float(_env("SHOPFRONT_STATUS_MINUTES") or 5)

    # The hosted read-only demo (app/public_demo.py, infra/template.yaml): made-up shop, nobody is the owner.
    public_demo: bool = _env("SHOPFRONT_PUBLIC_DEMO") == "1"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()
