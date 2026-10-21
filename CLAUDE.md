# Shopfront
One line: the carrier scanned "business closed"; your doorbell says you were open. A receiving desk for small shops, built on the Ring doorbell they already have.
For: owner of a one-door shop (bakery, café, boutique) that orders stock from suppliers · Job: check a carrier's "business closed" / "delivery attempted" scan against posted hours and the door, and hand the owner a dispute pack they send themselves · Done: a pack for a real carrier scan, backed by real Ring events with request IDs (and Bedrock labels where a frame exists).
Sponsor mechanism (critical path): orders/invoices CSV → Ring Event History + signed webhooks (motion sub_type `package_delivery` = courier at stage 0, no model call) → Bedrock vision label from the Ring frame for the rest (rules otherwise, marked as guesses) → scan check (reconcile.py) → dispute pack (dispute.py). Live door: Ring WHEP live view (the Developer Playground simulator plays here) → sampled frames → Bedrock → one visit. Ask Shopfront: Strands agent on Bedrock with tools over the same data (no AgentCore).
Features in scope: orders import · receiving desk (orders vs couriers seen) · business-closed / attempted scan check · dispute pack + zip · staff tap · Ask Shopfront · morning briefing (secondary: doorbell presses while closed, staff at the door, traffic vs last week).
Not building: alerts/push, multi-shop, Ring Appstore listing/certification, account-linking UI.

Stack: Python 3.11+, FastAPI, Jinja, httpx, boto3. Demo: `.venv/bin/python -m scripts.demo` · Run: `.venv/bin/uvicorn app.main:app --reload --port 8000` · Test: `.venv/bin/pip install -r requirements-dev.txt` then `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check .` · Proof: `.venv/bin/python -m scripts.ring_proof` (guided; `--dry-run` rehearses on mocks)
CI: .github/workflows/ci.yml runs `ruff check .` and `pytest -q` on Python 3.11 and 3.13 on every push and PR, installing requirements-dev.txt (pinned). Tests never touch the live Ring or AWS APIs; they use MockTransport and fakes, and tests/conftest.py blocks sockets and pins the environment.

Repo map: app/ring/api.py (live Ring client) · app/ring/synthetic.py (demo generator) · app/ring/webhooks.py · app/live.py (live-view sessions) · app/classify/{rules,bedrock}.py · app/briefing.py · app/orders.py · app/reconcile.py · app/dispute.py · app/main.py · app/agent.py (Strands agent) · scripts/feed_webhook.py · scripts/demo.py + samples/orders.csv · docs/{ring,bedrock,receiving-desk,receipts,aws}.md · evidence/ (live captures only) · scripts/ring_proof.py · infra/template.yaml + app/store/ (webhook Lambda and DynamoDB log; template ready, not deployed)

Invariants (never):
- never fall back from live Ring to synthetic data; a Ring failure renders its RingStatus
- never let a classifier read DoorEvent.truth (the generator's answer key); tests only
- never label an event "bedrock" unless Bedrock saw that event's Ring frame
- never put tokens, client secrets, HMAC keys or AWS keys in the repo; `.env` and `.data/` are git-ignored
- never write device or account IDs to evidence/ unhashed; never commit Ring frames (evidence/raw/ is ignored)
- never add a Ring endpoint or field that isn't in https://developer.amazon.com/docs/ring/api-documentation.html; cite the section
- never claim AgentCore (it isn't used) or a live Bedrock/Ring-webhook capture that evidence/ doesn't hold
- never put a real carrier's name or trademark in samples/ or the demo; the demo carrier is made up

Design: follow design.md; tokens in app/templates/_style.html. One accent (blue) for today's data, warn orange only for things the owner should act on.

Rules that disqualify (Devpost, Ring track): repo must call Ring at runtime (not just name it) · video < 3 min, public on YouTube/Vimeo, shows Ring working through a simulator or real device · open-source licence visible in the GitHub About box, or private repo shared with the Amazon reviewers · product feedback for every tool used · English.

Evidence rules: a claim is done only with a row in the private hackathon/evidence.md and an artifact (Ring X-Request-ID, Bedrock RequestId, test output). Mocks and synthetic data are never evidence. The README proof table shows labels.
Forbidden artifacts: plans, research, briefs, audits and the tracker live in the private parent folder (../amazon-appdev-2026/hackathon/), never in this repo. Recover, don't delete.
Workflow: prove the live Ring path first, then Bedrock, then polish, then the demo.
Context rules: no MCP unless needed; cite Ring docs by URL and section.
