# Shopfront on AWS

**Status: template ready, not yet deployed.** [infra/template.yaml](../infra/template.yaml) passes
`sam validate --lint` and `cfn-lint`, and `sam build` packages both functions, but no stack has been
deployed, no Ring webhook has reached it, and there is no public demo URL yet. Until someone deploys it,
every claim below is about the template and its tests, not about a running service.

Shopfront runs on the shop's own computer. Two things need more than that:

- **Ring webhooks need a public HTTPS URL.** Ring sends notifications (`motion_detected`,
  `button_press`) to a Webhook URL you register (API reference, Notifications), and expects an
  answer within 5 seconds. A laptop behind a shop router has no such URL, and it is off at night.
- **Bedrock** labels Ring frames and runs Ask Shopfront ([bedrock.md](bedrock.md)). That part
  already calls AWS from the app with the owner's AWS sign-in, and is unchanged here.

The template adds the first: a small, always-on endpoint that takes Ring's webhooks, checks them
exactly as the app does and queues them, and a second function that stores them in DynamoDB, where the app
reads them when it runs.

It also adds a **public demo** (section 2): the app itself, read-only, over the one-command demo's
made-up shop, at a link anyone can open. Same `sam deploy`; the link is the `DemoUrl` output.

## 1. What the template deploys

```
Ring ──HTTPS POST /webhooks/ring──▶ Lambda Function URL (RingWebhookFunction, 4 s timeout)
                                        │  size cap, then app.ring.webhooks.check: HMAC-SHA256 over the
                                        │  raw body (key from Secrets Manager, cached 5 min); 200 QUEUED
                                        ▼
                                    SQS queue (SSE) ──after 5 failed receives──▶ dead-letter queue (14 days)
                                        │  batches of up to 10
                                        ▼
                                    Lambda WebhookConsumerFunction
                                        │  device/account filter, conditional writes on meta.request_id;
                                        │  reports only the failed records (partial batch response)
                                        ▼
                                    DynamoDB table  ◀── Shopfront app (SHOPFRONT_WEBHOOK_STORE=dynamodb)
                                    (one item per event, TTL)
S3 bucket for door frames (private, encrypted, expires)      CloudWatch log groups (retention set)
```

| Resource | Why this service | Settings |
|---|---|---|
| Lambda function (Python 3.12, arm64) with a Function URL | Ring needs HTTPS and an answer in under 5 s; a Function URL is HTTPS with nothing in front, and the function costs nothing while the door is quiet. API Gateway would add a service and a bill for one POST route that authenticates by HMAC anyway. | 256 MB, 4 s timeout. `AuthType: NONE`, because Ring can't sign AWS requests; the HMAC over the body is the authentication, and anything unsigned gets 401 before it reaches the queue. It does only the size cap, the HMAC and one `SendMessage`, so a slow or throttled table never delays Ring's answer. |
| SQS queue and dead-letter queue | Decouples Ring's 5-second answer from storage. A write that fails is retried by SQS instead of by Ring, and an event that can't be stored lands somewhere visible instead of being lost. A standard queue, not FIFO: FIFO's deduplication only covers 5 minutes, and the table's conditional write already dedupes on `meta.request_id` for as long as the event is kept. | SSE with SQS-managed keys on both, TLS-only queue policy. Visibility timeout 180 s (6 × the consumer's 30 s timeout). `maxReceiveCount` 5, then the dead-letter queue, which keeps a message 14 days. |
| Lambda consumer (Python 3.12, arm64) | Stores what the queue holds. | 256 MB, 30 s timeout, batches of up to 10, `ReportBatchItemFailures` so only the failed records are retried, at most 2 concurrent pollers (one door's events). |
| DynamoDB table | Ring retries a delivery with the same `meta.request_id` (API reference, Notifications > Webhook Response Handling), and SQS can deliver a message more than once. A conditional write makes either a no-op, durably, with no server to run. TTL bounds how long door events are kept. | On-demand. `pk` = `<ShopId>#<UTC day>`, `sk` = `<timestamp ms>#<request_id>`, `expires_at` TTL (EventDays, default 90). Encrypted at rest with a DynamoDB-owned key. |
| Secrets Manager secret | The HMAC key never appears in the template, its parameters, the function's environment, or logs, and can be changed without a redeploy. | Created as an empty slot `{"RING_HMAC_KEY": ""}`; you set the value once after deploy. Until then the function answers 503, which Ring retries. |
| S3 bucket for frames | Door frames are pictures of people. If they move off the laptop, they need a private place that deletes them on schedule. | SSE-S3, all public access blocked, bucket-owner-enforced, TLS-only policy, lifecycle expiry after FrameDays (default 30, matching `SHOPFRONT_FRAME_DAYS`). **The app doesn't write to it yet**: frames stay in `.data/frames/` with the same 30-day limit. |
| CloudWatch log groups | One JSON line per request from the Function URL handler (status, request_id, event type, replayed) and one per record from the consumer, never the body. | Retention LogRetentionDays (default 30). |
| IAM roles for the two functions | Least privilege, and the public function can't touch the table. | Function URL handler: its own log group, `secretsmanager:GetSecretValue` on the one secret, `sqs:SendMessage` on the queue. Consumer: its own log group, `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on the queue, `dynamodb:PutItem` on the table. No wildcards. |
| IAM managed policy for the app | The machine running Shopfront reads the table (and could keep frames in the bucket). | `dynamodb:Query`/`Scan`/`PutItem` on the table, `s3:GetObject`/`PutObject`/`DeleteObject` on `frames/*`. Attach it to the IAM user or role the app runs as. |

Both functions run the app's own code, not a copy. [infra/package_lambda.py](../infra/package_lambda.py)
(called by `sam build` through [infra/Makefile](../infra/Makefile)) packages `app/ring/webhooks.py`,
`app/store/` and the two small modules they import. They need only the standard library and the
boto3 that the Lambda runtime ships, so nothing is pip-installed. The Function URL handler is
[app/store/lambda_webhook.py](../app/store/lambda_webhook.py):

| Request | Answer |
|---|---|
| Not `POST /webhooks/ring` | 404 / 405 |
| Body over 64 KB | 413, before any HMAC |
| No key in the secret yet | 503 (Ring retries) |
| Missing or wrong `X-Signature` | 401 |
| Signed, but not a v1.1 payload | 400 |
| SQS unreachable | 503 (Ring retries) |
| Otherwise | 200 `QUEUED`, with the row as the message body and `meta.request_id` (the dedupe key) and `replayed` as message attributes |

The consumer, [app/store/lambda_consumer.py](../app/store/lambda_consumer.py), logs one outcome per record:

| Record | Outcome |
|---|---|
| From a device or Ring account other than RingDeviceId / RingAccountId | `IGNORED`, deleted from the queue, not stored (Ring already had its 200, so it neither retries nor disables the endpoint) |
| A `meta.request_id` already stored (Ring's retry, or SQS delivering twice) | `DUPLICATE`, deleted |
| Otherwise | `ACCEPTED`, stored |
| Body unreadable, or DynamoDB fails | `FAILED`, reported in `batchItemFailures`: only this record is retried, and after 5 receives SQS moves it to the dead-letter queue |

To put dead-lettered events back once the cause is fixed:
`aws sqs start-message-move-task --source-arn <DeadLetterQueueArn>` moves them to the queue they came from.

Replays from [scripts/feed_webhook.py](../scripts/feed_webhook.py) are signed with the replay-only key
and stay marked as replays end to end: `"replayed": true` in the row and `replayed=1` on the message, and
the consumer stores them as replays, exactly as in the local log.

[tests/test_lambda_webhook.py](../tests/test_lambda_webhook.py) covers every row of both tables with the
payloads from [tests/test_webhooks.py](../tests/test_webhooks.py), against moto's in-process SQS, DynamoDB
and Secrets Manager (the test table and queues are built from the template's own properties): Ring's
retries stored once, a batch with a poison message and a failing write reporting exactly those two, and a
record that keeps failing landing in the dead-letter queue after its fifth receive. It also checks that
the packaged code imports with no third-party packages, and each role's IAM actions are exactly the ones above.

## 2. The public demo

A second function in the same stack serves Shopfront's own pages to anyone with the link, so a judge
can see the receiving desk, the business-closed check and the dispute pack without installing anything.
It is [app/public_demo.py](../app/public_demo.py), run by
[Mangum](https://github.com/Kludex/mangum) behind a Function URL.

```
Visitor ──HTTPS GET──▶ Function URL (no auth) ──▶ Lambda: Mangum ──▶ PublicDemoApp (allowlist + banner)
                                                                        │ only GET/HEAD reads
                                                                        ▼
                                                     app.main, with PublicDemoGate and read-only stores
                                                     over /tmp: the made-up shop, seeded at cold start
```

**What it shows.** At each cold start it builds the shop `python -m scripts.demo` builds, with that
script's own functions: the most recent open day before today whose synthetic door leaves a scan the
door can answer, [samples/orders.csv](../samples/orders.csv) dated to that day, one signed
`package_delivery` webhook replay for the flour courier, and the dairy order's "business closed" scan,
picked and checked through the app. `/` opens on that day, and every page carries the banner
**"Public demo: made-up shop, made-up deliveries."** with links to the desk, the check and the pack.

**What it can't do.** Nothing a visitor does is kept, and nothing reaches Ring or Bedrock:

| Layer | How |
|---|---|
| Allowlist ([`PublicDemoApp`](../app/public_demo.py)) | Only `GET`/`HEAD` on `/`, `/dispute`, `/dispute.zip`, `/setup`, `/api/{deliveries,events,briefing,attempt,dispute,webhooks}`, `/healthz`, `/illustrations/*`, `/frames/*` (and the favicon) reach the app. Everything else, on every path, is answered 403 before the body is read: a page that says what the action would do on the shop's own computer, or `{"error": "PUBLIC_DEMO"}` for `/api/*` and the webhook. New routes are refused until someone adds them. The business-closed check and the dispute pack are GETs that compute in memory, so they work. |
| Owner gate ([`PublicDemoGate`](../app/owner.py)) | There is no owner: no request is local, no cookie is valid, no code pairs, no secret exists. A request passes only inside the allowlist's `admit_visitor()` (a context variable, not anything a visitor sends). `app.main:app` started with `SHOPFRONT_PUBLIC_DEMO=1` but without the allowlist refuses every route, the webhook included. |
| Stores | Orders, profile, frames, webhook log, live sessions and the classification cache are swapped for versions that raise on any write. The Ring source is pinned to the synthetic generator and never reads a token file; pasting a token or pairing is refused. |
| No Bedrock | The rules classifier labels the door; Ask shows its labelled example answer; the screenshot reader is off; the agent factory raises. The function's IAM role can only write its own logs, so it couldn't call Bedrock, DynamoDB, S3 or Secrets Manager anyway. |
| No background work | It answers the ASGI lifespan itself, so nothing the app starts in the background (Ring status polling) runs. |
| Crawlers | `robots.txt` disallows everything and every response says `X-Robots-Tag: noindex, nofollow`. |

[tests/test_public_demo.py](../tests/test_public_demo.py) walks every route of `app.main` with every
HTTP method, plus the banner's links, and checks after each request that no file under the test's data
folder and no store changed, and that nothing opened a socket, an httpx connection, a boto3 client, a
live Ring client or the agent. It also runs the Mangum handler on Function URL events for `/`,
`/dispute` and `/dispute.zip`.

**Why Mangum and not the Lambda Web Adapter.** Mangum turns the Function URL event into an ASGI call in
the same Python process, so the handler is an ordinary function: the tests above call it with a Function
URL event, with no Docker and no emulator. The Web Adapter would run uvicorn as a second process behind
an extension layer published from another AWS account, pinned by region-specific ARN, which the template
would have to trust and keep current, for no gain on a few requests a day.

| Setting | Value | Why |
|---|---|---|
| Runtime | Python 3.12, arm64 | Same as the webhook function. [infra/package_demo.py](../infra/package_demo.py) (called by `sam build` through the Makefile) copies `app/`, `scripts/demo.py`, `scripts/feed_webhook.py` and `samples/orders.csv`, and pip-installs [infra/public-demo-requirements.txt](../infra/public-demo-requirements.txt) as arm64 wheels (about 16 MB). boto3 comes from the runtime. |
| Memory, timeout | 512 MB, 10 s | Lambda's CPU grows with memory. A cold start (imports, seeding, the story walk) took 0.8 s on a laptop; a warm page about 80 ms. Not yet measured on Lambda. |
| Reserved concurrency | `DemoConcurrency`, default 2 | The cost cap: at most two requests run at once; the rest get 429 until one finishes. `0` switches the demo off without deleting it. `-1` reserves nothing, for accounts whose concurrency limit (10 on new accounts) is too low to reserve any: then the demo is uncapped. |
| Logs | Own log group, `DemoLogRetentionDays` (default 7) | JSON, warnings and errors only. |
| Function URL | `AuthType: NONE` | It's a public demo: nothing private is in it, and it refuses every write. |

To see it on your machine first:

```sh
SHOPFRONT_PUBLIC_DEMO=1 .venv/bin/uvicorn app.public_demo:app --port 8404   # open http://127.0.0.1:8404
```

## 3. Deploy (one command, from `infra/`)

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
| LogRetentionDays | 30 | Days CloudWatch keeps the webhook functions' logs. |
| DemoConcurrency | 2 | Public demo requests at once (the cost cap). `0` = demo off, `-1` = no reservation (see section 2). |
| DemoLogRetentionDays | 7 | Days CloudWatch keeps the public demo's logs. |

SAM asks twice whether a function URL "has no authentication. Is this okay?": answer **y** to both (the
webhook's authentication is the HMAC; the demo is public on purpose and read-only), and **Y** to IAM role
creation. SAM saves the answers in `infra/samconfig.toml`, which is
git-ignored because it can hold your Ring IDs; later deploys are just `sam build && sam deploy`.

The stack's outputs: `DemoUrl`, `WebhookUrl`, `HmacSecretArn`, `WebhookTableName`, `FramesBucketName`,
`AppPolicyArn`, `LogGroupName`, `ConsumerLogGroupName`, `QueueUrl`, `DeadLetterQueueArn`, `DemoLogGroupName`.

`DemoUrl` (`https://<id>.lambda-url.<region>.on.aws/`) is the link for judges. Open it once yourself:
the first request is the cold start. To print it again later:

```sh
aws cloudformation describe-stacks --stack-name shopfront \
  --query "Stacks[0].Outputs[?OutputKey=='DemoUrl'].OutputValue" --output text
```

If the deploy fails with "decreases the account's UnreservedConcurrentExecution below its minimum
value", your account can't reserve concurrency: deploy with
`sam deploy --parameter-overrides DemoConcurrency=-1`.

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

The answer is `{"status": "QUEUED"}`, and a second later the consumer's log
(`aws logs tail <ConsumerLogGroupName>`) says `ACCEPTED`: the event is stored as a replay
(`ring-webhook-replay`), never as Ring's own. If you set RingAccountId, the consumer logs `IGNORED` instead:
the feeder's payloads carry the account `replay`, so the account filter drops them, which shows that filter
working.

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

## 4. What it costs at one shop's volume

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
| Public demo (arm64, 512 MB) | a few hundred visits × ~10 requests × ~0.1 s | under $0.01 (inside the free tier) |
| **Total** | | **under $1 a month** |

The two fixed-ish costs are the secret and the whole-table read on each page load. The public demo is
the only part strangers can make you pay for. With `DemoConcurrency=2` the worst case is two requests
running every second of the hour: 3,600 GB-s (about $0.05) plus ~72,000 requests (about $0.015), so
roughly $0.06 an hour, $1.50 a day, for as long as someone keeps it up. Set `DemoConcurrency=0` and
redeploy to stop it at once. Bedrock, which
the app calls directly, is separate ([bedrock.md](bedrock.md)).

## 5. Tear down

The bucket must be empty before CloudFormation can delete it:

```sh
aws s3 rm s3://<FramesBucketName> --recursive
cd infra && sam delete
```

`sam delete` removes the three functions and the two URLs (the demo link stops working), the queues (and
anything still in them), the table (and every event in it), the secret, the log groups, the roles and the policy. Remove the Webhook URL from the Ring developer console too, or Ring
keeps calling an address that no longer answers. Set `SHOPFRONT_WEBHOOK_STORE` back to empty in `.env`.

## 6. Checking the template yourself

```sh
pip install aws-sam-cli cfn-lint  # in its own virtualenv, not the app's
cd infra
sam validate --lint                # SAM transform + cfn-lint
cfn-lint template.yaml
sam build                          # runs package_lambda.py (twice) and package_demo.py; output in infra/.aws-sam/ (git-ignored)
cd .. && .venv/bin/python -m pytest -q tests/test_lambda_webhook.py tests/test_webhook_store.py tests/test_public_demo.py
```

`sam build` downloads the demo's wheels for arm64, so it needs network access to PyPI, and `python3` with
pip on the PATH. To run the built demo handler on this machine instead of Lambda, build it for this
machine with `python3 infra/package_demo.py --local /tmp/demo-build` and call
`app.public_demo.handler(<Function URL event>, None)` with `SHOPFRONT_PUBLIC_DEMO=1` and `/tmp/demo-build`
first on `sys.path` (boto3 must be importable, as it is on Lambda).
