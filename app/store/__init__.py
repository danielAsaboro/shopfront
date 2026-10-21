"""Where Ring's webhooks are kept.

- "file" (the default): `.data/ring-webhooks.jsonl` on this machine, written by the app's own
  `POST /webhooks/ring` (app/ring/webhooks.py).
- "dynamodb": the table the webhook Lambda in infra/template.yaml writes, so Ring can reach a public HTTPS
  URL while the app runs on a laptop that is sometimes off (docs/aws.md). Set SHOPFRONT_WEBHOOK_STORE=dynamodb
  and SHOPFRONT_WEBHOOK_TABLE to the stack's `WebhookTableName` output.

Both give the app the same WebhookLog interface, so nothing else changes.

This package also holds the two Lambda handlers: lambda_webhook.py (the Function URL: HMAC, then SQS) and
lambda_consumer.py (SQS to DynamoDB). They are packaged on their own with infra/package_lambda.py, so this
file imports nothing at module level.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings
    from app.ring.webhooks import WebhookLog

STORES = ("file", "dynamodb")


def make_webhook_log(settings: Settings, path: Path) -> WebhookLog:
    from app.ring.webhooks import WebhookLog

    store = (settings.webhook_store or "file").lower()
    if store == "file":
        return WebhookLog(path, settings.ring_device_id, settings.ring_account_id)
    if store != "dynamodb":
        raise ValueError(f"SHOPFRONT_WEBHOOK_STORE must be one of {', '.join(STORES)}, got {settings.webhook_store!r}")
    if not settings.webhook_table:
        raise ValueError("SHOPFRONT_WEBHOOK_STORE=dynamodb needs SHOPFRONT_WEBHOOK_TABLE "
                         "(the WebhookTableName output of the stack in infra/template.yaml)")
    import boto3

    from app.store.dynamodb import DynamoWebhookLog, EventTable

    client = boto3.client("dynamodb", region_name=settings.webhook_region or settings.aws_region)
    return DynamoWebhookLog(EventTable(client, settings.webhook_table, settings.shop_id),
                            settings.ring_device_id, settings.ring_account_id)
