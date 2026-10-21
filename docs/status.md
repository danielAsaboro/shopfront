# Status: what's live and what isn't

What Shopfront has shown against the real Ring and AWS APIs, what runs only in tests and the demo, and where
each piece lives. Back to the [README](../README.md).

**What's real today:** Shopfront calls the Ring Partner API live against the Ring Developer
Playground: it reads devices and Event History and opens a live-view session. The request IDs are
in the [proof table](#proof). Three things haven't been captured live yet: a Ring frame, a webhook
sent by Ring itself, and a Bedrock label. So the demo runs on synthetic door events plus one signed
webhook replay, and every page says so.

## Hackathon requirements

Built for the Amazon "Build, Ship, Shape" Developer Hackathon, Ring track and AWS Builder mini challenge.

| Requirement | Status |
|---|---|
| Repo calls Ring at runtime | ✓ [`app/ring/api.py`](../app/ring/api.py): devices, Event History, image download, WHEP live view, token refresh |
| Shown working through a Ring simulator or device | ✓ Live calls to the Ring Developer Playground (the simulator the hackathon FAQ accepts): devices, Event History and a live-view session, with request IDs below. ✗ Not yet: video frames from a playing Playground clip, and webhooks from Ring (the Playground sends none) |
| AWS service with documented integration | ✓ Amazon Bedrock: [docs/bedrock.md](bedrock.md) (frame labels, Converse) and the Strands agent in [`app/agent.py`](../app/agent.py). ✗ No live Bedrock call captured yet. Webhook endpoint and read-only public demo on AWS (Lambda Function URLs, DynamoDB, Secrets Manager, S3, CloudWatch Logs): [docs/aws.md](aws.md), **template ready, not yet deployed** (no demo URL yet) |
| Setup and run instructions | ✓ [Run the demo](../README.md#run-the-demo) |
| Repo access for judges | ✓ [MIT](../LICENSE) licence. ✗ Not yet on GitHub (private, shared with the Amazon reviewers at submission) |

## Proof

Labels follow one rule: a claim is **live** only with a Ring or AWS request ID. Everything else says what it is.

| Claim | Label | Artifact |
|---|---|---|
| Calls the Ring Partner API with a Bearer token and maps a 401 to a named state | live (error path) | `GET /v1/devices` → 401, X-Request-ID `90478809-976d-4bde-9881-5f5611afb2fb`, [evidence/](../evidence/) |
| Reads the Ring Developer Playground test doorbell: `GET /v1/devices` 200, Event History 200 (empty) | live (Playground) | X-Request-IDs `59df1289-…`, `ba6d001b-…` in [evidence/ring-proof-20261015T082514Z.json](../evidence/ring-proof-20261015T082514Z.json) |
| Opens a Ring live-view (WHEP) session from `/live`: 201, WebRTC connected | live (Playground) | X-Request-IDs `13622a41-…`, `a0349b9b-…` in [evidence/ring-live-view-20261015T0826Z.json](../evidence/ring-live-view-20261015T0826Z.json) (a hand-written record of the two sessions, see [evidence/README.md](../evidence/README.md)) |
| Receives video frames from a live session and labels them | **not yet verified** | No Playground clip was playing during the sessions above |
| Downloads a Ring image | **not yet verified** | The one try, `latest_in_range` over 24 h while Event History was empty, got 403 FORBIDDEN (X-Request-ID `843c63da-…`). `scripts/ring_proof.py` now asks at a Playground scene's filed event instead ([docs/ring.md](ring.md), section 6) |
| Labels a real Ring frame with Bedrock | **not yet verified** | No Bedrock RequestId captured |
| Receives a webhook sent by Ring | **not yet verified** | The Playground sends no webhooks; the demo uses signed replays marked `ring-webhook-replay` |
| Checks a carrier's "business closed" or "delivery attempted" scan against the posted hours and the door, and builds a dispute pack whose draft it never sends | local test and demo, on synthetic door events and signed webhook replays | [tests/test_receiving_desk.py](../tests/test_receiving_desk.py), [tests/test_reconcile.py](../tests/test_reconcile.py), [docs/receiving-desk.md](receiving-desk.md), `python -m scripts.demo` |
| Imports orders and invoices from CSV; routes a Ring `package_delivery` webhook to a courier label with no model call | local test | [tests/test_receiving_desk.py](../tests/test_receiving_desk.py) |
| Pages Event History, follows the 303 frame download without leaking the token, refreshes and rotates tokens, fails closed to named states | local test | `pytest -q` (all pass), [tests/test_ring_api.py](../tests/test_ring_api.py) |
| Keeps Playground live views (`on_demand`) out of the visitor counts, retries a frame Ring hasn't processed yet, distrusts an ignored `event_types` filter, samples device status for camera coverage | local test, on documented response shapes | [tests/test_ring_frames.py](../tests/test_ring_frames.py), [tests/test_ring_filters.py](../tests/test_ring_filters.py), [tests/test_status_sampling.py](../tests/test_status_sampling.py), `python -m scripts.ring_proof --dry-run` |
| Rejects unsigned or replayed Ring webhooks | local test | [tests/test_webhooks.py](../tests/test_webhooks.py) |
| Ask Shopfront returns each answer with the tool calls behind it, and names the failure instead of answering when Bedrock is unavailable | local test with a fake agent; not run against Bedrock here | [tests/test_door_desk.py](../tests/test_door_desk.py); [scripts/eval_agent.py](../scripts/eval_agent.py) is the eval for a live run |
| Morning briefing (doorbell presses while closed, staff arrival, traffic vs last week) | local test on synthetic data | [tests/test_briefing.py](../tests/test_briefing.py) |

## How it fits together

| Piece | File | Notes |
|---|---|---|
| Ring client | [`app/ring/api.py`](../app/ring/api.py) | `api.amazonvision.com` devices, Event History, image download, WHEP live view; `oauth.ring.com` refresh. [docs/ring.md](ring.md) |
| Ring webhooks | [`app/ring/webhooks.py`](../app/ring/webhooks.py) | `POST /webhooks/ring`, HMAC-SHA256 over the raw body, idempotent on `meta.request_id`; `package_delivery` is stage 0 |
| Webhook replays | [`scripts/feed_webhook.py`](../scripts/feed_webhook.py) | Signs payloads in Ring's documented shape; logged as replays, never as Ring's own |
| Live view | [`app/live.py`](../app/live.py), `/live` | Ring WHEP session; sampled frames go to Bedrock; a session becomes one door visit |
| Synthetic source | [`app/ring/synthetic.py`](../app/ring/synthetic.py) | Deterministic demo days for running without a Ring account. Not Ring data. |
| Classifier | [`app/classify/`](../app/classify/) | Bedrock Converse with the Ring frame ([docs/bedrock.md](bedrock.md)); time-of-day rules otherwise, labelled `rules` |
| Orders import | [`app/orders.py`](../app/orders.py), `POST /orders/import` | CSV with column aliases (Order ID, PO number, Vendor, Tracking number, …) |
| Receiving desk | [`app/deliveries.py`](../app/deliveries.py), [`app/reconcile.py`](../app/reconcile.py), [`app/dispute.py`](../app/dispute.py) | Orders matched to couriers, carrier scans checked, dispute packs. [docs/receiving-desk.md](receiving-desk.md), [docs/receipts.md](receipts.md) |
| Agent | [`app/agent.py`](../app/agent.py), `POST /api/ask` | Strands agent with a Bedrock model and five tools |
| AWS webhook endpoint | [`infra/template.yaml`](../infra/template.yaml), [`app/store/`](../app/store/) | **Template ready, not yet deployed.** SAM: Lambda Function URL running the same HMAC check, DynamoDB dedupe and log, key in Secrets Manager; `SHOPFRONT_WEBHOOK_STORE=dynamodb` reads it. [docs/aws.md](aws.md) |
| Hosted public demo | [`app/public_demo.py`](../app/public_demo.py), [`infra/template.yaml`](../infra/template.yaml) | **Template ready, not yet deployed.** The app on a Lambda Function URL (`DemoUrl`), seeded with the one-command demo's shop: GET-only allowlist, no owner (`PublicDemoGate`), read-only stores, no Ring or Bedrock calls, capped by reserved concurrency. [docs/aws.md](aws.md#2-the-public-demo) |
| Briefing | [`app/briefing.py`](../app/briefing.py) | Today vs the same weekday last week |
| Demo | [`scripts/demo.py`](../scripts/demo.py), [`samples/orders.csv`](../samples/orders.csv) | The one-command demo in the [README](../README.md#run-the-demo) |
| Web | [`app/main.py`](../app/main.py) and [`app/routes/`](../app/routes/), [`app/templates/`](../app/templates/) | Pages: `/`, `/setup`, `/dispute`, `/report` (the month's carrier record, printable), `/live`, `/connect`. JSON: `/api/deliveries`, `/api/attempt`, `/api/dispute`, `/api/events`, `/api/briefing`, `/api/webhooks`, `/api/ask`, `/healthz`. Zip: `/dispute.zip` |
