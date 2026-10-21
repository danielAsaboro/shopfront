"""What every route shares: the owner's stores, the page chrome, and reading the door for a day.

Routers read these as `common.<name>` when a request runs, never `from app.routes.common import <name>`, so a
test or the public demo that swaps a store on `app.main` (which hands the swap to this module) swaps it for
every route."""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.ask_probe import AskProbe
from app.briefing import build_briefing, classify_day, playground_mode
from app.cache import ClassificationCache
from app.classify import make_classifier
from app.classify import _aws_creds_available
from app.config import settings
from app import checks as kept_checks
from app import bedrock_budget, coverage, dispute, pack_signing, pages, provenance
from app.files import private_dir, prune_older_than
from app.owner import OwnerGate, PublicDemoGate
from app.scan_extract import ScanReader
from app.reconcile import ATTEMPTED, BUSINESS_CLOSED, CLAIMS, TOLERANCE, check_attempt, check_closed_scan
from app.deliveries import COST_SOURCE, DeliveryBook, FrameStore, default_cost, receipts
from app.hours import posted_hours
from app.live import LiveLog
from app.ring import RingSource
from app.ring.base import RingClient
from app.ring.synthetic import SyntheticRingClient
from app.ring.api import RingApiClient, RingError
from app.ring.link import RingLink
from app.ring.status import StatusLog
from app.shop import ShopProfile
from app.store import make_webhook_log


templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
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


def _ring_error_body(exc: RingError) -> dict:
    return {"error": exc.status.value, "detail": exc.detail, "http_status": exc.http_status,
            "request_id": exc.request_id, "retry_after": exc.retry_after}


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


def _cost() -> tuple[float | None, bool]:
    """A missed delivery's cost: the owner's number, else the survey default (said as such)."""
    p = profile.get()
    return (p.redelivery_cost, False) if p.redelivery_cost is not None else (default_cost(p.currency), True)


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


def _back(value) -> str:
    back = str(value or "")
    return back if back.startswith("/") and not back.startswith("//") else "/"


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


def _check_attempt(at: datetime, claim: str = ATTEMPTED) -> dict:
    at = _local(at)
    _focus_scan(at)
    status = _status_notices(at)
    if claim == BUSINESS_CLOSED:
        return check_closed_scan(at, _around(at, dispute.WINDOW), posted_hours(profile.hours(), at.date()),
                                 frames.url, status=status)
    return check_attempt(at, _around(at, TOLERANCE), frames.url, status=status)


_last_housekeeping = 0.0


def _housekeeping() -> None:
    """At most hourly: drop door frames older than SHOPFRONT_FRAME_DAYS (0 keeps them)."""
    global _last_housekeeping
    if time.time() - _last_housekeeping < 3600:
        return
    _last_housekeeping = time.time()
    for d in (DATA / "frames", DATA / "live-frames"):
        prune_older_than(d, settings.frame_days)
