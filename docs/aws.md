# Shopfront on AWS

**Status: template ready, not yet deployed.** [infra/template.yaml](../infra/template.yaml) passes
`sam validate --lint`, and `sam build` packages it, but no stack has been deployed and no Ring
webhook has reached it. Until someone deploys it and a Ring delivery lands in the table, every
claim below is about the template and its tests, not about a running service.

Shopfront runs on the shop's own computer. Two things need more than that:

- **Ring webhooks need a public HTTPS URL.** Ring sends notifications (`motion_detected`,
  `button_press`) to a Webhook URL you register (API reference, Notifications), and expects an
  answer within 5 seconds. A laptop behind a shop router has no such URL, and it is off at night.
- **Bedrock** labels Ring frames and runs Ask Shopfront ([bedrock.md](bedrock.md)). That part
  already calls AWS from the app with the owner's AWS sign-in, and is unchanged here.

The template adds the first: a small, always-on endpoint that takes Ring's webhooks, checks them
exactly as the app does, and keeps them in DynamoDB, where the app reads them when it runs.

## 1. What the template deploys

```
Ring ──HTTPS POST /webhooks/ring──▶ Lambda Function URL
                                        │  app.ring.webhooks.check: HMAC-SHA256 over the raw body
                                        │  RING_HMAC_KEY from Secrets Manager (cached 5 min)
                                        ▼
                                    DynamoDB table  ◀── Shopfront app (SHOPFRONT_WEBHOOK_STORE=dynamodb)
                                    (one item per event, TTL)
S3 bucket for door frames (private, encrypted, expires)      CloudWatch log group (retention set)
```

| Resource | Why this service | Settings |
|---|---|---|
| Lambda function (Python 3.12, arm64) with a Function URL | Ring needs HTTPS and an answer in under 5 s; a Function URL is HTTPS with nothing in front, and the function costs nothing while the door is quiet. API Gateway would add a service and a bill for one POST route that authenticates by HMAC anyway. | 256 MB, 4 s timeout. `AuthType: NONE`, because Ring can't sign AWS requests; the HMAC over the body is the authentication, and anything unsigned gets 401 before it touches the table. |
| DynamoDB table | Ring retries a delivery with the same `meta.request_id` (API reference, Notifications > Webhook Response Handling). A conditional write makes the retry a no-op, durably, with no server to run. TTL bounds how long door events are kept. | On-demand. `pk` = `<ShopId>#<UTC day>`, `sk` = `<timestamp ms>#<request_id>`, `expires_at` TTL (EventDays, default 90). Encrypted at rest with a DynamoDB-owned key. |
| Secrets Manager secret | The HMAC key never appears in the template, its parameters, the function's environment, or logs, and can be changed without a redeploy. | Created as an empty slot `{"RING_HMAC_KEY": ""}`; you set the value once after deploy. Until then the function answers 503, which Ring retries. |
| S3 bucket for frames | Door frames are pictures of people. If they move off the laptop, they need a private place that deletes them on schedule. | SSE-S3, all public access blocked, bucket-owner-enforced, TLS-only policy, lifecycle expiry after FrameDays (default 30, matching `SHOPFRONT_FRAME_DAYS`). **The app doesn't write to it yet**: frames stay in `.data/frames/` with the same 30-day limit. |
| CloudWatch log group | One JSON line per request (status, request_id, event type, replayed), never the body. | Retention LogRetentionDays (default 30). |
| IAM role for the function | Least privilege. | `logs:CreateLogStream`/`PutLogEvents` on its own log group, `dynamodb:PutItem` on the table, `secretsmanager:GetSecretValue` on the one secret. No wildcards. |
| IAM managed policy for the app | The machine running Shopfront reads the table (and could keep frames in the bucket). | `dynamodb:Query`/`Scan`/`PutItem` on the table, `s3:GetObject`/`PutObject`/`DeleteObject` on `frames/*`. Attach it to the IAM user or role the app runs as. |

The function runs the app's own code, not a copy. [infra/package_lambda.py](../infra/package_lambda.py)
(called by `sam build` through [infra/Makefile](../infra/Makefile)) packages `app/ring/webhooks.py`,
`app/store/` and the two small modules they import. They need only the standard library and the
boto3 that the Lambda runtime ships, so nothing is pip-installed. The handler is
[app/store/lambda_webhook.py](../app/store/lambda_webhook.py):

| Request | Answer |
|---|---|
| Not `POST /webhooks/ring` | 404 / 405 |
| Body over 64 KB | 413, before any HMAC |
| No key in the secret yet | 503 (Ring retries) |
| Missing or wrong `X-Signature` | 401 |
| Signed, but not a v1.1 payload | 400 |
| From a device or Ring account other than RingDeviceId / RingAccountId | 200 `IGNORED`, not stored (a 4xx could make Ring disable the endpoint) |
| A `meta.request_id` already stored | 200 `DUPLICATE` |
| Otherwise | 200 `ACCEPTED`, stored |

Replays from [scripts/feed_webhook.py](../scripts/feed_webhook.py) are signed with the replay-only key
and stay marked as replays in the table, exactly as in the local log.

[tests/test_lambda_webhook.py](../tests/test_lambda_webhook.py) covers every row above with the payloads
from [tests/test_webhooks.py](../tests/test_webhooks.py), against moto's in-process DynamoDB and Secrets
Manager (the test table is built from the template's own key schema). It also checks that the packaged
code imports with no third-party packages, and that the function's IAM actions are exactly the four above.

## 2. Deploy (one command, from `infra/`)

You need the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
`make`, `python3`, and an AWS sign-in that can create the stack's resources (`aws login`, SSO, or a profile).
No Docker.

```sh
cd infra
sam build && sam deploy --guided
```

`--guided` asks for a stack name (e.g. `shopfront`), a region, and the parameters:

| Parameter | Default | What it is |
|---|---|---|
| ShopId | `shop` | Partition prefix. Use the same value as `SHOPFRONT_SHOP_ID` in the app. |
| RingDeviceId | empty | The doorbell's Ring device ID (`data.attributes.source`). Empty accepts every device on the account. |
| RingAccountId | empty | The owner's Ring Account ID (`meta.account_id`, `GET /v1/users/me`). Empty accepts any account. |
| EventDays | 90 | Days a webhook event is kept. |
| FrameDays | 30 | Days a frame is kept in S3. |
| LogRetentionDays | 30 | Days CloudWatch keeps the function's logs. |

When it says the function's URL "has no authentication. Is this okay?", answer **y** (the HMAC is the
authentication) and **Y** to IAM role creation. SAM saves the answers in `infra/samconfig.toml`, which is
git-ignored because it can hold your Ring IDs; later deploys are just `sam build && sam deploy`.

The stack's outputs: `WebhookUrl`, `HmacSecretArn`, `WebhookTableName`, `FramesBucketName`, `AppPolicyArn`,
`LogGroupName`.

### Put the HMAC key in the secret

Ring gives you the webhook HMAC key at partner onboarding. Put it in the secret without it landing in
your shell history:

```sh
read -rs RING_HMAC_KEY   # paste the key, then Enter
aws secretsmanager put-secret-value --secret-id <HmacSecretArn> \
  --secret-string "{\"RING_HMAC_KEY\": \"$RING_HMAC_KEY\"}"
unset RING_HMAC_KEY
```

The function picks it up within five minutes (it caches the key per container); to change the key later,
run the same command.

### Register the URL with Ring

In the Ring developer console, set your app's **Webhook URL** to the `WebhookUrl` output
(`https://<id>.lambda-url.<region>.on.aws/webhooks/ring`). The Webhook URL is one of the three public
HTTPS endpoints that one-way account linking asks for (API reference, Partner-Initiated OAuth 2.0;
see [ring.md](ring.md), section 6). The Developer Playground sends no webhooks (organisers, forum
45511), so with a Playground token Ring itself won't call it. To see the path work end to end, replay
a signed event at it with the same key:

```sh
RING_HMAC_KEY=<the key> .venv/bin/python -m scripts.feed_webhook \
  --url <WebhookUrl> --sub-type package_delivery --at now --device <RingDeviceId, if you set one>
aws logs tail <LogGroupName> --since 10m
```

The answer is `{"status": "ACCEPTED"}` and the event is stored as a replay (`ring-webhook-replay`), never as
Ring's own. If you set RingAccountId, the answer is `IGNORED` instead: the feeder's payloads carry the
account `replay`, so the account filter drops them, which shows that filter working.

### Point the app at the table

Attach `AppPolicyArn` to the IAM user or role the app signs in as (an admin role already has it),
then in `.env`:

```sh
SHOPFRONT_WEBHOOK_STORE=dynamodb
SHOPFRONT_WEBHOOK_TABLE=<WebhookTableName>
SHOPFRONT_WEBHOOK_REGION=<the stack's region, if not AWS_REGION>
SHOPFRONT_SHOP_ID=<ShopId>
```

The app then reads the door timeline, receiving desk and `/api/webhooks` from the table through the same
`WebhookLog` interface ([app/store/dynamodb.py](../app/store/dynamodb.py)); nothing else changes. Leave
`SHOPFRONT_WEBHOOK_STORE` empty to keep the local `.data/ring-webhooks.jsonl`.
[tests/test_webhook_store.py](../tests/test_webhook_store.py) feeds the same day of events to both stores
and checks they give the same answers.

Differences from the file log, on purpose: events from another device or account are dropped rather than
stored, so `ignored()` is 0; and `recent()` reads the shop's whole table (bounded by the TTL), newest by
Ring's timestamp first.

## 3. What it costs at one shop's volume

Assumptions: one door, 200 Ring notifications a day (6,000 a month), the app open all day with about
100 page loads a day, 90 days of events kept. List prices in us-east-1; check the pricing pages for your
region.

| Service | Usage per month | Approx. cost |
|---|---|---|
| Lambda (arm64, 256 MB) | 6,000 requests × ~0.2 s = 300 GB-s | under $0.01 (inside the free tier) |
| Function URL | included with Lambda | $0 |
| DynamoDB writes | 6,000 events × 2 items in a transaction = 24,000 write units | ~$0.02 |
| DynamoDB reads | per page load, the day's queries plus one read of the whole table (the app's "any replays?" check); ~3 million read units | ~$0.40 |
| DynamoDB storage | ~18,000 items, under 10 MB | ~$0.01 |
| Secrets Manager | 1 secret, a few thousand calls | ~$0.40 |
| S3 | nothing written yet; frames for 30 days would be a few hundred MB | $0 now, under $0.02 if used |
| CloudWatch Logs | a few MB | under $0.01 |
| **Total** | | **under $1 a month** |

The two fixed-ish costs are the secret and the whole-table read on each page load. Bedrock, which
the app calls directly, is separate ([bedrock.md](bedrock.md)).

## 4. Tear down

The bucket must be empty before CloudFormation can delete it:

```sh
aws s3 rm s3://<FramesBucketName> --recursive
cd infra && sam delete
```

`sam delete` removes the function, its URL, the table (and every event in it), the secret, the log
group, the role and the policy. Remove the Webhook URL from the Ring developer console too, or Ring
keeps calling an address that no longer answers. Set `SHOPFRONT_WEBHOOK_STORE` back to empty in `.env`.

## 5. Checking the template yourself

```sh
pip install aws-sam-cli            # in its own virtualenv, not the app's
cd infra
sam validate --lint                # SAM transform + cfn-lint
sam build                          # runs package_lambda.py; output in infra/.aws-sam/ (git-ignored)
.venv/bin/python -m pytest -q tests/test_lambda_webhook.py tests/test_webhook_store.py   # from the repo root
```
