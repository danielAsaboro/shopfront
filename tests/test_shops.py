"""Several shops on one webhook stack (app/store/shops.py): the consumer names each event's shop from the map
in config, writes it to that shop's partition (pk = <shop_id>#<day>), and the single-shop default still works."""

import json

import boto3
import pytest

from app.ring.webhooks import ours, sign
from app.store import lambda_consumer as lc
from app.store import lambda_webhook as lw
from app.store.dynamodb import DynamoWebhookLog, EventTable
from app.store.shops import Shop, ShopMap
from tests import aws_fakes
from tests.test_lambda_webhook import TABLE, drain, request
from tests.test_webhooks import KEY, event

aws, ddb, sqs = aws_fakes.aws, aws_fakes.ddb, aws_fakes.sqs  # pytest fixtures

BAKERY, FLORIST = "ava1.ring.account.BAKERY", "ava1.ring.account.FLORIST"
MAP = {"shops": [
    {"shop_id": "corner-bakery", "account_id": BAKERY, "device_ids": ["bakery-front"]},
    {"shop_id": "rose-florist", "account_id": FLORIST},  # every doorbell on the florist's account
    {"shop_id": "bakery-annex", "account_id": BAKERY, "device_ids": ["annex-door"]},
]}


def row(account, device):
    return {"request_id": "r", "type": "motion_detected", "account": account, "device": device}


def test_events_go_to_the_shop_that_owns_the_account_and_doorbell():
    shops = ShopMap.parse(json.dumps(MAP))
    assert shops.shop_for(row(BAKERY, "bakery-front")) == "corner-bakery"
    assert shops.shop_for(row(BAKERY, "annex-door")) == "bakery-annex"
    assert shops.shop_for(row(FLORIST, "anything")) == "rose-florist"
    assert shops.shop_for(row(BAKERY, "backyard-cam")) is None  # the bakery's other camera: no shop's door
    assert shops.shop_for(row("ava1.ring.account.STRANGER", "bakery-front")) is None


@pytest.mark.parametrize("device_id,account_id", [("", ""), ("dev", ""), ("", "acct"), ("dev", "acct")])
@pytest.mark.parametrize("device,account", [("dev", "acct"), ("other", "acct"), ("dev", "other"), (None, None)])
def test_the_single_shop_default_is_the_local_logs_filter(device_id, account_id, device, account):
    r = {"device": device, "account": account}
    expected = "shop" if ours(r, device_id, account_id) else None
    assert ShopMap.single("shop", device_id, account_id).shop_for(r) == expected


@pytest.mark.parametrize("shops,why", [
    ([], "no shop"),
    ([Shop("a#b", "x")], "must match"),                                   # "#" would break the partition prefix
    ([Shop("a", "x"), Shop("a", "y")], "twice"),
    ([Shop("a", "x"), Shop("b", "")], "account_id"),                      # an empty account claims every account
    ([Shop("a", "x"), Shop("b", "x")], "same doorbell"),                  # same account, no devices named
    ([Shop("a", "x", ("d1",)), Shop("b", "x")], "same doorbell"),
    ([Shop("a", "x", ("d1", "d2")), Shop("b", "x", ("d2",))], "same doorbell"),
])
def test_an_ambiguous_or_broken_map_is_refused(shops, why):
    with pytest.raises(ValueError, match=why):
        ShopMap(shops)


@pytest.mark.parametrize("text", ["not json", "{}", '{"shops": [{"account_id": "x"}]}', '{"shops": 3}'])
def test_an_unreadable_map_is_refused(text):
    with pytest.raises(ValueError):
        ShopMap.parse(text)


# ---- end to end: Function URL -> SQS -> consumer with the map in SSM -> each shop's partition --------------
@pytest.fixture
def stack(ddb, sqs, monkeypatch):
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/shopfront/shops", Type="SecureString", Value=json.dumps(MAP))
    for k, v in {"TABLE_NAME": TABLE, "EVENT_DAYS": "90", "SHOP_ID": "shop", "RING_DEVICE_ID": "",
                 "RING_ACCOUNT_ID": "", "SHOP_MAP_PARAMETER": "/shopfront/shops"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lc, "_clients", {})
    monkeypatch.setattr(lc, "_map", {"value": None, "at": 0.0})
    return ssm


def enqueue(sqs, *raws):
    queue = lw.EventQueue(sqs[0], sqs[1])
    for raw in raws:
        assert lw.handle(request(raw, sign(raw, KEY)), key=lambda: KEY, queue=queue)["statusCode"] == 200


def test_each_shop_reads_only_its_own_partition(stack, sqs, ddb):
    enqueue(sqs, event("b1", 9, 0, device="bakery-front", account=BAKERY),
            event("f1", 9, 5, device="florist-door", account=FLORIST),
            event("a1", 9, 10, device="annex-door", account=BAKERY),
            event("x1", 9, 15, device="bakery-front", account="ava1.ring.account.STRANGER"))
    assert drain(sqs, lambda evt: lc.handler(evt, None)) == {"batchItemFailures": []}
    pks = sorted(i["pk"]["S"] for i in ddb.scan(TableName=TABLE)["Items"] if "row" in i)
    assert pks == ["bakery-annex#2026-10-07", "corner-bakery#2026-10-07", "rose-florist#2026-10-07"]
    for shop, rid in (("corner-bakery", "b1"), ("rose-florist", "f1"), ("bakery-annex", "a1")):
        assert [r["request_id"] for r in DynamoWebhookLog(EventTable(ddb, TABLE, shop)).recent(10)] == [rid]


def test_a_broken_map_fails_the_whole_batch_and_writes_nothing(stack, sqs, ddb):
    stack.put_parameter(Name="/shopfront/shops", Type="SecureString", Overwrite=True,
                        Value=json.dumps({"shops": [{"shop_id": "a", "account_id": BAKERY},
                                                    {"shop_id": "b", "account_id": BAKERY}]}))
    enqueue(sqs, event("b1", 9, 0, device="bakery-front", account=BAKERY))
    with pytest.raises(ValueError, match="same doorbell"):  # Lambda retries the batch, then dead-letters it
        drain(sqs, lambda evt: lc.handler(evt, None))
    assert [i for i in ddb.scan(TableName=TABLE)["Items"] if "row" in i] == []


def test_the_map_is_reread_after_its_ttl(stack):
    env = {"SHOP_MAP_PARAMETER": "/shopfront/shops"}
    first = lc.shop_map(lambda: stack, env, now=1000.0)
    stack.put_parameter(Name="/shopfront/shops", Type="SecureString", Overwrite=True,
                        Value=json.dumps({"shops": [{"shop_id": "new-shop", "account_id": BAKERY}]}))
    assert lc.shop_map(lambda: pytest.fail("read SSM again"), env, now=1000.0 + lc.MAP_TTL - 1) is first
    assert lc.shop_map(lambda: stack, env, now=1000.0 + lc.MAP_TTL).shop_for(row(BAKERY, "x")) == "new-shop"


def test_template_lets_the_consumer_read_only_the_named_parameter():
    t = aws_fakes.template()
    assert t["Parameters"]["ShopMapParameter"]["Default"] == ""  # the single shop by default
    policy = t["Resources"]["ShopMapReadPolicy"]
    assert policy["Condition"] == "HasShopMap"
    assert policy["Properties"]["Roles"] == [{"Ref": "WebhookConsumerRole"}]
    [stmt] = policy["Properties"]["PolicyDocument"]["Statement"]
    assert stmt["Action"] == "ssm:GetParameter"
    assert stmt["Resource"] == {
        "Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${ShopMapParameter}"}
