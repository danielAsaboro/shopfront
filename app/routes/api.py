"""The JSON APIs over the same data the pages show, the tracking-page screenshot reader, and Ask Shopfront (a
Strands agent on Bedrock with tools over that data, app/agent.py)."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import agent as shop_agent
from app.briefing import _is_open
from app.scan_extract import MAX_BYTES as MAX_SCAN_BYTES
from app.scan_extract import ScanReadError
from app.reconcile import ATTEMPTED
from app.hours import posted_hours
from app.routes import common, disputes

router = APIRouter()


@router.get("/api/briefing")
def api_briefing(date: str | None = Query(None)):
    return asdict(common._briefing(common._parse_day(date)))


@router.get("/api/events")
def api_events(date: str | None = Query(None)):
    visits = common._visits(common._parse_day(date))
    return [{"id": v.event.id, "time": v.event.timestamp.isoformat(), "kind": v.event.kind,
             "event_source": v.event.source, "type": v.visit_type.value, "confidence": v.confidence,
             "reason": v.reason, "classifier": v.source, "bedrock_request_id": v.request_id}
            for v in visits]


@router.get("/api/webhooks")
def api_webhooks():
    return common.webhook_log.recent()


@router.get("/api/deliveries")
def api_deliveries(date: str | None = Query(None)):
    return common._receipts(common._parse_day(date))


def _when(at: str) -> datetime:
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        raise ValueError("use YYYY-MM-DDTHH:MM")
    if when.date() > datetime.now(common.settings.tz).date():
        raise ValueError("that time is in the future")
    return when


@router.get("/api/attempt")
def api_attempt(at: str = Query(..., description="carrier's scan time, YYYY-MM-DDTHH:MM, shop local"),
                claim: str = Query(ATTEMPTED, description="attempted | business_closed")):
    try:
        when = _when(at)
    except ValueError as exc:
        return JSONResponse({"error": "BAD_TIME", "detail": str(exc)}, status_code=400)
    return common._check_attempt(when, common._claim(claim))


@router.post("/scan/extract")
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
        out = common.scan_reader.read(data, today=datetime.now(common.settings.tz).date())
    except ScanReadError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.http_status)
    return {"ok": True, "source": "bedrock", **out}


def _agent_tools(today: date) -> shop_agent.ShopTools:
    def visits(d: date) -> list[dict]:
        oh = posted_hours(common.profile.hours(), d)
        return [{"time": v.event.timestamp.strftime("%H:%M"), "ring_event": v.event.kind,
                 "event_source": v.event.source, "type": v.visit_type.value, "labelled_by": v.source,
                 "confidence": v.confidence, "reason": v.reason, "shop_open": _is_open(v, oh),
                 "frame_url": common.frames.url(v.event.id)} for v in common._visits(d)]

    def hours_text(d: date) -> str:
        oh = posted_hours(common.profile.hours(), d)
        return f"{oh[0]:%H:%M}-{oh[1]:%H:%M}" if oh else "closed"

    def pack(d: date, at: str, claim: str, order_ref: str) -> dict:
        e = next((x for x in common.book.for_day(d) if order_ref and order_ref.lower() in (x.order_ref.lower(), x.id)), None)
        if order_ref and not e:
            raise ValueError(f"no order {order_ref!r} on {d.isoformat()}")
        try:
            p = disputes._pack(d, at or None, claim or None, e.id if e else None, "", "")
        except HTTPException as exc:
            raise ValueError(exc.detail)
        link = "/dispute?" + "&".join(f"{k}={quote(v)}" for k, v in
                                      (("date", d.isoformat()), ("at", at), ("claim", claim), ("order", e.id if e else ""))
                                      if v)
        return {"finding": p["finding"], "order": p["order"], "frame": p["frame"], "draft": p["draft"],
                "no_draft_reason": p["no_draft_reason"], "limits": p["limits"], "pack_url": link}

    return shop_agent.ShopTools(visits, common._receipts, hours_text, today, common._check_attempt, pack)


@router.post("/api/ask")
async def api_ask(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "BAD_QUESTION", "detail": 'send {"question": "..."}'}, status_code=400)
    try:
        return await run_in_threadpool(shop_agent.ask, str(body.get("question", "")),
                                       _agent_tools(datetime.now(common.settings.tz).date()), common.profile.get().name,
                                       common.settings.aws_region, common.settings.bedrock_model_id, agent_factory=agent_factory)
    except ValueError as exc:
        return JSONResponse({"error": "BAD_QUESTION", "detail": str(exc)}, status_code=400)
    except shop_agent.AgentUnavailable as exc:  # reason: named state; message: the sentence to show
        common.ask_probe.mark(exc.reason)  # a lasting failure: the page stops offering the box
        return JSONResponse({"error": "AGENT_UNAVAILABLE", "reason": exc.reason, "message": exc.message,
                             "detail": str(exc)}, status_code=503)


agent_factory = shop_agent.build_agent  # tests swap in a fake
