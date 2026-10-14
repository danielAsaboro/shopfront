# Shopfront
One line: a morning briefing for small-shop owners, built from the Ring doorbell they already have.
For: owner of a one-door shop (bakery, café, salon) · Job: tell them what happened at the door yesterday: deliveries that rang while closed, when staff actually arrived, and foot traffic vs last week · Done: a briefing for a real day of real Ring events, labelled by Bedrock from Ring frames.
Sponsor mechanism (critical path): Ring Event History + image download → Bedrock vision label → briefing.
Features in scope: briefing headline · deliveries while closed · staff first/last at the door · hourly foot traffic vs same weekday · per-event label with reason.
Not building: live video, alerts/push, multi-shop, Ring Appstore listing/certification, account-linking UI.

Stack: Python 3.11+, FastAPI, Jinja, httpx, boto3. Run: `.venv/bin/uvicorn app.main:app --reload --port 8000` · Test: `.venv/bin/python -m pytest -q` · Proof: `.venv/bin/python -m scripts.ring_proof --bedrock`
CI does NOT exist. Tests never touch the live Ring or AWS APIs; they use MockTransport and fakes.

Repo map: app/ring/api.py (live Ring client) · app/ring/synthetic.py (demo generator) · app/ring/webhooks.py · app/classify/{rules,bedrock}.py · app/briefing.py · app/main.py · docs/{ring,bedrock}.md · evidence/ (live captures only) · scripts/ring_proof.py

Invariants (never):
- never fall back from live Ring to synthetic data; a Ring failure renders its RingStatus
- never let a classifier read DoorEvent.truth (the generator's answer key); tests only
- never label an event "bedrock" unless Bedrock saw that event's Ring frame
- never put tokens, client secrets, HMAC keys or AWS keys in the repo; `.env` and `.data/` are git-ignored
- never write device or account IDs to evidence/ unhashed; never commit Ring frames (evidence/raw/ is ignored)
- never add a Ring endpoint or field that isn't in https://developer.amazon.com/docs/ring/api-documentation.html; cite the section

Design: tokens and layout in app/templates/_style.html. One accent (blue) for today's data, warn orange only for things the owner should act on.

Rules that disqualify (Devpost, Ring track): repo must call Ring at runtime (not just name it) · video < 3 min, public on YouTube/Vimeo, shows Ring working through a simulator or real device · open-source licence visible in the GitHub About box, or private repo shared with the Amazon reviewers · product feedback for every tool used · English.

Evidence rules: a claim is done only with a row in the private hackathon/evidence.md and an artifact (Ring X-Request-ID, Bedrock RequestId, test output). Mocks and synthetic data are never evidence. The README proof table shows labels.
Forbidden artifacts: plans, research, briefs, audits and the tracker live in the private parent folder (../amazon-appdev-2026/hackathon/), never in this repo. Recover, don't delete.
Workflow: prove the live Ring path first, then Bedrock, then polish, then the demo.
Context rules: no MCP unless needed; cite Ring docs by URL and section.
