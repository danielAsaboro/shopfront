import pytest

import app.main as main
from app.ring.webhooks import WebhookLog


@pytest.fixture(autouse=True)
def no_local_webhooks(tmp_path, monkeypatch):
    """Keep tests off any webhook log on this machine: those events now feed the door timeline."""
    monkeypatch.setattr(main, "webhook_log", WebhookLog(tmp_path / "ring-webhooks.jsonl"))
