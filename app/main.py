from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.briefing import build_briefing, classify_day
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.config import settings
from app.hours import parse_hours
from app.ring import make_ring_client
from app.ring.api import RingError, RingStatus
from app.ring.webhooks import WebhookLog, WebhookStatus

app = FastAPI(title="Shopfront")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

DATA = Path(settings.data_dir)
hours = parse_hours(settings.shop_hours)
classifier = make_classifier(settings)
cache = ClassificationCache(DATA / "classifications.json")
webhook_log = WebhookLog(DATA / "ring-webhooks.jsonl")
try:
    ring = make_ring_client(settings)
    ring_config_error: RingError | None = None
except RingError as exc:  # live mode without credentials: every page shows the named error
    ring, ring_config_error = None, exc


def _parse_day(value: str | None) -> date:
    if not value:
        return datetime.now(settings.tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


def _briefing(day: date):
    if ring_config_error:
        raise ring_config_error
    return build_briefing(ring, classifier, day, settings.shop_name, hours, cache)


def _ring_error_body(exc: RingError) -> dict:
    return {"error": exc.status.value, "detail": exc.detail, "http_status": exc.http_status,
            "request_id": exc.request_id}


@app.exception_handler(RingError)
def ring_error(request: Request, exc: RingError):
    # Fail closed: a Ring failure is shown as itself, never replaced with synthetic data.
    code = 401 if exc.status in (RingStatus.TOKEN_REJECTED, RingStatus.REFRESH_FAILED) else 502
    if exc.status == RingStatus.NOT_CONFIGURED:
        code = 503
    if request.url.path.startswith("/api/"):
        return JSONResponse(_ring_error_body(exc), status_code=code)
    return templates.TemplateResponse(request, "ring_error.html", {"e": exc, "shop": settings.shop_name},
                                      status_code=code)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str | None = Query(None)):
    day = _parse_day(date)
    b = _briefing(day)
    peak = max(b.hourly_customers + b.hourly_customers_last_week) or 1
    return templates.TemplateResponse(request, "dashboard.html", {
        "b": b, "peak": peak, "device": getattr(ring, "_device_id", "") or "",
        "request_id": getattr(ring, "last_request_id", None)})


@app.get("/api/briefing")
def api_briefing(date: str | None = Query(None)):
    return asdict(_briefing(_parse_day(date)))


@app.get("/api/events")
def api_events(date: str | None = Query(None)):
    if ring_config_error:
        raise ring_config_error
    visits = classify_day(ring, classifier, _parse_day(date), hours, cache)
    return [{"id": v.event.id, "time": v.event.timestamp.isoformat(), "kind": v.event.kind,
             "event_source": v.event.source, "type": v.visit_type.value, "confidence": v.confidence,
             "reason": v.reason, "classifier": v.source, "bedrock_request_id": v.request_id}
            for v in visits]


@app.post("/webhooks/ring")
async def ring_webhook(request: Request):
    status = webhook_log.handle(await request.body(), request.headers.get("X-Signature"),
                                settings.ring_hmac_key)
    code = {WebhookStatus.ACCEPTED: 200, WebhookStatus.DUPLICATE: 200, WebhookStatus.MALFORMED: 400,
            WebhookStatus.BAD_SIGNATURE: 401, WebhookStatus.NOT_CONFIGURED: 503}[status]
    return JSONResponse({"status": status.value}, status_code=code)


@app.get("/api/webhooks")
def api_webhooks():
    return webhook_log.recent()


@app.get("/healthz")
def healthz():
    return {"ok": ring_config_error is None, "ring": ring.name if ring else None,
            "ring_error": ring_config_error.status.value if ring_config_error else None,
            "classifier": classifier.name}
