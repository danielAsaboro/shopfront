"""Settings loaded from environment / .env. Every external dependency is optional."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    shop_name: str = _env("SHOP_NAME", "Corner Bakery")
    timezone: str = _env("SHOP_TIMEZONE", "America/Los_Angeles")

    # Ring Partner API. Leave RING_CLIENT_ID empty to use the mock client.
    ring_mode: str = _env("RING_MODE", "mock")  # mock | partner
    ring_api_base: str = _env("RING_API_BASE", "")
    ring_token_url: str = _env("RING_TOKEN_URL", "")
    ring_client_id: str = _env("RING_CLIENT_ID", "")
    ring_client_secret: str = _env("RING_CLIENT_SECRET", "")
    ring_device_id: str = _env("RING_DEVICE_ID", "")

    # Classifier: stub (no AWS) or bedrock.
    classifier: str = _env("CLASSIFIER", "auto")  # auto | stub | bedrock
    aws_region: str = _env("AWS_REGION", "us-east-1")
    bedrock_model_id: str = _env("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()
