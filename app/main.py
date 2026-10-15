from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.briefing import build_briefing, classify_day
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.config import settings
from app.hours import parse_hours, posted_hours
from app.live import LiveError, LiveLog
from app.ring import RingSource
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.webhooks import WebhookLog, WebhookStatus

app = FastAPI(title="Shopfront")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

DATA = Path(settings.data_dir)
hours = parse_hours(settings.shop_hours)
classifier = make_classifier(settings)
cache = ClassificationCache(DATA / "classifications.json")
webhook_log = WebhookLog(DATA / "ring-webhooks.jsonl")
source = RingSource(settings, DATA / "ring-access-token")
live = LiveLog(DATA, settings.tz)


def _parse_day(value: str | None) -> date:
    if not value:  # a morning briefing is about yesterday
        return datetime.now(settings.tz).date() - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


def _briefing(day: date):
    return build_briefing(source.get(), classifier, day, settings.shop_name, hours, cache, live)


def _local_only(request: Request) -> None:
    # The token page writes a credential to disk, so it only answers this machine.
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "/connect is only available from this machine")


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
    ring = source.get()
    return templates.TemplateResponse(request, "dashboard.html", {
        "b": b, "peak": peak, "device": getattr(ring, "_device_id", "") or "",
        "request_id": getattr(ring, "last_request_id", None)})


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    _local_only(request)
    return templates.TemplateResponse(request, "connect.html", {
        "shop": settings.shop_name, "connected": bool(source.pasted_token()),
        "env_mode": settings.ring_mode})


@app.post("/connect")
def connect_save(request: Request, token: str = Form(...)):
    _local_only(request)
    if not token.strip():
        raise HTTPException(400, "paste a token")
    source.save_token(token)
    return RedirectResponse("/", status_code=303)


@app.post("/connect/forget")
def connect_forget(request: Request):
    _local_only(request)
    source.forget_token()
    return RedirectResponse("/", status_code=303)


@app.get("/api/briefing")
def api_briefing(date: str | None = Query(None)):
    return asdict(_briefing(_parse_day(date)))


@app.get("/api/events")
def api_events(date: str | None = Query(None)):
    visits = classify_day(source.get(), classifier, _parse_day(date), hours, cache, live)
    return [{"id": v.event.id, "time": v.event.timestamp.isoformat(), "kind": v.event.kind,
             "event_source": v.event.source, "type": v.visit_type.value, "confidence": v.confidence,
             "reason": v.reason, "classifier": v.source, "bedrock_request_id": v.request_id}
            for v in visits]


def _live_ring() -> RingApiClient:
    ring = source.get()
    if not isinstance(ring, RingApiClient):
        raise HTTPException(409, "Connect Ring first: live view needs a Ring token (/connect)")
    return ring


@app.exception_handler(LiveError)
def live_error(request: Request, exc: LiveError):
    return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=404 if exc.code == "UNKNOWN_SESSION" else 400)


@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    ring = source.get()
    return templates.TemplateResponse(request, "live.html", {
        "shop": settings.shop_name, "connected": isinstance(ring, RingApiClient),
        "classifier": classifier.name, "sessions": live.rows()[-10:][::-1]})


@app.post("/api/live/start")
async def live_start(request: Request):
    ring = _live_ring()
    offer = (await request.body()).decode()
    if "v=0" not in offer:
        raise HTTPException(400, "body must be an SDP offer")
    answer, session_url = ring.whep_start(offer)
    s = live.start(ring.device_id, session_url)
    return {"session": s.id, "answer": answer, "ring_request_id": ring.last_request_id}


@app.post("/api/live/{session_id}/frame")
async def live_frame(session_id: str, request: Request):
    day = datetime.now(settings.tz).date()
    return live.frame(session_id, await request.body(), classifier, posted_hours(hours, day))


@app.post("/api/live/{session_id}/stop")
def live_stop(session_id: str):
    s = live.get(session_id)
    ring_error = None
    try:
        _live_ring().whep_stop(s.session_url)
    except (RingError, HTTPException) as exc:  # still record the session; report why the stop failed
        ring_error = getattr(exc, "status", None) and exc.status.value
    return {**live.stop(session_id), "ring_stop_error": ring_error}


@app.get("/api/live/sessions")
def live_sessions():
    return live.rows()


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
    try:
        ring, err = source.get(), None
    except RingError as exc:
        ring, err = None, exc
    return {"ok": err is None, "ring": ring.name if ring else None,
            "ring_error": err.status.value if err else None, "classifier": classifier.name,
            "token_pasted": bool(source.pasted_token())}
