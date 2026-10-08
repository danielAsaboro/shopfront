from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.briefing import build_briefing, classify_day
from app.classify import make_classifier
from app.config import settings
from app.ring import make_ring_client

app = FastAPI(title="Shopfront")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

ring = make_ring_client(settings)
classifier = make_classifier(settings)


def _parse_day(value: str | None) -> date:
    if not value:
        return datetime.now(settings.tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str | None = Query(None)):
    day = _parse_day(date)
    b = build_briefing(ring, classifier, day, settings.shop_name)
    peak = max(b.hourly_customers + b.hourly_customers_last_week) or 1
    return templates.TemplateResponse(request, "dashboard.html", {"b": b, "peak": peak})


@app.get("/api/briefing")
def api_briefing(date: str | None = Query(None)):
    return asdict(build_briefing(ring, classifier, _parse_day(date), settings.shop_name))


@app.get("/api/events")
def api_events(date: str | None = Query(None)):
    visits = classify_day(ring, classifier, _parse_day(date))
    return [{
        "id": v.event.id, "time": v.event.timestamp.isoformat(), "kind": v.event.kind,
        "answered": v.event.answered, "type": v.visit_type.value,
        "confidence": v.confidence, "reason": v.reason, "classifier": v.source,
    } for v in visits]


@app.get("/healthz")
def healthz():
    return {"ok": True, "ring": ring.name, "classifier": classifier.name}
