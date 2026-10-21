from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import agent as shop_agent
from app.briefing import _is_open, build_briefing, classify_day
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.classify import _aws_creds_available
from app.config import settings
from app import dispute, pages
from app.orders import parse_orders
from app.reconcile import ATTEMPTED, BUSINESS_CLOSED, CLAIMS, TOLERANCE, check_attempt, check_closed_scan
from app.deliveries import (RECEIVED, STATUS_TEXT, DeliveryBook, FrameStore, frame_name, headline, money_line,
                            receipts)
from app.hours import posted_hours
from app.live import LiveError, LiveLog
from app.ring import RingSource
from app.ring.base import RingClient
from app.ring.synthetic import SyntheticRingClient
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.webhooks import WebhookLog, WebhookStatus
from app.shop import DAYS, Profile, ShopProfile, hours_for_form, hours_from_form

app = FastAPI(title="Shopfront")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters.update(
    local_time=lambda v, day=None: pages.local_time(v, settings.tz, day),
    local_stamp=lambda v: pages.local_stamp(v, settings.tz), by=pages.by, verdict=pages.verdict,
    long_date=pages.long_date, how=pages.how)

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
        day = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    # A briefing reaches a week back and a scan check a day either side; year 1 or 9999 would overflow
    if not date(2000, 1, 1) <= day <= datetime.now(settings.tz).date() + timedelta(days=366):
        raise HTTPException(400, "pick a date between 2000 and a year from today")
    return day


_ring_override: ContextVar[RingClient | None] = ContextVar("ring_override", default=None)


class _NoHistory(RingClient):
    """Ring's Event History is unreachable (expired sign-in, outage): the dashboard is built from
    the webhooks Ring already pushed and the owner's orders. Never synthetic data."""

    name = "ring"

    def __init__(self, tz):
        self.tz = tz

    def events_for_day(self, day: date) -> list:
        return []


def _ring():
    ring = _ring_override.get() or source.get()
    if isinstance(ring, SyntheticRingClient):
        ring.hours = profile.hours()  # demo days follow the hours the owner set
    return ring


def _briefing(day: date, stats: dict | None = None):
    return build_briefing(_ring(), classifier, day, profile.get().name, profile.hours(), cache, live, frames,
                          webhook_log, stats)


def _visits(day: date):
    return classify_day(_ring(), classifier, day, profile.hours(), cache, live, frames, webhook_log)


def _receipts(day: date, visits=None) -> dict:
    visits = _visits(day) if visits is None else visits
    return receipts(book.for_day(day), visits, posted_hours(profile.hours(), day), frames, now=datetime.now(settings.tz))


def _week_money(day: date) -> str | None:
    p = profile.get()
    if p.redelivery_cost is None:
        return None
    closed = missing = 0
    for i in range(7):
        d = day - timedelta(days=i)
        r = _receipts(d)
        closed += r["closed_couriers"]  # Ring- or Bedrock-confirmed couriers only, staff not in
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
    _ring_seen(exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(_ring_error_body(exc), status_code=code)
    return _page(request, "ring_error.html", {"e": exc}, code)


# --- What every HTML page shares: the header, the data-source banner and Ring's last known state ---

_ring_problems: dict[tuple, str] = {}  # (source, token key) -> last RingStatus a page hit; a new token starts clean


def _ring_key() -> tuple:
    return id(source), getattr(source, "_key", None)


def _ring_seen(exc: RingError | None) -> None:
    key = _ring_key()
    if exc is None:
        _ring_problems.pop(key, None)
    else:
        _ring_problems[key] = exc.status.value


def _replayed_anywhere() -> bool:
    try:
        return any(r.get("replayed") for r in webhook_log.recent(1_000_000))
    except ValueError:  # a damaged log line; the pages still render
        return False


def _chrome(request: Request) -> dict:
    try:
        shop = profile.get().name
    except (OSError, ValueError, TypeError):
        shop = settings.shop_name
    try:
        mode, problem = source.get().name, _ring_problems.get(_ring_key())
    except RingError as exc:
        mode, problem = "error", exc.status.value
    return {"shop": shop, "ring_mode": mode, "ring_problem": None if mode == "synthetic" else problem,
            "replayed": _replayed_anywhere(), "nav_here": request.url.path}


def _page(request: Request, name: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {**_chrome(request), **(ctx or {})}, status_code=status_code)


def _wants_html(request: Request) -> bool:
    path = request.url.path
    return not (path.startswith(("/api/", "/webhooks/")) or path == "/healthz")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request) or exc.status_code < 400:
        return await http_exception_handler(request, exc)
    return _page(request, "error.html", {"err": pages.plain_error(exc.status_code, str(exc.detail), request.url.path)},
                 exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return await request_validation_exception_handler(request, exc)
    detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg', '')}" for e in exc.errors())
    return _page(request, "error.html", {"err": pages.plain_error(422, detail, request.url.path)}, 422)


@app.exception_handler(Exception)
def server_error(request: Request, exc: Exception):
    if not _wants_html(request):
        return JSONResponse({"error": "INTERNAL_ERROR", "detail": type(exc).__name__}, status_code=500)
    return _page(request, "error.html", {"err": pages.plain_error(500, type(exc).__name__, request.url.path)}, 500)


def _claim(value: str | None) -> str:
    value = (value or ATTEMPTED).strip().lower().replace(" ", "_")
    if value not in CLAIMS:
        raise HTTPException(400, f"claim must be one of {', '.join(CLAIMS)}")
    return value


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str | None = Query(None), attempt: str | None = Query(None),
              claim: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    day = _parse_day(date)
    claim = _claim(claim)
    when = None
    if attempt:
        try:
            when = datetime.combine(day, datetime.strptime(attempt, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "attempt must be HH:MM")
    ring_down = None
    try:
        desk = _desk(day, when, claim)
        _ring_seen(None)
    except RingError as exc:
        if exc.status == RingStatus.NOT_CONFIGURED:
            raise
        # Degrade, don't die: the orders and what Ring pushed by webhook still make a receiving desk.
        # No scan check without the door history: "nothing recorded" would be a guess.
        _ring_seen(exc)
        ring_down, token = exc, _ring_override.set(_NoHistory(settings.tz))
        try:
            desk = _desk(day, None, claim)
        finally:
            _ring_override.reset(token)
        r = desk["r"]  # "no courier seen" would be a guess without the history
        desk["lead"] = [f"{r['seen']} of {r['total']} expected deliveries confirmed by Ring's webhooks. "
                        "The rest can't be checked until Ring is reconnected."] if r["total"] else []
    return _page(request, "dashboard.html", {
        **desk, "ring_down": ring_down, "asked_time": attempt or "",
        **({"ring_problem": None} if ring_down else {}),  # the page's own notice says it, with what still works
        "status_text": STATUS_TEXT,
        "profile": profile.get(), "claims": CLAIMS, "claim": claim, "carrier": carrier.strip()[:40],
        "tracking": tracking.strip()[:60], "received": RECEIVED, "disputable": dispute.DISPUTE,
        "can_ask": classifier.name == "bedrock" or _aws_creds_available(settings.aws_region)})


def _desk(day: date, when: datetime | None, claim: str) -> dict:
    """The dashboard's data. Receiving lines lead; the rest of the briefing goes below them."""
    checked = _check_attempt(when, claim) if when else None
    stats: dict = {}
    b = _briefing(day, stats)
    ring = _ring()
    r = _receipts(day)
    matched = {x["visit"]["time"] for x in r["expected"] if x["status"].startswith("seen_while_closed")}
    # The briefing's closed-shop lines belong with the deliveries; the rest of it goes below
    courier = [ln for ln in b.headline if "while the shop was closed" in ln]
    presses = [ln for ln in b.headline if ln.startswith("Doorbell pressed while closed")]
    rest = [ln for ln in b.headline if ln not in courier + presses]
    if {m["time"] for m in b.deliveries_while_closed} <= matched:
        courier = []  # the receipts already say it
    closed = courier + presses
    return {"b": b, "lead": headline(r) + closed, "rest": rest, "r": r, "rows": pages.desk_rows(r),
            "peak": max(b.hourly_customers + b.hourly_customers_last_week) or 1,
            "device": getattr(ring, "_device_id", "") or "", "request_id": getattr(ring, "last_request_id", None),
            "money": _week_money(day), "attempt": checked, "funnel": stats,
            "labels": pages.labelled_by_text(stats, b.classifier), "first_run": not book._all(),
            "replayed": bool(stats.get("replayed"))}


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    _local_only(request)
    return _page(request, "connect.html", {"connected": bool(source.pasted_token()), "env_mode": settings.ring_mode})


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
    return _page(request, "live.html", {"connected": isinstance(ring, RingApiClient),
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
                                settings.ring_hmac_key, replayed=request.headers.get("X-Shopfront-Replay") == "1")
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
def setup_page(request: Request, error: str | None = None, imported: str | None = None,
               notice: str | None = None, s: str = "shop"):
    _local_only(request)
    return _setup(request, s, error=error, notice=imported or notice)


ORDERS_SHOWN = 60


def _setup(request: Request, section: str, error: str | None = None, notice: str | None = None,
           form: dict | None = None, status_code: int = 200) -> HTMLResponse:
    """/setup with any message shown inside the section it is about, next to where the owner lands."""
    p = profile.get()
    try:
        ring, ring_error = source.get(), None
        if isinstance(ring, RingApiClient):  # the client loads lazily: ask Ring now, or "connected" is a guess
            ring.list_devices()
            _ring_seen(None)
    except RingError as exc:
        ring, ring_error = None, exc.status.value
        if _ring_key()[1]:  # a built client that Ring refused, not a missing configuration
            _ring_seen(exc)
    today = datetime.now(settings.tz).date()
    orders = sorted(book._all(), key=lambda e: (e.window_start or "99", e.supplier.lower()))
    orders.sort(key=lambda e: e.day, reverse=True)  # newest day first, each day in time order
    hours = hours_for_form(p.hours)
    if form:  # a refused save: show what the owner typed, not what was saved before
        p = Profile(name=form.get("name", p.name), hours=p.hours, redelivery_cost=p.redelivery_cost,
                    currency=form.get("currency", p.currency))
        hours = [form.get(f"h{i}", "") or "closed" for i in range(7)]
    return _page(request, "setup.html", {
        "p": p, "days": list(zip(DAYS, hours)), "error": error, "notice": notice, "section": section,
        "cost_typed": (form or {}).get("redelivery_cost"),
        "ring": ring.name if ring else None, "ring_error": ring_error, "today": today.isoformat(),
        "orders": orders[:ORDERS_SHOWN], "orders_more": max(0, len(orders) - ORDERS_SHOWN),
        "order_days": len({e.day for e in orders})}, status_code)


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
        return _setup(request, "shop", error=str(exc), status_code=400,
                      form={k: str(v) for k, v in form.items()})
    return RedirectResponse(f"/setup?s=shop&notice={quote('Saved. Shopfront uses these hours from now on.')}#shop",
                            status_code=303)


@app.post("/deliveries")
async def delivery_add(request: Request):
    _local_only(request)
    form = await request.form()
    back = str(form.get("back", ""))
    back = back if back.startswith("/") and not back.startswith("//") else ""
    try:
        e = book.add(date.fromisoformat(str(form.get("day", ""))), str(form.get("supplier", "")),
                     str(form.get("window_start", "")), str(form.get("window_end", "")), str(form.get("note", "")),
                     order_ref=str(form.get("order_ref", "")), carrier=str(form.get("carrier", "")))
    except ValueError as exc:
        return RedirectResponse(f"/setup?s=orders&error={quote(str(exc))}#orders", status_code=303)
    return RedirectResponse(back or f"/setup?s=orders&notice={quote(f'Added {e.supplier} for {e.day}.')}#orders",
                            status_code=303)


@app.post("/deliveries/{expected_id}/remove")
def delivery_remove(request: Request, expected_id: str):
    _local_only(request)
    e = book.get(expected_id)
    book.remove(expected_id)
    msg = f"Removed {e.supplier} ({e.day})." if e else "That order was already gone."
    return RedirectResponse(f"/setup?s=orders&notice={quote(msg)}#orders", status_code=303)


@app.get("/api/deliveries")
def api_deliveries(date: str | None = Query(None)):
    return _receipts(_parse_day(date))


def _around(at: datetime, reach: timedelta) -> list:
    days = sorted({(at - reach).date(), at.date(), (at + reach).date()})  # scans near midnight
    return [v for d in days for v in _visits(d)]


def _local(at: datetime) -> datetime:
    return at.replace(tzinfo=settings.tz) if at.tzinfo is None else at.astimezone(settings.tz)


def _check_attempt(at: datetime, claim: str = ATTEMPTED) -> dict:
    at = _local(at)
    if claim == BUSINESS_CLOSED:
        return check_closed_scan(at, _around(at, dispute.WINDOW), posted_hours(profile.hours(), at.date()),
                                 frames.url)
    return check_attempt(at, _around(at, TOLERANCE), frames.url)


def _when(at: str) -> datetime:
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        raise ValueError("use YYYY-MM-DDTHH:MM")
    if when.date() > datetime.now(settings.tz).date():
        raise ValueError("that time is in the future")
    return when


@app.get("/api/attempt")
def api_attempt(at: str = Query(..., description="carrier's scan time, YYYY-MM-DDTHH:MM, shop local"),
                claim: str = Query(ATTEMPTED, description="attempted | business_closed")):
    try:
        when = _when(at)
    except ValueError as exc:
        return JSONResponse({"error": "BAD_TIME", "detail": str(exc)}, status_code=400)
    return _check_attempt(when, _claim(claim))


def _pack(day: date, at: str | None, claim: str | None, order: str | None, carrier: str, tracking: str) -> dict:
    visits = _visits(day)
    e = book.get(order) if order else None
    if order and not e:
        raise HTTPException(404, "no such order")
    if not e and tracking.strip():  # the carrier's tracking number names the order it was for
        e = next((x for x in book.for_day(day) if x.tracking and x.tracking == tracking.strip()), None)
    check = None
    if at:
        try:
            when = datetime.combine(day, datetime.strptime(at, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "at must be HH:MM")
        check = _check_attempt(when, _claim(claim))
    receipt = None
    if e:
        r = _receipts(date.fromisoformat(e.day))
        receipt = next((x for x in r["expected"] if x["id"] == e.id), None)
    oh = posted_hours(profile.hours(), day)
    try:
        return dispute.build_pack(profile.get().name, day, visits, frames,
                                  f"{oh[0]:%H:%M}–{oh[1]:%H:%M}" if oh else "closed", check, e, receipt,
                                  carrier.strip()[:40], tracking.strip()[:60], tz=settings.tz)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/dispute", response_class=HTMLResponse)
def dispute_page(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                 claim: str | None = Query(None), order: str | None = Query(None),
                 carrier: str = Query(""), tracking: str = Query("")):
    day = _parse_day(date)
    p = _pack(day, at, claim, order, carrier, tracking)
    query = request.url.query
    return _page(request, "dispute.html", {"p": p, "query": query, "status_text": STATUS_TEXT,
                                           "replayed": "ring-webhook-replay" in p["event_sources"]})


@app.get("/api/dispute")
def api_dispute(date: str | None = Query(None), at: str | None = Query(None), claim: str | None = Query(None),
                order: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    return _pack(_parse_day(date), at, claim, order, carrier, tracking)


@app.get("/dispute.zip")
def dispute_zip(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                claim: str | None = Query(None), order: str | None = Query(None),
                carrier: str = Query(""), tracking: str = Query("")):
    _local_only(request)  # the zip carries the Ring frame, which stays on this machine
    p = _pack(_parse_day(date), at, claim, order, carrier, tracking)
    name = f"shopfront-dispute-{p['day']}" + (f"-{p['claim']['time'][-5:].replace(':', '')}" if p["claim"] else "")
    return Response(dispute.as_zip(p, frames), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@app.post("/orders/import")
async def orders_import(request: Request):
    _local_only(request)
    form = await request.form()
    upload = form.get("file")
    text = ""
    if upload is not None and hasattr(upload, "read"):
        text = (await upload.read()).decode("utf-8-sig", errors="replace")
    text = text or str(form.get("csv", ""))
    try:
        if len(text) > 2_000_000:
            raise ValueError("that file is too big for one import")
        parsed = parse_orders(text, day_first=form.get("day_first") == "1")
    except ValueError as exc:
        return RedirectResponse(f"/setup?s=orders&error={quote(str(exc))}#orders", status_code=303)
    counts = book.import_rows(parsed["rows"])
    msg = (f"Imported {counts['added']} new and updated {counts['updated']} orders ({parsed['format']} format)"
           + (f". Skipped {len(parsed['errors'])}: " + "; ".join(parsed["errors"][:5]) if parsed["errors"] else "."))
    return RedirectResponse(f"/setup?s=orders&imported={quote(msg)}#orders", status_code=303)


@app.post("/deliveries/{expected_id}/received")
async def delivery_received(request: Request, expected_id: str):
    _local_only(request)
    form = await request.form()
    try:
        e = book.mark_received(expected_id, str(form.get("status", "")), str(form.get("note", "")),
                               datetime.now(settings.tz))
    except KeyError:
        raise HTTPException(404, "no such delivery")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(f"/?date={e.day}#dlv", status_code=303)


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

    def pack(d: date, at: str, claim: str, order_ref: str) -> dict:
        e = next((x for x in book.for_day(d) if order_ref and order_ref.lower() in (x.order_ref.lower(), x.id)), None)
        if order_ref and not e:
            raise ValueError(f"no order {order_ref!r} on {d.isoformat()}")
        try:
            p = _pack(d, at or None, claim or None, e.id if e else None, "", "")
        except HTTPException as exc:
            raise ValueError(exc.detail)
        link = "/dispute?" + "&".join(f"{k}={quote(v)}" for k, v in
                                      (("date", d.isoformat()), ("at", at), ("claim", claim), ("order", e.id if e else ""))
                                      if v)
        return {"finding": p["finding"], "order": p["order"], "frame": p["frame"], "draft": p["draft"],
                "no_draft_reason": p["no_draft_reason"], "limits": p["limits"], "pack_url": link}

    return shop_agent.ShopTools(visits, _receipts, hours_text, today, _check_attempt, pack)


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
