from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import agent as shop_agent
from app.briefing import _is_open, build_briefing, classify_day
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.classify import _aws_creds_available
from app.config import settings
from app.reconcile import TOLERANCE, check_attempt
from app.deliveries import STATUS_TEXT, DeliveryBook, FrameStore, frame_name, headline, money_line, receipts
from app.hours import posted_hours
from app.live import LiveError, LiveLog
from app.ring import RingSource
from app.ring.synthetic import SyntheticRingClient
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.webhooks import WebhookLog, WebhookStatus
from app.shop import DAYS, Profile, ShopProfile, hours_for_form, hours_from_form

app = FastAPI(title="Shopfront")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

DATA = Path(settings.data_dir)
profile = ShopProfile(DATA / "shop.json", settings.shop_name, settings.shop_hours)
book = DeliveryBook(DATA / "expected-deliveries.json")
frames = FrameStore(DATA)
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


def _ring():
    ring = source.get()
    if isinstance(ring, SyntheticRingClient):
        ring.hours = profile.hours()  # demo days follow the hours the owner set
    return ring


def _briefing(day: date):
    return build_briefing(_ring(), classifier, day, profile.get().name, profile.hours(), cache, live, frames)


def _visits(day: date):
    return classify_day(_ring(), classifier, day, profile.hours(), cache, live, frames)


def _receipts(day: date, visits=None) -> dict:
    visits = _visits(day) if visits is None else visits
    return receipts(book.for_day(day), visits, posted_hours(profile.hours(), day), frames)


def _week_money(day: date) -> str | None:
    p = profile.get()
    if p.redelivery_cost is None:
        return None
    closed = missing = 0
    for i in range(7):
        d = day - timedelta(days=i)
        vs = _visits(d)
        oh = posted_hours(profile.hours(), d)
        r = _receipts(d, vs)
        closed += sum(1 for v in vs if v.visit_type.value == "delivery" and not _is_open(v, oh))
        missing += sum(1 for x in r["expected"] if x["status"] == "not_seen")
    return money_line(closed, missing, p.redelivery_cost, p.currency)


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
    r = _receipts(day)
    matched = {x["visit"]["time"] for x in r["expected"] if x["status"].startswith("seen_while_closed")}
    if b.deliveries_while_closed and {m["time"] for m in b.deliveries_while_closed} <= matched:
        b.headline = [ln for ln in b.headline if "while the shop was closed" not in ln]  # receipts already say it
    b.headline = headline(r) + b.headline  # what the owner was waiting for comes first
    return templates.TemplateResponse(request, "dashboard.html", {
        "b": b, "peak": peak, "device": getattr(ring, "_device_id", "") or "",
        "request_id": getattr(ring, "last_request_id", None), "r": r, "status_text": STATUS_TEXT,
        "money": _week_money(day), "profile": profile.get(), "frames": frames,
        "can_ask": classifier.name == "bedrock" or _aws_creds_available(settings.aws_region)})


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
    visits = _visits(_parse_day(date))
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
    return live.frame(session_id, await request.body(), classifier, posted_hours(profile.hours(), day))


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


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None):
    _local_only(request)
    p = profile.get()
    try:
        ring, ring_error = source.get(), None
    except RingError as exc:
        ring, ring_error = None, exc.status.value
    today = datetime.now(settings.tz).date()
    return templates.TemplateResponse(request, "setup.html", {
        "p": p, "days": list(zip(DAYS, hours_for_form(p.hours))), "error": error,
        "ring": ring.name if ring else None, "ring_error": ring_error, "today": today.isoformat(),
        "expected": [e for i in (-1, 0, 1) for e in book.for_day(today + timedelta(days=i))],
        "shop": p.name})


@app.post("/setup")
async def setup_save(request: Request):
    _local_only(request)
    form = await request.form()
    try:
        cost = str(form.get("redelivery_cost", "")).strip()
        p = Profile(name=str(form.get("name", "")).strip()[:60] or settings.shop_name,
                    hours=hours_from_form([str(form.get(f"h{i}", "")) for i in range(7)]),
                    redelivery_cost=float(cost) if cost else None,
                    currency=(str(form.get("currency", "$")).strip() or "$")[:3])
        if p.redelivery_cost is not None and not 0 <= p.redelivery_cost < 100_000:
            raise ValueError("redelivery cost looks wrong")
        profile.save(p)
    except ValueError as exc:
        return RedirectResponse(f"/setup?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/setup#deliveries", status_code=303)


@app.post("/deliveries")
async def delivery_add(request: Request):
    _local_only(request)
    form = await request.form()
    back = str(form.get("back", "/setup#deliveries"))
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    try:
        book.add(date.fromisoformat(str(form.get("day", ""))), str(form.get("supplier", "")),
                 str(form.get("window_start", "")), str(form.get("window_end", "")), str(form.get("note", "")))
    except ValueError as exc:
        return RedirectResponse(f"/setup?error={quote(str(exc))}#deliveries", status_code=303)
    return RedirectResponse(back, status_code=303)


@app.post("/deliveries/{expected_id}/remove")
def delivery_remove(request: Request, expected_id: str):
    _local_only(request)
    book.remove(expected_id)
    return RedirectResponse("/setup#deliveries", status_code=303)


@app.get("/api/deliveries")
def api_deliveries(date: str | None = Query(None)):
    return _receipts(_parse_day(date))


def _check_attempt(at: datetime) -> dict:
    at = at.replace(tzinfo=settings.tz) if at.tzinfo is None else at.astimezone(settings.tz)
    days = sorted({(at - TOLERANCE).date(), at.date(), (at + TOLERANCE).date()})  # attempts near midnight
    return check_attempt(at, [v for d in days for v in _visits(d)], frames.url)


@app.get("/api/attempt")
def api_attempt(at: str = Query(..., description="carrier's attempted-delivery time, YYYY-MM-DDTHH:MM, shop local")):
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return JSONResponse({"error": "BAD_TIME", "detail": "use YYYY-MM-DDTHH:MM"}, status_code=400)
    if when.date() > datetime.now(settings.tz).date():
        return JSONResponse({"error": "BAD_TIME", "detail": "that time is in the future"}, status_code=400)
    return _check_attempt(when)


@app.get("/frames/{name}")
def frame(request: Request, name: str):
    _local_only(request)  # door frames stay on this machine
    if frame_name(name.removesuffix(".jpg")) != name:
        raise HTTPException(404)
    path = frames.path(name.removesuffix(".jpg"))
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


def _agent_tools(today: date) -> shop_agent.ShopTools:
    def visits(d: date) -> list[dict]:
        oh = posted_hours(profile.hours(), d)
        return [{"time": v.event.timestamp.strftime("%H:%M"), "ring_event": v.event.kind,
                 "event_source": v.event.source, "type": v.visit_type.value, "labelled_by": v.source,
                 "confidence": v.confidence, "reason": v.reason, "shop_open": _is_open(v, oh),
                 "frame_url": frames.url(v.event.id)} for v in _visits(d)]

    def hours_text(d: date) -> str:
        oh = posted_hours(profile.hours(), d)
        return f"{oh[0]:%H:%M}-{oh[1]:%H:%M}" if oh else "closed"

    return shop_agent.ShopTools(visits, _receipts, hours_text, today, _check_attempt)


@app.post("/api/ask")
async def api_ask(request: Request):
    _local_only(request)
    body = await request.json()
    try:
        return shop_agent.ask(str(body.get("question", "")), _agent_tools(datetime.now(settings.tz).date()),
                              profile.get().name, settings.aws_region, settings.bedrock_model_id,
                              agent_factory=agent_factory)
    except ValueError as exc:
        return JSONResponse({"error": "BAD_QUESTION", "detail": str(exc)}, status_code=400)
    except shop_agent.AgentUnavailable as exc:
        return JSONResponse({"error": "AGENT_UNAVAILABLE", "detail": str(exc)}, status_code=503)


agent_factory = shop_agent.build_agent  # tests swap in a fake
