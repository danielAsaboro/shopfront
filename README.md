# Shopfront

**The carrier scanned "business closed". Your doorbell says you were open.**

Shopfront is a receiving desk for a one-door shop (a bakery, café or boutique) that already has a
Ring doorbell. When a carrier's tracking says "business closed" or "delivery attempted", Shopfront
checks that scan against the shop's posted hours and what the Ring doorbell recorded at the door,
then builds a dispute pack the owner reviews and sends themselves.

**For:** the owner who orders stock from suppliers and loses a day of trading when a courier scans
the shop closed while customers were walking in.

**How Ring and AWS do the work:**

1. **Orders in.** Import a CSV of orders or invoices (any POS, inventory or accounting export), so
   Shopfront knows what should arrive, from whom and when.
2. **Ring sees the door.** The Ring Partner API's Event History gives every ding and motion at the
   door. A signed Ring webhook whose motion `sub_type` is `package_delivery` (Ring's own Smart Alert)
   marks a courier straight away, with no model call. Other visits go to **Amazon Bedrock**, which
   labels the Ring frame (courier, customer, staff). Without a frame, time-of-day rules make a guess,
   and the page says it's a guess.
3. **Check the scan.** Type the carrier's scan and its time. Shopfront compares it with the posted
   hours and every door event within 30 minutes.
4. **Dispute pack.** You get the finding, the door log, the posted hours, the order, and the request
   IDs of the Ring and Bedrock calls it rests on. It also lists what it can't show and drafts a
   message. Shopfront never sends anything.

**What's real today:** Shopfront calls the Ring Partner API live against the Ring Developer
Playground: it reads devices and Event History and opens a live-view session. The request IDs are
in the [proof table](#proof). Three things haven't been captured live yet: a Ring frame, a webhook
sent by Ring itself, and a Bedrock label. So the demo runs on synthetic door events plus one signed
webhook replay, and every page says so.

## Run the demo

Needs Python 3.11+. One command after install:

```bash
git clone <this repo> shopfront && cd shopfront
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m scripts.demo
```

`scripts/demo.py` picks the most recent day the demo shop was open (and its synthetic door leaves a quiet
moment for the scan) and imports the three orders in
[samples/orders.csv](samples/orders.csv), dated to that day. It starts Shopfront at
http://127.0.0.1:8000 on synthetic door events and replays one signed Ring `package_delivery`
webhook at 10:12, signed the way [scripts/feed_webhook.py](scripts/feed_webhook.py) does it. Then it
prints three links:

1. **The receiving desk** (`/?date=…`): the day's orders, which ones a courier was seen for, and how.
   Ring's 10:12 alert is the flour order's (PO-1041, due 10:00–11:00) confirmed courier; the script
   checks the desk says so and stops if it doesn't.
2. **The business-closed check:** the made-up carrier "Northline Parcel" scanned the dairy order
   PO-1042 "business closed" inside its 13:00–16:00 window, during posted hours, at a time when the door
   recorded motion within half an hour but nothing within 12 minutes, after Ring's only courier of the
   day. The script asks the running app what the door shows and prints its answer.
3. **The dispute pack** (`/dispute?…`): the finding, the door log around the scan, the posted hours,
   what the pack can't show, a draft to copy, and a zip (`/dispute.zip`).

The demo keeps its data in `.data/demo` (git-ignored) and starts fresh each run. Use `--port` if 8000
is taken. To run the app on its own, use `.venv/bin/uvicorn app.main:app --port 8000`. With no Ring
token it uses synthetic events, and the banner says so.

Tests and lint need the dev requirements (pytest, hypothesis, ruff, mypy):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

The suite blocks network access, so nothing in it calls Ring or AWS. CI runs the same two commands
on Python 3.11 and 3.13 ([.github/workflows/ci.yml](.github/workflows/ci.yml)).

### On your phone

On the computer itself (127.0.0.1) Shopfront never asks who you are. Any other device has to pair
first, so someone else on the shop Wi-Fi can't read your orders or door log:

1. Start the app so the network can reach it: `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000`.
2. On the computer, open http://127.0.0.1:8000/setup. "Use Shopfront on your phone" shows a 6-digit code.
3. On the phone, open the computer's address (e.g. `http://192.168.1.20:8000`). It asks for the code
   instead of showing the page; enter it. It works once, lasts 10 minutes and allows 5 tries. The phone then stays
   signed in for 180 days.
4. "Sign out every paired device" on `/setup` revokes every phone at once.

IP addresses and localhost always work. To reach Shopfront by a name instead (e.g.
`http://shop-mac.local:8000`), add it to `SHOPFRONT_ALLOWED_HOSTS`; other names are refused, which
stops DNS-rebinding pages. Forms and API calls from other websites are refused too.

### With a real Ring doorbell

1. Sign in to the [Ring Developer Playground](https://developer.amazon.com/ring/console/playground) (a free Amazon Developer account) and generate an access token. It lasts about 30 minutes.
2. Open http://127.0.0.1:8000/connect and paste it. Or, in `.env`, set `RING_MODE=live` and `RING_ACCESS_TOKEN=<token>`. Optionally set `RING_DEVICE_ID`; otherwise Shopfront picks the first camera or doorbell from `GET /v1/devices`. Set `RING_ACCOUNT_ID` to your Ring account ID (`GET /v1/users/me`) to ignore webhooks for any other account; when empty, Shopfront learns it from the live token.
3. For Bedrock labels and Ask Shopfront, sign in to AWS (`aws login`) with Bedrock access to the model in `BEDROCK_MODEL_ID`.
4. Restart and open the page. The footer shows the Ring X-Request-ID of the last call.
5. Live view: open http://127.0.0.1:8000/live and press a Playground scene (Package, Vehicle, Motion). The Playground plays its clips over the WHEP live-view stream, so this is where the simulator shows up.
6. To record proof: `.venv/bin/python -m scripts.ring_proof` walks you through it: it lists the devices and their status, asks you to press a Playground scene, waits for that event in Event History, downloads its frame, has Bedrock label it, and writes `evidence/ring-proof-*.json` with every X-Request-ID and the Bedrock RequestId. It uses the pasted token when `RING_ACCESS_TOKEN` is empty. `--dry-run` rehearses it on mocked Ring and Bedrock responses and writes no evidence.

If Ring refuses, the page shows the named reason (`TOKEN_REJECTED`, `FORBIDDEN`, …). It never swaps in synthetic data.
Real Ring webhooks need a public HTTPS URL for `POST /webhooks/ring` and the HMAC key from partner
onboarding in `RING_HMAC_KEY`. The `demo-key` in `.env.example` is only for local replays.
[infra/template.yaml](infra/template.yaml) is that URL on AWS (`cd infra && sam build && sam deploy --guided`,
see [docs/aws.md](docs/aws.md)); it has not been deployed yet.

Door frames are pictures of people, so Shopfront deletes them after `SHOPFRONT_FRAME_DAYS` days
(default 30; `0` keeps them). Each frame on the receiving desk and the dispute pack also has a
"Delete frame" button.

## Why

- **It happens, and it costs the sale.** In November 2024 a family boutique in Clear Lake, Texas, was
  waiting for six boxes of denim for its Black Friday jeans sale. UPS tracking read "Business closed.
  Will deliver the next business day," while the store was open
  ([KPRC Click2Houston, 28 Nov 2024](https://www.click2houston.com/news/local/2024/11/29/small-business-scrambles-after-ups-mix-up-threatens-black-friday-plans/)).
- **The scan code itself has been disputed.** In 2015 UPS agreed to pay $25 million to settle a
  False Claims Act case. The US alleged that UPS applied inapplicable exception codes such as
  "business closed" to excuse late deliveries to federal customers. These were allegations only:
  UPS made no admission, and there was no finding of liability
  ([US Department of Justice, 19 May 2015](https://www.justice.gov/opa/pr/united-parcel-service-agrees-settle-alleged-civil-false-claims-act-violations)).
- **Ring's Package Alerts answer a different question.** They say a box landed in your Package
  Zone. Ring says they "work best with medium-to-large shipping boxes" and that "envelopes, tubes,
  small boxes, and food deliveries might not be detected". They need a compatible subscription
  ([Ring support](https://ring.com/support/articles/rk2nf/How-to-Get-Package-Alerts)). None of that
  helps when nothing was delivered. Shopfront checks the carrier's claim instead, against the
  posted hours and a door that was busy with customers. It needs no package in view, and it ties the
  scan to the order it was for.

## Hackathon requirements

Built for the Amazon "Build, Ship, Shape" Developer Hackathon, Ring track and AWS Builder mini challenge.

| Requirement | Status |
|---|---|
| Repo calls Ring at runtime | ✓ [`app/ring/api.py`](app/ring/api.py): devices, Event History, image download, WHEP live view, token refresh |
| Shown working through a Ring simulator or device | ✓ Live calls to the Ring Developer Playground (the simulator the hackathon FAQ accepts): devices, Event History and a live-view session, with request IDs below. ✗ Not yet: video frames from a playing Playground clip, and webhooks from Ring (the Playground sends none) |
| AWS service with documented integration | ✓ Amazon Bedrock: [docs/bedrock.md](docs/bedrock.md) (frame labels, Converse) and the Strands agent in [`app/agent.py`](app/agent.py). ✗ No live Bedrock call captured yet. Webhook endpoint on AWS (Lambda Function URL, DynamoDB, Secrets Manager, S3, CloudWatch Logs): [docs/aws.md](docs/aws.md), **template ready, not yet deployed** |
| Setup and run instructions | ✓ [Run the demo](#run-the-demo) |
| Repo access for judges | ✓ [MIT](LICENSE) licence. ✗ Not yet on GitHub (private, shared with the Amazon reviewers at submission) |

## Proof

Labels follow one rule: a claim is **live** only with a Ring or AWS request ID. Everything else says what it is.

| Claim | Label | Artifact |
|---|---|---|
| Calls the Ring Partner API with a Bearer token and maps a 401 to a named state | live (error path) | `GET /v1/devices` → 401, X-Request-ID `90478809-976d-4bde-9881-5f5611afb2fb`, [evidence/](evidence/) |
| Reads the Ring Developer Playground test doorbell: `GET /v1/devices` 200, Event History 200 (empty) | live (Playground) | X-Request-IDs `59df1289-…`, `ba6d001b-…` in [evidence/ring-proof-20261015T082514Z.json](evidence/ring-proof-20261015T082514Z.json) |
| Opens a Ring live-view (WHEP) session from `/live`: 201, WebRTC connected | live (Playground) | X-Request-IDs `13622a41-…`, `a0349b9b-…` in [evidence/ring-live-view-20261015T0826Z.json](evidence/ring-live-view-20261015T0826Z.json) (a hand-written record of the two sessions, see [evidence/README.md](evidence/README.md)) |
| Receives video frames from a live session and labels them | **not yet verified** | No Playground clip was playing during the sessions above |
| Downloads a Ring image | **not yet verified** | The one try, `latest_in_range` over 24 h while Event History was empty, got 403 FORBIDDEN (X-Request-ID `843c63da-…`). `scripts/ring_proof.py` now asks at a Playground scene's filed event instead ([docs/ring.md](docs/ring.md), section 6) |
| Labels a real Ring frame with Bedrock | **not yet verified** | No Bedrock RequestId captured |
| Receives a webhook sent by Ring | **not yet verified** | The Playground sends no webhooks; the demo uses signed replays marked `ring-webhook-replay` |
| Checks a carrier's "business closed" or "delivery attempted" scan against the posted hours and the door, and builds a dispute pack whose draft it never sends | local test and demo, on synthetic door events and signed webhook replays | [tests/test_receiving_desk.py](tests/test_receiving_desk.py), [tests/test_reconcile.py](tests/test_reconcile.py), [docs/receiving-desk.md](docs/receiving-desk.md), `python -m scripts.demo` |
| Imports orders and invoices from CSV; routes a Ring `package_delivery` webhook to a courier label with no model call | local test | [tests/test_receiving_desk.py](tests/test_receiving_desk.py) |
| Pages Event History, follows the 303 frame download without leaking the token, refreshes and rotates tokens, fails closed to named states | local test | `pytest -q` (all pass), [tests/test_ring_api.py](tests/test_ring_api.py) |
| Keeps Playground live views (`on_demand`) out of the visitor counts, retries a frame Ring hasn't processed yet, distrusts an ignored `event_types` filter, samples device status for camera coverage | local test, on documented response shapes | [tests/test_ring_frames.py](tests/test_ring_frames.py), [tests/test_ring_filters.py](tests/test_ring_filters.py), [tests/test_status_sampling.py](tests/test_status_sampling.py), `python -m scripts.ring_proof --dry-run` |
| Rejects unsigned or replayed Ring webhooks | local test | [tests/test_webhooks.py](tests/test_webhooks.py) |
| Ask Shopfront returns each answer with the tool calls behind it, and names the failure instead of answering when Bedrock is unavailable | local test with a fake agent; not run against Bedrock here | [tests/test_door_desk.py](tests/test_door_desk.py); [scripts/eval_agent.py](scripts/eval_agent.py) is the eval for a live run |
| Morning briefing (doorbell presses while closed, staff arrival, traffic vs last week) | local test on synthetic data | [tests/test_briefing.py](tests/test_briefing.py) |

## Also in the app

- **Ask Shopfront** (`POST /api/ask`, the box on the dashboard): a [Strands](https://strandsagents.com) agent on Amazon Bedrock with five tools over the same data: door visits, deliveries, week, check_attempt and dispute_pack. It answers from what the tools return and lists every tool call and Bedrock RequestId with the answer. It needs AWS credentials with Bedrock access.
- **Morning briefing:** yesterday at the door, under the receiving desk. It covers doorbell presses while the shop was closed, when staff were first and last at the door, and foot traffic against the same weekday last week.
- **Staff tap:** once a courier has been seen for an order, staff can mark it All there, Short or Damaged. A short or damaged order gets its own dispute pack.

## How it fits together

| Piece | File | Notes |
|---|---|---|
| Ring client | [`app/ring/api.py`](app/ring/api.py) | `api.amazonvision.com` devices, Event History, image download, WHEP live view; `oauth.ring.com` refresh. [docs/ring.md](docs/ring.md) |
| Ring webhooks | [`app/ring/webhooks.py`](app/ring/webhooks.py) | `POST /webhooks/ring`, HMAC-SHA256 over the raw body, idempotent on `meta.request_id`; `package_delivery` is stage 0 |
| Webhook replays | [`scripts/feed_webhook.py`](scripts/feed_webhook.py) | Signs payloads in Ring's documented shape; logged as replays, never as Ring's own |
| Live view | [`app/live.py`](app/live.py), `/live` | Ring WHEP session; sampled frames go to Bedrock; a session becomes one door visit |
| Synthetic source | [`app/ring/synthetic.py`](app/ring/synthetic.py) | Deterministic demo days for running without a Ring account. Not Ring data. |
| Classifier | [`app/classify/`](app/classify/) | Bedrock Converse with the Ring frame ([docs/bedrock.md](docs/bedrock.md)); time-of-day rules otherwise, labelled `rules` |
| Orders import | [`app/orders.py`](app/orders.py), `POST /orders/import` | CSV with column aliases (Order ID, PO number, Vendor, Tracking number, …) |
| Receiving desk | [`app/deliveries.py`](app/deliveries.py), [`app/reconcile.py`](app/reconcile.py), [`app/dispute.py`](app/dispute.py) | Orders matched to couriers, carrier scans checked, dispute packs. [docs/receiving-desk.md](docs/receiving-desk.md), [docs/receipts.md](docs/receipts.md) |
| Agent | [`app/agent.py`](app/agent.py), `POST /api/ask` | Strands agent with a Bedrock model and five tools |
| AWS webhook endpoint | [`infra/template.yaml`](infra/template.yaml), [`app/store/`](app/store/) | **Template ready, not yet deployed.** SAM: Lambda Function URL running the same HMAC check, DynamoDB dedupe and log, key in Secrets Manager; `SHOPFRONT_WEBHOOK_STORE=dynamodb` reads it. [docs/aws.md](docs/aws.md) |
| Briefing | [`app/briefing.py`](app/briefing.py) | Today vs the same weekday last week |
| Demo | [`scripts/demo.py`](scripts/demo.py), [`samples/orders.csv`](samples/orders.csv) | The one-command demo above |
| Web | [`app/main.py`](app/main.py), [`app/templates/`](app/templates/) | Pages: `/`, `/setup`, `/dispute`, `/live`, `/connect`. JSON: `/api/deliveries`, `/api/attempt`, `/api/dispute`, `/api/events`, `/api/briefing`, `/api/webhooks`, `/api/ask`, `/healthz`. Zip: `/dispute.zip` |

## Limits

- Ring has no "door answered" signal. Shopfront can say a courier was at the door. It can't say a
  parcel was handed over.
- A pack shows the shop was open and the door was busy. It supports the owner's account but can't
  prove that nobody came, because motion detection can miss people. Every pack says so.
- Labels from the time-of-day rules are guesses. The rules can't tell a customer's ring from a
  courier's, so only Ring's `package_delivery` alert or a Bedrock-labelled frame counts as a courier.
- Ring only returns events from after the owner connected the app, so "vs last week" is empty for
  the first week.
- One shop and one doorbell. Shopfront hasn't been through Ring account linking or certification.

## License

MIT
