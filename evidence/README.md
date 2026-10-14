# Evidence

Each `ring-proof-*.json` file is the output of one run of `scripts/ring_proof.py` against the
live Ring Partner API (and, with `--bedrock`, Amazon Bedrock). Tokens are never written; device
and account IDs are hashed. Frames are kept out of git in `evidence/raw/`.

So far there is one live capture, `ring-invalid-token-check-*.json`: a deliberately invalid token
sent to `GET /v1/devices`, which returned 401 with an X-Request-ID. It proves the client reaches
the production API and handles rejection. It does not prove reading a real doorbell. Until a
success capture exists, the README proof table marks Ring and Bedrock as not yet verified.
