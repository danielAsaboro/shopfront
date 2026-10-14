# Evidence

Each `ring-proof-*.json` file is the output of one run of `scripts/ring_proof.py` against the
live Ring Partner API (and, with `--bedrock`, Amazon Bedrock). Tokens are never written; device
and account IDs are hashed. Frames are kept out of git in `evidence/raw/`.

No live capture exists yet. Until one does, the README proof table marks Ring and Bedrock as
not yet verified.
