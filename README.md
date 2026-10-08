# Shopfront

A daily briefing for small-shop owners, built from their Ring front-door camera.

Shopfront reads front-door events and snapshots through the Ring Partner API, classifies each
visit with Amazon Bedrock vision (customer, delivery, staff, after-hours), and turns the day into
a short briefing: foot traffic by hour, the busiest window compared with the same day last week,
missed deliveries, and when the shop actually opened and closed.

Built for the Amazon "Build, Ship, Shape" hackathon (Ring track).

## Run it

Needs Python 3.11+. No Ring or AWS credentials needed: by default it runs on synthetic events.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # optional
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 and pick a date. JSON is at `/api/briefing?date=YYYY-MM-DD`
and `/api/events?date=YYYY-MM-DD`; `/healthz` shows which Ring source and classifier are active.

Tests: `.venv/bin/python -m pytest -q`

## How it fits together

| Piece | File | Notes |
|---|---|---|
| Ring source | `app/ring/` | `RingClient` interface. `MockRingClient` generates deterministic synthetic days (same date → same events). `PartnerRingClient` is the OAuth 2.0 client for the Partner API sandbox; endpoint paths are placeholders until we have sandbox access. |
| Classifier | `app/classify/` | `StubClassifier` (rules, no AWS). `BedrockClassifier` sends the snapshot to a Bedrock vision model via the Converse API and falls back to the rules when there's no image or a call fails. |
| Briefing | `app/briefing.py` | Today vs the same weekday last week. |
| Web | `app/main.py`, `app/templates/` | FastAPI and a server-rendered dashboard. |

## Configuration

All settings live in `.env` (see `.env.example`). `.env` is git-ignored; never commit secrets.

- `RING_MODE=partner` plus the `RING_*` values switches to the real Ring Partner API.
- `CLASSIFIER=auto` uses Bedrock when the standard AWS credential chain resolves (for example
  after `aws login`), otherwise the stub. Force it with `stub` or `bedrock`.

## Roadmap

- Wire up the Ring Partner API sandbox (real endpoints, snapshot download).
- Run Bedrock classification against real snapshots and cache results per event.
- Stretch: a small Streamable HTTP MCP server exposing the briefing and events.

## License

MIT
