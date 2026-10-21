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


# ---- whose door: device and account --------------------------------------------------------------------
from dataclasses import replace  # noqa: E402
from datetime import date, datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.ring.webhooks import MAX_BODY, replay_key  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)


def event(rid, hh, mm, *, device="dev", account="ava1.ring.account.SHOP", kind="motion_detected",
          sub_type="package_delivery", with_id=True, replay=False):
    ms = int(datetime(2026, 10, 7, hh, mm, tzinfo=TZ).timestamp() * 1000)
    attrs = {"source": device, "source_type": "devices", "timestamp": ms}
    if sub_type and kind == "motion_detected":
        attrs["sub_type"] = sub_type
    meta = {"version": "1.1", "time": "2026-10-07T19:00:00Z", "request_id": rid, "account_id": account}
    if replay:
        meta["shopfront_replay"] = True
    data = {"type": kind, "attributes": attrs}
    if with_id:
        data["id"] = f"{device}_{kind}_{ms}"
    return json.dumps({"meta": meta, "data": data}).encode()


def test_other_accounts_and_devices_never_reach_the_shop_door(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl", device_id="dev", account_id="ava1.ring.account.SHOP")
    for raw in (event("a", 12, 0), event("b", 12, 5, device="ava1.ring.device.BACKYARD"),
                event("c", 12, 10, account="ava1.ring.account.SOMEONE_ELSE")):
        assert log.handle(raw, sign(raw, KEY), KEY) == WebhookStatus.ACCEPTED  # 200: Ring mustn't retry or disable us
    d = log.day(DAY, TZ)
    assert [v.event.timestamp.strftime("%H:%M") for v in d["ring_labelled"]] == ["12:00"]
    assert log.ignored(DAY, TZ) == 2
    # Nothing configured (the synthetic demo): every signed event counts, as before
    assert len(WebhookLog(tmp_path / "w.jsonl").day(DAY, TZ)["ring_labelled"]) == 3


def test_presses_without_a_data_id_are_not_merged(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    for i, mm in enumerate((10, 20, 30)):
        raw = event(f"r{i}", 12, mm, kind="button_press", with_id=False)
        log.handle(raw, sign(raw, KEY), KEY)
    assert len(log.day(DAY, TZ)["events"]) == 3


def test_a_truncated_log_line_is_skipped_not_fatal(tmp_path, caplog):
    p = tmp_path / "w.jsonl"
    log = WebhookLog(p)
    raw = event("ok", 12, 0)
    log.handle(raw, sign(raw, KEY), KEY)
    with p.open("a") as f:
        f.write('{"request_id": "half-writ')  # the process died mid-write
    again = WebhookLog(p)  # used to raise JSONDecodeError at app import
    assert len(again.day(DAY, TZ)["ring_labelled"]) == 1 and len(again.recent()) == 1
    assert again.handle(raw, sign(raw, KEY), KEY) == WebhookStatus.DUPLICATE
    assert "skipped 1 unreadable line" in caplog.text


# ---- replays can't pass as Ring's own -------------------------------------------------------------------
def test_replay_signed_with_the_replay_key_stays_a_replay_without_any_header(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    raw = event("rep", 12, 0)  # body marker stripped too
    assert log.handle(raw, sign(raw, replay_key(KEY)), KEY) == WebhookStatus.ACCEPTED
    assert log.recent()[0]["replayed"] is True
    assert log.day(DAY, TZ)["ring_labelled"][0].event.source == "ring-webhook-replay"


def test_body_marker_makes_a_replay_even_with_rings_key(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    raw = event("rep", 12, 0, replay=True)
    log.handle(raw, sign(raw, KEY), KEY)
    assert log.day(DAY, TZ)["ring_labelled"][0].event.source == "ring-webhook-replay"


def test_rings_own_payload_still_verifies_as_ring(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    raw = json.dumps(PAYLOAD).encode()
    assert log.handle(raw, sign(raw, KEY), KEY) == WebhookStatus.ACCEPTED
    assert "replayed" not in log.recent()[0]
    other = event("x", 12, 0)
    assert log.handle(other, sign(other, "some-other-key"), KEY) == WebhookStatus.BAD_SIGNATURE


def test_feeder_payload_is_a_replay_through_the_api_even_without_its_header(monkeypatch):
    from scripts.feed_webhook import payload, signed
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=KEY))
    raw, sig = signed(payload("motion_detected", datetime(2026, 10, 7, 9, 30, tzinfo=TZ), "dev", "package_delivery"), KEY)
    r = TestClient(main.app).post("/webhooks/ring", content=raw, headers={"X-Signature": sig})  # no X-Shopfront-Replay
    assert r.status_code == 200
    assert main.webhook_log.day(DAY, TZ)["ring_labelled"][0].event.source == "ring-webhook-replay"


def test_oversized_bodies_are_refused_before_hmac(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=KEY))
    big = b'{"meta": {}, "pad": "' + b"x" * MAX_BODY + b'"}'
    r = TestClient(main.app).post("/webhooks/ring", content=big, headers={"X-Signature": sign(big, KEY)})
    assert r.status_code == 413 and main.webhook_log.recent() == []
