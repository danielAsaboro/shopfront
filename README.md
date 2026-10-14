# Shopfront

**A morning briefing for small-shop owners, built from the Ring doorbell they already have.**

Shopfront reads the shop's Ring doorbell history through the Ring Partner API, asks Amazon Bedrock
to label each visit from the Ring frame (customer, delivery, staff, after hours), and turns the day
into four lines the owner reads with their coffee: which deliveries rang while the shop was closed,
when staff were actually at the door, and how foot traffic compared with the same day last week.

Built for the Amazon "Build, Ship, Shape" Developer Hackathon, Ring track and AWS Builder mini challenge.

## Hackathon requirements

| Requirement | Status |
|---|---|
| Repo calls Ring at runtime | ✓ [`app/ring/api.py`](app/ring/api.py): Event History, image download, token refresh |
| Shown working on a real Ring device | ✗ Not yet. Needs a live capture, see the proof table |
| AWS service with documented integration | ✓ [docs/bedrock.md](docs/bedrock.md). ✗ No live Bedrock call captured yet |
| Setup and run instructions | ✓ Below |
| Open-source licence | ✓ [MIT](LICENSE) |

## Proof

Labels follow one rule: a claim is **live** only with a Ring or AWS request ID. Everything else says what it is.

| Claim | Label | Artifact |
|---|---|---|
| Calls the Ring Partner API with a Bearer token and maps a 401 to a named state | live (error path) | `GET /v1/devices` → 401, X-Request-ID `90478809-976d-4bde-9881-5f5611afb2fb`, [evidence/](evidence/) |
| Reads Event History and frames for a real doorbell | **not yet verified** | — |
| Labels a real Ring frame with Bedrock | **not yet verified** | — |
| Pages Event History, follows the 303 frame download without leaking the token, refreshes and rotates tokens, fails closed to 14 named states | local test | `pytest -q` (33 pass), [tests/test_ring_api.py](tests/test_ring_api.py) |
| Rejects unsigned or replayed Ring webhooks | local test | [tests/test_webhooks.py](tests/test_webhooks.py) |
| Briefing logic (deliveries while closed, staff arrival, traffic vs last week) | local test on synthetic data | [tests/test_briefing.py](tests/test_briefing.py) |

## Run it

Needs Python 3.11+.

```bash
git clone <this repo> && cd shopfront
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. With no Ring token it runs on **synthetic demo events**, and every page
says so in a banner. Tests: `.venv/bin/python -m pytest -q`.

### With a real Ring doorbell

1. Sign in to the [Ring Developer Playground](https://developer.amazon.com/ring/console/playground) and generate an access token. It lasts about 30 minutes.
2. In `.env` set `RING_MODE=live` and `RING_ACCESS_TOKEN=<token>`. Optionally set `RING_DEVICE_ID`; otherwise the first device is used.
3. For Bedrock labels, sign in to AWS (`aws login`) with Bedrock access to the model in `BEDROCK_MODEL_ID`.
4. Restart and open the page. The footer shows the Ring X-Request-ID of the last call.
5. To record proof: `.venv/bin/python -m scripts.ring_proof --bedrock` writes `evidence/ring-proof-*.json`.

If Ring refuses, the page shows the named reason (`TOKEN_REJECTED`, `FORBIDDEN`, …) instead of a briefing.

## How it fits together

| Piece | File | Notes |
|---|---|---|
| Ring client | [`app/ring/api.py`](app/ring/api.py) | `api.amazonvision.com` Event History and image download, `oauth.ring.com` refresh. [docs/ring.md](docs/ring.md) |
| Ring webhooks | [`app/ring/webhooks.py`](app/ring/webhooks.py) | `POST /webhooks/ring`, HMAC-SHA256 over the raw body, idempotent on `meta.request_id` |
| Synthetic source | [`app/ring/synthetic.py`](app/ring/synthetic.py) | Deterministic demo days for running without a Ring account. Not Ring data. |
| Classifier | [`app/classify/`](app/classify/) | Bedrock Converse with the Ring frame ([docs/bedrock.md](docs/bedrock.md)). Time-of-day rules otherwise. |
| Briefing | [`app/briefing.py`](app/briefing.py) | Today vs the same weekday last week |
| Web | [`app/main.py`](app/main.py), [`app/templates/`](app/templates/) | `/`, `/api/briefing`, `/api/events`, `/api/webhooks`, `/healthz` |

## Limits

Ring only returns events after the owner connected the app, so "vs last week" is empty for the
first week. Ring has no "door answered" signal, so Shopfront reports doorbell presses while closed
rather than claiming a delivery was missed. One shop and one doorbell for now.

## License

MIT
