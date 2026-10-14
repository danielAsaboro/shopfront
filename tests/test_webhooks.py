import json

from app.ring.webhooks import WebhookLog, WebhookStatus, sign

KEY = "test-hmac-key"
# Shape of Ring's documented button_press v1.1 payload, placeholder IDs.
PAYLOAD = {"meta": {"version": "1.1", "time": "2026-10-07T16:02:53Z", "request_id": "req-1",
                    "account_id": "ava1.ring.account.TEST"},
           "data": {"id": "dev_button_press_1791388973000", "type": "button_press",
                    "attributes": {"source": "dev", "source_type": "devices", "timestamp": 1791388973000}}}


def test_signed_payload_is_accepted_once(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    raw = json.dumps(PAYLOAD).encode()
    assert log.handle(raw, sign(raw, KEY), KEY) == WebhookStatus.ACCEPTED
    assert log.handle(raw, sign(raw, KEY), KEY) == WebhookStatus.DUPLICATE
    assert WebhookLog(tmp_path / "w.jsonl").handle(raw, sign(raw, KEY), KEY) == WebhookStatus.DUPLICATE
    assert log.recent()[0]["type"] == "button_press"


def test_signature_is_over_raw_bytes(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    raw = json.dumps(PAYLOAD).encode()
    reserialised = json.dumps(PAYLOAD, indent=2).encode()
    assert log.handle(reserialised, sign(raw, KEY), KEY) == WebhookStatus.BAD_SIGNATURE
    assert log.handle(raw, None, KEY) == WebhookStatus.BAD_SIGNATURE


def test_no_key_refuses_everything(tmp_path):
    raw = json.dumps(PAYLOAD).encode()
    assert WebhookLog(tmp_path / "w.jsonl").handle(raw, sign(raw, ""), "") == WebhookStatus.NOT_CONFIGURED


def test_signed_garbage_is_malformed(tmp_path):
    raw = b'{"hello": 1}'
    assert WebhookLog(tmp_path / "w.jsonl").handle(raw, sign(raw, KEY), KEY) == WebhookStatus.MALFORMED
