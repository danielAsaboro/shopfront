"""The dispute pack: /dispute, its JSON (/api/dispute), its signed zip, and the public key packs are signed
with."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import dispute, pack_signing
from app.deliveries import STATUS_TEXT
from app.hours import posted_hours
from app.routes import common

router = APIRouter()


def _made(value: str | None) -> datetime | None:
    """The "made" time a pack page passes to its zip link, so the zip matches the manifest digest the page
    showed. Only a time in the last day counts; anything else (or nothing) means now."""
    try:
        made = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    now = datetime.now(common.settings.tz)
    if made.tzinfo is None or not now - timedelta(days=1) <= made <= now + timedelta(minutes=1):
        return None
    return made


def _pack(day: date, at: str | None, claim: str | None, order: str | None, carrier: str, tracking: str,
          made: str | None = None) -> dict:
    try:
        if at:  # the scan is being checked: Bedrock may look around it before the pack reads the door
            common._focus_scan(datetime.combine(day, datetime.strptime(at, "%H:%M").time()))
    except ValueError:
        pass  # refused below with a plain message
    visits = common._visits(day)
    e = common.book.get(order) if order else None
    if order and not e:
        raise HTTPException(404, "no such order")
    if not e and tracking.strip():  # the carrier's tracking number names the order it was for
        e = next((x for x in common.book.for_day(day) if x.tracking and x.tracking == tracking.strip()), None)
    check = None
    if at:
        try:
            when = datetime.combine(day, datetime.strptime(at, "%H:%M").time())
        except ValueError:
            raise HTTPException(400, "at must be HH:MM")
        check = common._check_attempt(when, common._claim(claim))
    receipt = None
    if e:
        r = common._receipts(date.fromisoformat(e.day))
        receipt = next((x for x in r["expected"] if x["id"] == e.id), None)
    oh = posted_hours(common.profile.hours(), day)
    try:
        return dispute.build_pack(common.profile.get().name, day, visits, common.frames,
                                  f"{oh[0]:%H:%M}–{oh[1]:%H:%M}" if oh else "closed", check, e, receipt,
                                  carrier.strip()[:40], tracking.strip()[:60], tz=common.settings.tz, made_at=_made(made))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/dispute", response_class=HTMLResponse)
def dispute_page(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                 claim: str | None = Query(None), order: str | None = Query(None),
                 carrier: str = Query(""), tracking: str = Query("")):
    day = common._parse_day(date)
    p = _pack(day, at, claim, order, carrier, tracking)
    query = request.url.query
    zip_query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "made"]
                          + [("made", p["made_at"])])
    return common._page(request, "dispute.html", {"p": p, "query": query, "zip_query": zip_query,
                                           "manifest_sha256": dispute.manifest_digest(p, common.frames),
                                           "status_text": STATUS_TEXT,
                                           # said once, in the watermark, not again as bullets
                                           "source_limits": (dispute.DEMO_LIMIT, dispute.REPLAY_LIMIT),
                                           "replayed": "ring-webhook-replay" in p["event_sources"]})


@router.get("/api/dispute")
def api_dispute(date: str | None = Query(None), at: str | None = Query(None), claim: str | None = Query(None),
                order: str | None = Query(None), carrier: str = Query(""), tracking: str = Query("")):
    return _pack(common._parse_day(date), at, claim, order, carrier, tracking)


@router.get("/dispute.zip")
def dispute_zip(request: Request, date: str | None = Query(None), at: str | None = Query(None),
                claim: str | None = Query(None), order: str | None = Query(None),
                carrier: str = Query(""), tracking: str = Query(""), made: str | None = Query(None)):
    p = _pack(common._parse_day(date), at, claim, order, carrier, tracking, made)
    name = f"shopfront-dispute-{p['day']}" + (f"-{p['claim']['time'][-5:].replace(':', '')}" if p["claim"] else "")
    body = pack_signing.sign_zip(dispute.as_zip(p, common.frames), common.pack_signer, f"{name}.zip")  # + manifest.sig, VERIFY.txt
    return Response(body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"',
                             "X-Manifest-SHA256": dispute.manifest_digest(p, common.frames),
                             "X-Pack-Key-Id": common.pack_signer.jwk()["kid"]})


@router.get(pack_signing.KEY_PATH)
def pack_key():
    """The public key dispute packs are signed with, as a JWK (public: a recipient checks a pack against it)."""
    try:
        return JSONResponse(common.pack_signer.jwk(), headers={"Cache-Control": "max-age=300"})
    except Exception as exc:  # KMS unreachable or misconfigured: say so, never a key we don't have
        return JSONResponse({"error": "KEY_UNAVAILABLE", "detail": type(exc).__name__}, status_code=503)
