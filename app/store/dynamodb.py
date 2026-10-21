"""Ring's webhooks in DynamoDB: what the webhook Lambda (infra/template.yaml) writes and the app reads.

Table layout (one table per stack, on-demand, TTL on `expires_at`):

| item | pk | sk | other attributes |
|---|---|---|---|
| event | `<shop>#<YYYY-MM-DD>`, the UTC day of Ring's `data.attributes.timestamp` | `<timestamp ms, 13 digits>#<request_id>` | `row`: the JSON row the local log writes; `expires_at` |
| request marker | `<shop>#request#<request_id>` | `request` | `expires_at` |

The day is the UTC day so the Lambda needs no time-zone setting; the app reads the one or two UTC days its
shop-local day covers, and app/ring/webhooks.py filters to the local day exactly as it does for the file log.

Dedupe on `meta.request_id` (Ring retries a delivery with the same request_id: API reference, Notifications >
Webhook Response Handling): the marker and the event are written in one transaction, each with
`attribute_not_exists(pk)`. A retry cancels the transaction and nothing is written twice.

Only events from the shop's own doorbell and Ring account are stored (`accept`); anything else is answered
200 so Ring neither retries nor disables the endpoint, and is dropped. So `ignored()` is always 0 here.

boto3 is passed in or imported lazily: the Lambda package (infra/package_lambda.py) is stdlib plus the
boto3 the Lambda runtime ships.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.ring.webhooks import WebhookLog, WebhookStatus, check, ours

DEFAULT_KEEP_DAYS = 90
ACCEPTED, DUPLICATE, IGNORED = "ACCEPTED", "DUPLICATE", "IGNORED"


def _ms(value: Any) -> int | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return ms if 0 <= ms < 10**13 else None


class EventTable:
    """One shop's webhook events in the DynamoDB table. `client` is a boto3 DynamoDB client."""

    def __init__(self, client: Any, table: str, shop: str = "shop", keep_days: float = DEFAULT_KEEP_DAYS):
        self.client = client
        self.table = table
        self.shop = shop
        self.keep_days = keep_days

    def day_key(self, d: date) -> str:
        return f"{self.shop}#{d.isoformat()}"

    def keys(self, row: dict, now: float | None = None) -> tuple[str, str]:
        """(pk, sk) for an event row. No usable timestamp: filed under the day it arrived, sorted first."""
        ms = _ms(row.get("timestamp"))
        when = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None
                else datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc))
        return self.day_key(when.date()), f"{ms or 0:013d}#{row['request_id']}"

    def put(self, row: dict, now: float | None = None) -> bool:
        """Store a row once per request_id. False when that request_id is already stored."""
        now = time.time() if now is None else now
        expires = {"N": str(int(now + self.keep_days * 86400))}
        pk, sk = self.keys(row, now)
        marker = {"pk": {"S": f"{self.shop}#request#{row['request_id']}"}, "sk": {"S": "request"},
                  "expires_at": expires}
        event = {"pk": {"S": pk}, "sk": {"S": sk}, "row": {"S": json.dumps(row)}, "expires_at": expires}
        once = "attribute_not_exists(pk)"
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table, "Item": marker, "ConditionExpression": once}},
                {"Put": {"TableName": self.table, "Item": event, "ConditionExpression": once}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons") or []
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                return False
            raise
        return True

    def accept(self, row: dict, device_id: str = "", account_id: str = "") -> str:
        """ACCEPTED (stored), DUPLICATE (request_id seen before) or IGNORED (another device or account)."""
        if not ours(row, device_id, account_id):
            return IGNORED
        return ACCEPTED if self.put(row) else DUPLICATE

    def _pages(self, op: str, **kw: Any) -> list[dict]:
        rows, start = [], None
        while True:
            page = getattr(self.client, op)(TableName=self.table, **kw,
                                            **({"ExclusiveStartKey": start} if start else {}))
            rows += [json.loads(item["row"]["S"]) for item in page.get("Items", []) if "row" in item]
            start = page.get("LastEvaluatedKey")
            if not start:
                return rows

    def query_day(self, d: date) -> list[dict]:
        """Rows filed under one UTC day, in timestamp order."""
        return self._pages("query", KeyConditionExpression="pk = :pk",
                           ExpressionAttributeValues={":pk": {"S": self.day_key(d)}})

    def scan(self) -> list[dict]:
        """Every stored row for this shop. The TTL keeps the table to a few months of one door's events."""
        return self._pages("scan", FilterExpression="begins_with(pk, :shop) AND attribute_exists(#row)",
                           ExpressionAttributeNames={"#row": "row"},
                           ExpressionAttributeValues={":shop": {"S": f"{self.shop}#"}})


def utc_days(day: date, tz: ZoneInfo) -> list[date]:
    """The UTC days a shop-local day overlaps (one or two)."""
    start = datetime.combine(day, datetime.min.time(), tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz) - timedelta(microseconds=1)
    return sorted({start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()})


class DynamoWebhookLog(WebhookLog):
    """WebhookLog backed by the Lambda's table. Same methods and results as the file log, so the app's
    door timeline, receiving desk and /api/webhooks read Ring's webhooks the same way from either.

    It doesn't read a local file, so WebhookLog.__init__ (which loads one) isn't called."""

    def __init__(self, table: EventTable, device_id: str = "", account_id: str = ""):
        self.table = table
        self.device_id = device_id
        self.account_id = account_id

    def handle(self, raw: bytes, signature: str | None, key: str, replayed: bool = False) -> WebhookStatus:
        """Webhooks that still reach the app (e.g. scripts/feed_webhook.py) go to the same table, under the
        same rules as the Lambda's. Another device or account is answered as ACCEPTED (200) and dropped."""
        status, row = check(raw, signature, key, replayed)
        if row is None:
            return status
        return WebhookStatus.DUPLICATE if self.table.accept(row, self.device_id,
                                                            self.account_id) == DUPLICATE else WebhookStatus.ACCEPTED

    def _rows(self) -> list[dict]:
        return self.table.scan()

    def _rows_for(self, day: date, tz: ZoneInfo) -> list[dict]:
        return [r for d in utc_days(day, tz) for r in self.table.query_day(d)]

    def recent(self, n: int = 20) -> list[dict]:
        rows = sorted(self._rows(), key=lambda r: (_ms(r.get("timestamp")) or 0, str(r.get("request_id"))))
        return rows[::-1][:n]
