"""After `sam deploy`: check the stack works end to end and keep a record of it.

    .venv/bin/python -m scripts.check_deploy --stack shopfront            # against the deployed stack
    .venv/bin/python -m scripts.check_deploy --stack shopfront --dry-run  # rehearsal, in-process (moto)

Steps, each recorded with the AWS request IDs it produced:

1. stack     CloudFormation DescribeStacks: the stack is in a *_COMPLETE state and has its outputs.
2. demo      GET <DemoUrl>healthz answers 200 with {"ok": true}.
3. webhook   POST <WebhookUrl> a signed replay (scripts/feed_webhook.py's payload: signed with the replay-only
             key derived from the HMAC key in Secrets Manager, marked replay inside the signed body), a
             `motion_detected` with sub_type `other_motion`, which Shopfront skips at stage 0 anyway. 200 QUEUED.
4. stored    the queue consumer stored it: the DynamoDB request marker and the event item appear (polled), and
             the stored row says replayed.
5. bad_hmac  the same body with a wrong X-Signature: 401 BAD_SIGNATURE, nothing queued. (One such request is
             below the HmacFailures alarm's default threshold of 5 in 5 minutes, so it doesn't page anyone.)
6. malformed a body signed correctly but not a Ring v1.1 payload: 400 MALFORMED.
7. replay    the step-3 body and signature sent again (what Ring's retry looks like): 200 QUEUED, then the
             consumer logs DUPLICATE for that request_id and the table still holds the one event.
8. logs      CloudWatch Logs since the check started: the webhook function logged QUEUED and the consumer
             ACCEPTED for this request_id, and no log group (webhook, consumer, demo) has an ERROR line,
             FAILED, QUEUE_UNAVAILABLE or BAD_SIGNATURE, other than the one BAD_SIGNATURE step 5 caused.
             The consumer's recent lines are printed and their statuses counted in the record.
9. alarms    CloudFormation ListStackResources names the stack's CloudWatch alarms; DescribeAlarms gives each
             one's state. Passes when none is in ALARM (INSUFFICIENT_DATA is a quiet alarm, not a failure).
10. cleanup  the two items the check stored are deleted again (--keep leaves them), so the owner's door and
             replay banner don't carry the test.

At the end it prints a pass/fail table of the steps.

The record goes to evidence/aws-deploy-<UTC>.json. It holds request IDs, statuses and counts, never the HMAC
key, a token or a signature; the webhook URL's host and any Ring account or device ID are hashed (CLAUDE.md).
A --dry-run builds the stack's pieces in moto (the table, queue, secret and log groups; DescribeStacks is
answered by botocore's Stubber with the stack's outputs), answers the two URLs with the real Lambda handlers in-process, and writes its
record to --out (never evidence/: a rehearsal on mocks is not evidence). Exit 0 when every step passed.

Needs, for a real run: an AWS sign-in that can DescribeStacks and ListStackResources, GetSecretValue on
HmacSecretArn, GetItem and DeleteItem on the table, FilterLogEvents on the three log groups, and CloudWatch
DescribeAlarms (an admin sign-in has them all).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
BAD_LOG_TERMS = ("FAILED", "QUEUE_UNAVAILABLE", "BAD_SIGNATURE")
WRONG_KEY = "check-deploy-wrong-key"  # signs the bad_hmac probe; never a key anything accepts
MALFORMED_BODY = b'{"check_deploy": "signed, but not a Ring v1.1 payload"}'
PROBES = (("bad_hmac", 401, "BAD_SIGNATURE"), ("malformed", 400, "MALFORMED"), ("replay", 200, "QUEUED"))
ALARMS = ("DeadLetterQueueAlarm", "HmacFailureAlarm", "ConsumerErrorsAlarm", "WebhookErrorsAlarm",
          "DemoThrottlesAlarm")  # infra/template.yaml's alarms, for the dry run
CONSUMER_TAIL = 5  # recent consumer log lines printed after the table
NEEDED = ("DemoUrl", "WebhookUrl", "HmacSecretArn", "WebhookTableName", "LogGroupName", "ConsumerLogGroupName",
          "DemoLogGroupName")


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def aws_request_id(resp: dict) -> str | None:
    return (resp.get("ResponseMetadata") or {}).get("RequestId")


def http_request_id(r: httpx.Response) -> str | None:
    return r.headers.get("x-amzn-requestid") or r.headers.get("x-amzn-request-id")


class Check:
    """The run's record. Every step adds one entry; nothing secret goes in."""

    def __init__(self, stack: str, region: str, dry_run: bool):
        self.record: dict[str, Any] = {"kind": "shopfront-aws-deploy-check", "stack": stack, "region": region,
                                       "dry_run": dry_run, "started_at": now_iso(), "steps": []}
        self.consumer_tail: list[str] = []  # printed, not recorded

    def step(self, name: str, ok: bool, detail: str, **facts: Any) -> bool:
        self.record["steps"].append({"step": name, "ok": ok, "detail": detail, **facts})
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        return ok

    @property
    def ok(self) -> bool:
        return bool(self.record["steps"]) and all(s["ok"] for s in self.record["steps"])


def answer_status(r: httpx.Response) -> str | None:
    if not r.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        return r.json().get("status")
    except (ValueError, AttributeError):
        return None


def failure_probes(http: httpx.Client, url: str, raw: bytes, signature: str,
                   malformed_signature: str) -> dict[str, tuple[bool, str, dict]]:
    """POST the webhook URL's failure paths: a wrong signature, a signed body that isn't Ring's, and the
    step-3 body again. {name: (answered as expected, detail, facts)}; the replay's dedupe is checked after."""
    sends = {"bad_hmac": (raw, sign_raw(raw, WRONG_KEY)), "malformed": (MALFORMED_BODY, malformed_signature),
             "replay": (raw, signature)}
    out: dict[str, tuple[bool, str, dict]] = {}
    for name, code, status in PROBES:
        body, sig = sends[name]
        try:
            r = http.post(url, content=body, headers={"X-Signature": sig, "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            out[name] = (False, f"POST failed: {type(exc).__name__}", {})
            continue
        got = answer_status(r)
        out[name] = (r.status_code == code and got == status, f"POST -> {r.status_code} {got} (want {code} {status})",
                     {"status_code": r.status_code, "lambda_request_id": http_request_id(r)})
    return out


def sign_raw(raw: bytes, key: str) -> str:
    from app.ring.webhooks import sign
    return sign(raw, key)


def alarm_states(cfn: Any, cw: Any, stack: str) -> tuple[dict[str, str], str | None]:
    """The stack's CloudWatch alarms by logical ID (DeadLetterQueueAlarm, ...) -> StateValue (OK, ALARM,
    INSUFFICIENT_DATA), and the DescribeAlarms request ID. The template doesn't name its alarms, so their
    physical names come from the stack's resources."""
    names: dict[str, str] = {}
    token = None
    while True:
        page = cfn.list_stack_resources(StackName=stack, **({"NextToken": token} if token else {}))
        for res in page.get("StackResourceSummaries", []):
            if res.get("ResourceType") == "AWS::CloudWatch::Alarm" and res.get("PhysicalResourceId"):
                names[res["PhysicalResourceId"]] = res["LogicalResourceId"]
        token = page.get("NextToken")
        if not token:
            break
    states: dict[str, str] = {}
    request_id, token = None, None
    while names:
        resp = cw.describe_alarms(AlarmNames=sorted(names)[:100], **({"NextToken": token} if token else {}))
        request_id = request_id or aws_request_id(resp)
        for a in resp.get("MetricAlarms", []) + resp.get("CompositeAlarms", []):
            if a.get("AlarmName") in names:
                states[names[a["AlarmName"]]] = a.get("StateValue", "")
        token = resp.get("NextToken")
        if not token:
            break
    for logical in names.values():
        states.setdefault(logical, "MISSING")  # in the stack but DescribeAlarms didn't return it
    return dict(sorted(states.items())), request_id


def log_message(message: str) -> str:
    """The app's own line inside Lambda's JSON log format, else the line as logged."""
    try:
        inner = json.loads(message).get("message")
    except (ValueError, AttributeError):
        inner = None
    return str(inner if inner is not None else message).strip()


def consumer_statuses(messages: list[str]) -> dict[str, int]:
    """ACCEPTED / DUPLICATE / IGNORED / FAILED counts in the consumer's lines (each logs {"status": ...})."""
    counts: dict[str, int] = {}
    for m in messages:
        try:
            status = json.loads(log_message(m)).get("status")
        except (ValueError, AttributeError):
            continue
        if status:
            counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def summary_table(c: Check) -> str:
    """The run as a short pass/fail table."""
    steps = c.record["steps"]
    width = max([len(s["step"]) for s in steps] + [4])
    lines = [f"{'step':<{width}}  result  detail"]
    lines += [f"{s['step']:<{width}}  {'pass' if s['ok'] else 'FAIL':<6}  {s['detail']}" for s in steps]
    return "\n".join(lines)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wait_for(fn: Callable[[], Any], timeout: float, every: float, tick: Callable[[], None] | None = None) -> Any:
    """fn() until it returns something truthy or `timeout` seconds pass. `tick` runs before each try (the
    dry run uses it to let its in-process consumer drain the queue)."""
    end = time.monotonic() + timeout
    while True:
        if tick:
            tick()
        got = fn()
        if got or time.monotonic() >= end:
            return got
        time.sleep(every)


def run(stack: str, clients: Callable[[str], Any], http: httpx.Client, *, region: str, timeout: float = 90,
        every: float = 3.0, keep: bool = False, dry_run: bool = False, tick: Callable[[], None] | None = None) -> Check:
    from app.ring.webhooks import REPLAY_MARKER, replay_key
    from app.store.dynamodb import EventTable
    from scripts.feed_webhook import payload, signed

    c = Check(stack, region, dry_run)
    started_ms = int(time.time() * 1000)

    # 1. the stack and its outputs
    try:
        resp = clients("cloudformation").describe_stacks(StackName=stack)
    except Exception as exc:
        c.step("stack", False, f"DescribeStacks failed: {type(exc).__name__}")
        return c
    st = resp["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}
    params = {p["ParameterKey"]: p.get("ParameterValue", "") for p in st.get("Parameters", [])}
    missing = [k for k in NEEDED if not outputs.get(k)]
    if not c.step("stack", st["StackStatus"].endswith("_COMPLETE") and "ROLLBACK" not in st["StackStatus"]
                  and not missing, f"{st['StackStatus']}" + (f"; missing outputs {missing}" if missing else ""),
                  request_id=aws_request_id(resp), outputs=sorted(outputs)):
        return c

    # 2. the public demo answers
    demo = outputs["DemoUrl"].rstrip("/") + "/healthz"
    try:
        r = http.get(demo)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        c.step("demo", r.status_code == 200 and body.get("ok") is True, f"GET /healthz -> {r.status_code}",
               url=demo, lambda_request_id=http_request_id(r))
    except httpx.HTTPError as exc:
        c.step("demo", False, f"GET /healthz failed: {type(exc).__name__}", url=demo)

    # 3. a signed replay to the webhook URL
    try:
        secret = clients("secretsmanager").get_secret_value(SecretId=outputs["HmacSecretArn"])
        raw_secret = secret.get("SecretString") or ""
        try:
            key = str(json.loads(raw_secret).get("RING_HMAC_KEY") or "")
        except (ValueError, AttributeError):
            key = raw_secret
    except Exception as exc:
        c.step("webhook", False, f"couldn't read the HMAC key: {type(exc).__name__}")
        return c
    if not key.strip():
        c.step("webhook", False, "the HMAC secret is still empty: put RING_HMAC_KEY in it first (docs/aws.md)")
        return c
    device = params.get("RingDeviceId") or "check-deploy-doorbell"
    body = payload("motion_detected", datetime.now(timezone.utc), device, "other_motion")
    rid = f"check-deploy-{uuid.uuid4().hex[:12]}"
    body["meta"]["request_id"] = rid
    if params.get("RingAccountId"):  # so the consumer's account filter keeps it; it is still marked a replay
        body["meta"]["account_id"] = params["RingAccountId"]
    assert body["meta"][REPLAY_MARKER] is True
    raw, signature = signed(body, key.strip())
    malformed_signature = sign_raw(MALFORMED_BODY, replay_key(key.strip()))
    del key, raw_secret, secret
    ms = body["data"]["attributes"]["timestamp"]
    try:
        r = http.post(outputs["WebhookUrl"], content=raw,
                      headers={"X-Signature": signature, "Content-Type": "application/json"})
        answer = r.json().get("status") if r.headers.get("content-type", "").startswith("application/json") else None
    except (httpx.HTTPError, ValueError) as exc:
        c.step("webhook", False, f"POST failed: {type(exc).__name__}", webhook_host=h(urlsplit(outputs["WebhookUrl"]).netloc))
        return c
    if not c.step("webhook", r.status_code == 200 and answer == "QUEUED", f"POST -> {r.status_code} {answer}",
                  webhook_host=h(urlsplit(outputs["WebhookUrl"]).netloc), lambda_request_id=http_request_id(r),
                  ring_request_id=rid, account=h(body["meta"]["account_id"]), device=h(device)):
        return c

    # 4. the consumer stored it
    ddb, table = clients("dynamodb"), outputs["WebhookTableName"]
    shop = params.get("ShopId") or "shop"
    et = EventTable(ddb, table, shop)
    pk, sk = et.keys({"timestamp": ms, "request_id": rid})
    marker_key = {"pk": {"S": f"{shop}#request#{rid}"}, "sk": {"S": "request"}}
    event_key = {"pk": {"S": pk}, "sk": {"S": sk}}

    def stored():
        got = ddb.get_item(TableName=table, Key=event_key, ConsistentRead=True)
        return got if got.get("Item") else None
    got = wait_for(stored, timeout, every, tick)
    marker = ddb.get_item(TableName=table, Key=marker_key, ConsistentRead=True)
    row = json.loads(got["Item"]["row"]["S"]) if got else {}
    c.step("stored", bool(got and marker.get("Item") and row.get("replayed") is True and row.get("request_id") == rid),
           "event and request marker in DynamoDB, stored as a replay" if got else
           f"nothing stored for {rid} after {timeout:.0f} s (consumer IGNORED it, failed, or is behind)",
           dynamodb_request_id=aws_request_id(got or marker), shop=shop)

    logs = clients("logs")
    groups = {"webhook": outputs["LogGroupName"], "consumer": outputs["ConsumerLogGroupName"],
              "demo": outputs["DemoLogGroupName"]}

    def events(group: str) -> list[dict]:
        out, token = [], None
        while True:
            kw = {"logGroupName": group, "startTime": started_ms - 60_000, **({"nextToken": token} if token else {})}
            try:
                page = logs.filter_log_events(**kw)
            except logs.exceptions.ResourceNotFoundException:
                return out
            out += page.get("events", [])
            token = page.get("nextToken")
            if not token:
                return out

    # 5-7. the failure paths: a wrong signature and a body that isn't Ring's are refused; the same body again is
    # queued (the Function URL answers fast) and the consumer stores it once
    probes = failure_probes(http, outputs["WebhookUrl"], raw, signature, malformed_signature)
    for name in ("bad_hmac", "malformed"):
        ok, detail, facts = probes[name]
        c.step(name, ok, detail, **facts)
    replay_ok, replay_detail, replay_facts = probes["replay"]

    def deduped():
        return [e for e in events(groups["consumer"]) if rid in e["message"] and "DUPLICATE" in e["message"]]
    dup = wait_for(deduped, timeout, every, tick) if replay_ok else []
    again = ddb.get_item(TableName=table, Key=event_key, ConsistentRead=True) if dup else {}
    c.step("replay", bool(replay_ok and dup and again.get("Item")),
           replay_detail + ("; consumer logged DUPLICATE, the table holds one event" if dup and again.get("Item") else
                            "; consumer stored it once but the event is gone" if dup else
                            f"; no DUPLICATE line for {rid} after {timeout:.0f} s" if replay_ok else ""),
           **replay_facts)

    # 8. the logs
    def ours_logged():
        w = [e for e in events(groups["webhook"]) if rid in e["message"] and "QUEUED" in e["message"]]
        k = [e for e in events(groups["consumer"]) if rid in e["message"] and "ACCEPTED" in e["message"]]
        return (w, k) if w and k else None
    found = wait_for(ours_logged, timeout, every, tick) or ([], [])
    bad, consumer_lines = {}, []
    expected_bad_signatures = 1 if probes["bad_hmac"][2].get("status_code") == 401 else 0
    for name, group in groups.items():
        lines = [e["message"] for e in events(group)]
        if name == "consumer":
            consumer_lines = lines
        hits = [m for m in lines if '"level": "ERROR"' in m or '"level":"ERROR"' in m
                or any(t in m for t in BAD_LOG_TERMS)]
        if name == "webhook":  # the bad_hmac probe's own line is expected; any more is someone else's
            probe_lines = [m for m in hits if "BAD_SIGNATURE" in m and "ERROR" not in m]
            if len(probe_lines) <= expected_bad_signatures:
                hits = [m for m in hits if m not in probe_lines]
        if hits:
            bad[name] = len(hits)
    c.step("logs", bool(found[0] and found[1]) and not bad,
           ("QUEUED and ACCEPTED logged for this request" if found[0] and found[1] else
            "this request's QUEUED/ACCEPTED lines not found") + (f"; error lines: {bad}" if bad else "; no errors"),
           log_lambda_request_ids=sorted({lambda_rid(e["message"]) for e in found[0] + found[1]} - {None}),
           error_lines=bad, consumer_statuses=consumer_statuses(consumer_lines))
    c.consumer_tail = [log_message(m)[:200] for m in consumer_lines[-CONSUMER_TAIL:]]

    # 9. the alarms
    try:
        states, cw_rid = alarm_states(clients("cloudformation"), clients("cloudwatch"), stack)
    except Exception as exc:
        c.step("alarms", False, f"couldn't read the alarms: {type(exc).__name__}")
    else:
        firing = sorted(k for k, v in states.items() if v != "OK" and v != "INSUFFICIENT_DATA")
        counts: dict[str, int] = {}
        for v in states.values():
            counts[v] = counts.get(v, 0) + 1
        c.step("alarms", bool(states) and not firing,
               (f"{len(states)} alarms: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
                if states else "the stack has no CloudWatch alarms")
               + (f"; not OK: {', '.join(f'{k} {states[k]}' for k in firing)}" if firing else ""),
               alarms=states, cloudwatch_request_id=cw_rid)

    # 6. clean up
    if keep:
        c.step("cleanup", True, "--keep: the replayed event stays in the table")
    elif got or marker.get("Item"):
        d1 = ddb.delete_item(TableName=table, Key=event_key)
        ddb.delete_item(TableName=table, Key=marker_key)
        c.step("cleanup", True, "deleted the check's event and marker", dynamodb_request_id=aws_request_id(d1))
    return c


def lambda_rid(message: str) -> str | None:
    """Lambda's JSON log format carries the invocation's requestId on every line."""
    try:
        return json.loads(message).get("requestId")
    except (ValueError, AttributeError):
        return None


def write(c: Check, out: Path | None) -> Path:
    c.record.update(finished_at=now_iso(), ok=c.ok)
    text = json.dumps(c.record, indent=1)
    if any(word in text for word in ("RING_HMAC_KEY", "sha256=")):  # belt and braces: no key, no signature
        raise RuntimeError("refusing to write: the record would hold a key or signature")
    if out is None:
        EVIDENCE.mkdir(exist_ok=True)
        out = EVIDENCE / f"aws-deploy-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    elif out.is_dir():
        out = out / f"aws-deploy-dry-run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(text + "\n")
    return out


# ---- the dry run: the stack's pieces in moto, the two URLs answered by the real handlers in-process --------
DRY_KEY = "dry-run-hmac-key"


@contextmanager
def rehearsal(stack_name: str = "shopfront", region: str = "us-east-1", account_id: str = "", device_id: str = "",
              alarms: dict[str, str] | None = None):
    """Yields (clients, http, tick) wired to an in-process copy of the stack. `alarms` is each alarm's state
    (logical ID -> OK | ALARM | INSUFFICIENT_DATA), answered by botocore's Stubber; default: the template's
    five, all OK."""
    import os

    import boto3
    from moto import mock_aws

    from app.store import lambda_consumer, lambda_webhook
    from app.store.dynamodb import EventTable

    env = {"AWS_ACCESS_KEY_ID": "dry", "AWS_SECRET_ACCESS_KEY": "dry", "AWS_DEFAULT_REGION": region,
           "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with ExitStack() as stack:
            stack.enter_context(mock_aws())

            from botocore.stub import Stubber

            def client(name: str):
                return {"cloudformation": cfn, "cloudwatch": cw}.get(name) or boto3.client(name, region_name=region)
            sm, sqs, ddb, logs = (boto3.client(n, region_name=region) for n in ("secretsmanager", "sqs", "dynamodb", "logs"))
            # moto's CloudFormation needs extras the app doesn't install; DescribeStacks is answered by botocore's
            # Stubber, in the documented response shape, with the outputs and parameters of the stack built here.
            cfn = boto3.client("cloudformation", region_name=region)
            secret = sm.create_secret(Name="dry-hmac", SecretString=json.dumps({"RING_HMAC_KEY": DRY_KEY}))["ARN"]
            queue = sqs.create_queue(QueueName="dry-webhooks")["QueueUrl"]
            ddb.create_table(TableName="dry-webhooks", BillingMode="PAY_PER_REQUEST",
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                   {"AttributeName": "sk", "AttributeType": "S"}],
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                        {"AttributeName": "sk", "KeyType": "RANGE"}])
            groups = {"webhook": "/shopfront/dry/ring-webhook", "consumer": "/shopfront/dry/ring-webhook-consumer",
                      "demo": "/shopfront/dry/public-demo"}
            for g in groups.values():
                logs.create_log_group(logGroupName=g)
                logs.create_log_stream(logGroupName=g, logStreamName="dry")
            outputs = {"DemoUrl": "https://dry-demo.lambda-url.us-east-1.on.aws/",
                       "WebhookUrl": "https://dry-webhook.lambda-url.us-east-1.on.aws/webhooks/ring",
                       "HmacSecretArn": secret, "WebhookTableName": "dry-webhooks", "LogGroupName": groups["webhook"],
                       "ConsumerLogGroupName": groups["consumer"], "DemoLogGroupName": groups["demo"]}
            stub = Stubber(cfn)
            stub.add_response("describe_stacks", {"Stacks": [{
                "StackName": stack_name, "CreationTime": datetime.now(timezone.utc), "StackStatus": "CREATE_COMPLETE",
                "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
                "Parameters": [{"ParameterKey": "ShopId", "ParameterValue": "shop"},
                               {"ParameterKey": "RingAccountId", "ParameterValue": account_id},
                               {"ParameterKey": "RingDeviceId", "ParameterValue": device_id}]}],
                "ResponseMetadata": {"RequestId": "dry-run-cfn", "HTTPStatusCode": 200, "HTTPHeaders": {}, "RetryAttempts": 0}},
                {"StackName": stack_name})
            states = alarms if alarms is not None else {a: "OK" for a in ALARMS}
            physical = {logical: f"{stack_name}-{logical}-DRYRUN" for logical in states}
            meta = {"HTTPStatusCode": 200, "HTTPHeaders": {}, "RetryAttempts": 0}
            stub.add_response("list_stack_resources", {"StackResourceSummaries": [
                {"LogicalResourceId": logical, "PhysicalResourceId": name, "ResourceType": "AWS::CloudWatch::Alarm",
                 "LastUpdatedTimestamp": datetime.now(timezone.utc), "ResourceStatus": "CREATE_COMPLETE"}
                for logical, name in physical.items()], "ResponseMetadata": {"RequestId": "dry-run-cfn-2", **meta}},
                {"StackName": stack_name})
            stack.enter_context(stub)
            cw = boto3.client("cloudwatch", region_name=region)
            cw_stub = Stubber(cw)
            if physical:
                cw_stub.add_response("describe_alarms", {"MetricAlarms": [
                    {"AlarmName": physical[logical], "StateValue": state} for logical, state in states.items()],
                    "ResponseMetadata": {"RequestId": "dry-run-cloudwatch", **meta}},
                    {"AlarmNames": sorted(physical.values())})
            stack.enter_context(cw_stub)

            def ship(group: str, records: list[logging.LogRecord], request_id: str) -> None:
                """What Lambda's JSON log format would put in CloudWatch for these log calls."""
                if records:
                    logs.put_log_events(logGroupName=group, logStreamName="dry", logEvents=[
                        {"timestamp": int(r.created * 1000), "message": json.dumps(
                            {"level": r.levelname, "message": r.getMessage(), "requestId": request_id})} for r in records])

            @contextmanager
            def captured(name: str):
                records: list[logging.LogRecord] = []

                class Keep(logging.Handler):
                    def emit(self, record):
                        records.append(record)
                handler, logger = Keep(), logging.getLogger(name)
                logger.addHandler(handler)
                try:
                    yield records
                finally:
                    logger.removeHandler(handler)

            def answer(req: httpx.Request) -> httpx.Response:
                rid = str(uuid.uuid4())
                if req.url.host == "dry-demo.lambda-url.us-east-1.on.aws" and req.url.path == "/healthz":
                    return httpx.Response(200, json={"ok": True, "ring": "synthetic"}, headers={"x-amzn-RequestId": rid})
                if req.url.host == "dry-webhook.lambda-url.us-east-1.on.aws":
                    event = {"rawPath": req.url.path, "requestContext": {"http": {"method": req.method}},
                             "headers": dict(req.headers), "body": req.content.decode(), "isBase64Encoded": False}
                    lambda_webhook._key.update(value="", at=0.0)
                    with captured("shopfront.webhook") as records:
                        out = lambda_webhook.handle(event, key=lambda: lambda_webhook.secret_key(sm, secret),
                                                    queue=lambda_webhook.EventQueue(sqs, queue))
                    ship(groups["webhook"], records, rid)
                    return httpx.Response(out["statusCode"], content=out["body"].encode(),
                                          headers={**out["headers"], "x-amzn-RequestId": rid})
                return httpx.Response(404)

            def tick() -> None:
                """The consumer Lambda's event source: drain the queue into the table."""
                got = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10, MessageAttributeNames=["All"])
                msgs = got.get("Messages", [])
                if not msgs:
                    return
                event = {"Records": [{"messageId": m["MessageId"], "body": m["Body"],
                                      "messageAttributes": {k: {"stringValue": v.get("StringValue")}
                                                            for k, v in (m.get("MessageAttributes") or {}).items()}}
                                     for m in msgs]}
                with captured("shopfront.webhook.consumer") as records:
                    lambda_consumer.consume(event, shop_for=lambda_consumer.single_shop("shop", device_id, account_id),
                                            table_for=lambda shop: EventTable(ddb, "dry-webhooks", shop))
                ship(groups["consumer"], records, str(uuid.uuid4()))
                for m in msgs:
                    sqs.delete_message(QueueUrl=queue, ReceiptHandle=m["ReceiptHandle"])

            http = httpx.Client(transport=httpx.MockTransport(answer))
            try:
                yield client, http, tick
            finally:
                http.close()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--stack", required=True, help="the stack name you deployed (sam deploy --guided asked for it)")
    ap.add_argument("--region", help="the stack's region (default: your AWS sign-in's)")
    ap.add_argument("--timeout", type=float, default=90, help="seconds to wait for the consumer and the logs")
    ap.add_argument("--keep", action="store_true", help="leave the check's replayed event in the table")
    ap.add_argument("--dry-run", action="store_true", help="rehearse against moto, in-process; never writes evidence/")
    ap.add_argument("--out", type=Path, help="where to write the record (a file or a folder); dry run default: ./")
    a = ap.parse_args(argv)
    if a.dry_run:
        with rehearsal(a.stack) as (clients, http, tick):
            c = run(a.stack, clients, http, region="us-east-1", timeout=min(a.timeout, 5), every=0.05,
                    keep=a.keep, dry_run=True, tick=tick)
        out = a.out or Path(".")
        if out.resolve() == EVIDENCE.resolve() or EVIDENCE.resolve() in out.resolve().parents:
            ap.error("a dry run is not evidence: write it somewhere other than evidence/")
    else:
        import boto3
        session = boto3.Session(region_name=a.region)
        region = session.region_name or "us-east-1"
        with httpx.Client(timeout=15) as http:
            c = run(a.stack, lambda name: session.client(name, region_name=region), http, region=region,
                    timeout=a.timeout, keep=a.keep)
        out = a.out
    path = write(c, out)
    print("\n" + summary_table(c))
    if c.consumer_tail:
        print("\nRecent consumer log lines:")
        print("\n".join(f"  {line}" for line in c.consumer_tail))
    print(f"\n{'PASSED' if c.ok else 'FAILED'}; record: {path}")
    return 0 if c.ok else 1


if __name__ == "__main__":
    sys.exit(main())
