"""The webhook Lambda (app/store/lambda_webhook.py, deployed by infra/template.yaml), on moto's DynamoDB and
Secrets Manager, with the same payloads and key as tests/test_webhooks.py."""

import base64
import json
import subprocess
import sys

import boto3
import pytest

from app.ring.webhooks import MAX_BODY, replay_key, sign
from app.store import lambda_webhook as lw
from app.store.dynamodb import EventTable
from infra.package_lambda import package
from tests import aws_fakes
from tests.test_webhooks import KEY, PAYLOAD, event

aws, ddb = aws_fakes.aws, aws_fakes.ddb  # pytest fixtures
template = aws_fakes.template

SHOP_DEVICE, SHOP_ACCOUNT = "dev", "ava1.ring.account.SHOP"


def request(raw: bytes, signature: str | None = None, *, path="/webhooks/ring", method="POST",
            b64=False, headers=None) -> dict:
    """A Lambda Function URL event (payload format 2.0); header names arrive lower-cased."""
    h = {"content-type": "application/json", **(headers or {})}
    if signature is not None:
        h["x-signature"] = signature
    return {"version": "2.0", "rawPath": path, "headers": h, "isBase64Encoded": b64,
            "body": base64.b64encode(raw).decode() if b64 else raw.decode(),
            "requestContext": {"http": {"method": method, "path": path}}}


def call(evt, table, key=KEY, device="", account=""):
    out = lw.handle(evt, key=lambda: key, table=table, device_id=device, account_id=account)
    return out["statusCode"], json.loads(out["body"])["status"]


@pytest.fixture
def table(ddb):
    return EventTable(ddb, "shopfront-webhooks", "corner-bakery")


def stored(ddb):
    return [i for i in ddb.scan(TableName="shopfront-webhooks")["Items"] if "row" in i]


def test_good_signature_is_stored_once(table, ddb):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), table) == (200, "ACCEPTED")
    [item] = stored(ddb)
    assert item["pk"]["S"] == "corner-bakery#2026-10-07"  # UTC day of Ring's timestamp
    assert item["sk"]["S"] == "1791388973000#req-1"
    assert json.loads(item["row"]["S"])["type"] == "button_press" and "expires_at" in item


def test_bad_or_missing_signature_is_401_and_nothing_stored(table, ddb):
    raw = json.dumps(PAYLOAD).encode()
    reserialised = json.dumps(PAYLOAD, indent=2).encode()  # same JSON, different bytes
    assert call(request(reserialised, sign(raw, KEY)), table) == (401, "BAD_SIGNATURE")
    assert call(request(raw), table) == (401, "BAD_SIGNATURE")
    assert call(request(raw, sign(raw, "some-other-key")), table) == (401, "BAD_SIGNATURE")
    assert stored(ddb) == []


def test_ring_retry_with_the_same_request_id_is_a_duplicate(table, ddb):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), table) == (200, "ACCEPTED")
    assert call(request(raw, sign(raw, KEY)), table) == (200, "DUPLICATE")
    # same request_id, different body: still the one delivery (the dedupe is on meta.request_id)
    other = json.loads(raw)
    other["data"]["attributes"]["timestamp"] += 60_000
    raw2 = json.dumps(other).encode()
    assert call(request(raw2, sign(raw2, KEY)), table) == (200, "DUPLICATE")
    assert len(stored(ddb)) == 1


def test_wrong_account_or_device_is_200_and_not_stored(table, ddb):
    kw = {"device": SHOP_DEVICE, "account": SHOP_ACCOUNT}
    for raw in (event("b", 12, 5, device="ava1.ring.device.BACKYARD"),
                event("c", 12, 10, account="ava1.ring.account.SOMEONE_ELSE")):
        assert call(request(raw, sign(raw, KEY)), table, **kw) == (200, "IGNORED")  # Ring mustn't retry or disable
    ours = event("a", 12, 0)
    assert call(request(ours, sign(ours, KEY)), table, **kw) == (200, "ACCEPTED")
    assert [json.loads(i["row"]["S"])["request_id"] for i in stored(ddb)] == ["a"]


def test_oversize_body_is_413_before_the_key_is_even_fetched(table, ddb):
    big = b'{"meta": {}, "pad": "' + b"x" * MAX_BODY + b'"}'
    out = lw.handle(request(big, sign(big, KEY)), key=lambda: pytest.fail("fetched the key"), table=table)
    assert out["statusCode"] == 413 and stored(ddb) == []


def test_signed_garbage_and_broken_base64_are_400(table):
    raw = b'{"hello": 1}'
    assert call(request(raw, sign(raw, KEY)), table) == (400, "MALFORMED")
    evt = request(b"x", sign(b"x", KEY), b64=True) | {"body": "not base64!"}
    assert call(evt, table) == (400, "MALFORMED")


def test_base64_body_verifies_over_the_decoded_bytes(table):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY), b64=True), table) == (200, "ACCEPTED")


def test_no_key_yet_is_503_so_ring_retries(table):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), table, key="") == (503, "NOT_CONFIGURED")


def test_only_post_to_the_route(table):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY), path="/"), table)[0] == 404
    assert call(request(raw, sign(raw, KEY), method="GET"), table)[0] == 405


def test_replays_stay_replays(table, ddb):
    raw = event("rep", 12, 0)
    assert call(request(raw, sign(raw, replay_key(KEY))), table) == (200, "ACCEPTED")
    assert json.loads(stored(ddb)[0]["row"]["S"])["replayed"] is True


# ---- the entry point the template names, with the key from Secrets Manager ------------------------------
@pytest.fixture
def deployed(ddb, monkeypatch):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    arn = sm.create_secret(Name="shopfront-hmac", SecretString='{"RING_HMAC_KEY": ""}')["ARN"]
    env = template()["Resources"]["RingWebhookFunction"]["Properties"]["Environment"]["Variables"]
    values = {"TABLE_NAME": "shopfront-webhooks", "SHOP_ID": "corner-bakery", "EVENT_DAYS": "90",
              "HMAC_SECRET_ARN": arn, "RING_DEVICE_ID": SHOP_DEVICE, "RING_ACCOUNT_ID": SHOP_ACCOUNT}
    assert set(env) == set(values)  # the handler reads exactly what the template sets
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lw, "_clients", {})
    monkeypatch.setattr(lw, "_key", {"value": "", "at": 0.0})
    return sm, arn


def test_handler_reads_the_key_from_secrets_manager(deployed, ddb):
    sm, arn = deployed
    raw = event("a", 12, 0)
    assert lw.handler(request(raw, sign(raw, KEY)), None)["statusCode"] == 503  # slot still empty
    sm.put_secret_value(SecretId=arn, SecretString=json.dumps({"RING_HMAC_KEY": KEY}))
    assert lw.handler(request(raw, sign(raw, KEY)), None)["statusCode"] == 200
    assert len(stored(ddb)) == 1


def test_a_bare_secret_string_is_the_key_and_is_cached(deployed):
    sm, arn = deployed
    sm.put_secret_value(SecretId=arn, SecretString=KEY)
    assert lw.secret_key(sm, arn, now=1000.0) == KEY
    sm.put_secret_value(SecretId=arn, SecretString="rotated")
    assert lw.secret_key(sm, arn, now=1000.0 + lw.KEY_TTL - 1) == KEY
    assert lw.secret_key(sm, arn, now=1000.0 + lw.KEY_TTL + 1) == "rotated"


def test_template_wires_the_handler_and_keeps_secrets_out():
    t = template()
    fn = t["Resources"]["RingWebhookFunction"]["Properties"]
    assert fn["Handler"] == "app.store.lambda_webhook.handler" and callable(lw.handler)
    assert fn["Runtime"] == "python3.12" and fn["Architectures"] == ["arm64"]
    secret = t["Resources"]["RingHmacSecret"]["Properties"]
    assert "SecretString" not in secret
    assert json.loads(secret["GenerateSecretString"]["SecretStringTemplate"]) == {"RING_HMAC_KEY": ""}
    statements = t["Resources"]["WebhookFunctionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = sorted(a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))
    assert actions == ["dynamodb:PutItem", "logs:CreateLogStream", "logs:PutLogEvents",
                       "secretsmanager:GetSecretValue"]
    assert all("*" not in json.dumps(s["Resource"]) for s in statements)


def test_the_lambda_package_needs_only_the_standard_library(tmp_path):
    """infra/package_lambda.py is what `sam build` ships. It must import with no site-packages at all
    (boto3 is only imported inside the handler, from the Lambda runtime)."""
    package(tmp_path)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import app.store.lambda_webhook as m; "
            "assert 'boto3' not in sys.modules and 'httpx' not in sys.modules; print(m.ROUTE)")
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp_path)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "/webhooks/ring"
