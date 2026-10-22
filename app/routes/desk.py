"""The receiving desk (/) and what it changes: the carrier record, a kept scan check, an order received or
perishable, asking Bedrock about a day. Also the month's carrier record as one printable page (/report)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import checks as kept_checks
from app import bedrock_budget, dispute, pages, provenance
from app.reconcile import CLAIMS, REFUSED
from app.deliveries import RECEIVED, STATUS_TEXT, default_cost, headline, money_line
from app.briefing import presses_line
from app.hours import posted_hours
from app.ring.api import RingError, RingStatus
from app.routes import common

router = APIRouter()


def _week_money(day: date, sourced_above: bool = False) -> str | None:
    p = common.profile.get()
    cost = p.redelivery_cost if p.redelivery_cost is not None else default_cost(p.currency)  # labelled default
    if cost is None:
        return None
    closed = missing = 0
    for i in range(7):
        d = day - timedelta(days=i)
        try:
            r = common._receipts(d)
        except RingError:  # e.g. RATE_LIMITED on one past day: leave the week's line out, keep the page
            return None
        closed += r["closed_couriers"]  # Ring- or Bedrock-confirmed couriers only, staff not in
        missing += sum(1 for x in r["expected"] if x["status"] == "not_seen" and not x.get("possible_elsewhere"))
    return money_line(closed, missing, cost, p.currency, default=p.redelivery_cost is None, sourced_above=sourced_above)


def _month_confirmed(day: date) -> int | None:
    """Orders this month, up to `day`, whose courier Ring or Bedrock saw at the door. Only days with
    orders are read; a Ring error leaves the number out rather than undercounting."""
    today = datetime.now(common.settings.tz).date()
    n, d = 0, day.replace(day=1)
    while d <= min(day, today):
        if common.book.for_day(d):
            try:
                n += sum(1 for x in common._receipts(d)["expected"] if x["status"].startswith("seen"))
            except RingError:
                return None
        d += timedelta(days=1)
    return n


def _record(day: date) -> dict:
    """The kept checks' carrier record and the month's money, for the desk."""
    kept = common.checks.all()
    cost, default = common._cost()
    record = kept_checks.carrier_record(kept, day, cost, common.profile.get().currency, dispute.DISPUTE)
    for c in record:  # the desk's one-line summary: the pattern without the money, which sits with its source
        c["summary"] = c["headline"].removesuffix(f", about {c['at_stake']} at stake") if c["at_stake"] else c["headline"]
    return {"record": record,
            "perishable_at_risk": kept_checks.perishable_at_risk(kept, day, dispute.DISPUTE),
            "month_money": kept_checks.month_money(kept, day, _month_confirmed(day), cost, common.profile.get().currency,
                                                   default, dispute.DISPUTE)}


@router.get("/report", response_class=HTMLResponse)
def month_report(request: Request, month: str | None = Query(None)):
    """The month's carrier record as one printable page for a carrier's account manager or a supplier. Owner-only,
    like every page (the owner gate); it reads the kept checks and changes nothing."""
    today = datetime.now(common.settings.tz).date()
    try:
        first = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    cost, default = common._cost()
    rep = kept_checks.month_report(common.checks.all(), first, cost, common.profile.get().currency, default, dispute.DISPUTE)
    return common._page(request, "report.html", {"rep": rep, "made": datetime.now(common.settings.tz), "limit": dispute.LIMITS[0]})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, date: str | None = Query(None), attempt: str | None = Query(None),
              claim: str | None = Query(None), carrier: str = Query(""), tracking: str = Query(""),
              order: str = Query("")):
    day = common._parse_day(date)
    if not date:  # the receiving desk opens on today once today has orders to watch for; else yesterday's door
        today = datetime.now(common.settings.tz).date()
        if common.book.for_day(today):
            day = today
    claim = common._claim(claim)
    when = None
    if attempt:
        try:
            when = datetime.combine(day, datetime.strptime(attempt, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "attempt must be HH:MM")
    ring_down = None
    try:
        desk = _desk(day, when, claim, carrier.strip()[:40])
        common._ring_seen(None)
    except RingError as exc:
        if exc.status == RingStatus.NOT_CONFIGURED:
            raise
        # Degrade, don't die: the orders and what Ring pushed by webhook still make a receiving desk.
        # No scan check without the door history: "nothing recorded" would be a guess.
        common._ring_seen(exc)
        ring_down, token = exc, common._ring_override.set(common._NoHistory(common.settings.tz))
        try:
            desk = _desk(day, None, claim)
            # Without the door history no missed-delivery count or money is a fact: the month and the week
            # would count every order Ring didn't push as "no courier seen"
            desk.update(record=[], month_money=None, perishable_at_risk=[], money=None, cost_said=False)
        finally:
            common._ring_override.reset(token)
        desk["lead"] = _webhook_lead(desk["r"])
    return common._page(request, "dashboard.html", {
        **desk, "ring_down": ring_down, "asked_time": attempt or "", "demo_scan": common._demo_scan(),
        "kept": common.checks.find(day.isoformat(), attempt, claim, carrier.strip()[:40], tracking.strip()[:60])
        if attempt and desk.get("attempt") else None,
        **({"ring_problem": None} if ring_down else {}),  # the page's own notice says it, with what still works
        "status_text": STATUS_TEXT,
        "profile": common.profile.get(), "claims": CLAIMS, "claim": claim, "carrier": carrier.strip()[:40],
        "tracking": tracking.strip()[:60], "order_id": order.strip()[:40], "received": RECEIVED, "disputable": dispute.DISPUTE, "refused": REFUSED,
        "posted": f"{oh[0]:%H:%M}–{oh[1]:%H:%M}" if (oh := posted_hours(common.profile.hours(), day)) else "closed",
        **({"can_ask": True, "ask_reason": None} if (ask := common._ask_state()) is None else
           {"can_ask": False, "ask_reason": ask}), "ask_example": pages.ASK_EXAMPLE,
        "can_read_scan": common.scan_reader.available()})


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
    checked = common._check_attempt(when, claim) if when else None
    stats: dict = {}
    b = common._briefing(day, stats)
    ring = common._ring()
    r = common._receipts(day)
    matched = {x["visit"]["time"] for x in r["expected"] if x["status"].startswith("seen_while_closed")}
    # The briefing's closed-shop lines belong with the deliveries; the rest of it goes below
    courier = [ln for ln in b.headline if "while the shop was closed" in ln]
    presses = [ln for ln in b.headline if ln.startswith("Doorbell pressed while closed")]
    rest = [ln for ln in b.headline if ln not in courier + presses]
    # A press already offered as an order's possible courier (one inside a window set outside posted hours) is
    # listed there only, never also as a press while closed
    offered = {p["event_id"] for x in r["expected"] for p in x["possible"]}
    left = [m for m in b.presses_while_closed if m["id"] not in offered]
    presses = [presses_line(left)] if presses and left else []
    if {m["time"] for m in b.deliveries_while_closed} <= matched:
        courier = []  # the receipts already say it
    closed = courier + presses
    scan_first = bool(checked and checked["outcome"] in dispute.DISPUTE)
    # The survey figure's caveat is said once, where the money first shows: the month's line in the carrier record
    # when there is one; the week's line then only points back to it
    record = _record(day)
    mm = record["month_money"]
    cost_said = bool(mm and mm["each"] and mm["default"])
    lead = headline(r) + closed  # a checked scan has its own verdict card above these lines
    return {"b": b, "lead": lead, "lead_is_scan": scan_first, "rest": rest, "r": r, "rows": pages.desk_rows(r),
            "peak": max(b.hourly_customers + b.hourly_customers_last_week) or 1,
            "device": getattr(ring, "_device_id", "") or "", "request_id": getattr(ring, "last_request_id", None),
            "money": _week_money(day, cost_said), "cost_said": cost_said, "attempt": checked, "funnel": stats,
            "labels": pages.labelled_by_text(stats, b.classifier), "first_run": not common.book._all(),
            "bedrock_cost": _bedrock_cost(day, b.classifier),
            "bedrock_held": stats.get("held_from_bedrock", 0) if b.classifier == "bedrock" else 0,
            "replayed": bool(stats.get("replayed")), **record}


def _bedrock_cost(day: date, labeller: str) -> str | None:
    """The footer's Bedrock line for `day`: calls, tokens and the estimated cost (app/bedrock_budget.py)."""
    calls = common.DATA / "bedrock-calls.jsonl"
    if labeller != "bedrock" and not calls.exists():
        return None
    c = bedrock_budget.day_cost(calls, day, common.settings.bedrock_price_in, common.settings.bedrock_price_out, common.settings.tz)
    return bedrock_budget.cost_line(c, common.settings.bedrock_price_set)


@router.post("/bedrock/label")
async def bedrock_label(request: Request):
    """The owner asks Bedrock to label the rest of a day's door events (owner-only and same-site, app/owner.py)."""
    form = await request.form()
    day = common._parse_day(str(form.get("date") or "") or None)
    common.label_requests.ask(day)
    return RedirectResponse(f"/?{urlencode({'date': day.isoformat()})}#events", status_code=303)


@router.post("/checks")
async def check_keep(request: Request):
    """Keep a scan check in the carrier record. The check is computed again here from the scan's details;
    the page's answer is never taken from the form."""
    form = await request.form()
    claim = common._claim(str(form.get("claim", "")))
    try:
        day = date.fromisoformat(str(form.get("date", "")))
        when = datetime.combine(day, datetime.strptime(str(form.get("at", "")), "%H:%M").time())
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD and at HH:MM")
    carrier, tracking = str(form.get("carrier", "")).strip()[:40], str(form.get("tracking", "")).strip()[:60]

    def keep():
        found = common._check_attempt(when, claim)
        order = common.book.for_scan(day, tracking, str(form.get("order", "")))
        return common.checks.keep(found, carrier, tracking, order, demo=common._ring().name == "synthetic")
    try:
        await run_in_threadpool(keep)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(common._back(form.get("back")) + "#record", status_code=303)


@router.post("/checks/{check_id}/delete")
async def check_delete(request: Request, check_id: str):
    form = await request.form()
    common.checks.remove(check_id)
    return RedirectResponse(common._back(form.get("back")) + "#record", status_code=303)


@router.post("/deliveries/{expected_id}/received")
async def delivery_received(request: Request, expected_id: str):
    form = await request.form()
    try:
        e = common.book.mark_received(expected_id, str(form.get("status", "")), str(form.get("note", "")),
                               datetime.now(common.settings.tz))
    except KeyError:
        raise HTTPException(404, "no such delivery")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(f"/?date={e.day}#dlv", status_code=303)


@router.post("/deliveries/{expected_id}/perishable")
async def delivery_perishable(request: Request, expected_id: str):
    """The owner marks an order perishable (or not): a missed delivery of it is lost trading, not only a fee."""
    form = await request.form()
    try:
        e = common.book.set_perishable(expected_id, str(form.get("on", "")) == "1")
    except KeyError:
        raise HTTPException(404, "no such delivery")
    back = form.get("back")
    return RedirectResponse(common._back(back) if back else f"/?date={e.day}#dlv", status_code=303)
