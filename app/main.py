"""The Shopfront app: the owner gate, the error pages, the status sampler, and the routes from app/routes/.

`app.main:app` is what uvicorn, scripts/demo.py and app/public_demo.py serve. The routes live by area in
app/routes/ (desk, disputes, setup, ring, api, site) and share the owner's stores through app/routes/common.py.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import ModuleType

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import pages
from app.live import LiveError
from app.owner import RING_SERVER_PATHS, UNSAFE
from app.ring.api import RingError, RingStatus
from app.ring.status import sampler
from app.routes import api, common, desk, disputes, ring, setup, site


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """While Ring is live, ask it every SHOPFRONT_STATUS_MINUTES whether the doorbell is online (app/ring/status.py)."""
    task = asyncio.create_task(sampler(ring.sample_status, common.settings.status_minutes * 60)) \
        if common.settings.status_minutes > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="Shopfront", lifespan=_lifespan)
for _routes in (desk, disputes, setup, ring, api, site):
    app.include_router(_routes.router)


def _not_owner(request: Request) -> Response:
    detail = ("Shopfront only answers the shop owner. To use it on this device, open Set up on the shop's "
              "computer and enter the pairing code it shows at /pair.")
    path = request.url.path
    if path.startswith(("/api/", "/frames/", "/dispute.zip")):
        return JSONResponse({"error": "OWNER_ONLY", "detail": detail}, status_code=403)
    nxt = path + (f"?{request.url.query}" if request.url.query else "")
    return common._page(request, "pair.html", {"next": nxt if request.method == "GET" else "/", "error": None}, 403)


@app.middleware("http")
async def owner_only(request: Request, call_next):
    """Everything but Ring's webhook and /healthz is the owner's. See app/owner.py."""
    path, headers, gate = request.url.path, request.headers, common.gate
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
    await run_in_threadpool(common._housekeeping)
    token = common._request_visits.set({})
    try:
        return await call_next(request)
    finally:
        common._request_visits.reset(token)


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
    common._ring_seen(exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(common._ring_error_body(exc), status_code=code, headers=headers)
    page = common._page(request, "ring_error.html", {"e": exc}, code)
    if headers:
        page.headers.update(headers)
    return page


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not common._wants_html(request) or exc.status_code < 400:
        return await http_exception_handler(request, exc)
    return common._page(request, "error.html",
                        {"err": pages.plain_error(exc.status_code, str(exc.detail), request.url.path)}, exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not common._wants_html(request):
        return await request_validation_exception_handler(request, exc)
    detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg', '')}" for e in exc.errors())
    return common._page(request, "error.html", {"err": pages.plain_error(422, detail, request.url.path)}, 422)


@app.exception_handler(Exception)
def server_error(request: Request, exc: Exception):
    if not common._wants_html(request):
        return JSONResponse({"error": "INTERNAL_ERROR", "detail": type(exc).__name__}, status_code=500)
    return common._page(request, "error.html", {"err": pages.plain_error(500, type(exc).__name__, request.url.path)},
                        500)


@app.exception_handler(LiveError)
def live_error(request: Request, exc: LiveError):
    code = {"UNKNOWN_SESSION": 404, "BAD_KEY": 403, "TOO_MANY_SESSIONS": 409}.get(exc.code, 400)
    return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=code)


# --- app.main stays the one namespace for the stores, as before the routes moved to app/routes/ ---
# Tests, scripts/eval_agent.py and app/public_demo.py read and swap the stores and helpers on app.main
# (main.book, monkeypatch.setattr(main, "source", ...)). Each name now lives in exactly one module of
# app/routes/, and the routes read it there when a request runs, so app.main reads a name from that module
# and sets it there too: a swap on app.main reaches every route. tests/test_routes.py holds the rule.

_HOLDERS = (common, desk, disputes, setup, ring, api, site)


def _holder(name: str) -> ModuleType | None:
    return next((m for m in _HOLDERS if name in vars(m)), None)


class _Main(ModuleType):
    def __getattr__(self, name: str):
        holder = _holder(name)
        if holder is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(holder, name)

    def __setattr__(self, name: str, value) -> None:
        holder = None if name in vars(self) else _holder(name)
        if holder:
            setattr(holder, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        holder = None if name in vars(self) else _holder(name)
        if holder:
            delattr(holder, name)
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _Main
