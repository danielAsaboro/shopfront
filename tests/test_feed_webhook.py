"""scripts/feed_webhook.py: the demo's replay feeder."""

from datetime import datetime

import httpx
import pytest

from app.config import settings
from app.ring.webhooks import WebhookLog, WebhookStatus
from scripts import feed_webhook as fw


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("RING_HMAC_KEY", "demo-key")
    return "demo-key"


def test_bad_at_is_a_clean_usage_error(key, capsys):
    with pytest.raises(SystemExit) as e:
        fw.main(["--at", "yesterday-ish"])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "use now, HH:MM" in err and "Traceback" not in err


def test_future_at_is_refused(key, capsys):
    with pytest.raises(SystemExit):
        fw.main(["--at", "2999-01-01T10:00"])
    assert "hasn't happened yet" in capsys.readouterr().err


def test_port_and_url_flags_and_readme_default(key, tmp_path, capsys):
    log = WebhookLog(tmp_path / "w.jsonl")
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        status = log.handle(req.content, req.headers.get("X-Signature"), key)
        return httpx.Response(200 if status == WebhookStatus.ACCEPTED else 401, json={"status": status.value})

    t = httpx.MockTransport(handler)
    assert fw.main(["--sub-type", "package_delivery"], transport=t) == 0
    assert fw.main(["--port", "8203", "--at", "00:01"], transport=t) == 0
    assert fw.main(["--url", "https://shop.example/webhooks/ring", "--type", "button_press"], transport=t) == 0
    assert seen == ["http://127.0.0.1:8000/webhooks/ring", "http://127.0.0.1:8203/webhooks/ring",
                    "https://shop.example/webhooks/ring"]
    assert all(r["replayed"] for r in log.recent())


def test_server_not_running_is_one_line(key, capsys):
    def refuse(req):
        raise httpx.ConnectError("refused")
    assert fw.main([], transport=httpx.MockTransport(refuse)) == 1
    err = capsys.readouterr().err
    assert "Is it running?" in err and "Traceback" not in err


def test_hh_mm_is_today_in_shop_time():
    now = datetime(2026, 10, 21, 12, 5, tzinfo=settings.tz)
    assert fw.parse_at("10:15", now) == datetime(2026, 10, 21, 10, 15, tzinfo=settings.tz)
    with pytest.raises(ValueError):
        fw.parse_at("17:30", now)
