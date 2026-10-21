# Ring Partner API in Shopfront

## 1. The rule we enforce

Every door event Shopfront shows, on the receiving desk, in a scan check, in a dispute pack or in the briefing, comes from Ring for the shop's doorbell, or the page says it doesn't: Shopfront never fills a Ring failure with made-up data.

## 2. Enforcement layers

| Layer | Where | What it blocks if the others fail |
|---|---|---|
| Mode switch | `RING_MODE` in [app/ring/\_\_init\_\_.py](../app/ring/__init__.py) | Synthetic and live sources are never mixed in one process. Synthetic mode puts a banner on every page. |
| Config check at start-up | `RingAuth.__init__` in [app/ring/api.py](../app/ring/api.py) | Live mode with no token, or both token kinds, becomes `NOT_CONFIGURED`. Every page and API call then returns that state (HTTP 503). |
| Per-call status mapping | `RingApiClient._request` | Every non-2xx response becomes a named `RingStatus`. The FastAPI handler in [app/main.py](../app/main.py) renders it as an error page or JSON error, never a briefing. |
| Response shape check | `events_for_day`, `snapshot` | A 2xx body we can't read becomes `MALFORMED_RESPONSE` instead of a guess. |
| Webhook signature | [app/ring/webhooks.py](../app/ring/webhooks.py) | An unsigned, wrongly signed, or unconfigured webhook is refused before any parsing. |

## 3. Flow

1. The owner opens the dashboard (receiving desk and briefing) for a date, or checks a carrier's scan.
2. Shopfront gets an access token: the Playground token as-is, or `POST https://oauth.ring.com/oauth/token` with `grant_type=refresh_token`. Ring rotates the refresh token, so the new one is saved to `.data/ring-refresh-token.json`.
3. If `RING_DEVICE_ID` is empty, `GET /v1/devices?include=capabilities` and take the first device whose capabilities carry `video` (a camera or doorbell, not a sensor or chime).
4. `GET /v1/history/devices/{id}/events?event_types=ding,motion,on_demand`. Pages are newest first. Shopfront follows `links.next` until an event is older than the requested day, then stops. `on_demand` events (API reference, Event History > Event types: "User or partner initiated a live view session") are kept and counted in the footer as Ring live views, never as visitors, and never sent to a classifier. When the day has motion, it asks again with the composite filters `event_types=motion.package_delivery` and `event_types=motion.human` (API reference, Event History > Event type filtering; `motion.human` is in its Event types table) and tags those motion events as Ring's courier alerts, which skip the classifier, and Ring's person alerts, which make a "person (Ring)" where the rules only guessed a customer ([receiving-desk.md](receiving-desk.md), section 2), unless an answer looks unfiltered (section 6).
5. For each event, when Bedrock is on: `POST /v1/devices/{id}/media/image/download` with `{"type": "at_timestamp", "timestamp": <ms>}` and nothing else (`image_options` is optional, Image Snapshots > Request Body). The time is the event's start for a ding or motion, and 5 s into a live view, kept inside its `end`. Ring answers 303, and the client follows the pre-signed `Location` to the JPEG. httpx drops the Bearer header on that cross-origin hop. A 416 or 425 on a recent event means Ring is still processing: a caller that can wait (the proof script) retries the unchanged request at 5, 10, 20, 30 s plus jitter until 2 minutes after the requested time (Media Clips > Download video after a webhook event); a page load never waits, and asks again on the next load.
6. The frame goes to Bedrock (see [bedrock.md](bedrock.md)), and the label is cached per Ring event ID so a frame is classified once.
7. The same steps run for the same weekday last week, and the briefing compares the two.

**Live door** (`/live`): the browser builds a WebRTC offer (video, receive-only) and posts it to Shopfront. Shopfront forwards it to `POST /v1/devices/{id}/media/streaming/whep/sessions` with the Bearer token (the token never reaches the browser), returns Ring's SDP answer, and keeps the session URL from `Location`. The page samples a frame every 3 seconds. Up to 6 frames per session go to Bedrock, the session becomes one visit labelled by its most confident frame, and `DELETE <session URL>` ends it. Sessions are logged to `.data/live-sessions.jsonl` and merged into the briefing.

**Status sampling:** while Ring is live (`RING_MODE=live` or a pasted token), a task in the app's lifespan calls `GET /v1/devices/{id}/status` every `SHOPFRONT_STATUS_MINUTES` (default 5) and appends `{at, device, online, request_id}` to `.data/ring-status.jsonl` (0600, 90 days). A failed check is kept as `online: null`. A scan check counts the camera as watching only between two consecutive checks that both said online, no more than 1.5 intervals apart, and the pack's camera line names the window ("every answer from 12:30 to 12:50 said online") or the gap ([app/coverage.py](../app/coverage.py)). Ring's `device_offline` webhooks still mark an outage.

Signed webhooks (`motion_detected`, `button_press`, …) are accepted at `POST /webhooks/ring`, logged, and fed into the door timeline as stage 0 of the funnel: the motion `sub_type` routes `package_delivery` straight to a courier label and skips `vehicle` and `other_motion` ([receiving-desk.md](receiving-desk.md), section 2). The briefing still reads Event History, so it works when no webhook URL is registered.

## 4. Endpoints used

| Endpoint | Purpose | Auth | Code |
|---|---|---|---|
| `POST https://oauth.ring.com/oauth/token` (`refresh_token`) | New access token | client id + secret | `RingAuth._do_refresh` |
| `GET /v1/devices?include=capabilities` | Find the doorbell | Bearer | `RingApiClient.list_devices`, `device_id` |
| `GET /v1/devices/{id}/status` | Online right now (sampled every few minutes) | Bearer | `RingApiClient.device_status`, `app.main.sample_status` |
| `GET /v1/users/me` | Account ID, to match webhooks' `meta.account_id` | Bearer | `RingApiClient.account_id` |
| `GET /v1/history/devices/{id}/events` | The day's dings, motion and live views | Bearer | `RingApiClient.events_for_day`, `recent_events` |
| `POST /v1/devices/{id}/media/image/download` → 303 → GET | Frame at the event time | Bearer, then pre-signed URL | `RingApiClient.frame`, `snapshot` |
| `POST /v1/devices/{id}/media/streaming/whep/sessions` (SDP) → 201 + `Location` | Live view | Bearer | `RingApiClient.whep_start` |
| `DELETE <Location>` | End live view | Bearer | `RingApiClient.whep_stop` |
| `POST /webhooks/ring` (ours) | Receive Ring notifications | `X-Signature: sha256=<hex>` | `WebhookLog.handle` |
| `POST https://oauth.ring.com/oauth/token` (`authorization_code`) | Account linking: redeem Ring's code | client id + secret | `RingLink.exchange` |
| `POST /v1/accounts/me/app-integrations` | Account linking: confirm with the nonce (status `awaiting`) | Bearer (the matched token) | `RingLink.claim` |
| `PATCH /v1/accounts/me/app-integrations` | Account linking: `{"status": "completed"}` (mandatory) | Bearer (the matched token) | `RingLink.claim` |
| `POST /ring/link/token`, `GET`/`POST /ring/link`, `GET /ring/link/home` (ours) | Token Exchange URL, Account Link URL, App Homepage URL | see section 10 | `app/main.py`, [app/ring/link.py](../app/ring/link.py) |

All Ring calls go to the production base URL `https://api.amazonvision.com`. Ring's docs list no separate staging API host.

## 5. Failure handling

| Failure | Detected in | Resulting state | What the owner sees |
|---|---|---|---|
| No token, or both token kinds | `RingAuth.__init__` | `NOT_CONFIGURED` (503) | "Can't read Ring right now", with setup steps |
| 401 with a Playground token | `_request` | `TOKEN_REJECTED` (401) | Same page: generate a fresh Playground token |
| 401 with a refresh token | `_request` | One forced refresh and retry, then `TOKEN_REJECTED` | Same page |
| Refresh refused by oauth.ring.com | `_do_refresh` | `REFRESH_FAILED` (401) | "Re-link the Ring account" |
| 403 | `_request` | `FORBIDDEN` (502) | "This token can't see the doorbell" |
| (not sent) a frame from before a Playground token was issued | `frame` | `BEFORE_TOKEN`, with both times | Never asked: a Playground token only reads media recorded after its issue time (its JWT `iat`, else when it was pasted on /connect, else when Shopfront loaded it; `scripts/ring_proof.py` records its own start). Another team logged the 403 TIME_RANGE_NOT_AUTHORIZED Ring gives otherwise; the API reference doesn't say. `at_timestamp` is always sent in whole seconds (Image Snapshots > Request Body: an integer, epoch ms): the same team logged a fractional value answered 403 REQUEST_FORBIDDEN. |
| 403 on a frame | `frame` | `FORBIDDEN`, with Ring's `errors[0].code` | That one frame isn't asked for again. Only `REQUEST_FORBIDDEN` ("Client is not authorized for the device", Image Snapshots > Error Codes) stops every frame of that device. |
| 404 | `_request` | `DEVICE_NOT_FOUND` (502) | Same |
| Token sees no devices | `device_id` | `NO_DEVICES` (502) | Same |
| 429 | `_request` | `RATE_LIMITED` (502) | "Try again in a minute" |
| 5xx, incl. 503 device offline | `_request` | `RING_UNAVAILABLE` (502) | Generic Ring error, X-Request-ID shown |
| DNS, TLS or timeout | `_request` | `NETWORK_ERROR` (502) | Generic Ring error |
| 2xx body we can't parse | `_json`, `events_for_day`, `snapshot` | `MALFORMED_RESPONSE` (502) | Generic Ring error |
| Any other status | `_request` | `UNEXPECTED_STATUS` (502) | Generic Ring error |
| 416 / 422 / 425 on a frame | `frame` | `MEDIA_NOT_FOUND` / `CORRUPT_RECORDING` / `RECORDING_NOT_READY` | That one event is labelled by the rules, and its reason reads "no Ring frame: MEDIA_NOT_FOUND". The rest of the briefing is unaffected. A 416 within 2 minutes of the event and any 425 are asked again later; a 422 or an older 416 is not. |
| Webhook without `RING_HMAC_KEY` | `WebhookLog.handle` | `NOT_CONFIGURED` (503) | n/a |
| Webhook with a bad or missing signature | same | `BAD_SIGNATURE` (401) | n/a |
| Webhook signed but not a v1.1 payload | same | `MALFORMED` (400) | n/a |
| Repeated `meta.request_id` | same | `DUPLICATE` (200, not stored twice) | n/a |

Every row is covered by a test in [tests/test_ring_api.py](../tests/test_ring_api.py) or [tests/test_webhooks.py](../tests/test_webhooks.py), run against responses shaped like Ring's documentation. Two rows have also been seen live: `TOKEN_REJECTED` (401 on a bad token) and `FORBIDDEN` (403 on a `latest_in_range` image download with a Playground token), see section 8.

## 6. Docs vs live API differences

| Finding | Source | How we found it | Status |
|---|---|---|---|
| Event History has no "answered" or "door opened" field, so Shopfront can't tell whether anyone answered a ding. We dropped "missed deliveries" and report "doorbell while closed" instead. | API reference, Event History response fields | Reading the docs while replacing the placeholder client | Docs only |
| The token grants are `authorization_code` and `refresh_token`. There is no `client_credentials` grant, so a server can't fetch its own token. A user has to link an account first, or use a Playground token. | API reference, Authentication Endpoints | The original scaffold assumed `client_credentials` | Docs only |
| Partner-initiated OAuth (PKCE) is invite-only. The open path is one-way linking, which needs three public HTTPS endpoints (Token Exchange URL, Account Link URL, Webhook URL) before a single API call. | API reference, Partner-Initiated OAuth 2.0 | Docs | Docs only |
| Ring's sample app (`AmazonAppDev/ring-api-helloworld`) checks webhooks with a Bearer secret (`RING_WEBHOOK_SECRET`), while the API reference says HMAC-SHA256 in `X-Signature`. Shopfront follows the API reference. | Sample repo `.env.example` vs API reference | Comparing the two | Unverified against a live webhook |
| History is time-gated to events after the user's consent, so "same day last week" is empty for the first week after linking. The briefing says so instead of showing 0 as a real comparison. | API reference, Event History | Docs | Docs only |
| Whether `event_types=motion.human` returns anything when Smart Alerts is off is not documented. Shopfront defaults to `ding,motion,on_demand`. | API reference | Docs | Open question |
| A composite `event_types` filter may not be applied: another team logged the Playground answering `motion.vehicle` with its `on_demand` events. A `package_delivery` tag makes an event a courier with no model call, so Shopfront tags nothing when a filtered answer holds an event type it didn't ask for, or is every one of the day's motion events (more than one). The filter is then marked unreliable for that device, not asked again, and the reason is logged. | Another team's friction log | Their live Playground calls | Unverified by us live; guarded in code ([tests/test_ring_filters.py](../tests/test_ring_filters.py)) |
| Event History items carry no `sub_type`, and its Event Types table lists `motion.human`, `.vehicle`, `.animal`, `.other_motion`, not `motion.package_delivery` (a detection subtype in the Motion Detection webhook section). Shopfront asks for `motion.package_delivery` and, on a 400, stops asking and leaves those events untagged. | API reference, Event History and Notifications > Motion Detection | Docs | Open question |
| An invalid Bearer token on `GET /v1/devices` returns 401 with an `X-Request-ID` header. | Live call | `scripts/ring_proof.py` with a deliberately invalid token | **Seen live**, see section 8 |
| Our one image download on the Playground got 403 FORBIDDEN. That request was `latest_in_range` over the last 24 h, sent when Event History was empty: nothing had been recorded in that window. Another team's public friction log shows the same kind of request (`latest_in_range` over the last hour, Playground token) answered `403 TIME_RANGE_NOT_AUTHORIZED` ("Requested time range is not within authorized boundaries"), a code the API reference doesn't list. It is not evidence that Playground tokens can't download images: other teams report Playground frames from `at_timestamp` at a filed event's time. The old client then cached that 403 for every frame; now a 403 refuses only its own (device, time) unless Ring says `REQUEST_FORBIDDEN`. | Live call, plus another team's friction log | `scripts/ring_proof.py --latest-frame` (since removed) | **Seen live**, X-Request-ID 843c63da-…; the cause is our reading |
| How a Playground frame can be had: press a Playground scene; the live view it plays is filed in Event History as `on_demand` about 30 s after the clip ends; then `at_timestamp` a few seconds after that event's start, retrying 416/425 for up to 2 minutes. That is what `scripts/ring_proof.py` does, step by step. | Other teams' public Playground notes and code, and the API reference's retry guidance | Reading their code and logs | **Not yet run by us live** |
| A WHEP session on the Playground doorbell connects (ICE and DTLS up) but carries no frames unless a Playground clip is playing at that moment. | Live call | `/live` twice, 2026-10-15 08:25 and 08:26 UTC | **Seen live** |
| The Developer Playground (the hackathon's accepted "simulator") provisions one test doorbell. Our Event History query (then `event_types=ding,motion`, so it would not have asked for live views) came back empty after a simulated event; other teams saw the scene filed as `on_demand`, which Shopfront now asks for. Its Package / Vehicle / Motion buttons choose a recorded clip (31 s / 21 s / 39 s) that plays over the WHEP live-view endpoint. The clip is chosen through a console-only call, so a partner app can't pick it. That's why Shopfront has a live-view path, not just a history path. | Playground, 2026-10-15 | `GET /v1/devices` returned 1 device. `GET /v1/history/devices/{id}/events` returned `{"data": []}` before and after a simulated Package event. | **Seen live** in the Playground UI |
| The sample app offers `audio` as `sendrecv`, while the API reference says live streams are receive-only. Shopfront offers video `recvonly` only. | Sample `useWebRTCStream.ts` vs API reference | Comparing the two | Unverified live |

## 7. Config

`RING_MODE`, `RING_ACCESS_TOKEN`, `RING_REFRESH_TOKEN`, `RING_CLIENT_ID`, `RING_CLIENT_SECRET`, `RING_DEVICE_ID`, `RING_EVENT_TYPES`, `RING_HMAC_KEY`, `SHOPFRONT_PUBLIC_URL` (account linking, section 10), `SHOPFRONT_DATA_DIR`. See [.env.example](../.env.example). Secrets live only in `.env`, which git ignores.

## 8. Live proof

| Action | Environment | Response id | Date |
|---|---|---|---|
| `GET /v1/devices` with a deliberately invalid token → 401, mapped to `TOKEN_REJECTED` | Production API | X-Request-ID `90478809-976d-4bde-9881-5f5611afb2fb` ([file](../evidence/)) | 2026-10-14 |
| `GET /v1/devices` → 200, 1 device (Playground test doorbell) | Production API, Playground token | `59df1289-b5c0-4083-9374-9d88c146145e` | 2026-10-15 |
| `GET /v1/history/devices/{id}/events?event_types=ding,motion` → 200, `data: []` | same | `ba6d001b-5c40-450a-a0de-ade4d8444c07` | 2026-10-15 |
| `POST /v1/devices/{id}/media/image/download` (`latest_in_range`) → **403 FORBIDDEN** | same | `843c63da-2847-48a0-b7ef-62bb088e82cc` | 2026-10-15 |
| `POST …/media/streaming/whep/sessions` → 201 + SDP answer + Location; WebRTC connected; no frames (no clip playing) | same | `13622a41-1419-4b03-a905-5a4b976f11cf`, `a0349b9b-31ff-4e1f-96c9-7d8ab407483e` | 2026-10-15 |

To add rows, run `RING_ACCESS_TOKEN=<Playground token> .venv/bin/python -m scripts.ring_proof` and follow its prompts: it lists the devices and their status, asks you to press a Playground scene, waits for the new event in Event History, downloads that event's frame, has Bedrock label it, and writes `evidence/ring-proof-<utc>.json` with every X-Request-ID and the Bedrock RequestId (device, account and event IDs hashed; the frame goes to the git-ignored `evidence/raw/`). Commit the JSON only. `--dry-run` runs the same steps against a MockTransport and a fake Bedrock and never writes `evidence/`.

## 9. Honest scope

- Seen live, against the Developer Playground: `GET /v1/devices` (200), Event History (200, empty), a WHEP live-view session (201, WebRTC connected), and two error paths (401, 403). Not seen live: a non-empty Event History page, an `on_demand` event, a downloaded frame, `GET /v1/devices/{id}/status`, a video frame over live view, and a webhook from Ring. Those paths are tested only against Ring's documented response shapes.
- One shop, one doorbell, the first camera the token sees. Multi-camera devices (Ring Elite) would need a `components` entry on frame downloads, which Shopfront doesn't send yet.
- Shopfront implements one-way account linking as the API reference describes it (section 10), but has not been through a real Ring AppStore install or certification. Live mode has run only on a Playground token.
- Webhook events feed the briefing, but none has arrived from Ring itself: the Playground sends none, so the demo uses signed replays marked `ring-webhook-replay`. Whether `sub_type` arrives on a real account is unverified.
- Synthetic mode exists so the app runs without a Ring account. Its events are generated, and the UI labels them that way on every page.

## 10. Account linking (one-way)

How a shop uses Shopfront every day: the owner adds Shopfront from the Ring AppStore once, and Shopfront stays signed in on a refresh token it rotates. The Playground token paste on `/connect` stays, for trying Shopfront out. `/setup` offers both. Code: [app/ring/link.py](../app/ring/link.py), routes in [app/main.py](../app/main.py), tests in [tests/test_ring_link.py](../tests/test_ring_link.py) (MockTransport shaped like the reference's examples).

All section names below are from the API reference, https://developer.amazon.com/docs/ring/api-documentation.html.

**What to register** (Getting Started 1.3 and 4.1: four partner-hosted HTTPS endpoints, all required). Set `SHOPFRONT_PUBLIC_URL` to the HTTPS address Ring reaches the app at, add its host name to `SHOPFRONT_ALLOWED_HOSTS`, and `/ring/link` lists them:

| Ring's name | Shopfront | Who calls it | Gate |
|---|---|---|---|
| Token Exchange URL | `POST /ring/link/token` | Ring's backend | Public: the code is useless without our client secret. Body capped at 8 KB. |
| Account Link URL | `GET /ring/link?nonce=…&time=…`, then `POST /ring/link` | The owner's browser, redirected by Ring | Owner only: the shop computer, or a device paired with its code. The pairing page is the sign-in. |
| Webhook URL | `POST /webhooks/ring` (or the Lambda's `WebhookUrl`, [aws.md](aws.md)) | Ring's backend | HMAC (section 3) |
| App Homepage URL | `GET /ring/link/home` → `/setup#ring` | The owner, from the Ring app | Owner only |

**The steps**, each as the reference describes it:

1. **Token exchange** (Getting Started 5, "Ring Backend ... sends it directly to your Token Exchange URL"; 5.3, the code lives 60 seconds). Shopfront redeems the code at once: `POST https://oauth.ring.com/oauth/token`, form-encoded `grant_type=authorization_code`, `client_id`, `code`, `client_secret` (API Reference > Authentication Endpoints > Exchange Authorization Code). It answers 200 when that worked (7.1, Step 4).
2. **Account ID** (Users API > Store Account ID at Token Receipt): `GET /v1/users/me`, `data.id`. The token pair is kept "unclaimed" under that ID in `.data/ring-link-unclaimed.json` (0600) for at most an hour, at most 20 of them.
3. **The redirect** (One-Way Account Linking, Steps 1 to 3). Ring sends the owner to the Account Link URL with `nonce` and `time`. `time` must be at most 600 seconds old and not in the future (Nonce Technical Reference). The page is a sign-in and a button, never an automatic verification (Step 2, "Important"): reaching it needs the owner gate, and nothing is matched or sent to Ring until the owner presses **Link**.
4. **Matching** (Step 4). For each unclaimed token, Shopfront recomputes `base64url_nopad(HMAC-SHA256(RING_HMAC_KEY, "<time>:<account_id>"))` and compares in constant time. The same key signs webhooks in hex; the nonce is URL-safe Base64 without padding ("Same key, different encoding").
5. **Confirm** (Step 5; App Integrations > POST): `POST /v1/accounts/me/app-integrations` with the matched token and `{"account_identifier": <masked>, "nonce": <nonce>}`. Shopfront has no user emails; the identifier is the shop name masked the way the reference masks an email (`Corner Bakery` → `C***y`), which is optional per the field table.
6. **Complete** (Step 7; App Integrations > PATCH, "mandatory"): `PATCH` the same endpoint with `{"status": "completed"}`. Never sent after a failed POST.
7. **Keep the tokens**. The refresh token goes to `.data/ring-refresh-token.json`, created 0600 and swapped in atomically, the file `RingAuth` already reads and rewrites on every refresh (Refresh Tokens: rotated "with each exchange"). The link record (`.data/ring-link.json`, 0600) holds the Account ID, status and the POST/PATCH X-Request-IDs, no token. A pasted Playground token is removed when a link succeeds; one pasted later wins until removed.

**CSRF and forgery.** The confirm is a POST that the owner gate refuses from another site (Origin, Referer or Sec-Fetch-Site), and it must carry a token Shopfront put in its own form (HMAC of the nonce and time under a per-process secret). A forged nonce, a stale `time` or a nonce for another time claims nothing and calls nothing.

**Unlinking.** One-way apps get 403 on `DELETE /v1/accounts/me/app-integrations` (App Integrations > Removing the integration), so Shopfront never calls it. "Forget the linked account" deletes the tokens here and says to remove Shopfront in the Ring app. A signed `app_integration_removed` webhook for the linked account (Notifications > App Integration Removed) deletes them too; a replay signed with the feeder's key can't. `app_integration_added` records when Ring confirmed the link.

**Not verified** (no Ring AppStore install has been run against Shopfront; none of this has an X-Request-ID in evidence/):

- The shape of Ring's request to the Token Exchange URL. The reference says only that Ring "POSTs the auth code" and expects a 200. Shopfront reads `code` (or `authorization_code`) from a JSON body, a form body or the query string. If Ring signs that request, the reference doesn't say how, so Shopfront checks no signature on it.
- What Ring does with a non-200 from the Token Exchange URL (Shopfront answers 502 when oauth.ring.com refuses the code, 503 when linking isn't configured).
- That `x-request-id` comes back on the app-integrations calls (the reference documents it for the device APIs).
- The whole flow end to end, including the confirmation email and the `app_integration_added` webhook.
