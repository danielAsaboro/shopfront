"""AWS Lambda that drains the webhook queue into DynamoDB (infra/template.yaml, WebhookConsumerFunction).

The Function URL handler (app/store/lambda_webhook.py) answers Ring as soon as a checked row is on the SQS
queue. This function takes the rows off in batches and, for each one:

- names the shop it belongs to from the shop map in config (app/store/shops.py): by default the stack's one
  ShopId, RingDeviceId and RingAccountId (the same filter as the app's local log), or, for several shops, the
  map in the SSM parameter SHOP_MAP_PARAMETER. A row no shop claims is IGNORED, logged and deleted;
- stores it in that shop's partition (`pk` = `<shop_id>#<UTC day>`) with `EventTable.put`: the event and a `meta.request_id` marker in one transaction, each with
  `attribute_not_exists(pk)`, so Ring's retries and SQS's at-least-once redelivery store it once (DUPLICATE).

A record that fails (unreadable body, DynamoDB error) is reported back in `batchItemFailures` (the event
source mapping has ReportBatchItemFailures on), so only that record is retried. After 5 receives SQS moves
it to the dead-letter queue, where the DlqDepth alarm sees it. The rest of the batch is not retried.

Replays stay replays: the row carries `"replayed": true` from the Function URL handler, and a `replayed=1`
message attribute marks it too, in case a row ever arrives without the field.

Configuration (environment, set by the template): TABLE_NAME, EVENT_DAYS, SHOP_ID, RING_DEVICE_ID,
RING_ACCOUNT_ID, and SHOP_MAP_PARAMETER (empty: the single shop). A shop map that can't be read or is
ambiguous fails the whole batch, so nothing is misfiled; SQS retries it and then dead-letters it.
Logs one JSON line per record (status, shop, request_id, type, replayed), never IDs of people.

Like the Function URL handler, it imports only the standard library and app modules that do; boto3 comes
from the Lambda runtime, imported inside the entry point.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from app.store.dynamodb import ACCEPTED, DEFAULT_KEEP_DAYS, DUPLICATE, IGNORED, EventTable
from app.store.shops import ShopMap

MAP_TTL = 300  # seconds a shop map read from SSM is reused; a new shop is picked up within five minutes

log = logging.getLogger("shopfront.webhook.consumer")
log.setLevel(logging.INFO)


class BadRecord(ValueError):
    """A queue record whose body isn't a row the Function URL handler wrote."""


def row_of(record: dict) -> dict:
    """The row in one SQS record (the Lambda event's shape: body, messageAttributes.*.stringValue)."""
    try:
        row = json.loads(record.get("body") or "")
    except ValueError:
        raise BadRecord("body is not JSON")
    if not isinstance(row, dict) or not str(row.get("request_id") or "") or "type" not in row:
        raise BadRecord("body is not a webhook row")
    attrs = record.get("messageAttributes") or {}
    if (attrs.get("replayed") or {}).get("stringValue") == "1":
        row["replayed"] = True
    return row


def consume(event: dict, *, shop_for: Callable[[dict], str | None],
            table_for: Callable[[str], EventTable]) -> dict:
    """One batch from the SQS event source. `shop_for(row)` names the shop a row belongs to (None: not ours);
    `table_for(shop)` is that shop's EventTable. Returns the partial batch response."""
    failures = []
    for record in event.get("Records") or []:
        message_id = record.get("messageId", "")
        receives = (record.get("attributes") or {}).get("ApproximateReceiveCount")
        try:
            row = row_of(record)
            shop = shop_for(row)
            outcome = IGNORED if shop is None else (ACCEPTED if table_for(shop).put(row) else DUPLICATE)
        except Exception as exc:  # this record only; SQS redelivers it, then dead-letters it
            log.error(json.dumps({"status": "FAILED", "message_id": message_id, "receives": receives,
                                  "error": type(exc).__name__}))
            failures.append({"itemIdentifier": message_id})
            continue
        log.info(json.dumps({"status": outcome, "shop": shop, "request_id": row["request_id"], "type": row["type"],
                             "replayed": bool(row.get("replayed"))}))
    return {"batchItemFailures": failures}


def single_shop(shop_id: str, device_id: str = "", account_id: str = "") -> Callable[[dict], str | None]:
    """The one-shop default: every row from the shop's doorbell and account is that shop's."""
    return ShopMap.single(shop_id, device_id, account_id).shop_for


# ---- the Lambda entry point ---------------------------------------------------------------------------
_clients: dict[str, Any] = {}
_map: dict[str, Any] = {"value": None, "at": 0.0}


def _client(name: str) -> Any:
    if name not in _clients:
        import boto3  # in the Lambda runtime; not packaged

        _clients[name] = boto3.client(name)
    return _clients[name]


def shop_map(ssm: Any, env: Any, now: float | None = None) -> ShopMap:
    """The shop map: from the SSM parameter SHOP_MAP_PARAMETER (cached MAP_TTL seconds), or the single shop
    in SHOP_ID / RING_DEVICE_ID / RING_ACCOUNT_ID when that is empty."""
    name = env.get("SHOP_MAP_PARAMETER", "")
    if not name:
        return ShopMap.single(env.get("SHOP_ID", "shop"), env.get("RING_DEVICE_ID", ""), env.get("RING_ACCOUNT_ID", ""))
    now = time.time() if now is None else now
    if _map["value"] is None or now - _map["at"] >= MAP_TTL:
        text = ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        _map.update(value=ShopMap.parse(text), at=now)
    return _map["value"]


def handler(event: dict, context: Any) -> dict:
    env = os.environ
    keep = float(env.get("EVENT_DAYS") or DEFAULT_KEEP_DAYS)
    shops = shop_map(lambda: _client("ssm"), env)  # unreadable or ambiguous: raise, the whole batch retries
    return consume(event, shop_for=shops.shop_for,
                   table_for=lambda shop: EventTable(_client("dynamodb"), env["TABLE_NAME"], shop, keep))
