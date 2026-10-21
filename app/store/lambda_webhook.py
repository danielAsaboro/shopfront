"""AWS Lambda for Ring's webhooks: `POST /webhooks/ring` on a Lambda Function URL (infra/template.yaml).

Ring delivers webhooks to a public HTTPS URL and expects a 2xx within 5 seconds (API reference,
Notifications > Webhook Response Handling); a shop laptop has no such URL. This function is that URL, and
it does only what has to happen before the answer: the size cap, `app.ring.webhooks.check` (the same
HMAC-SHA256 over the raw body as the app's own route, which also pulls out `meta.request_id`, the dedupe
key), and one SQS SendMessage. The queue's consumer (app/store/lambda_consumer.py) does the device/account
filtering and the DynamoDB conditional writes, so a slow or throttled table never makes Ring wait, retry or
disable the endpoint; a message the consumer can't store goes to the dead-letter queue after 5 tries.

Answers:
- not `POST /webhooks/ring`: 404 / 405
- body over MAX_BODY (64 KB): 413, before any HMAC
- no RING_HMAC_KEY in Secrets Manager yet: 503 (Ring retries 5xx, so nothing is lost while it's set)
- missing or wrong signature: 401 (logged as BAD_SIGNATURE: the HmacFailures metric filter counts these)
- signed but not a v1.1 payload: 400
- SQS unreachable: 503, so Ring retries
- otherwise: 200 QUEUED. Whether the event is stored, a duplicate or another shop's is the consumer's log.

Replays (signed with the replay-only key, or sent with X-Shopfront-Replay: 1) carry `"replayed": true` in the
row and a `replayed=1` message attribute, and the consumer stores them as replays.

Configuration (environment, set by the template): QUEUE_URL, HMAC_SECRET_ARN. The HMAC key itself is only
ever in Secrets Manager.

Logs one JSON line per request (status, request_id, type, replayed), never the body or IDs of people.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from app.ring.webhooks import MAX_BODY, WebhookStatus, check

ROUTE = "/webhooks/ring"
CODES = {WebhookStatus.MALFORMED: 400, WebhookStatus.BAD_SIGNATURE: 401, WebhookStatus.NOT_CONFIGURED: 503}
KEY_TTL = 300  # seconds a fetched HMAC key is reused; a rotated key is picked up within five minutes

log = logging.getLogger("shopfront.webhook")
log.setLevel(logging.INFO)


def respond(code: int, status: str) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"},
            "body": json.dumps({"status": status})}


def raw_body(event: dict) -> bytes | None:
    """The exact bytes Ring sent (the HMAC is over them). None if the encoding is broken."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            return None
    return body.encode()


class EventQueue:
    """The SQS queue between this function and the consumer. `client` is a boto3 SQS client."""

    def __init__(self, client: Any, url: str):
        self.client = client
        self.url = url

    def send(self, row: dict, trace: str = "") -> str:
        """Enqueue one checked row. `trace` is the X-Ray trace header (`_X_AMZN_TRACE_ID`), passed as the
        message's AWSTraceHeader so the consumer's segment joins this request's trace."""
        attrs = {"replayed": {"DataType": "String", "StringValue": "1" if row.get("replayed") else "0"}}
        if str(row.get("request_id") or ""):
            attrs["request_id"] = {"DataType": "String", "StringValue": str(row["request_id"])}  # the dedupe key
        extra = ({"MessageSystemAttributes": {"AWSTraceHeader": {"DataType": "String", "StringValue": trace}}}
                 if trace else {})
        out = self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(row),
                                       MessageAttributes=attrs, **extra)
        return str(out.get("MessageId", ""))


def handle(event: dict, *, key: Callable[[], str], queue: EventQueue, trace: str = "") -> dict:
    """One Function URL request (payload format 2.0). `key` is called only for a POST to the route
    with a body of acceptable size, so stray requests cost no Secrets Manager call."""
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    if event.get("rawPath") != ROUTE:
        return respond(404, "NOT_FOUND")
    if method != "POST":
        return respond(405, "METHOD_NOT_ALLOWED")
    raw = raw_body(event)
    if raw is None:
        return respond(400, WebhookStatus.MALFORMED.value)
    if len(raw) > MAX_BODY:
        log.info(json.dumps({"status": "TOO_LARGE", "bytes": len(raw)}))
        return respond(413, "TOO_LARGE")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    status, row = check(raw, headers.get("x-signature"), key(),
                        replayed=headers.get("x-shopfront-replay") == "1")
    if row is None:
        log.info(json.dumps({"status": status.value}))
        return respond(CODES[status], status.value)
    facts = {"request_id": row["request_id"], "type": row["type"], "replayed": bool(row.get("replayed"))}
    try:
        queue.send(row, trace)
    except Exception as exc:  # any SDK or network failure: Ring retries a 5xx, so the event isn't lost
        log.error(json.dumps({"status": "QUEUE_UNAVAILABLE", "error": type(exc).__name__, **facts}))
        return respond(503, "QUEUE_UNAVAILABLE")
    log.info(json.dumps({"status": "QUEUED", **facts}))
    return respond(200, "QUEUED")


# ---- the Lambda entry point: clients and the key are built once per container --------------------------
_clients: dict[str, Any] = {}
_key: dict[str, Any] = {"value": "", "at": 0.0}


def _client(name: str) -> Any:
    if name not in _clients:
        import boto3  # in the Lambda runtime; not packaged

        _clients[name] = boto3.client(name)
    return _clients[name]


def secret_key(secrets: Any, arn: str, now: float | None = None) -> str:
    """RING_HMAC_KEY from the Secrets Manager secret: JSON {"RING_HMAC_KEY": "..."} or the bare key.
    A found key is reused for KEY_TTL seconds; an empty one is looked up again next time."""
    now = time.time() if now is None else now
    if _key["value"] and now - _key["at"] < KEY_TTL:
        return str(_key["value"])
    value = secrets.get_secret_value(SecretId=arn).get("SecretString") or ""
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    found = str(parsed.get("RING_HMAC_KEY") or "") if isinstance(parsed, dict) else str(parsed)
    _key.update(value=found.strip(), at=now)
    return _key["value"]


def handler(event: dict, context: Any) -> dict:
    env = os.environ
    return handle(event, key=lambda: secret_key(_client("secretsmanager"), env["HMAC_SECRET_ARN"]),
                  queue=EventQueue(_client("sqs"), env["QUEUE_URL"]), trace=env.get("_X_AMZN_TRACE_ID", ""))
