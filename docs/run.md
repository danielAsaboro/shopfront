# Running Shopfront

The [README](../README.md#run-the-demo) has the one-command demo. This page has the rest: what the demo
shows, the hosted demo, tests, using it from a phone, and connecting a real Ring doorbell.

## What the demo shows

`scripts/demo.py` picks the most recent day the demo shop was open (and its synthetic door was in use around
the scan time) and imports the three orders in
[samples/orders.csv](../samples/orders.csv), dated to that day. It starts Shopfront at
http://127.0.0.1:8000 on synthetic door events and replays one signed Ring `package_delivery`
webhook at 10:12, signed the way [scripts/feed_webhook.py](../scripts/feed_webhook.py) does it. Then it
prints three links, the check first:

1. **The business-closed check:** the made-up carrier "Northline Parcel" scanned the dairy order
   PO-1042 "business closed" at 14:30, inside its 13:00–16:00 window and during posted hours. It's an
   ordinary busy afternoon minute, not a hand-picked quiet one: any open-hours time with the door in use
   around it gives "You were open" (a test sweeps the day every 15 minutes). An unidentified doorbell
   press near the scan is named as unidentified, never offered as the courier. The script asks the
   running app what the door shows and prints its answer. On the demo's desk, "Try the business-closed
   check" opens the same check, already filled in.
2. **The receiving desk** (`/?date=…`): the day's orders, which ones a courier was seen for, and how.
   Ring's 10:12 alert is the flour order's (PO-1041, due 10:00–11:00) confirmed courier; the script
   checks the desk says so and stops if it doesn't. The carrier record shows two earlier demo checks of
   Northline Parcel this month; "Keep in Northline Parcel's record" under a check adds it.
3. **The dispute pack** (`/dispute?…`): the finding, the door log around the scan, the posted hours,
   what the pack can't show, two drafts to pick from (to the supplier, and to the carrier about its
   scan), and a signed zip (`/dispute.zip`): `manifest.sha256` lists every file's SHA-256, `manifest.sig`
   signs it with the shop's key (Ed25519, or an AWS KMS key), and anyone can check it offline with
   `python3 scripts/verify_pack.py <zip> --kid <key ID>`. The key ID is on Set up and the public key at
   `/.well-known/shopfront-pack-key`.

The demo keeps its data in `.data/demo` (git-ignored) and starts fresh each run. Use `--port` if 8000
is taken. To run the app on its own, use `.venv/bin/uvicorn app.main:app --port 8000`. With no Ring
token it uses synthetic events, and the banner says so.

**Hosted demo, no install:** the same made-up shop, read-only, at a public link: the `DemoUrl` that
`cd infra && sam build && sam deploy --guided` prints ([docs/aws.md](aws.md#2-the-public-demo)).
Template ready, not yet deployed, so there is no link yet. Try that mode locally with
`SHOPFRONT_PUBLIC_DEMO=1 .venv/bin/uvicorn app.public_demo:app --port 8404`.

## Tests

Tests and lint need the dev requirements (pytest, hypothesis, ruff, mypy):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

The suite blocks network access, so nothing in it calls Ring or AWS. CI runs the same two commands
on Python 3.11 and 3.13 ([.github/workflows/ci.yml](../.github/workflows/ci.yml)).

## On your phone

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

## With a real Ring doorbell

1. Sign in to the [Ring Developer Playground](https://developer.amazon.com/ring/console/playground) (a free Amazon Developer account) and generate an access token. It lasts about 30 minutes.
2. Open http://127.0.0.1:8000/connect and paste it. Or, in `.env`, set `RING_MODE=live` and `RING_ACCESS_TOKEN=<token>`. Optionally set `RING_DEVICE_ID`; otherwise Shopfront picks the first camera or doorbell from `GET /v1/devices`. Set `RING_ACCOUNT_ID` to your Ring account ID (`GET /v1/users/me`) to ignore webhooks for any other account; when empty, Shopfront learns it from the live token.
3. For Bedrock labels and Ask Shopfront, sign in to AWS (`aws login`) with Bedrock access to the model in `BEDROCK_MODEL_ID`.
4. Restart and open the page. The footer shows the Ring X-Request-ID of the last call.
5. Live view: open http://127.0.0.1:8000/live and press a Playground scene (Package, Vehicle, Motion). The Playground plays its clips over the WHEP live-view stream, so this is where the simulator shows up.
6. To record proof: `.venv/bin/python -m scripts.ring_proof` walks you through it: it lists the devices and their status, asks you to press a Playground scene, waits for that event in Event History, downloads its frame, has Bedrock label it, and writes `evidence/ring-proof-*.json` with every X-Request-ID and the Bedrock RequestId. It uses the pasted token when `RING_ACCESS_TOKEN` is empty. `--dry-run` rehearses it on mocked Ring and Bedrock responses and writes no evidence.

If Ring refuses, the page shows the named reason (`TOKEN_REJECTED`, `FORBIDDEN`, …). It never swaps in synthetic data.
Real Ring webhooks need a public HTTPS URL for `POST /webhooks/ring` and the HMAC key from partner
onboarding in `RING_HMAC_KEY`. The `demo-key` in `.env.example` is only for local replays.
[infra/template.yaml](../infra/template.yaml) is that URL on AWS (`cd infra && sam build && sam deploy --guided`,
see [docs/aws.md](aws.md)), and the same deploy prints `DemoUrl`, the read-only public demo; it has
not been deployed yet.

Door frames are pictures of people, so Shopfront deletes them after `SHOPFRONT_FRAME_DAYS` days
(default 30; `0` keeps them). Each frame on the receiving desk and the dispute pack also has a
"Delete frame" button.
