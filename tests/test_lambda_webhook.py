"""The webhook Lambdas (infra/template.yaml): the Function URL handler (app/store/lambda_webhook.py: HMAC, then
SQS) and the queue consumer (app/store/lambda_consumer.py: SQS to DynamoDB), on moto's SQS, DynamoDB and
Secrets Manager, with the same payloads and key as tests/test_webhooks.py."""

import base64
import json
import subprocess
import sys

import boto3
import pytest

from app.ring.webhooks import MAX_BODY, replay_key, sign
from app.store import lambda_consumer as lc
from app.store import lambda_webhook as lw
from app.store.dynamodb import EventTable
from infra.package_lambda import package
from tests import aws_fakes
from tests.test_webhooks import KEY, PAYLOAD, event

aws, ddb, sqs = aws_fakes.aws, aws_fakes.ddb, aws_fakes.sqs  # pytest fixtures
template = aws_fakes.template

SHOP_DEVICE, SHOP_ACCOUNT = "dev", "ava1.ring.account.SHOP"
TABLE = "shopfront-webhooks"


def request(raw: bytes, signature: str | None = None, *, path="/webhooks/ring", method="POST",
            b64=False, headers=None) -> dict:
    """A Lambda Function URL event (payload format 2.0); header names arrive lower-cased."""
    h = {"content-type": "application/json", **(headers or {})}
    if signature is not None:
        h["x-signature"] = signature
    return {"version": "2.0", "rawPath": path, "headers": h, "isBase64Encoded": b64,
            "body": base64.b64encode(raw).decode() if b64 else raw.decode(),
            "requestContext": {"http": {"method": method, "path": path}}}


@pytest.fixture
def queue(sqs):
    client, url, _ = sqs
    return lw.EventQueue(client, url)


def call(evt, queue, key=KEY, trace=""):
    out = lw.handle(evt, key=lambda: key, queue=queue, trace=trace)
    return out["statusCode"], json.loads(out["body"])["status"]


def receive(client, url, n=10) -> list[dict]:
    """Messages as Lambda's SQS event source hands them to the consumer (its record shape)."""
    got = client.receive_message(QueueUrl=url, MaxNumberOfMessages=n, VisibilityTimeout=0,
                                 MessageAttributeNames=["All"], MessageSystemAttributeNames=["All"])
    return [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
             "attributes": m.get("Attributes", {}), "eventSource": "aws:sqs",
             "messageAttributes": {k: {"stringValue": v["StringValue"], "dataType": v["DataType"]}
                                   for k, v in m.get("MessageAttributes", {}).items()}}
            for m in got.get("Messages", [])]


def drain(sqs, consume) -> dict:
    """One poll of the event source: hand a batch to `consume`, then delete what it didn't report failed
    (what Lambda does with ReportBatchItemFailures)."""
    client, url, _ = sqs
    records = receive(client, url)
    out = consume({"Records": records})
    failed = {f["itemIdentifier"] for f in out["batchItemFailures"]}
    for r in records:
        if r["messageId"] not in failed:
            client.delete_message(QueueUrl=url, ReceiptHandle=r["receiptHandle"])
    return out


def consumer(ddb, device=SHOP_DEVICE, account=SHOP_ACCOUNT, shop="corner-bakery"):
    return lambda evt: lc.consume(evt, shop_for=lc.single_shop(shop, device, account),
                                  table_for=lambda s: EventTable(ddb, TABLE, s))


def stored(ddb):
    return [i for i in ddb.scan(TableName=TABLE)["Items"] if "row" in i]


def depth(client, url) -> int:
    attrs = client.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                                      "ApproximateNumberOfMessagesNotVisible"])
    return sum(int(v) for v in attrs["Attributes"].values())


# ---- the Function URL handler: check, enqueue, answer ----------------------------------------------------
def test_good_signature_is_queued_not_written(queue, sqs):
    client, url, _ = sqs
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), queue) == (200, "QUEUED")
    [msg] = receive(client, url)
    row = json.loads(msg["body"])
    assert row["request_id"] == "req-1" and row["type"] == "button_press" and "replayed" not in row
    assert msg["messageAttributes"]["request_id"]["stringValue"] == "req-1"  # the dedupe key
    assert msg["messageAttributes"]["replayed"]["stringValue"] == "0"


def test_bad_or_missing_signature_is_401_and_nothing_queued(queue, sqs, caplog):
    client, url, _ = sqs
    raw = json.dumps(PAYLOAD).encode()
    reserialised = json.dumps(PAYLOAD, indent=2).encode()  # same JSON, different bytes
    with caplog.at_level("INFO", logger="shopfront.webhook"):
        assert call(request(reserialised, sign(raw, KEY)), queue) == (401, "BAD_SIGNATURE")
        assert call(request(raw), queue) == (401, "BAD_SIGNATURE")
        assert call(request(raw, sign(raw, "some-other-key")), queue) == (401, "BAD_SIGNATURE")
    assert depth(client, url) == 0
    # the term the template's HmacFailures metric filter counts
    assert sum("BAD_SIGNATURE" in r.getMessage() for r in caplog.records) == 3


def test_oversize_body_is_413_before_the_key_is_even_fetched(queue, sqs):
    big = b'{"meta": {}, "pad": "' + b"x" * MAX_BODY + b'"}'
    out = lw.handle(request(big, sign(big, KEY)), key=lambda: pytest.fail("fetched the key"), queue=queue)
    assert out["statusCode"] == 413 and depth(sqs[0], sqs[1]) == 0


def test_signed_garbage_and_broken_base64_are_400(queue):
    raw = b'{"hello": 1}'
    assert call(request(raw, sign(raw, KEY)), queue) == (400, "MALFORMED")
    evt = request(b"x", sign(b"x", KEY), b64=True) | {"body": "not base64!"}
    assert call(evt, queue) == (400, "MALFORMED")


def test_base64_body_verifies_over_the_decoded_bytes(queue):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY), b64=True), queue) == (200, "QUEUED")


def test_no_key_yet_is_503_so_ring_retries(queue):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), queue, key="") == (503, "NOT_CONFIGURED")


def test_only_post_to_the_route(queue):
    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY), path="/"), queue)[0] == 404
    assert call(request(raw, sign(raw, KEY), method="GET"), queue)[0] == 405


def test_queue_down_is_503_so_ring_retries():
    class Down:
        def send_message(self, **kw):
            raise ConnectionError("no route to SQS")

    raw = json.dumps(PAYLOAD).encode()
    assert call(request(raw, sign(raw, KEY)), lw.EventQueue(Down(), "q")) == (503, "QUEUE_UNAVAILABLE")


def test_the_trace_header_rides_the_message(queue, sqs):
    raw = json.dumps(PAYLOAD).encode()
    trace = "Root=1-67891233-abcdef012345678912345678;Parent=53995c3f42cd8ad8;Sampled=1"
    assert call(request(raw, sign(raw, KEY)), queue, trace=trace) == (200, "QUEUED")
    [msg] = receive(sqs[0], sqs[1])
    assert msg["attributes"]["AWSTraceHeader"] == trace


# ---- the consumer: filter, store once, report only the failures -------------------------------------------
def test_queued_event_is_stored_once_by_the_consumer(queue, sqs, ddb):
    raw = json.dumps(PAYLOAD).encode()
    for _ in range(3):  # Ring's first delivery and two retries with the same request_id
        assert call(request(raw, sign(raw, KEY)), queue) == (200, "QUEUED")
    out = drain(sqs, consumer(ddb, device="", account=""))
    assert out == {"batchItemFailures": []}
    [item] = stored(ddb)
    assert item["pk"]["S"] == "corner-bakery#2026-10-07"  # UTC day of Ring's timestamp
    assert item["sk"]["S"] == "1791388973000#req-1"
    assert json.loads(item["row"]["S"])["type"] == "button_press" and "expires_at" in item
    assert depth(sqs[0], sqs[1]) == 0


def test_consumer_outcomes_per_record(queue, sqs, ddb, caplog):
    raws = [event("a", 12, 0), event("a", 12, 0),
            event("b", 12, 5, device="ava1.ring.device.BACKYARD"),
            event("c", 12, 10, account="ava1.ring.account.SOMEONE_ELSE")]
    for raw in raws:
        assert call(request(raw, sign(raw, KEY)), queue) == (200, "QUEUED")  # Ring mustn't retry or disable
    with caplog.at_level("INFO", logger="shopfront.webhook.consumer"):
        assert drain(sqs, consumer(ddb)) == {"batchItemFailures": []}
    statuses = sorted(json.loads(r.getMessage())["status"] for r in caplog.records
                      if r.name == "shopfront.webhook.consumer")
    assert statuses == ["ACCEPTED", "DUPLICATE", "IGNORED", "IGNORED"]
    assert [json.loads(i["row"]["S"])["request_id"] for i in stored(ddb)] == ["a"]


def test_replays_stay_replays_end_to_end(queue, sqs, ddb):
    raw = event("rep", 12, 0)
    assert call(request(raw, sign(raw, replay_key(KEY))), queue) == (200, "QUEUED")
    [msg] = receive(sqs[0], sqs[1])
    assert msg["messageAttributes"]["replayed"]["stringValue"] == "1"
    drain(sqs, consumer(ddb))
    assert json.loads(stored(ddb)[0]["row"]["S"])["replayed"] is True


def test_the_replay_attribute_alone_marks_a_replay(ddb):
    row = json.loads(json.dumps({"request_id": "r", "type": "motion_detected", "device": SHOP_DEVICE,
                                 "account": SHOP_ACCOUNT, "timestamp": 1791388973000}))
    rec = {"messageId": "m", "body": json.dumps(row), "messageAttributes": {"replayed": {"stringValue": "1"}}}
    assert consumer(ddb)({"Records": [rec]}) == {"batchItemFailures": []}
    assert json.loads(stored(ddb)[0]["row"]["S"])["replayed"] is True


def test_partial_batch_failure_reports_only_the_bad_records(queue, sqs, ddb):
    client, url, _ = sqs
    for rid in ("ok-1", "boom", "ok-2"):
        raw = event(rid, 12, 0)
        call(request(raw, sign(raw, KEY)), queue)
    client.send_message(QueueUrl=url, MessageBody="not json")  # a poison message

    class Flaky(EventTable):
        def put(self, row, now=None):
            if row["request_id"] == "boom":
                raise RuntimeError("DynamoDB said no")
            return super().put(row, now)

    records = receive(client, url)
    out = lc.consume({"Records": records}, shop_for=lc.single_shop("corner-bakery"),
                     table_for=lambda s: Flaky(ddb, TABLE, s))
    by_body = {r["messageId"]: r["body"] for r in records}
    failed = sorted("poison" if by_body[f["itemIdentifier"]] == "not json" else json.loads(
        by_body[f["itemIdentifier"]])["request_id"] for f in out["batchItemFailures"])
    assert failed == ["boom", "poison"]
    assert sorted(json.loads(i["row"]["S"])["request_id"] for i in stored(ddb)) == ["ok-1", "ok-2"]


def test_a_record_that_keeps_failing_is_dead_lettered_after_five_receives(queue, sqs, ddb):
    client, url, dlq = sqs
    raw = event("ok", 12, 0)
    call(request(raw, sign(raw, KEY)), queue)
    client.send_message(QueueUrl=url, MessageBody='{"type": "motion_detected"}')  # no request_id: never storable
    first = drain(sqs, consumer(ddb))
    assert len(first["batchItemFailures"]) == 1 and len(stored(ddb)) == 1
    for _ in range(4):  # receives 2..5
        assert len(drain(sqs, consumer(ddb))["batchItemFailures"]) == 1
    assert drain(sqs, consumer(ddb)) == {"batchItemFailures": []}  # 6th poll: SQS moved it to the DLQ
    [dead] = client.receive_message(QueueUrl=dlq)["Messages"]
    assert dead["Body"] == '{"type": "motion_detected"}'
    assert depth(client, url) == 0


# ---- the entry points the template names, with the template's environment ---------------------------------
def env_of(resource: str) -> dict:
    return template()["Resources"][resource]["Properties"]["Environment"]["Variables"]


@pytest.fixture
def deployed(ddb, sqs, monkeypatch):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    arn = sm.create_secret(Name="shopfront-hmac", SecretString='{"RING_HMAC_KEY": ""}')["ARN"]
    ingest = {"HMAC_SECRET_ARN": arn, "QUEUE_URL": sqs[1]}
    consume = {"TABLE_NAME": TABLE, "SHOP_ID": "corner-bakery", "EVENT_DAYS": "90",
               "RING_DEVICE_ID": SHOP_DEVICE, "RING_ACCOUNT_ID": SHOP_ACCOUNT, "SHOP_MAP_PARAMETER": ""}
    assert set(env_of("RingWebhookFunction")) == set(ingest)  # each handler reads exactly what the template sets
    assert set(env_of("WebhookConsumerFunction")) == set(consume)
    for k, v in (ingest | consume).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lw, "_clients", {})
    monkeypatch.setattr(lc, "_clients", {})
    monkeypatch.setattr(lc, "_map", {"value": None, "at": 0.0})
    monkeypatch.setattr(lw, "_key", {"value": "", "at": 0.0})
    return sm, arn


def test_handlers_read_the_key_from_secrets_manager_and_store_through_the_queue(deployed, sqs, ddb):
    sm, arn = deployed
    raw = event("a", 12, 0)
    assert lw.handler(request(raw, sign(raw, KEY)), None)["statusCode"] == 503  # slot still empty
    sm.put_secret_value(SecretId=arn, SecretString=json.dumps({"RING_HMAC_KEY": KEY}))
    assert lw.handler(request(raw, sign(raw, KEY)), None)["statusCode"] == 200
    assert stored(ddb) == []  # nothing written until the consumer runs
    assert drain(sqs, lambda evt: lc.handler(evt, None)) == {"batchItemFailures": []}
    [item] = stored(ddb)
    assert item["pk"]["S"].startswith("corner-bakery#")


def test_a_bare_secret_string_is_the_key_and_is_cached(deployed):
    sm, arn = deployed
    sm.put_secret_value(SecretId=arn, SecretString=KEY)
    assert lw.secret_key(sm, arn, now=1000.0) == KEY
    sm.put_secret_value(SecretId=arn, SecretString="rotated")
    assert lw.secret_key(sm, arn, now=1000.0 + lw.KEY_TTL - 1) == KEY
    assert lw.secret_key(sm, arn, now=1000.0 + lw.KEY_TTL + 1) == "rotated"


# ---- the template ------------------------------------------------------------------------------------------
def actions_of(role: str) -> list[str]:
    statements = template()["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert all("*" not in json.dumps(s["Resource"]) for s in statements)
    return sorted(a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))


def test_template_wires_the_handlers_and_keeps_secrets_out():
    t = template()
    res = t["Resources"]
    fn, cons = res["RingWebhookFunction"]["Properties"], res["WebhookConsumerFunction"]["Properties"]
    assert fn["Handler"] == "app.store.lambda_webhook.handler" and callable(lw.handler)
    assert cons["Handler"] == "app.store.lambda_consumer.handler" and callable(lc.handler)
    for f in (fn, cons):
        assert f["Runtime"] == "python3.12" and f["Architectures"] == ["arm64"]
    assert fn["Timeout"] < 5  # Ring wants its answer within 5 seconds
    secret = res["RingHmacSecret"]["Properties"]
    assert "SecretString" not in secret
    assert json.loads(secret["GenerateSecretString"]["SecretStringTemplate"]) == {"RING_HMAC_KEY": ""}
    # the public function can't touch the table; the consumer can't read the key
    assert actions_of("WebhookFunctionRole") == ["logs:CreateLogStream", "logs:PutLogEvents",
                                                 "secretsmanager:GetSecretValue", "sqs:SendMessage"]
    assert actions_of("WebhookConsumerRole") == ["dynamodb:PutItem", "logs:CreateLogStream", "logs:PutLogEvents",
                                                 "sqs:DeleteMessage", "sqs:GetQueueAttributes",
                                                 "sqs:ReceiveMessage"]


def test_queue_and_dead_letter_queue_config():
    res = template()["Resources"]
    q, dlq = res["WebhookQueue"]["Properties"], res["WebhookDeadLetterQueue"]["Properties"]
    assert q["SqsManagedSseEnabled"] is True and dlq["SqsManagedSseEnabled"] is True
    assert q["RedrivePolicy"] == {"deadLetterTargetArn": {"GetAtt": "WebhookDeadLetterQueue.Arn"},
                                  "maxReceiveCount": 5}
    assert dlq["MessageRetentionPeriod"] > q["MessageRetentionPeriod"]  # a dead letter outlives its source
    cons = res["WebhookConsumerFunction"]["Properties"]
    assert q["VisibilityTimeout"] >= 6 * cons["Timeout"]
    src = cons["Events"]["Queue"]["Properties"]
    assert src["Queue"] == {"GetAtt": "WebhookQueue.Arn"}
    assert src["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    for name, queue in (("WebhookQueuesPolicy", "WebhookQueue"), ("WebhookDeadLetterQueuePolicy", "WebhookDeadLetterQueue")):
        pol = res[name]["Properties"]
        assert pol["Queues"] == [{"Ref": queue}]
        for st in pol["PolicyDocument"]["Statement"]:  # SQS refuses a statement naming more than one resource
            assert st["Resource"] == {"GetAtt": f"{queue}.Arn"}
        tls = pol["PolicyDocument"]["Statement"][0]
        assert tls["Effect"] == "Deny" and tls["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_the_lambda_package_needs_only_the_standard_library(tmp_path):
    """infra/package_lambda.py is what `sam build` ships for both functions. It must import with no
    site-packages at all (boto3 is only imported inside the handlers, from the Lambda runtime)."""
    package(tmp_path)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import app.store.lambda_webhook as m; "
            "import app.store.lambda_consumer as c; "
            "assert 'boto3' not in sys.modules and 'httpx' not in sys.modules; print(m.ROUTE, callable(c.handler))")
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp_path)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "/webhooks/ring True"


# ---- alarms and tracing ------------------------------------------------------------------------------------
def test_hmac_failures_and_only_they_match_the_metric_filter(queue, caplog):
    term = template()["Resources"]["HmacFailureMetricFilter"]["Properties"]["FilterPattern"].strip('"')
    raw = json.dumps(PAYLOAD).encode()
    with caplog.at_level("INFO", logger="shopfront.webhook"):
        call(request(raw, sign(raw, "wrong")), queue)                  # BAD_SIGNATURE
        call(request(raw, sign(raw, KEY)), queue)                      # QUEUED
        call(request(raw, sign(raw, KEY)), queue, key="")              # NOT_CONFIGURED
        call(request(b'{"x": 1}', sign(b'{"x": 1}', KEY)), queue)      # MALFORMED
    lines = [r.getMessage() for r in caplog.records if r.name == "shopfront.webhook"]
    assert [json.loads(line)["status"] for line in lines if term in line] == ["BAD_SIGNATURE"]


def test_only_failed_records_are_logged_at_error_level(ddb, caplog):
    """ConsumerFailureMetricFilter counts $.level = ERROR in the consumer's JSON log."""
    pattern = template()["Resources"]["ConsumerFailureMetricFilter"]["Properties"]["FilterPattern"]
    assert pattern == '{ $.level = "ERROR" }'
    good = {"request_id": "g", "type": "motion_detected", "device": SHOP_DEVICE, "account": SHOP_ACCOUNT,
            "timestamp": 1791388973000}
    records = [{"messageId": "1", "body": json.dumps(good)}, {"messageId": "2", "body": json.dumps(good)},
               {"messageId": "3", "body": json.dumps(good | {"request_id": "x", "account": "other"})},
               {"messageId": "4", "body": "{"}]
    with caplog.at_level("INFO", logger="shopfront.webhook.consumer"):
        out = consumer(ddb)({"Records": records})
    assert out == {"batchItemFailures": [{"itemIdentifier": "4"}]}
    levels = {json.loads(r.getMessage())["status"]: r.levelname for r in caplog.records
              if r.name == "shopfront.webhook.consumer"}
    assert levels == {"ACCEPTED": "INFO", "DUPLICATE": "INFO", "IGNORED": "INFO", "FAILED": "ERROR"}


def test_alarms_notify_one_topic_with_an_optional_email():
    t = template()
    res = t["Resources"]
    alarms = {k: v["Properties"] for k, v in res.items() if v["Type"] == "AWS::CloudWatch::Alarm"}
    assert set(alarms) == {"DeadLetterQueueAlarm", "HmacFailureAlarm", "ConsumerErrorsAlarm", "WebhookErrorsAlarm",
                           "DemoThrottlesAlarm"}
    assert all(a["AlarmActions"] == [{"Ref": "AlarmTopic"}] for a in alarms.values())
    assert all(a["TreatMissingData"] == "notBreaching" for a in alarms.values())
    dlq = alarms["DeadLetterQueueAlarm"]
    assert (dlq["Namespace"], dlq["MetricName"], dlq["Threshold"], dlq["ComparisonOperator"]) == (
        "AWS/SQS", "ApproximateNumberOfMessagesVisible", 0, "GreaterThanThreshold")
    assert dlq["Dimensions"] == [{"Name": "QueueName", "Value": {"GetAtt": "WebhookDeadLetterQueue.QueueName"}}]
    hmac_alarm = alarms["HmacFailureAlarm"]
    assert hmac_alarm["MetricName"] == "HmacFailures" and hmac_alarm["Threshold"] == {"Ref": "HmacFailureThreshold"}
    assert (res["HmacFailureMetricFilter"]["Properties"]["LogGroupName"] == {"Ref": "WebhookLogGroup"})
    consumer_metrics = {m["Id"]: m for m in alarms["ConsumerErrorsAlarm"]["Metrics"]}
    assert consumer_metrics["errors"]["MetricStat"]["Metric"]["Dimensions"] == [
        {"Name": "FunctionName", "Value": {"Ref": "WebhookConsumerFunction"}}]
    assert consumer_metrics["failed"]["MetricStat"]["Metric"]["MetricName"] == "ConsumerFailedRecords"
    throttles = alarms["DemoThrottlesAlarm"]
    assert throttles["MetricName"] == "Throttles"
    assert throttles["Dimensions"] == [{"Name": "FunctionName", "Value": {"Ref": "PublicDemoFunction"}}]
    sub = res["AlarmEmailSubscription"]
    assert sub["Condition"] == "HasAlarmEmail" and sub["Properties"]["Protocol"] == "email"
    assert t["Parameters"]["AlarmEmail"]["Default"] == ""


def test_every_function_traces_with_xray():
    t = template()
    res = t["Resources"]
    assert t["Globals"]["Function"]["Tracing"] == "Active"
    functions = [k for k, v in res.items() if v["Type"] == "AWS::Serverless::Function"]
    assert sorted(functions) == ["PublicDemoFunction", "RingWebhookFunction", "WebhookConsumerFunction"]
    assert all(res[f]["DependsOn"] == "TracingPolicy" and "Tracing" not in res[f]["Properties"] for f in functions)
    policy = res["TracingPolicy"]["Properties"]
    roles = sorted(r["Ref"] for r in policy["Roles"])
    assert roles == sorted(res[f]["Properties"]["Role"]["GetAtt"].split(".")[0] for f in functions)
    [stmt] = policy["PolicyDocument"]["Statement"]
    assert stmt["Action"] == ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]  # and nothing else on "*"
