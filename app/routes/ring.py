"""Ring: pasting a Playground token (/connect), the live door (/live, /api/live/*), Ring's signed webhooks, one-way
account linking (/ring/link; docs/ring.md, section 10) and the doorbell's status samples."""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.hours import posted_hours
from app.live import LiveError
from app.ring.api import RingApiClient, RingError
from app.ring.webhooks import MAX_BODY as MAX_WEBHOOK_BODY
from app.ring.webhooks import WebhookStatus
from app.ring.webhooks import verify as ring_signed
from app.ring.link import LinkError, freshness as link_freshness
from app.routes import common

router = APIRouter()


@router.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return common._page(request, "connect.html", {"connected": bool(common.source.pasted_token()), "env_mode": common.settings.ring_mode})


@router.post("/connect")
def connect_save(request: Request, token: str = Form(...)):
    if not token.strip():
        raise HTTPException(400, "paste a token")
    common.source.save_token(token)
    return RedirectResponse("/", status_code=303)


@router.post("/connect/forget")
def connect_forget(request: Request):
    common.source.forget_token()
    return RedirectResponse("/", status_code=303)


def _live_ring() -> RingApiClient:
    ring = common.source.get()
    if not isinstance(ring, RingApiClient):
        raise HTTPException(409, "Connect Ring first: live view needs a Ring token (/connect)")
    return ring


@router.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    ring = common.source.get()
    demo = common._demo_scan()  # without Ring, /live leads back to the desk's check (the demo's, when there is one)
    check_href = "/?" + urlencode(demo) if demo else "/#atth"
    return common._page(request, "live.html", {"connected": isinstance(ring, RingApiClient), "demo_scan": demo,
                                        "check_href": check_href, "classifier": common.classifier.name,
                                        "sessions": common.live.rows()[-10:][::-1]})


# The async handlers below read the request body, then run every blocking call (Ring over httpx, Bedrock
# over boto3, a Strands agent turn) in the thread pool, so Ring's webhook (5 s limit) is never kept waiting.
@router.post("/api/live/start")
async def live_start(request: Request):
    offer = (await request.body()).decode()
    if "v=0" not in offer:
        raise HTTPException(400, "body must be an SDP offer")

    def start():
        ring = _live_ring()
        answer, session_url = ring.whep_start(offer)
        try:
            s = common.live.start(ring.device_id, session_url)
        except LiveError:
            ring.whep_stop(session_url)  # don't leave a Ring session open that nothing will use
            raise
        # the key goes back only to the owner's page that asked; every frame must carry it
        return {"session": s.id, "key": s.key, "answer": answer, "ring_request_id": ring.last_request_id}
    return await run_in_threadpool(start)


@router.post("/api/live/{session_id}/frame")
async def live_frame(session_id: str, request: Request):
    day = datetime.now(common.settings.tz).date()
    jpeg = await request.body()
    return await run_in_threadpool(common.live.frame, session_id, jpeg, common.classifier, posted_hours(common.profile.hours(), day),
                                   request.headers.get("X-Live-Key"))


@router.post("/api/live/{session_id}/stop")
def live_stop(session_id: str):
    s = common.live.get(session_id)
    ring_error = None
    try:
        _live_ring().whep_stop(s.session_url)
    except RingError as exc:  # still record the session; report why the stop failed
        ring_error = exc.status.value
    except HTTPException:  # Ring was disconnected meanwhile
        ring_error = "NOT_CONNECTED"
    return {**common.live.stop(session_id), "ring_stop_error": ring_error}


@router.get("/api/live/sessions")
def live_sessions():
    return common.live.rows()


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


@router.post("/webhooks/ring")
async def ring_webhook(request: Request):
    raw = await _capped_body(request, MAX_WEBHOOK_BODY)
    if raw is None:
        return JSONResponse({"status": "TOO_LARGE"}, status_code=413)
    # A replay is told apart by its signing key and body marker (app/ring/webhooks.py); the header can only
    # ever mark one as a replay, never vouch for it being Ring's.
    status = common.webhook_log.handle(raw, request.headers.get("X-Signature"), common.settings.ring_hmac_key,
                                replayed=request.headers.get("X-Shopfront-Replay") == "1")
    if status == WebhookStatus.ACCEPTED and ring_signed(raw, request.headers.get("X-Signature"), common.settings.ring_hmac_key):
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
    if kind in ("app_integration_added", "app_integration_removed") and common.ring_link.on_webhook(kind, account):
        common.source._key = None  # rebuild the Ring client: the linked tokens changed


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


@router.post("/ring/link/token")
async def ring_link_token(request: Request):
    """Token Exchange URL (Getting Started 4.1): Ring's backend sends the authorization code here."""
    raw = await _capped_body(request, LINK_CODE_BYTES)
    if raw is None:
        return JSONResponse({"status": "TOO_LARGE"}, status_code=413)
    code = _link_code(request, raw)
    if not code:
        return JSONResponse({"status": "NO_CODE"}, status_code=400)
    try:
        await run_in_threadpool(common.ring_link.exchange, code)  # within Ring's 60 seconds, never queued
    except LinkError as exc:
        return JSONResponse({"status": "FAILED", "step": exc.step, "detail": exc.detail},
                            status_code=503 if exc.step == "config" else 502)
    return JSONResponse({"status": "OK"})


def _link_ctx(request: Request) -> dict:
    row = common.ring_link.linked()
    base = common.settings.public_url
    return {"linked": row, "link_missing": common.ring_link.configured(), "unclaimed": common.ring_link.unclaimed_count(),
            "public_url": base, "pasted": bool(common.source.pasted_token()),
            "urls": [("Token Exchange URL", f"{base}/ring/link/token"), ("Account Link URL", f"{base}/ring/link"),
                     ("Webhook URL", f"{base}/webhooks/ring"), ("App Homepage URL", f"{base}/ring/link/home")]
            if base else []}


@router.get("/ring/link", response_class=HTMLResponse)
def ring_link_page(request: Request, nonce: str | None = Query(None), time: str | None = Query(None)):
    """Account Link URL. Ring redirects the owner here with nonce and time; reaching this page at all needs the
    owner's sign-in (the owner gate). Nothing is matched or claimed until the owner presses Link (a POST)."""
    ctx = _link_ctx(request)
    if nonce or time:
        problem = ctx["link_missing"] and f"Shopfront isn't set up for account linking: set {ctx['link_missing']}."
        problem = problem or link_freshness(time) or (None if nonce else "the link from Ring has no nonce")
        ctx.update(nonce=nonce or "", time=time or "", problem=problem,
                   form_token=common.ring_link.form_token(nonce or "", time or "") if not problem else "")
    return common._page(request, "ring_link.html", ctx, 400 if ctx.get("problem") else 200)


@router.post("/ring/link")
def ring_link_confirm(request: Request, nonce: str = Form(""), time: str = Form(""), form_token: str = Form("")):
    ctx = _link_ctx(request)
    ctx.update(nonce=nonce, time=time, form_token="")
    if not common.ring_link.form_ok(nonce, time, form_token):
        ctx["problem"] = "This confirmation didn't come from Shopfront's own page. Start again from the Ring app."
        return common._page(request, "ring_link.html", ctx, 403)
    try:
        row = common.ring_link.claim(nonce, time, common.profile.get().name)
    except LinkError as exc:
        ctx.update(problem=exc.detail[:1].upper() + exc.detail[1:] + ".", step=exc.step,
                   request_id=exc.request_id, http_status=exc.http_status)
        return common._page(request, "ring_link.html", ctx, 400 if exc.step == "nonce" else 502)
    common.source.forget_token()  # the linked account replaces a pasted Playground token
    common.source._key = None
    return common._page(request, "ring_link.html", {**_link_ctx(request), "just_linked": row})


@router.get("/ring/link/home")
def ring_link_home():
    """App Homepage URL (Getting Started 4.1): "custom app configuration page for post-linking setup"."""
    return RedirectResponse("/setup#ring", status_code=303)


@router.post("/ring/link/forget")
def ring_link_forget():
    common.ring_link.forget()
    common.source._key = None
    return RedirectResponse("/ring/link", status_code=303)


def sample_status() -> dict | None:
    """One status sample, when Ring is live (RING_MODE=live or a pasted token): GET /v1/devices/{id}/status,
    kept in .data/ring-status.jsonl. A check Ring didn't answer is kept as online null, a gap. Synthetic
    mode samples nothing."""
    try:
        ring = common.source.get()
    except RingError:
        return None
    if not isinstance(ring, RingApiClient):
        return None
    every = common.settings.status_minutes * 60
    try:
        device = common.settings.ring_device_id or ring.device_id
    except RingError as exc:
        return common.status_log.add("", None, every, exc.request_id, exc.status.value)
    try:
        online, request_id = ring.device_status(device)
    except RingError as exc:
        return common.status_log.add(device, None, every, exc.request_id, exc.status.value)
    return common.status_log.add(device, online, every, request_id)
