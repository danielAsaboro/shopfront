from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timedelta
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import agent as shop_agent
from app.ask_probe import AskProbe
from app.briefing import _is_open, build_briefing, classify_day, playground_mode
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.classify import _aws_creds_available
from app.config import settings
from app import checks as kept_checks
from app import bedrock_budget, coverage, dispute, pack_signing, pages, provenance
from app.files import private_dir, prune_older_than
from app.orders import parse_orders
from app.owner import COOKIE, COOKIE_DAYS, RING_SERVER_PATHS, UNSAFE, OwnerGate, PublicDemoGate
from app.scan_extract import MAX_BYTES as MAX_SCAN_BYTES
from app.scan_extract import ScanReader, ScanReadError
from app.reconcile import ATTEMPTED, BUSINESS_CLOSED, CLAIMS, REFUSED, TOLERANCE, check_attempt, check_closed_scan
from app.deliveries import (COST_SOURCE, RECEIVED, STATUS_TEXT, DeliveryBook, FrameStore, default_cost, frame_name,
                            headline, money_line, receipts)
from app.hours import normalize_clock, posted_hours
from app.live import LiveError, LiveLog
from app.ring import RingSource
from app.ring.base import RingClient
from app.ring.synthetic import SyntheticRingClient
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.webhooks import MAX_BODY as MAX_WEBHOOK_BODY
from app.ring.webhooks import WebhookStatus
from app.ring.webhooks import verify as ring_signed
from app.ring.link import LinkError, RingLink, freshness as link_freshness
from app.ring.status import StatusLog, sampler
from app.shop import DAYS, Profile, ShopProfile, hours_for_form, hours_from_form
from app.store import make_webhook_log



@asynccontextmanager
async def _lifespan(app: FastAPI):
    """While Ring is live, ask it every SHOPFRONT_STATUS_MINUTES whether the doorbell is online (app/ring/status.py)."""
    task = asyncio.create_task(sampler(sample_status, settings.status_minutes * 60)) \
        if settings.status_minutes > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="Shopfront", lifespan=_lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters.update(
    local_time=lambda v, day=None: pages.local_time(v, settings.tz, day),
    local_stamp=lambda v: pages.local_stamp(v, settings.tz), by=pages.by, verdict=pages.verdict,
    long_date=pages.long_date, how=pages.how, visit_label=pages.visit_label, badge=pages.badge,
    pack_stills=pages.pack_stills, month_name=lambda v: date.fromisoformat(str(v)[:10]).strftime("%B %Y"),
    relative_day=lambda v: pages.relative_day(v, datetime.now(settings.tz).date()), not_whose=provenance.not_whose, frame_label=provenance.frame_label,
    verdict_facts=pages.verdict_facts, answer=pages.answer, chip=pages.chip, strip=pages.door_strip, pack_check=pages.pack_check)

# Inline, so every page (the pairing page too) has its icon without a request the owner gate would answer
templates.env.globals.update(cost_source=COST_SOURCE, default_cost=default_cost)
templates.env.globals["favicon_uri"] = "data:image/svg+xml," + quote(pages.FAVICON)

DATA = private_dir(Path(settings.data_dir))  # tokens, logs and door frames: this user only
profile = ShopProfile(DATA / "shop.json", settings.shop_name, settings.shop_hours)
book = DeliveryBook(DATA / "expected-deliveries.json")
checks = kept_checks.CheckBook(DATA / "checks.json")  # the scan checks the owner kept: the carrier record
frames = FrameStore(DATA)
classifier = make_classifier(settings)
cache = ClassificationCache(DATA / "classifications.json")
webhook_log = make_webhook_log(settings, DATA / "ring-webhooks.jsonl")  # file, or the Lambda's DynamoDB table
status_log = StatusLog(DATA / "ring-status.jsonl")  # Shopfront's own GET /v1/devices/{id}/status samples
# Ring account linking (app/ring/link.py): the unclaimed-token pool and the linked account's refresh token
ring_link = RingLink(DATA, client_id=settings.ring_client_id, client_secret=settings.ring_client_secret,
                     hmac_key=settings.ring_hmac_key)
source = RingSource(settings, DATA / "ring-access-token", ring_link)
live = LiveLog(DATA, settings.tz)
label_requests = bedrock_budget.LabelRequests(DATA / "bedrock-requests.json")  # days the owner asked Bedrock about
pack_signer = pack_signing.make_signer(settings, DATA)  # dispute packs are signed (app/pack_signing.py)
scan_reader = ScanReader(settings.aws_region, settings.bedrock_model_id)  # tracking-page screenshots
ask_probe = AskProbe(settings.aws_region, settings.bedrock_model_id)  # can Ask reach Bedrock from here?


def _ask_state() -> str | None:
    """None when Ask Shopfront can answer from this machine, else why not (app/ask_probe.py)."""
    if classifier.name != "bedrock" and not _aws_creds_available(settings.aws_region):
        return "NO_AWS_SIGN_IN"
    return ask_probe.state()


def make_gate(s, secret_file: Path) -> OwnerGate:
    if s.public_demo:  # no owner at all; only app/public_demo.py's allowlist admits a request
        return PublicDemoGate(hosts=s.allowed_hosts.split(","))
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
    return _page(request, "pair.html", {"next": nxt if request.method == "GET" else "/", "error": None}, 403)


@app.middleware("http")
async def owner_only(request: Request, call_next):
    """Everything but Ring's webhook and /healthz is the owner's. See app/owner.py."""
    path, headers = request.url.path, request.headers
    if path in RING_SERVER_PATHS and path in gate.public_paths:  # Ring's own server calls, under any public name
        return await call_next(request)
    if not gate.host_ok(headers.get("host")):
        return PlainTextResponse("Shopfront doesn't answer to this address. Open it at http://localhost:<port> "
                                 "or add the name to SHOPFRONT_ALLOWED_HOSTS.", status_code=400)
    if gate.cross_site(request.method, headers.get("host"), headers):
        return JSONResponse({"error": "CROSS_SITE", "detail": "This request came from another website, so "
                             "Shopfront ignored it. Use Shopfront's own pages."}, status_code=403)
    if request.method in UNSAFE and path.startswith("/api/") and not gate.api_body_ok(headers.get("content-type")):
        return JSONResponse({"error": "BAD_CONTENT_TYPE", "detail": "send application/json"}, status_code=415)
    if path not in gate.public_paths and not gate.is_owner(request.client.host if request.client else None,
                                                       headers, request.cookies):
        return _not_owner(request)
    await run_in_threadpool(_housekeeping)
    token = _request_visits.set({})
    try:
        return await call_next(request)
    finally:
        _request_visits.reset(token)


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
    elif isinstance(ring, RingApiClient):
        # webhooks count only for the shop's own doorbell and Ring account (app/ring/webhooks.py)
        webhook_log.device_id = settings.ring_device_id or ring.device_id
        webhook_log.account_id = settings.ring_account_id or ring.account_id() or ""
    return ring


def _briefing(day: date, stats: dict | None = None):
    ring = _ring()
    return build_briefing(ring, classifier, day, profile.get().name, profile.hours(), cache, live, frames,
                          webhook_log, stats, _focus(), playground_mode(ring, settings.ring_playground))


_request_visits: ContextVar[dict | None] = ContextVar("request_visits", default=None)
_scan_focus: ContextVar[list | None] = ContextVar("scan_focus", default=None)


def _kept_scans() -> list[datetime]:
    out = []
    for c in checks.all():
        try:
            out.append(datetime.combine(date.fromisoformat(c["day"]), datetime.strptime(c["at"], "%H:%M").time(),
                                        settings.tz))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _focus() -> bedrock_budget.Focus:
    """Which events Bedrock may look at (app/bedrock_budget.py): near an order's window, a scan being checked
    (this request's or one the owner kept), or on a day the owner asked about."""
    return bedrock_budget.Focus(settings.tz, book.for_day, lambda: (_scan_focus.get() or []) + _kept_scans(),
                                label_requests.asked)


def _focus_scan(at: datetime) -> None:
    """A scan is being checked: Bedrock may look at the door around it, so the days it touches are labelled
    again in this request (the cache keeps what Bedrock already said)."""
    at = _local(at)
    scans = _scan_focus.get()
    if scans is None:
        scans = []
        _scan_focus.set(scans)
    scans.append(at)
    memo = _request_visits.get()
    if memo is not None:
        for d in {(at - bedrock_budget.REACH).date(), at.date(), (at + bedrock_budget.REACH).date()}:
            memo.pop(d, None)


def _visits(day: date):
    """The day's labelled visits, built once per request however many parts of the page need them."""
    memo = _request_visits.get()
    if memo is not None and day in memo:
        return list(memo[day])
    ring = _ring()
    vs = classify_day(ring, classifier, day, profile.hours(), cache, live, frames, webhook_log, focus=_focus(),
                      playground=playground_mode(ring, settings.ring_playground))
    if memo is not None:
        memo[day] = vs
    return list(vs)


def _receipts(day: date, visits=None) -> dict:
    visits = _visits(day) if visits is None else visits
    return receipts(book.for_day(day), visits, posted_hours(profile.hours(), day), frames, now=datetime.now(settings.tz))


def _week_money(day: date) -> str | None:
    p = profile.get()
    cost = p.redelivery_cost if p.redelivery_cost is not None else default_cost(p.currency)  # labelled default
    if cost is None:
        return None
    closed = missing = 0
    for i in range(7):
        d = day - timedelta(days=i)
        try:
            r = _receipts(d)
        except RingError:  # e.g. RATE_LIMITED on one past day: leave the week's line out, keep the page
            return None
        closed += r["closed_couriers"]  # Ring- or Bedrock-confirmed couriers only, staff not in
        missing += sum(1 for x in r["expected"] if x["status"] == "not_seen" and not x.get("possible_elsewhere"))
    return money_line(closed, missing, cost, p.currency, default=p.redelivery_cost is None)


def _ring_error_body(exc: RingError) -> dict:
    return {"error": exc.status.value, "detail": exc.detail, "http_status": exc.http_status,
            "request_id": exc.request_id, "retry_after": exc.retry_after}


@app.exception_handler(RingError)
def ring_error(request: Request, exc: RingError):
    # Fail closed: a Ring failure is shown as itself, never replaced with synthetic data.
    code = 401 if exc.status in (RingStatus.TOKEN_REJECTED, RingStatus.REFRESH_FAILED) else 502
    if exc.status == RingStatus.NOT_CONFIGURED:
        code = 503
    headers = None
    if exc.status == RingStatus.RATE_LIMITED:  # Ring said slow down: say when to try again
        code = 429
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    _ring_seen(exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(_ring_error_body(exc), status_code=code, headers=headers)
    page = _page(request, "ring_error.html", {"e": exc}, code)
    if headers:
        page.headers.update(headers)
    return page


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


def _cost() -> tuple[float | None, bool]:
    """A missed delivery's cost: the owner's number, else the survey default (said as such)."""
    p = profile.get()
    return (p.redelivery_cost, False) if p.redelivery_cost is not None else (default_cost(p.currency), True)


def _month_confirmed(day: date) -> int | None:
    """Orders this month, up to `day`, whose courier Ring or Bedrock saw at the door. Only days with
    orders are read; a Ring error leaves the number out rather than undercounting."""
    today = datetime.now(settings.tz).date()
    n, d = 0, day.replace(day=1)
    while d <= min(day, today):
        if book.for_day(d):
            try:
                n += sum(1 for x in _receipts(d)["expected"] if x["status"].startswith("seen"))
            except RingError:
                return None
        d += timedelta(days=1)
    return n


def _record(day: date) -> dict:
    """The kept checks' carrier record and the month's money, for the desk."""
    kept = checks.all()
    cost, default = _cost()
    record = kept_checks.carrier_record(kept, day, cost, profile.get().currency, dispute.DISPUTE)
    for c in record:  # the desk's one-line summary: the pattern without the money, which sits with its source
        c["summary"] = c["headline"].removesuffix(f", about {c['at_stake']} at stake") if c["at_stake"] else c["headline"]
    return {"record": record,
            "perishable_at_risk": kept_checks.perishable_at_risk(kept, day, dispute.DISPUTE),
            "month_money": kept_checks.month_money(kept, day, _month_confirmed(day), cost, profile.get().currency,
                                                   default, dispute.DISPUTE)}


@app.get("/report", response_class=HTMLResponse)
def month_report(request: Request, month: str | None = Query(None)):
    """The month's carrier record as one printable page for a carrier's account manager or a supplier. Owner-only,
    like every page (the owner gate); it reads the kept checks and changes nothing."""
    today = datetime.now(settings.tz).date()
    try:
        first = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    cost, default = _cost()
    rep = kept_checks.month_report(checks.all(), first, cost, profile.get().currency, default, dispute.DISPUTE)
    return _page(request, "report.html", {"rep": rep, "made": datetime.now(settings.tz), "limit": dispute.LIMITS[0]})


def _demo_scan() -> dict | None:
    """On a demo install (scripts/demo.py's marker in the data folder), the demo's business-closed scan,
    so the desk can offer it already filled in."""
    if not (DATA / ".shopfront-demo").exists():
        return None
    try:
        scan = json.loads((DATA / "demo-scan.json").read_text())
    except (OSError, ValueError):
        return None
    keys = ("date", "attempt", "claim", "carrier", "tracking")
    return {k: str(scan.get(k, "")) for k in keys} if isinstance(scan, dict) and scan.get("attempt") else None


def _claim(value: str | None) -> str:
    value = (value or ATTEMPTED).strip().lower().replace(" ", "_")
    if value not in CLAIMS:
        raise HTTPException(400, f"claim must be one of {', '.join(CLAIMS)}")
    return value


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str | None = Query(None), attempt: str | None = Query(None),
              claim: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    day = _parse_day(date)
    if not date:  # the receiving desk opens on today once today has orders to watch for; else yesterday's door
        today = datetime.now(settings.tz).date()
        if book.for_day(today):
            day = today
    claim = _claim(claim)
    when = None
    if attempt:
        try:
            when = datetime.combine(day, datetime.strptime(attempt, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "attempt must be HH:MM")
    ring_down = None
    try:
        desk = _desk(day, when, claim, carrier.strip()[:40])
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
            # Without the door history no missed-delivery count or money is a fact: the month and the week
            # would count every order Ring didn't push as "no courier seen"
            desk.update(record=[], month_money=None, perishable_at_risk=[], money=None)
        finally:
            _ring_override.reset(token)
        desk["lead"] = _webhook_lead(desk["r"])
    return _page(request, "dashboard.html", {
        **desk, "ring_down": ring_down, "asked_time": attempt or "", "demo_scan": _demo_scan(),
        "kept": checks.find(day.isoformat(), attempt, claim, carrier.strip()[:40], tracking.strip()[:60])
        if attempt and desk.get("attempt") else None,
        **({"ring_problem": None} if ring_down else {}),  # the page's own notice says it, with what still works
        "status_text": STATUS_TEXT,
        "profile": profile.get(), "claims": CLAIMS, "claim": claim, "carrier": carrier.strip()[:40],
        "tracking": tracking.strip()[:60], "received": RECEIVED, "disputable": dispute.DISPUTE, "refused": REFUSED,
        "posted": f"{oh[0]:%H:%M}–{oh[1]:%H:%M}" if (oh := posted_hours(profile.hours(), day)) else "closed",
        **({"can_ask": True, "ask_reason": None} if (ask := _ask_state()) is None else
           {"can_ask": False, "ask_reason": ask}), "ask_example": pages.ASK_EXAMPLE,
        "can_read_scan": scan_reader.available()})


def _webhook_lead(r: dict) -> list[str]:
    """The desk's one line while Ring's door history is missing: what webhooks confirmed, said exactly. A replayed
    or synthetic event is never credited to Ring; "no courier seen" would be a guess, so it isn't said."""
    if not r["total"]:
        return []
    by = [x["visit"] for x in r["expected"] if x["status"].startswith("seen") and x.get("visit")]
    kinds = {provenance.of(v) for v in by}
    how = "by webhook" + (" (replayed)" if by and kinds <= {provenance.REPLAY} else
                          " (some replayed)" if provenance.REPLAY in kinds else "")
    return [f"{r['seen']} of {r['total']} expected deliveries confirmed {how}. "
            "The rest can't be checked until Ring is reconnected."]


def _desk(day: date, when: datetime | None, claim: str, carrier: str = "") -> dict:
    """The dashboard's data. Receiving lines lead (what Ring or Bedrock confirmed, then a checked scan, then
    the guesses; a scan the door disputes leads them all); the rest of the briefing goes below them."""
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
    scan_first = bool(checked and checked["outcome"] in dispute.DISPUTE)
    lead = headline(r) + closed  # a checked scan has its own verdict card above these lines
    return {"b": b, "lead": lead, "lead_is_scan": scan_first, "rest": rest, "r": r, "rows": pages.desk_rows(r),
            "peak": max(b.hourly_customers + b.hourly_customers_last_week) or 1,
            "device": getattr(ring, "_device_id", "") or "", "request_id": getattr(ring, "last_request_id", None),
            "money": _week_money(day), "attempt": checked, "funnel": stats,
            "labels": pages.labelled_by_text(stats, b.classifier), "first_run": not book._all(),
            "bedrock_cost": _bedrock_cost(day, b.classifier),
            "bedrock_held": stats.get("held_from_bedrock", 0) if b.classifier == "bedrock" else 0,
            "replayed": bool(stats.get("replayed")), **_record(day)}


def _bedrock_cost(day: date, labeller: str) -> str | None:
    """The footer's Bedrock line for `day`: calls, tokens and the estimated cost (app/bedrock_budget.py)."""
    calls = DATA / "bedrock-calls.jsonl"
    if labeller != "bedrock" and not calls.exists():
        return None
    c = bedrock_budget.day_cost(calls, day, settings.bedrock_price_in, settings.bedrock_price_out, settings.tz)
    return bedrock_budget.cost_line(c, settings.bedrock_price_set)


@app.post("/bedrock/label")
async def bedrock_label(request: Request):
    """The owner asks Bedrock to label the rest of a day's door events (owner-only and same-site, app/owner.py)."""
    form = await request.form()
    day = _parse_day(str(form.get("date") or "") or None)
    label_requests.ask(day)
    return RedirectResponse(f"/?{urlencode({'date': day.isoformat()})}#events", status_code=303)


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return _page(request, "connect.html", {"connected": bool(source.pasted_token()), "env_mode": settings.ring_mode})


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
    code = {"UNKNOWN_SESSION": 404, "BAD_KEY": 403, "TOO_MANY_SESSIONS": 409}.get(exc.code, 400)
    return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=code)


@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    ring = source.get()
    demo = _demo_scan()  # without Ring, /live leads back to the desk's check (the demo's, when there is one)
    check_href = "/?" + urlencode(demo) if demo else "/#atth"
    return _page(request, "live.html", {"connected": isinstance(ring, RingApiClient), "demo_scan": demo,
                                        "check_href": check_href, "classifier": classifier.name,
                                        "sessions": live.rows()[-10:][::-1]})


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
        try:
            s = live.start(ring.device_id, session_url)
        except LiveError:
            ring.whep_stop(session_url)  # don't leave a Ring session open that nothing will use
            raise
        # the key goes back only to the owner's page that asked; every frame must carry it
        return {"session": s.id, "key": s.key, "answer": answer, "ring_request_id": ring.last_request_id}
    return await run_in_threadpool(start)


@app.post("/api/live/{session_id}/frame")
async def live_frame(session_id: str, request: Request):
    day = datetime.now(settings.tz).date()
    jpeg = await request.body()
    return await run_in_threadpool(live.frame, session_id, jpeg, classifier, posted_hours(profile.hours(), day),
                                   request.headers.get("X-Live-Key"))


@app.post("/api/live/{session_id}/stop")
def live_stop(session_id: str):
    s = live.get(session_id)
    ring_error = None
    try:
        _live_ring().whep_stop(s.session_url)
    except RingError as exc:  # still record the session; report why the stop failed
        ring_error = exc.status.value
    except HTTPException:  # Ring was disconnected meanwhile
        ring_error = "NOT_CONNECTED"
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
    if status == WebhookStatus.ACCEPTED and ring_signed(raw, request.headers.get("X-Signature"), settings.ring_hmac_key):
        _link_webhook(raw)  # Ring's own key only: a replay can't unlink the account
    code = {WebhookStatus.ACCEPTED: 200, WebhookStatus.DUPLICATE: 200, WebhookStatus.MALFORMED: 400,
            WebhookStatus.BAD_SIGNATURE: 401, WebhookStatus.NOT_CONFIGURED: 503}[status]
    return JSONResponse({"status": status.value}, status_code=code)


def _link_webhook(raw: bytes) -> None:
    """app_integration_added / _removed for the linked account (API reference, Notifications)."""
    try:
        body = json.loads(raw)
        kind, account = body["data"]["type"], body["meta"].get("account_id")
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    if kind in ("app_integration_added", "app_integration_removed") and ring_link.on_webhook(kind, account):
        source._key = None  # rebuild the Ring client: the linked tokens changed


# --- Ring account linking (app/ring/link.py; docs/ring.md, section 10) ---

LINK_CODE_BYTES = 8 * 1024


def _link_code(request: Request, raw: bytes) -> str:
    """The authorization code from Ring's POST to the Token Exchange URL. The docs don't show that request's
    body, so a JSON body, a form body and the query string are all read (docs/ring.md, section 10)."""
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    code = None
    if ctype == "application/json":
        try:
            body = json.loads(raw or b"{}")
            code = body.get("code") or body.get("authorization_code") if isinstance(body, dict) else None
        except ValueError:
            code = None
    elif ctype == "application/x-www-form-urlencoded":
        from urllib.parse import parse_qs
        form = parse_qs(raw.decode("utf-8", "replace"))
        code = (form.get("code") or form.get("authorization_code") or [None])[0]
    code = code or request.query_params.get("code")
    return code.strip() if isinstance(code, str) else ""


@app.post("/ring/link/token")
async def ring_link_token(request: Request):
    """Token Exchange URL (Getting Started 4.1): Ring's backend sends the authorization code here."""
    raw = await _capped_body(request, LINK_CODE_BYTES)
    if raw is None:
        return JSONResponse({"status": "TOO_LARGE"}, status_code=413)
    code = _link_code(request, raw)
    if not code:
        return JSONResponse({"status": "NO_CODE"}, status_code=400)
    try:
        await run_in_threadpool(ring_link.exchange, code)  # within Ring's 60 seconds, never queued
    except LinkError as exc:
        return JSONResponse({"status": "FAILED", "step": exc.step, "detail": exc.detail},
                            status_code=503 if exc.step == "config" else 502)
    return JSONResponse({"status": "OK"})


def _link_ctx(request: Request) -> dict:
    row = ring_link.linked()
    base = settings.public_url
    return {"linked": row, "link_missing": ring_link.configured(), "unclaimed": ring_link.unclaimed_count(),
            "public_url": base, "pasted": bool(source.pasted_token()),
            "urls": [("Token Exchange URL", f"{base}/ring/link/token"), ("Account Link URL", f"{base}/ring/link"),
                     ("Webhook URL", f"{base}/webhooks/ring"), ("App Homepage URL", f"{base}/ring/link/home")]
            if base else []}


@app.get("/ring/link", response_class=HTMLResponse)
def ring_link_page(request: Request, nonce: str | None = Query(None), time: str | None = Query(None)):
    """Account Link URL. Ring redirects the owner here with nonce and time; reaching this page at all needs the
    owner's sign-in (the owner gate). Nothing is matched or claimed until the owner presses Link (a POST)."""
    ctx = _link_ctx(request)
    if nonce or time:
        problem = ctx["link_missing"] and f"Shopfront isn't set up for account linking: set {ctx['link_missing']}."
        problem = problem or link_freshness(time) or (None if nonce else "the link from Ring has no nonce")
        ctx.update(nonce=nonce or "", time=time or "", problem=problem,
                   form_token=ring_link.form_token(nonce or "", time or "") if not problem else "")
    return _page(request, "ring_link.html", ctx, 400 if ctx.get("problem") else 200)


@app.post("/ring/link")
def ring_link_confirm(request: Request, nonce: str = Form(""), time: str = Form(""), form_token: str = Form("")):
    ctx = _link_ctx(request)
    ctx.update(nonce=nonce, time=time, form_token="")
    if not ring_link.form_ok(nonce, time, form_token):
        ctx["problem"] = "This confirmation didn't come from Shopfront's own page. Start again from the Ring app."
        return _page(request, "ring_link.html", ctx, 403)
    try:
        row = ring_link.claim(nonce, time, profile.get().name)
    except LinkError as exc:
        ctx.update(problem=exc.detail[:1].upper() + exc.detail[1:] + ".", step=exc.step,
                   request_id=exc.request_id, http_status=exc.http_status)
        return _page(request, "ring_link.html", ctx, 400 if exc.step == "nonce" else 502)
    source.forget_token()  # the linked account replaces a pasted Playground token
    source._key = None
    return _page(request, "ring_link.html", {**_link_ctx(request), "just_linked": row})


@app.get("/ring/link/home")
def ring_link_home():
    """App Homepage URL (Getting Started 4.1): "custom app configuration page for post-linking setup"."""
    return RedirectResponse("/setup#ring", status_code=303)


@app.post("/ring/link/forget")
def ring_link_forget():
    ring_link.forget()
    source._key = None
    return RedirectResponse("/ring/link", status_code=303)


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
    return _page(request, "pair.html", {"next": _safe_next(next), "error": None})


@app.post("/pair")
def pair_device(request: Request, code: str = Form(""), next: str = Form("/")):
    value = gate.pair(code)
    if not value:
        return _page(request, "pair.html", {
            "next": _safe_next(next),
            "error": "That code didn't match or has expired. Use the code Set up shows on the shop's computer now."},
            403)
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
def setup_page(request: Request, error: str | None = None, imported: str | None = None,
               notice: str | None = None, s: str = "shop"):
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
        "ring_linked": source.linked() if not source.pasted_token() else None, "pack_key": _pack_key_summary(),
        "orders": orders[:ORDERS_SHOWN], "orders_more": max(0, len(orders) - ORDERS_SHOWN),
        "order_days": len({e.day for e in orders}), "pairing": _pairing(request)}, status_code)


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
        return _setup(request, "shop", error=str(exc), status_code=400,
                      form={k: str(v) for k, v in form.items()})
    return RedirectResponse(f"/setup?s=shop&notice={quote('Saved. Shopfront uses these hours from now on.')}#shop",
                            status_code=303)


@app.post("/deliveries")
async def delivery_add(request: Request):
    form = await request.form()
    back = str(form.get("back", ""))
    back = back if back.startswith("/") and not back.startswith("//") else ""
    try:
        e = book.add(date.fromisoformat(str(form.get("day", ""))), str(form.get("supplier", "")),
                     normalize_clock(str(form.get("window_start", ""))), normalize_clock(str(form.get("window_end", ""))),
                     str(form.get("note", "")), order_ref=str(form.get("order_ref", "")), carrier=str(form.get("carrier", "")))
    except ValueError as exc:
        return RedirectResponse(f"/setup?s=orders&error={quote(str(exc))}#orders", status_code=303)
    return RedirectResponse(back or f"/setup?s=orders&notice={quote(f'Added {e.supplier} for {e.day}.')}#orders",
                            status_code=303)


@app.post("/deliveries/{expected_id}/remove")
def delivery_remove(request: Request, expected_id: str):
    e = book.get(expected_id)
    book.remove(expected_id)
    msg = f"Removed {e.supplier} ({e.day})." if e else "That order was already gone."
    return RedirectResponse(f"/setup?s=orders&notice={quote(msg)}#orders", status_code=303)


def _back(value) -> str:
    back = str(value or "")
    return back if back.startswith("/") and not back.startswith("//") else "/"


@app.post("/checks")
async def check_keep(request: Request):
    """Keep a scan check in the carrier record. The check is computed again here from the scan's details;
    the page's answer is never taken from the form."""
    form = await request.form()
    claim = _claim(str(form.get("claim", "")))
    try:
        day = date.fromisoformat(str(form.get("date", "")))
        when = datetime.combine(day, datetime.strptime(str(form.get("at", "")), "%H:%M").time())
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD and at HH:MM")
    carrier, tracking = str(form.get("carrier", "")).strip()[:40], str(form.get("tracking", "")).strip()[:60]

    def keep():
        found = _check_attempt(when, claim)
        order = next((e for e in book.for_day(day) if tracking and e.tracking == tracking), None)
        return checks.keep(found, carrier, tracking, order, demo=_ring().name == "synthetic")
    try:
        await run_in_threadpool(keep)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(_back(form.get("back")) + "#record", status_code=303)


@app.post("/checks/{check_id}/delete")
async def check_delete(request: Request, check_id: str):
    form = await request.form()
    checks.remove(check_id)
    return RedirectResponse(_back(form.get("back")) + "#record", status_code=303)


@app.get("/api/deliveries")
def api_deliveries(date: str | None = Query(None)):
    return _receipts(_parse_day(date))


def _around(at: datetime, reach: timedelta) -> list:
    days = sorted({(at - reach).date(), at.date(), (at + reach).date()})  # scans near midnight
    return [v for d in days for v in _visits(d)]


def _local(at: datetime) -> datetime:
    return at.replace(tzinfo=settings.tz) if at.tzinfo is None else at.astimezone(settings.tz)


def _status_notices(around: datetime | None = None) -> list[dict]:
    """The doorbell's device_online / device_offline notifications Ring sent, and Shopfront's own status
    samples (app/coverage.py). In synthetic mode with no samples of its own, the demo's made-up status checks
    around `around`, marked demo."""
    samples = status_log.samples(settings.tz, webhook_log.device_id)
    try:
        ring = _ring()
    except RingError:
        ring = None
    if around is not None and isinstance(ring, SyntheticRingClient) and not samples:  # real checks always win
        samples = samples + [s for d in (around.date() - timedelta(days=1), around.date(), around.date() + timedelta(days=1))
                             for s in ring.status_samples(d)]
    try:
        rows = webhook_log.recent(1_000_000)
    except ValueError:  # a damaged log line: no notices, so coverage falls back to the samples and door events
        return samples
    return coverage.notices(rows, settings.tz, webhook_log.device_id, webhook_log.account_id) + samples


def sample_status() -> dict | None:
    """One status sample, when Ring is live (RING_MODE=live or a pasted token): GET /v1/devices/{id}/status,
    kept in .data/ring-status.jsonl. A check Ring didn't answer is kept as online null, a gap. Synthetic
    mode samples nothing."""
    try:
        ring = source.get()
    except RingError:
        return None
    if not isinstance(ring, RingApiClient):
        return None
    every = settings.status_minutes * 60
    try:
        device = settings.ring_device_id or ring.device_id
    except RingError as exc:
        return status_log.add("", None, every, exc.request_id, exc.status.value)
    try:
        online, request_id = ring.device_status(device)
    except RingError as exc:
        return status_log.add(device, None, every, exc.request_id, exc.status.value)
    return status_log.add(device, online, every, request_id)


def _check_attempt(at: datetime, claim: str = ATTEMPTED) -> dict:
    at = _local(at)
    _focus_scan(at)
    status = _status_notices(at)
    if claim == BUSINESS_CLOSED:
        return check_closed_scan(at, _around(at, dispute.WINDOW), posted_hours(profile.hours(), at.date()),
                                 frames.url, status=status)
    return check_attempt(at, _around(at, TOLERANCE), frames.url, status=status)


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


def _made(value: str | None) -> datetime | None:
    """The "made" time a pack page passes to its zip link, so the zip matches the manifest digest the page
    showed. Only a time in the last day counts; anything else (or nothing) means now."""
    try:
        made = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    now = datetime.now(settings.tz)
    if made.tzinfo is None or not now - timedelta(days=1) <= made <= now + timedelta(minutes=1):
        return None
    return made


def _pack(day: date, at: str | None, claim: str | None, order: str | None, carrier: str, tracking: str,
          made: str | None = None) -> dict:
    try:
        if at:  # the scan is being checked: Bedrock may look around it before the pack reads the door
            _focus_scan(datetime.combine(day, datetime.strptime(at, "%H:%M").time()))
    except ValueError:
        pass  # refused below with a plain message
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
                                  carrier.strip()[:40], tracking.strip()[:60], tz=settings.tz, made_at=_made(made))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/dispute", response_class=HTMLResponse)
def dispute_page(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                 claim: str | None = Query(None), order: str | None = Query(None),
                 carrier: str = Query(""), tracking: str = Query("")):
    day = _parse_day(date)
    p = _pack(day, at, claim, order, carrier, tracking)
    query = request.url.query
    zip_query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "made"]
                          + [("made", p["made_at"])])
    return _page(request, "dispute.html", {"p": p, "query": query, "zip_query": zip_query,
                                           "manifest_sha256": dispute.manifest_digest(p, frames),
                                           "status_text": STATUS_TEXT,
                                           # said once, in the watermark, not again as bullets
                                           "source_limits": (dispute.DEMO_LIMIT, dispute.REPLAY_LIMIT),
                                           "replayed": "ring-webhook-replay" in p["event_sources"]})


@app.get("/api/dispute")
def api_dispute(date: str | None = Query(None), at: str | None = Query(None), claim: str | None = Query(None),
                order: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    return _pack(_parse_day(date), at, claim, order, carrier, tracking)


@app.get("/dispute.zip")
def dispute_zip(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                claim: str | None = Query(None), order: str | None = Query(None),
                carrier: str = Query(""), tracking: str = Query(""), made: str | None = Query(None)):
    p = _pack(_parse_day(date), at, claim, order, carrier, tracking, made)
    name = f"shopfront-dispute-{p['day']}" + (f"-{p['claim']['time'][-5:].replace(':', '')}" if p["claim"] else "")
    body = pack_signing.sign_zip(dispute.as_zip(p, frames), pack_signer, f"{name}.zip")  # + manifest.sig, VERIFY.txt
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"',
                             "X-Manifest-SHA256": dispute.manifest_digest(p, frames),
                             "X-Pack-Key-Id": pack_signer.jwk()["kid"]})


@app.get(pack_signing.KEY_PATH)
def pack_key():
    """The public key dispute packs are signed with, as a JWK (public: a recipient checks a pack against it)."""
    try:
        return JSONResponse(pack_signer.jwk(), headers={"Cache-Control": "max-age=300"})
    except Exception as exc:  # KMS unreachable or misconfigured: say so, never a key we don't have
        return JSONResponse({"error": "KEY_UNAVAILABLE", "detail": type(exc).__name__}, status_code=503)


def _pack_key_summary() -> dict | None:
    try:
        jwk = pack_signer.jwk()
    except Exception:
        return None
    return {"kid": jwk["kid"], "alg": jwk["alg"], "signer": pack_signer.name, "url": pack_signing.KEY_PATH}


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
        return RedirectResponse(f"/setup?s=orders&error={quote(str(exc))}#orders", status_code=303)
    counts = book.import_rows(parsed["rows"])
    day_col = parsed["columns"].get("day", "")
    msg = (f"Imported {counts['added']} new and updated {counts['updated']} orders ({parsed['format']} format)"
           + (f", dated by the '{day_col}' column because the file has no delivery date"
              if day_col.lower().replace(" ", "") == "orderdate" else "")
           + (f". Skipped {len(parsed['errors'])}: " + "; ".join(parsed["errors"][:5]) if parsed["errors"] else "."))
    return RedirectResponse(f"/setup?s=orders&imported={quote(msg)}#orders", status_code=303)


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


@app.post("/deliveries/{expected_id}/perishable")
async def delivery_perishable(request: Request, expected_id: str):
    """The owner marks an order perishable (or not): a missed delivery of it is lost trading, not only a fee."""
    form = await request.form()
    try:
        e = book.set_perishable(expected_id, str(form.get("on", "")) == "1")
    except KeyError:
        raise HTTPException(404, "no such delivery")
    back = form.get("back")
    return RedirectResponse(_back(back) if back else f"/?date={e.day}#dlv", status_code=303)


@app.post("/scan/extract")
def scan_extract(request: Request, shot: UploadFile = File(...)):
    """Read a carrier tracking-page screenshot (JPEG or PNG) into values for the scan form.

    Owner-only and same-site like every other write (app/owner.py middleware). The answer is a prefill
    the page shows for the owner to confirm; nothing is checked or submitted here, and the picture
    isn't kept. Errors are JSON with a plain sentence in `detail`."""
    if int(request.headers.get("content-length") or 0) > MAX_SCAN_BYTES + 64 * 1024:
        return JSONResponse({"error": "TOO_BIG", "detail": "That picture is over 3.75 MB. Crop it to the "
                             "tracking details, or save it smaller, and try again."}, status_code=413)
    data = shot.file.read(MAX_SCAN_BYTES + 1)
    try:
        out = scan_reader.read(data, today=datetime.now(settings.tz).date())
    except ScanReadError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.http_status)
    return {"ok": True, "source": "bedrock", **out}


@app.get("/frames/{name}")
def frame(request: Request, name: str):
    if frame_name(name.removesuffix(".jpg")) != name:
        raise HTTPException(404)
    path = frames.path(name.removesuffix(".jpg"))
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/favicon.ico")
def favicon():  # for clients that ask for it anyway; the pages carry the icon inline
    return Response(pages.FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.post("/frames/{name}/delete")
async def frame_delete(request: Request, name: str):
    """Delete a door frame now (and, for a live view, every frame kept from that session). A form gets
    sent back to the page it came from; a script gets {"deleted": n}."""
    form = await request.form() if "form" in request.headers.get("content-type", "") else None
    gone = await run_in_threadpool(_delete_frame, name)
    if form is not None:
        back = _safe_next(str(form.get("back", "/")))
        return RedirectResponse(back + ("&" if "?" in back else "?") + f"notice={quote('Frame deleted.')}",
                                status_code=303)
    return {"deleted": gone}


def _delete_frame(name: str) -> int:
    stem = name.removesuffix(".jpg")
    if frame_name(stem) != name:
        raise HTTPException(404)
    gone = 0
    for d in (DATA / "frames", DATA / "live-frames"):
        for p in [d / name, *(d.glob(f"{stem}-f*.jpg") if stem.startswith("live-") else [])]:
            if p.is_file():
                p.unlink()
                gone += 1
    return gone


_last_housekeeping = 0.0


def _housekeeping() -> None:
    """At most hourly: drop door frames older than SHOPFRONT_FRAME_DAYS (0 keeps them)."""
    global _last_housekeeping
    if time.time() - _last_housekeeping < 3600:
        return
    _last_housekeeping = time.time()
    for d in (DATA / "frames", DATA / "live-frames"):
        prune_older_than(d, settings.frame_days)


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
    except shop_agent.AgentUnavailable as exc:  # reason: named state; message: the sentence to show
        ask_probe.mark(exc.reason)  # a lasting failure: the page stops offering the box
        return JSONResponse({"error": "AGENT_UNAVAILABLE", "reason": exc.reason, "message": exc.message,
                             "detail": str(exc)}, status_code=503)


agent_factory = shop_agent.build_agent  # tests swap in a fake
