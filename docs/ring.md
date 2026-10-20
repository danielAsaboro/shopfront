# Ring Partner API in Shopfront

## 1. The rule we enforce

Every number in the briefing comes from Ring events for the shop's doorbell, or the page says it doesn't: Shopfront never fills a Ring failure with made-up data.

## 2. Enforcement layers

| Layer | Where | What it blocks if the others fail |
|---|---|---|
| Mode switch | `RING_MODE` in [app/ring/\_\_init\_\_.py](../app/ring/__init__.py) | Synthetic and live sources are never mixed in one process. Synthetic mode puts a banner on every page. |
| Config check at start-up | `RingAuth.__init__` in [app/ring/api.py](../app/ring/api.py) | Live mode with no token, or both token kinds, becomes `NOT_CONFIGURED`. Every page and API call then returns that state (HTTP 503). |
| Per-call status mapping | `RingApiClient._request` | Every non-2xx response becomes a named `RingStatus`. The FastAPI handler in [app/main.py](../app/main.py) renders it as an error page or JSON error, never a briefing. |
| Response shape check | `events_for_day`, `snapshot` | A 2xx body we can't read becomes `MALFORMED_RESPONSE` instead of a guess. |
| Webhook signature | [app/ring/webhooks.py](../app/ring/webhooks.py) | An unsigned, wrongly signed, or unconfigured webhook is refused before any parsing. |

## 3. Flow

1. The owner opens the briefing for a date.
2. Shopfront gets an access token: the Playground token as-is, or `POST https://oauth.ring.com/oauth/token` with `grant_type=refresh_token`. Ring rotates the refresh token, so the new one is saved to `.data/ring-refresh-token.json`.
3. If `RING_DEVICE_ID` is empty, `GET /v1/devices` and take the first device.
4. `GET /v1/history/devices/{id}/events?event_types=ding,motion`. Pages are newest first. Shopfront follows `links.next` until an event is older than the requested day, then stops.
5. For each event, when Bedrock is on: `POST /v1/devices/{id}/media/image/download` with `{"type": "at_timestamp", "timestamp": <event start ms>}`. Ring answers 303, and the client follows the pre-signed `Location` to the JPEG. httpx drops the Bearer header on that cross-origin hop.
6. The frame goes to Bedrock (see [bedrock.md](bedrock.md)), and the label is cached per Ring event ID so a frame is classified once.
7. The same steps run for the same weekday last week, and the briefing compares the two.

**Live door** (`/live`): the browser builds a WebRTC offer (video, receive-only) and posts it to Shopfront. Shopfront forwards it to `POST /v1/devices/{id}/media/streaming/whep/sessions` with the Bearer token (the token never reaches the browser), returns Ring's SDP answer, and keeps the session URL from `Location`. The page samples a frame every 3 seconds. Up to 6 frames per session go to Bedrock, the session becomes one visit labelled by its most confident frame, and `DELETE <session URL>` ends it. Sessions are logged to `.data/live-sessions.jsonl` and merged into the briefing.

Signed webhooks (`motion_detected`, `button_press`, …) are accepted at `POST /webhooks/ring`, logged, and fed into the door timeline as stage 0 of the funnel: the motion `sub_type` routes `package_delivery` straight to a courier label and skips `vehicle` and `other_motion` ([receiving-desk.md](receiving-desk.md), section 2). The briefing still reads Event History, so it works when no webhook URL is registered.

## 4. Endpoints used

| Endpoint | Purpose | Auth | Code |
|---|---|---|---|
| `POST https://oauth.ring.com/oauth/token` (`refresh_token`) | New access token | client id + secret | `RingAuth._do_refresh` |
| `GET /v1/devices` | Find the doorbell | Bearer | `RingApiClient.list_devices` |
| `GET /v1/history/devices/{id}/events` | The day's dings and motion | Bearer | `RingApiClient.events_for_day` |
| `POST /v1/devices/{id}/media/image/download` → 303 → GET | Frame at the event time | Bearer, then pre-signed URL | `RingApiClient.snapshot` |
| `POST /v1/devices/{id}/media/streaming/whep/sessions` (SDP) → 201 + `Location` | Live view | Bearer | `RingApiClient.whep_start` |
| `DELETE <Location>` | End live view | Bearer | `RingApiClient.whep_stop` |
| `POST /webhooks/ring` (ours) | Receive Ring notifications | `X-Signature: sha256=<hex>` | `WebhookLog.handle` |

All Ring calls go to the production base URL `https://api.amazonvision.com`. Ring's docs list no separate staging API host.

## 5. Failure handling

| Failure | Detected in | Resulting state | What the owner sees |
|---|---|---|---|
| No token, or both token kinds | `RingAuth.__init__` | `NOT_CONFIGURED` (503) | "Can't read Ring right now", with setup steps |
| 401 with a Playground token | `_request` | `TOKEN_REJECTED` (401) | Same page: generate a fresh Playground token |
| 401 with a refresh token | `_request` | One forced refresh and retry, then `TOKEN_REJECTED` | Same page |
| Refresh refused by oauth.ring.com | `_do_refresh` | `REFRESH_FAILED` (401) | "Re-link the Ring account" |
| 403 | `_request` | `FORBIDDEN` (502) | "This token can't see the doorbell" |
| 404 | `_request` | `DEVICE_NOT_FOUND` (502) | Same |
| Token sees no devices | `device_id` | `NO_DEVICES` (502) | Same |
| 429 | `_request` | `RATE_LIMITED` (502) | "Try again in a minute" |
| 5xx, incl. 503 device offline | `_request` | `RING_UNAVAILABLE` (502) | Generic Ring error, X-Request-ID shown |
| DNS, TLS or timeout | `_request` | `NETWORK_ERROR` (502) | Generic Ring error |
| 2xx body we can't parse | `_json`, `events_for_day`, `snapshot` | `MALFORMED_RESPONSE` (502) | Generic Ring error |
| Any other status | `_request` | `UNEXPECTED_STATUS` (502) | Generic Ring error |
| 416 / 422 / 425 on a frame | `snapshot` | `MEDIA_NOT_FOUND` / `CORRUPT_RECORDING` / `RECORDING_NOT_READY` | That one event is labelled by the rules, and its reason reads "no Ring frame: MEDIA_NOT_FOUND". The rest of the briefing is unaffected. |
| Webhook without `RING_HMAC_KEY` | `WebhookLog.handle` | `NOT_CONFIGURED` (503) | n/a |
| Webhook with a bad or missing signature | same | `BAD_SIGNATURE` (401) | n/a |
| Webhook signed but not a v1.1 payload | same | `MALFORMED` (400) | n/a |
| Repeated `meta.request_id` | same | `DUPLICATE` (200, not stored twice) | n/a |

Every row is covered by a test in [tests/test_ring_api.py](../tests/test_ring_api.py) or [tests/test_webhooks.py](../tests/test_webhooks.py), run against responses shaped like Ring's documentation. Only `TOKEN_REJECTED` has also been seen live so far (section 8).

## 6. Docs vs live API differences

| Finding | Source | How we found it | Status |
|---|---|---|---|
| Event History has no "answered" or "door opened" field, so Shopfront can't tell whether anyone answered a ding. We dropped "missed deliveries" and report "doorbell while closed" instead. | API reference, Event History response fields | Reading the docs while replacing the placeholder client | Docs only |
| The token grants are `authorization_code` and `refresh_token`. There is no `client_credentials` grant, so a server can't fetch its own token. A user has to link an account first, or use a Playground token. | API reference, Authentication Endpoints | The original scaffold assumed `client_credentials` | Docs only |
| Partner-initiated OAuth (PKCE) is invite-only. The open path is one-way linking, which needs three public HTTPS endpoints (Token Exchange URL, Account Link URL, Webhook URL) before a single API call. | API reference, Partner-Initiated OAuth 2.0 | Docs | Docs only |
| Ring's sample app (`AmazonAppDev/ring-api-helloworld`) checks webhooks with a Bearer secret (`RING_WEBHOOK_SECRET`), while the API reference says HMAC-SHA256 in `X-Signature`. Shopfront follows the API reference. | Sample repo `.env.example` vs API reference | Comparing the two | Unverified against a live webhook |
| History is time-gated to events after the user's consent, so "same day last week" is empty for the first week after linking. The briefing says so instead of showing 0 as a real comparison. | API reference, Event History | Docs | Docs only |
| Whether `event_types=motion.human` returns anything when Smart Alerts is off is not documented. Shopfront defaults to `ding,motion`. | API reference | Docs | Open question |
| An invalid Bearer token on `GET /v1/devices` returns 401 with an `X-Request-ID` header. | Live call | `scripts/ring_proof.py` with a deliberately invalid token | **Seen live**, see section 8 |
| A Playground token gets 403 FORBIDDEN on image download, so frames for Playground devices can only come from live view. | Live call | `scripts/ring_proof.py --latest-frame` | **Seen live**, X-Request-ID 843c63da-… |
| A WHEP session on the Playground doorbell connects (ICE and DTLS up) but carries no frames unless a Playground clip is playing at that moment. | Live call | `/live` twice, 2026-10-15 08:25 and 08:26 UTC | **Seen live** |
| The Developer Playground (the hackathon's accepted "simulator") provisions one test doorbell. Its Event History stays empty, even after a simulated event. Its Package / Vehicle / Motion buttons choose a recorded clip (31 s / 21 s / 39 s) that plays over the WHEP live-view endpoint. The clip is chosen through a console-only call, so a partner app can't pick it. That's why Shopfront has a live-view path, not just a history path. | Playground, 2026-10-15 | `GET /v1/devices` returned 1 device. `GET /v1/history/devices/{id}/events` returned `{"data": []}` before and after a simulated Package event. | **Seen live** in the Playground UI |
| The sample app offers `audio` as `sendrecv`, while the API reference says live streams are receive-only. Shopfront offers video `recvonly` only. | Sample `useWebRTCStream.ts` vs API reference | Comparing the two | Unverified live |

## 7. Config

`RING_MODE`, `RING_ACCESS_TOKEN`, `RING_REFRESH_TOKEN`, `RING_CLIENT_ID`, `RING_CLIENT_SECRET`, `RING_DEVICE_ID`, `RING_EVENT_TYPES`, `RING_HMAC_KEY`, `SHOPFRONT_DATA_DIR`. See [.env.example](../.env.example). Secrets live only in `.env`, which git ignores.

## 8. Live proof

| Action | Environment | Response id | Date |
|---|---|---|---|
| `GET /v1/devices` with a deliberately invalid token → 401, mapped to `TOKEN_REJECTED` | Production API | X-Request-ID `90478809-976d-4bde-9881-5f5611afb2fb` ([file](../evidence/)) | 2026-10-14 |
| `GET /v1/devices` → 200, 1 device (Playground test doorbell) | Production API, Playground token | `59df1289-b5c0-4083-9374-9d88c146145e` | 2026-10-15 |
| `GET /v1/history/devices/{id}/events?event_types=ding,motion` → 200, `data: []` | same | `ba6d001b-5c40-450a-a0de-ade4d8444c07` | 2026-10-15 |
| `POST /v1/devices/{id}/media/image/download` (`latest_in_range`) → **403 FORBIDDEN** | same | `843c63da-2847-48a0-b7ef-62bb088e82cc` | 2026-10-15 |
| `POST …/media/streaming/whep/sessions` → 201 + SDP answer + Location; WebRTC connected; no frames (no clip playing) | same | `13622a41-1419-4b03-a905-5a4b976f11cf`, `a0349b9b-31ff-4e1f-96c9-7d8ab407483e` | 2026-10-15 |

To add a row, run `RING_MODE=live RING_ACCESS_TOKEN=… .venv/bin/python -m scripts.ring_proof --bedrock` and commit the JSON it writes to `evidence/`.

## 9. Honest scope

- So far, only the error path has been seen live. The success path (devices, events, frames) is tested only against documented response shapes.
- One shop, one doorbell, the first device the token sees. Multi-camera devices (Ring Elite) would need a `components` entry on frame downloads, which Shopfront doesn't send yet.
- Shopfront hasn't been through Ring account linking or certification. Live mode runs on a Playground token or a refresh token pasted into `.env`, which is fine for a demo but not for real customers.
- Webhook events feed the briefing, but none has arrived from Ring itself: the Playground sends none, so the demo uses signed replays marked `ring-webhook-replay`. Whether `sub_type` arrives on a real account is unverified.
- Synthetic mode exists so the app runs without a Ring account. Its events are generated, and the UI labels them that way on every page.
