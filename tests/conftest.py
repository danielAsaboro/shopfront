import pytest

import app.main as main
from app.cache import ClassificationCache
from app.deliveries import DeliveryBook, FrameStore
from app.live import LiveLog
from app.ring import RingSource
from app.ring.webhooks import WebhookLog
from app.shop import ShopProfile


@pytest.fixture(autouse=True)
def no_local_data(tmp_path, monkeypatch):
    """Keep every test off this machine's .data: a pasted Ring token there switches the app to live
    Ring, and a saved profile, delivery list or webhook log would change what the tests see."""
    d = tmp_path / "data"
    monkeypatch.setattr(main, "source", RingSource(main.settings, d / "ring-access-token"))
    monkeypatch.setattr(main, "profile", ShopProfile(d / "shop.json", main.settings.shop_name, main.settings.shop_hours))
    monkeypatch.setattr(main, "book", DeliveryBook(d / "expected-deliveries.json"))
    monkeypatch.setattr(main, "frames", FrameStore(d))
    monkeypatch.setattr(main, "cache", ClassificationCache(d / "classifications.json"))
    monkeypatch.setattr(main, "webhook_log", WebhookLog(d / "ring-webhooks.jsonl"))
    monkeypatch.setattr(main, "live", LiveLog(d, main.settings.tz))
