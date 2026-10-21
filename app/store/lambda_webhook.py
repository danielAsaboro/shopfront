"""AWS Lambda for Ring's webhooks: `POST /webhooks/ring` on a Lambda Function URL (infra/template.yaml).

Ring delivers webhooks to a public HTTPS URL (API reference, Notifications), which a shop laptop doesn't
have. This function is that URL. It runs the same code as the app's own route (app/main.py):
`app.ring.webhooks.check` verifies the HMAC-SHA256 `X-Signature` over the raw body and builds the row, then
`app.store.dynamodb.EventTable` stores it once per `meta.request_id`.

Answers, as the app's route gives them:
- not `POST /webhooks/ring`: 404 / 405
- body over MAX_BODY (64 KB): 413, before any HMAC
- no RING_HMAC_KEY in Secrets Manager yet: 503 (Ring retries 5xx, so nothing is lost while it's set)
- missing or wrong signature: 401; signed but not a v1.1 payload: 400
- another device or Ring account than RING_DEVICE_ID / RING_ACCOUNT_ID: 200 IGNORED, nothing stored
- a request_id already stored: 200 DUPLICATE

Configuration (environment, set by the template): TABLE_NAME, SHOP_ID, EVENT_DAYS, HMAC_SECRET_ARN,
RING_DEVICE_ID, RING_ACCOUNT_ID. The HMAC key itself is only ever in Secrets Manager.

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
from app.store.dynamodb import DEFAULT_KEEP_DAYS, EventTable

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


def handle(event: dict, *, key: Callable[[], str], table: EventTable,
           device_id: str = "", account_id: str = "") -> dict:
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
    outcome = table.accept(row, device_id, account_id)
    log.info(json.dumps({"status": outcome, "request_id": row["request_id"], "type": row["type"],
                         "replayed": bool(row.get("replayed"))}))
    return respond(200, outcome)


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
    table = EventTable(_client("dynamodb"), env["TABLE_NAME"], env.get("SHOP_ID", "shop"),
                       float(env.get("EVENT_DAYS") or DEFAULT_KEEP_DAYS))
    return handle(event, key=lambda: secret_key(_client("secretsmanager"), env["HMAC_SECRET_ARN"]),
                  table=table, device_id=env.get("RING_DEVICE_ID", ""), account_id=env.get("RING_ACCOUNT_ID", ""))
