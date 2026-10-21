# Evidence

Records of live calls to the Ring Partner API, each with Ring's `X-Request-ID`. Tokens are never
written; device and account IDs are hashed. Frames would go to `evidence/raw/`, which git ignores.
There are three files. They were made in two different ways:

| File | How it was made | What it shows |
|---|---|---|
| `ring-invalid-token-check-20261014T175049Z.json` | Written by `scripts/ring_proof.py` | A deliberately invalid token sent to `GET /v1/devices` → 401 with an X-Request-ID. Proves the client reaches the production API and maps a rejection to `TOKEN_REJECTED`. |
| `ring-proof-20261015T082514Z.json` | Written by `scripts/ring_proof.py` with a Ring Developer Playground token | `GET /v1/devices` → 200 (one test doorbell), Event History → 200 with no events, image download → 403 `FORBIDDEN`. |
| `ring-live-view-20261015T0826Z.json` | **Written by hand**, not by a script, to record two `/live` sessions started from a browser | Two WHEP live-view sessions on the Playground doorbell: 201 with an SDP answer, WebRTC connected in the second session, no video frames (no Playground clip was playing). X-Request-IDs `13622a41-1419-4b03-a905-5a4b976f11cf` and `a0349b9b-31ff-4e1f-96c9-7d8ab407483e`. No script wrote this file, so its fields are a summary, not raw output. |

**Not here yet:** a downloaded Ring frame, a video frame from live view, a webhook sent by Ring,
and any Amazon Bedrock call (no Bedrock RequestId has been captured). The README proof table marks
each of those as not yet verified.

To add a capture: `RING_MODE=live RING_ACCESS_TOKEN=… .venv/bin/python -m scripts.ring_proof --bedrock`.
