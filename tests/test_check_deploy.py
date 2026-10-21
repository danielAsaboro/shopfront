"""scripts/check_deploy.py, rehearsed in-process: moto for the stack's pieces, the real Lambda handlers for the
two URLs. These prove the script's logic, not a deployed stack."""

import json
import time
from datetime import datetime, timezone

import boto3
import httpx
import pytest
from botocore.stub import Stubber

from scripts import check_deploy as cd


def test_dry_run_passes_and_writes_a_record_without_secrets(tmp_path, capsys):
    assert cd.main(["--stack", "shopfront", "--dry-run", "--out", str(tmp_path)]) == 0
    [path] = list(tmp_path.glob("aws-deploy-dry-run-*.json"))
    rec = json.loads(path.read_text())
    assert rec["ok"] and rec["dry_run"] and [s["step"] for s in rec["steps"]] == [
        "stack", "demo", "webhook", "stored", "bad_hmac", "malformed", "replay", "logs", "alarms", "cleanup"]
    steps = {s["step"]: s for s in rec["steps"]}
    assert steps["demo"]["lambda_request_id"] and steps["webhook"]["lambda_request_id"]
    assert steps["webhook"]["ring_request_id"].startswith("check-deploy-")
    # the webhook function's QUEUED line for the first POST and for the replay, and the consumer's ACCEPTED
    assert steps["stored"]["dynamodb_request_id"] and len(steps["logs"]["log_lambda_request_ids"]) == 3
    assert steps["bad_hmac"]["status_code"] == 401 and steps["malformed"]["status_code"] == 400
    assert steps["replay"]["status_code"] == 200 and "DUPLICATE" in steps["replay"]["detail"]
    assert steps["logs"]["consumer_statuses"] == {"ACCEPTED": 1, "DUPLICATE": 1}
    assert steps["alarms"]["alarms"] == {a: "OK" for a in sorted(cd.ALARMS)}
    assert steps["alarms"]["cloudwatch_request_id"] == "dry-run-cloudwatch"
    text = path.read_text()
    assert cd.DRY_KEY not in text and "sha256=" not in text and "dry-webhook.lambda-url" not in text
    out = capsys.readouterr().out
    assert "PASSED" in out and "step       result  detail" in out and "bad_hmac   pass" in out
    assert "Recent consumer log lines:" in out and '"status": "DUPLICATE"' in out


def test_a_dry_run_is_never_written_to_evidence():
    with pytest.raises(SystemExit):
        cd.main(["--stack", "shopfront", "--dry-run", "--out", str(cd.EVIDENCE)])


def test_account_filter_keeps_the_check_when_the_stack_names_an_account(tmp_path):
    with cd.rehearsal(account_id="ava1.ring.account.SHOP", device_id="ava1.ring.device.DOOR") as (clients, http, tick):
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    assert c.ok, c.record
    web = next(s for s in c.record["steps"] if s["step"] == "webhook")
    assert web["account"] == cd.h("ava1.ring.account.SHOP") and "ava1" not in json.dumps(c.record)


def test_a_consumer_that_never_runs_fails_stored_and_logs():
    with cd.rehearsal() as (clients, http, _tick):
        t0 = time.monotonic()
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=0.2, every=0.05, dry_run=True,
                   tick=lambda: None)
    assert time.monotonic() - t0 < 5
    steps = {s["step"]: s for s in c.record["steps"]}
    assert not c.ok and not steps["stored"]["ok"] and not steps["logs"]["ok"]
    assert "nothing stored" in steps["stored"]["detail"] and "cleanup" not in steps


def test_error_lines_in_any_log_group_fail_the_check():
    with cd.rehearsal() as (clients, http, tick):
        logs = clients("logs")
        logs.put_log_events(logGroupName="/shopfront/dry/public-demo", logStreamName="dry", logEvents=[
            {"timestamp": int(time.time() * 1000), "message": json.dumps({"level": "ERROR", "message": "boom"})}])
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    logs_step = next(s for s in c.record["steps"] if s["step"] == "logs")
    assert not logs_step["ok"] and logs_step["error_lines"] == {"demo": 1}


def test_an_empty_hmac_secret_stops_before_sending():
    with cd.rehearsal() as (clients, http, tick):
        sm = clients("secretsmanager")
        arn = sm.list_secrets()["SecretList"][0]["ARN"]
        sm.put_secret_value(SecretId=arn, SecretString=json.dumps({"RING_HMAC_KEY": ""}))
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    web = next(s for s in c.record["steps"] if s["step"] == "webhook")
    assert not web["ok"] and "still empty" in web["detail"]


def test_write_refuses_a_record_with_a_signature(tmp_path):
    c = cd.Check("s", "us-east-1", True)
    c.step("webhook", True, "X-Signature sha256=abc")
    with pytest.raises(RuntimeError):
        cd.write(c, tmp_path)


# ---- the failure paths and the alarms, on botocore's Stubber and httpx's MockTransport -------------------

def test_a_webhook_url_that_takes_anything_fails_both_refusal_probes():
    seen = []

    def lax(req: httpx.Request) -> httpx.Response:
        seen.append((req.content, req.headers["x-signature"]))
        return httpx.Response(200, json={"status": "QUEUED"}, headers={"x-amzn-RequestId": "r"})
    with httpx.Client(transport=httpx.MockTransport(lax)) as http:
        got = cd.failure_probes(http, "https://hook.example/webhooks/ring", b'{"a": 1}', "sha256=good", "sha256=mal")
    assert [n for n, *_ in cd.PROBES] == list(got) == ["bad_hmac", "malformed", "replay"]
    assert not got["bad_hmac"][0] and "want 401 BAD_SIGNATURE" in got["bad_hmac"][1]
    assert not got["malformed"][0] and got["replay"][0]
    # the bad probe is the same body under another signature; the replay is byte-for-byte the first POST
    assert seen[0][0] == b'{"a": 1}' and seen[0][1] not in ("sha256=good", "sha256=mal")
    assert seen[1] == (cd.MALFORMED_BODY, "sha256=mal") and seen[2] == (b'{"a": 1}', "sha256=good")


def test_a_webhook_url_that_cant_be_reached_fails_each_probe_by_name():
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)
    with httpx.Client(transport=httpx.MockTransport(down)) as http:
        got = cd.failure_probes(http, "https://hook.example/webhooks/ring", b"{}", "s", "m")
    assert all(not ok and detail == "POST failed: ConnectError" for ok, detail, _ in got.values())


def _clients():
    kw = {"region_name": "us-east-1", "aws_access_key_id": "x", "aws_secret_access_key": "x"}
    return boto3.client("cloudformation", **kw), boto3.client("cloudwatch", **kw)


def _resource(logical, physical, kind="AWS::CloudWatch::Alarm"):
    return {"LogicalResourceId": logical, "PhysicalResourceId": physical, "ResourceType": kind,
            "LastUpdatedTimestamp": datetime.now(timezone.utc), "ResourceStatus": "CREATE_COMPLETE"}


def test_alarm_states_follow_the_stacks_resources_across_pages():
    cfn, cw = _clients()
    with Stubber(cfn) as s_cfn, Stubber(cw) as s_cw:
        s_cfn.add_response("list_stack_resources", {"StackResourceSummaries": [
            _resource("HmacFailureAlarm", "shopfront-HmacFailureAlarm-AB12"),
            _resource("WebhookQueue", "https://sqs/queue", "AWS::SQS::Queue")], "NextToken": "p2"},
            {"StackName": "shopfront"})
        s_cfn.add_response("list_stack_resources", {"StackResourceSummaries": [
            _resource("DeadLetterQueueAlarm", "shopfront-DeadLetterQueueAlarm-CD34"),
            _resource("DemoThrottlesAlarm", "shopfront-DemoThrottlesAlarm-EF56")]},
            {"StackName": "shopfront", "NextToken": "p2"})
        s_cw.add_response("describe_alarms", {"MetricAlarms": [
            {"AlarmName": "shopfront-HmacFailureAlarm-AB12", "StateValue": "ALARM"},
            {"AlarmName": "shopfront-DeadLetterQueueAlarm-CD34", "StateValue": "INSUFFICIENT_DATA"}],
            "ResponseMetadata": {"RequestId": "cw-1", "HTTPStatusCode": 200, "HTTPHeaders": {}, "RetryAttempts": 0}},
            {"AlarmNames": ["shopfront-DeadLetterQueueAlarm-CD34", "shopfront-DemoThrottlesAlarm-EF56",
                            "shopfront-HmacFailureAlarm-AB12"]})
        states, rid = cd.alarm_states(cfn, cw, "shopfront")
        s_cfn.assert_no_pending_responses()
        s_cw.assert_no_pending_responses()
    assert rid == "cw-1" and states == {"DeadLetterQueueAlarm": "INSUFFICIENT_DATA", "DemoThrottlesAlarm": "MISSING",
                                        "HmacFailureAlarm": "ALARM"}


def test_an_alarm_in_alarm_fails_the_check_and_is_named(tmp_path):
    states = {a: "OK" for a in cd.ALARMS} | {"DeadLetterQueueAlarm": "ALARM", "DemoThrottlesAlarm": "INSUFFICIENT_DATA"}
    with cd.rehearsal(alarms=states) as (clients, http, tick):
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    step = next(s for s in c.record["steps"] if s["step"] == "alarms")
    assert not c.ok and not step["ok"] and step["detail"].endswith("not OK: DeadLetterQueueAlarm ALARM")
    assert "1 INSUFFICIENT_DATA" in step["detail"]
    [row] = [r for r in cd.summary_table(c).splitlines() if r.startswith("alarms ")]
    assert row.split()[1] == "FAIL"


def test_a_stack_with_no_alarms_fails_the_alarm_step():
    with cd.rehearsal(alarms={}) as (clients, http, tick):
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    step = next(s for s in c.record["steps"] if s["step"] == "alarms")
    assert not step["ok"] and step["detail"] == "the stack has no CloudWatch alarms"


def test_only_the_probes_own_bad_signature_line_is_forgiven():
    with cd.rehearsal() as (clients, http, tick):
        clients("logs").put_log_events(logGroupName="/shopfront/dry/ring-webhook", logStreamName="dry", logEvents=[
            {"timestamp": int(time.time() * 1000), "message": json.dumps({"level": "INFO",
                                                                         "message": '{"status": "BAD_SIGNATURE"}'})}])
        c = cd.run("shopfront", clients, http, region="us-east-1", timeout=1, every=0.01, dry_run=True, tick=tick)
    steps = {s["step"]: s for s in c.record["steps"]}
    assert steps["bad_hmac"]["ok"] and not steps["logs"]["ok"] and steps["logs"]["error_lines"] == {"webhook": 2}
