"""Set up: the shop's name, hours and redelivery cost, the orders (one at a time or a CSV import), and pairing
a phone with the shop's computer."""
from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import pack_signing
from app.orders import parse_orders
from app.owner import COOKIE, COOKIE_DAYS
from app.hours import normalize_clock
from app.ring.api import RingApiClient, RingError
from app.shop import DAYS, Profile, hours_for_form, hours_from_form
from app.routes import common

router = APIRouter()


@router.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request, next: str = Query("/")):
    if common.gate.is_owner(request.client.host if request.client else None, request.headers, request.cookies):
        return RedirectResponse(common._safe_next(next), status_code=303)
    return common._page(request, "pair.html", {"next": common._safe_next(next), "error": None})


@router.post("/pair")
def pair_device(request: Request, code: str = Form(""), next: str = Form("/")):
    value = common.gate.pair(code)
    if not value:
        return common._page(request, "pair.html", {
            "next": common._safe_next(next),
            "error": "That code didn't match or has expired. Use the code Set up shows on the shop's computer now."},
            403)
    r = RedirectResponse(common._safe_next(next), status_code=303)
    r.set_cookie(COOKIE, value, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="strict",
                 secure=request.url.scheme == "https")
    return r


@router.post("/pair/forget")
def pair_forget():
    common.gate.forget_devices()
    return RedirectResponse("/setup#pairing", status_code=303)


def _pairing(request: Request) -> dict | None:
    """The code a phone pairs with, shown only on the shop's own computer."""
    if not common.gate.is_local(request.client.host if request.client else None, request.headers):
        return None
    code, expires = common.gate.pairing_code()
    return {"code": code, "minutes": max(1, round((expires - datetime.now().timestamp()) / 60))}


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None, imported: str | None = None,
               notice: str | None = None, s: str = "shop"):
    return _setup(request, s, error=error, notice=imported or notice)


ORDERS_SHOWN = 60


def _setup(request: Request, section: str, error: str | None = None, notice: str | None = None,
           form: dict | None = None, status_code: int = 200) -> HTMLResponse:
    """/setup with any message shown inside the section it is about, next to where the owner lands."""
    p = common.profile.get()
    try:
        ring, ring_error = common.source.get(), None
        if isinstance(ring, RingApiClient):  # the client loads lazily: ask Ring now, or "connected" is a guess
            ring.list_devices()
            common._ring_seen(None)
    except RingError as exc:
        ring, ring_error = None, exc.status.value
        if common._ring_key()[1]:  # a built client that Ring refused, not a missing configuration
            common._ring_seen(exc)
    today = datetime.now(common.settings.tz).date()
    orders = sorted(common.book._all(), key=lambda e: (e.window_start or "99", e.supplier.lower()))
    orders.sort(key=lambda e: e.day, reverse=True)  # newest day first, each day in time order
    hours = hours_for_form(p.hours)
    if form:  # a refused save: show what the owner typed, not what was saved before
        p = Profile(name=form.get("name", p.name), hours=p.hours, redelivery_cost=p.redelivery_cost,
                    currency=form.get("currency", p.currency))
        hours = [form.get(f"h{i}", "") or "closed" for i in range(7)]
    return common._page(request, "setup.html", {
        "p": p, "days": list(zip(DAYS, hours)), "error": error, "notice": notice, "section": section,
        "cost_typed": (form or {}).get("redelivery_cost"),
        "ring": ring.name if ring else None, "ring_error": ring_error, "today": today.isoformat(),
        "ring_linked": common.source.linked() if not common.source.pasted_token() else None, "pack_key": _pack_key_summary(),
        "orders": orders[:ORDERS_SHOWN], "orders_more": max(0, len(orders) - ORDERS_SHOWN),
        "order_days": len({e.day for e in orders}), "pairing": _pairing(request)}, status_code)


@router.post("/setup")
async def setup_save(request: Request):
    form = await request.form()
    try:
        cost = str(form.get("redelivery_cost", "")).strip()
        p = Profile(name=str(form.get("name", "")).strip()[:60] or common.settings.shop_name,
                    hours=hours_from_form([str(form.get(f"h{i}", "")) for i in range(7)]),
                    redelivery_cost=float(cost) if cost else None,
                    currency=(str(form.get("currency", "$")).strip() or "$")[:3])
        if p.redelivery_cost is not None and not 0 <= p.redelivery_cost < 100_000:
            raise ValueError("redelivery cost looks wrong")
        common.profile.save(p)
    except ValueError as exc:
        return _setup(request, "shop", error=str(exc), status_code=400,
                      form={k: str(v) for k, v in form.items()})
    return RedirectResponse(f"/setup?s=shop&notice={quote('Saved. Shopfront uses these hours from now on.')}#shop",
                            status_code=303)


@router.post("/deliveries")
async def delivery_add(request: Request):
    form = await request.form()
    back = str(form.get("back", ""))
    back = back if back.startswith("/") and not back.startswith("//") else ""
    try:
        e = common.book.add(date.fromisoformat(str(form.get("day", ""))), str(form.get("supplier", "")),
                     normalize_clock(str(form.get("window_start", ""))), normalize_clock(str(form.get("window_end", ""))),
                     str(form.get("note", "")), order_ref=str(form.get("order_ref", "")), carrier=str(form.get("carrier", "")))
    except ValueError as exc:
        return RedirectResponse(f"/setup?s=orders&error={quote(str(exc))}#orders", status_code=303)
    return RedirectResponse(back or f"/setup?s=orders&notice={quote(f'Added {e.supplier} for {e.day}.')}#orders",
                            status_code=303)


@router.post("/deliveries/{expected_id}/remove")
def delivery_remove(request: Request, expected_id: str):
    e = common.book.get(expected_id)
    common.book.remove(expected_id)
    msg = f"Removed {e.supplier} ({e.day})." if e else "That order was already gone."
    return RedirectResponse(f"/setup?s=orders&notice={quote(msg)}#orders", status_code=303)


def _pack_key_summary() -> dict | None:
    try:
        jwk = common.pack_signer.jwk()
    except Exception:
        return None
    return {"kid": jwk["kid"], "alg": jwk["alg"], "signer": common.pack_signer.name, "url": pack_signing.KEY_PATH}


@router.post("/orders/import")
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
    counts = common.book.import_rows(parsed["rows"])
    day_col = parsed["columns"].get("day", "")
    msg = (f"Imported {counts['added']} new and updated {counts['updated']} orders ({parsed['format']} format)"
           + (f", dated by the '{day_col}' column because the file has no delivery date"
              if day_col.lower().replace(" ", "") == "orderdate" else "")
           + (f". Skipped {len(parsed['errors'])}: " + "; ".join(parsed["errors"][:5]) if parsed["errors"] else "."))
    return RedirectResponse(f"/setup?s=orders&imported={quote(msg)}#orders", status_code=303)
