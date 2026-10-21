from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates

from app import agent as shop_agent
from app.briefing import _is_open, build_briefing, classify_day
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.classify import _aws_creds_available
from app.config import settings
from app import dispute
from app.orders import parse_orders
from app.owner import COOKIE, COOKIE_DAYS, PUBLIC_PATHS, UNSAFE, OwnerGate
from app.reconcile import ATTEMPTED, BUSINESS_CLOSED, CLAIMS, TOLERANCE, check_attempt, check_closed_scan
from app.deliveries import (RECEIVED, STATUS_TEXT, DeliveryBook, FrameStore, frame_name, headline, money_line,
                            receipts)
from app.hours import posted_hours
from app.live import LiveError, LiveLog
from app.ring import RingSource
from app.ring.synthetic import SyntheticRingClient
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.webhooks import MAX_BODY as MAX_WEBHOOK_BODY
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
webhook_log = WebhookLog(DATA / "ring-webhooks.jsonl", settings.ring_device_id, settings.ring_account_id)
source = RingSource(settings, DATA / "ring-access-token")
live = LiveLog(DATA, settings.tz)


def make_gate(s, secret_file: Path) -> OwnerGate:
    return OwnerGate(secret_file, hosts=s.allowed_hosts.split(","))


gate = make_gate(settings, DATA / "owner-secret")


def _safe_next(value: str | None) -> str:
    value = value or "/"
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"


def _not_owner(request: Request) -> Response:
    detail = ("Shopfront only answers the shop owner. To use it on this device, open Set up on the shop's "
              "computer and enter the pairing code it shows at /pair.")
    path = request.url.path
    if path.startswith(("/api/", "/frames/", "/dispute.zip")):
        return JSONResponse({"error": "OWNER_ONLY", "detail": detail}, status_code=403)
    nxt = path + (f"?{request.url.query}" if request.url.query else "")
    return templates.TemplateResponse(request, "pair.html", {"next": nxt if request.method == "GET" else "/",
                                                             "error": None, "shop": profile.get().name},
                                      status_code=403)


@app.middleware("http")
async def owner_only(request: Request, call_next):
    """Everything but Ring's webhook and /healthz is the owner's. See app/owner.py."""
    path, headers = request.url.path, request.headers
    if path == "/webhooks/ring":  # Ring signs it (HMAC) and reaches it under whatever public name the shop uses
        return await call_next(request)
    if not gate.host_ok(headers.get("host")):
        return PlainTextResponse("Shopfront doesn't answer to this address. Open it at http://localhost:<port> "
                                 "or add the name to SHOPFRONT_ALLOWED_HOSTS.", status_code=400)
    if gate.cross_site(request.method, headers.get("host"), headers):
        return JSONResponse({"error": "CROSS_SITE", "detail": "This request came from another website, so "
                             "Shopfront ignored it. Use Shopfront's own pages."}, status_code=403)
    if request.method in UNSAFE and path.startswith("/api/") and not gate.api_body_ok(headers.get("content-type")):
        return JSONResponse({"error": "BAD_CONTENT_TYPE", "detail": "send application/json"}, status_code=415)
    if path not in PUBLIC_PATHS and not gate.is_owner(request.client.host if request.client else None,
                                                       headers, request.cookies):
        return _not_owner(request)
    return await call_next(request)


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
    elif isinstance(ring, RingApiClient):
        # webhooks count only for the shop's own doorbell and Ring account (app/ring/webhooks.py)
        webhook_log.device_id = settings.ring_device_id or ring.device_id
        webhook_log.account_id = settings.ring_account_id or ring.account_id() or ""
    return ring


def _briefing(day: date, stats: dict | None = None):
    return build_briefing(_ring(), classifier, day, profile.get().name, profile.hours(), cache, live, frames,
                          webhook_log, stats)


def _visits(day: date):
    return classify_day(_ring(), classifier, day, profile.hours(), cache, live, frames, webhook_log)


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
    checked = None
    if attempt:
        try:
            when = datetime.combine(day, datetime.strptime(attempt, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "attempt must be HH:MM")
        checked = _check_attempt(when, claim)
    stats: dict = {}
    b = _briefing(day, stats)
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
        "money": _week_money(day), "profile": profile.get(), "frames": frames, "attempt": checked,
        "claims": CLAIMS, "claim": claim, "carrier": carrier.strip()[:40], "tracking": tracking.strip()[:60], "received": RECEIVED, "funnel": stats,
        "disputable": dispute.DISPUTE,
        "can_ask": classifier.name == "bedrock" or _aws_creds_available(settings.aws_region)})


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return templates.TemplateResponse(request, "connect.html", {
        "shop": settings.shop_name, "connected": bool(source.pasted_token()),
        "env_mode": settings.ring_mode})


@app.post("/connect")
def connect_save(request: Request, token: str = Form(...)):
    if not token.strip():
        raise HTTPException(400, "paste a token")
    source.save_token(token)
    return RedirectResponse("/", status_code=303)


@app.post("/connect/forget")
def connect_forget(request: Request):
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


# The async handlers below read the request body, then run every blocking call (Ring over httpx, Bedrock
# over boto3, a Strands agent turn) in the thread pool, so Ring's webhook (5 s limit) is never kept waiting.
@app.post("/api/live/start")
async def live_start(request: Request):
    offer = (await request.body()).decode()
    if "v=0" not in offer:
        raise HTTPException(400, "body must be an SDP offer")

    def start():
        ring = _live_ring()
        answer, session_url = ring.whep_start(offer)
        s = live.start(ring.device_id, session_url)
        return {"session": s.id, "answer": answer, "ring_request_id": ring.last_request_id}
    return await run_in_threadpool(start)


@app.post("/api/live/{session_id}/frame")
async def live_frame(session_id: str, request: Request):
    day = datetime.now(settings.tz).date()
    jpeg = await request.body()
    return await run_in_threadpool(live.frame, session_id, jpeg, classifier, posted_hours(profile.hours(), day))


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


async def _capped_body(request: Request, limit: int) -> bytes | None:
    """The body, or None once it passes `limit` bytes (checked while reading, not after)."""
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            return None
    except ValueError:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


@app.post("/webhooks/ring")
async def ring_webhook(request: Request):
    raw = await _capped_body(request, MAX_WEBHOOK_BODY)
    if raw is None:
        return JSONResponse({"status": "TOO_LARGE"}, status_code=413)
    # A replay is told apart by its signing key and body marker (app/ring/webhooks.py); the header can only
    # ever mark one as a replay, never vouch for it being Ring's.
    status = webhook_log.handle(raw, request.headers.get("X-Signature"), settings.ring_hmac_key,
                                replayed=request.headers.get("X-Shopfront-Replay") == "1")
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


@app.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request, next: str = Query("/")):
    if gate.is_owner(request.client.host if request.client else None, request.headers, request.cookies):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "pair.html", {"next": _safe_next(next), "error": None,
                                                             "shop": profile.get().name})


@app.post("/pair")
def pair_device(request: Request, code: str = Form(""), next: str = Form("/")):
    value = gate.pair(code)
    if not value:
        return templates.TemplateResponse(request, "pair.html", {
            "next": _safe_next(next), "shop": profile.get().name,
            "error": "That code didn't match or has expired. Use the code Set up shows on the shop's computer now."},
            status_code=403)
    r = RedirectResponse(_safe_next(next), status_code=303)
    r.set_cookie(COOKIE, value, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="strict",
                 secure=request.url.scheme == "https")
    return r


@app.post("/pair/forget")
def pair_forget():
    gate.forget_devices()
    return RedirectResponse("/setup#pairing", status_code=303)


def _pairing(request: Request) -> dict | None:
    """The code a phone pairs with, shown only on the shop's own computer."""
    if not gate.is_local(request.client.host if request.client else None, request.headers):
        return None
    code, expires = gate.pairing_code()
    return {"code": code, "minutes": max(1, round((expires - datetime.now().timestamp()) / 60))}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None, imported: str | None = None):
    p = profile.get()
    try:
        ring, ring_error = source.get(), None
    except RingError as exc:
        ring, ring_error = None, exc.status.value
    today = datetime.now(settings.tz).date()
    return templates.TemplateResponse(request, "setup.html", {
        "p": p, "days": list(zip(DAYS, hours_for_form(p.hours))), "error": error, "imported": imported,
        "ring": ring.name if ring else None, "ring_error": ring_error, "today": today.isoformat(),
        "expected": [e for i in (-1, 0, 1) for e in book.for_day(today + timedelta(days=i))],
        "shop": p.name, "pairing": _pairing(request)})


@app.post("/setup")
async def setup_save(request: Request):
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
    form = await request.form()
    back = str(form.get("back", "/setup#deliveries"))
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    try:
        book.add(date.fromisoformat(str(form.get("day", ""))), str(form.get("supplier", "")),
                 str(form.get("window_start", "")), str(form.get("window_end", "")), str(form.get("note", "")),
                 order_ref=str(form.get("order_ref", "")), carrier=str(form.get("carrier", "")))
    except ValueError as exc:
        return RedirectResponse(f"/setup?error={quote(str(exc))}#deliveries", status_code=303)
    return RedirectResponse(back, status_code=303)


@app.post("/deliveries/{expected_id}/remove")
def delivery_remove(request: Request, expected_id: str):
    book.remove(expected_id)
    return RedirectResponse("/setup#deliveries", status_code=303)


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
                                  carrier.strip()[:40], tracking.strip()[:60])
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/dispute", response_class=HTMLResponse)
def dispute_page(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                 claim: str | None = Query(None), order: str | None = Query(None),
                 carrier: str = Query(""), tracking: str = Query("")):
    day = _parse_day(date)
    p = _pack(day, at, claim, order, carrier, tracking)
    query = request.url.query
    return templates.TemplateResponse(request, "dispute.html", {"p": p, "query": query, "status_text": STATUS_TEXT})


@app.get("/api/dispute")
def api_dispute(date: str | None = Query(None), at: str | None = Query(None), claim: str | None = Query(None),
                order: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    return _pack(_parse_day(date), at, claim, order, carrier, tracking)


@app.get("/dispute.zip")
def dispute_zip(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                claim: str | None = Query(None), order: str | None = Query(None),
                carrier: str = Query(""), tracking: str = Query("")):
    p = _pack(_parse_day(date), at, claim, order, carrier, tracking)
    name = f"shopfront-dispute-{p['day']}" + (f"-{p['claim']['time'][-5:].replace(':', '')}" if p["claim"] else "")
    return Response(dispute.as_zip(p, frames), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@app.post("/orders/import")
async def orders_import(request: Request):
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
        return RedirectResponse(f"/setup?error={quote(str(exc))}#orders", status_code=303)
    counts = book.import_rows(parsed["rows"])
    msg = (f"Imported {counts['added']} new and updated {counts['updated']} orders ({parsed['format']} format)"
           + (f". Skipped {len(parsed['errors'])}: " + "; ".join(parsed["errors"][:5]) if parsed["errors"] else "."))
    return RedirectResponse(f"/setup?imported={quote(msg)}#orders", status_code=303)


@app.post("/deliveries/{expected_id}/received")
async def delivery_received(request: Request, expected_id: str):
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
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "BAD_QUESTION", "detail": 'send {"question": "..."}'}, status_code=400)
    try:
        return await run_in_threadpool(shop_agent.ask, str(body.get("question", "")),
                                       _agent_tools(datetime.now(settings.tz).date()), profile.get().name,
                                       settings.aws_region, settings.bedrock_model_id, agent_factory=agent_factory)
    except ValueError as exc:
        return JSONResponse({"error": "BAD_QUESTION", "detail": str(exc)}, status_code=400)
    except shop_agent.AgentUnavailable as exc:
        return JSONResponse({"error": "AGENT_UNAVAILABLE", "detail": str(exc)}, status_code=503)


agent_factory = shop_agent.build_agent  # tests swap in a fake
