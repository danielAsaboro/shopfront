"""scripts/check_deploy.py, rehearsed in-process: moto for the stack's pieces, the real Lambda handlers for the
two URLs. These prove the script's logic, not a deployed stack."""

import json
import time

import pytest

from scripts import check_deploy as cd


def test_dry_run_passes_and_writes_a_record_without_secrets(tmp_path, capsys):
    assert cd.main(["--stack", "shopfront", "--dry-run", "--out", str(tmp_path)]) == 0
    [path] = list(tmp_path.glob("aws-deploy-dry-run-*.json"))
    rec = json.loads(path.read_text())
    assert rec["ok"] and rec["dry_run"] and [s["step"] for s in rec["steps"]] == [
        "stack", "demo", "webhook", "stored", "logs", "cleanup"]
    steps = {s["step"]: s for s in rec["steps"]}
    assert steps["demo"]["lambda_request_id"] and steps["webhook"]["lambda_request_id"]
    assert steps["webhook"]["ring_request_id"].startswith("check-deploy-")
    assert steps["stored"]["dynamodb_request_id"] and len(steps["logs"]["log_lambda_request_ids"]) == 2
    text = path.read_text()
    assert cd.DRY_KEY not in text and "sha256=" not in text and "dry-webhook.lambda-url" not in text
    assert "PASSED" in capsys.readouterr().out


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
