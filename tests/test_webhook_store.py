"""SHOPFRONT_WEBHOOK_STORE=dynamodb: the app reads the Lambda's table through the same WebhookLog interface,
and gets the same answers as from the local file log."""

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from botocore.stub import Stubber

import app.main as main
from app.ring.webhooks import WebhookLog, WebhookStatus, sign
from app.store import make_webhook_log
from app.store.dynamodb import DynamoWebhookLog, EventTable, utc_days
from tests import aws_fakes
from tests.test_webhooks import KEY, event

aws, ddb = aws_fakes.aws, aws_fakes.ddb  # pytest fixtures

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)


def feed(log, raws):
    return [log.handle(raw, sign(raw, KEY), KEY) for raw in raws]


def summary(log, day=DAY):
    d = log.day(day, TZ)
    return {k: sorted((e.event if hasattr(e, "event") else e).timestamp.isoformat() for e in v) for k, v in d.items()}


DAY_OF_EVENTS = [
    event("a", 9, 0),                                             # package_delivery: Ring's courier
    event("b", 9, 30, kind="button_press"),                       # doorbell press: to the classifier
    event("c", 10, 0, sub_type="vehicle"),                        # skipped at stage 0
    event("d", 11, 15, sub_type="human"),
    event("e", 12, 0, kind="button_press", with_id=False),       # no data.id: keyed by request_id
    event("f", 23, 30),                                           # 23:30 in Los Angeles is the next UTC day
    event("a", 9, 0),                                             # Ring's retry
]


def test_dynamodb_log_answers_like_the_file_log(ddb, tmp_path):
    file_log = WebhookLog(tmp_path / "w.jsonl")
    dyn = DynamoWebhookLog(EventTable(ddb, "shopfront-webhooks", "corner-bakery"))
    assert feed(dyn, DAY_OF_EVENTS) == feed(file_log, DAY_OF_EVENTS)
    assert feed(dyn, DAY_OF_EVENTS)[-1] == WebhookStatus.DUPLICATE
    assert summary(dyn) == summary(file_log)
    assert len(summary(dyn)["ring_labelled"]) == 2  # 09:00 and 23:30, across two UTC days
    assert summary(dyn, date(2026, 10, 8)) == summary(file_log, date(2026, 10, 8))
    assert {r["request_id"] for r in dyn.recent(100)} == {r["request_id"] for r in file_log.recent(100)}
    assert dyn.recent(1)[0]["request_id"] == "f"  # newest first
    assert dyn.ignored(DAY, TZ) == 0


def test_another_shop_in_the_same_table_is_invisible(ddb):
    ours = DynamoWebhookLog(EventTable(ddb, "shopfront-webhooks", "corner-bakery"))
    theirs = DynamoWebhookLog(EventTable(ddb, "shopfront-webhooks", "corner-bakery-2"))
    feed(theirs, [event("x", 9, 0)])
    assert ours.recent() == [] and summary(ours)["ring_labelled"] == []
    assert feed(ours, [event("x", 9, 0)]) == [WebhookStatus.ACCEPTED]  # its own dedupe, too


def test_other_devices_are_answered_but_not_stored(ddb):
    dyn = DynamoWebhookLog(EventTable(ddb, "shopfront-webhooks"), device_id="dev")
    assert feed(dyn, [event("b", 12, 5, device="ava1.ring.device.BACKYARD")]) == [WebhookStatus.ACCEPTED]
    assert dyn.recent() == []


def test_utc_days_cover_the_shop_day():
    assert utc_days(DAY, TZ) == [date(2026, 10, 7), date(2026, 10, 8)]
    assert utc_days(DAY, ZoneInfo("UTC")) == [DAY]


def test_query_pages_are_followed(ddb):
    table = EventTable(ddb, "shopfront-webhooks")
    with Stubber(ddb) as stub:
        row = '{"request_id": "r%d", "type": "button_press", "timestamp": 1791388973000}'
        stub.add_response("query", {"Items": [{"row": {"S": row % 1}}], "LastEvaluatedKey": {"pk": {"S": "k"}}})
        stub.add_response("query", {"Items": [{"row": {"S": row % 2}}]})
        assert [r["request_id"] for r in table.query_day(DAY)] == ["r1", "r2"]


def test_a_failed_write_is_not_mistaken_for_a_duplicate(ddb):
    table = EventTable(ddb, "shopfront-webhooks")
    with Stubber(ddb) as stub:
        stub.add_client_error("transact_write_items", "TransactionCanceledException",
                              response_meta={}, modeled_fields={"CancellationReasons": [
                                  {"Code": "None"}, {"Code": "ThrottlingError"}]})
        with pytest.raises(ddb.exceptions.TransactionCanceledException):
            table.put({"request_id": "r", "type": "button_press", "timestamp": 1})


# ---- choosing the store ----------------------------------------------------------------------------------
def test_file_is_the_default(tmp_path):
    log = make_webhook_log(main.settings, tmp_path / "w.jsonl")
    assert type(log) is WebhookLog and main.settings.webhook_store == "file"


def test_dynamodb_store_needs_its_table(aws, tmp_path):
    s = replace(main.settings, webhook_store="dynamodb", webhook_table="", ring_device_id="dev")
    with pytest.raises(ValueError, match="SHOPFRONT_WEBHOOK_TABLE"):
        make_webhook_log(s, tmp_path / "w.jsonl")
    log = make_webhook_log(replace(s, webhook_table="t", shop_id="corner-bakery", webhook_region="eu-west-1"),
                           tmp_path / "w.jsonl")
    assert isinstance(log, DynamoWebhookLog) and log.device_id == "dev"
    assert log.table.shop == "corner-bakery" and log.table.client.meta.region_name == "eu-west-1"
    with pytest.raises(ValueError, match="SHOPFRONT_WEBHOOK_STORE"):
        make_webhook_log(replace(s, webhook_store="s3"), tmp_path / "w.jsonl")


def test_the_app_shows_webhooks_from_the_table(ddb, monkeypatch):
    from fastapi.testclient import TestClient

    dyn = DynamoWebhookLog(EventTable(ddb, "shopfront-webhooks"))
    feed(dyn, [event("a", 9, 0)])  # as the Lambda would have stored it
    monkeypatch.setattr(main, "webhook_log", dyn)
    r = TestClient(main.app).get("/api/webhooks")
    assert r.status_code == 200 and [w["request_id"] for w in r.json()] == ["a"]
    assert [v.event.timestamp for v in main.webhook_log.day(DAY, TZ)["ring_labelled"]] == [
        datetime(2026, 10, 7, 9, 0, tzinfo=TZ)]
