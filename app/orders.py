"""Import the shop's purchase orders and invoices as expected deliveries.

This is how Shopfront knows what should arrive and what each courier was for: any POS, inventory
system or accountant's export that can save a CSV works. Columns are matched by name, ignoring case,
spaces and punctuation, so `Order ID`, `order_id` and `PO number` all land in `order_ref`.

Shopfront's own format (header row required, only `day` and `supplier` are needed):

    day,supplier,window_start,window_end,order_ref,carrier,tracking,items,amount
    2026-10-21,Bay Mills,09:00,11:00,PO-1042,UPS,1Z999AA10123456784,"Flour 25kg x4",$212.40

Exports from other systems are read through the aliases below. The Amazon Business order-history
report is one of them; its column names here are taken from public examples, not checked against
a live account, so `docs/receiving-desk.md` labels that path as unverified.

Every row is checked the same way as one typed on /setup. A bad row is reported with its line
number and the rest of the file still imports.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from app.deliveries import Expected, make_expected

FIELDS = ["day", "supplier", "window_start", "window_end", "order_ref", "carrier", "tracking", "items", "amount"]

ALIASES = {
    "day": ["day", "date", "expecteddate", "expecteddelivery", "expecteddeliverydate", "deliverydate", "duedate",
            "deliverby", "estimateddelivery", "estimateddeliverydate"],
    "supplier": ["supplier", "vendor", "seller", "sellername", "suppliername", "vendorname", "from"],
    "window_start": ["windowstart", "from_time", "fromtime", "earliest", "deliverywindowstart"],
    "window_end": ["windowend", "totime", "latest", "deliverywindowend"],
    "order_ref": ["orderref", "orderid", "ordernumber", "ponumber", "po", "purchaseorder", "invoice", "invoicenumber",
                  "invoiceno", "reference"],
    "carrier": ["carrier", "carriername", "shippingcarrier", "courier"],
    "tracking": ["tracking", "trackingnumber", "trackingno", "trackingid"],
    "items": ["items", "item", "title", "description", "productname", "lineitems"],
    "amount": ["amount", "total", "ordertotal", "itemsubtotal", "invoicetotal", "value", "itemtotal"],
}
# Amazon Business puts carrier and tracking in one column: "UPS(1Z999AA10123456784)"
COMBINED_CARRIER = ["carriernametrackingnumber", "carrierandtracking"]

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d"]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _column_map(header: list[str]) -> tuple[dict[str, int], int | None]:
    keys = [_key(h) for h in header]
    found: dict[str, int] = {}
    for field, names in ALIASES.items():
        for n in names:
            if _key(n) in keys:
                found[field] = keys.index(_key(n))
                break
    combined = next((keys.index(k) for k in COMBINED_CARRIER if k in keys), None)
    return found, combined


def _day(value: str, day_first: bool) -> date:
    value = value.strip()
    if "T" in value:
        value = value.split("T")[0]
    formats = [f for f in DATE_FORMATS if not (f == "%m/%d/%Y" and day_first) and not (f == "%d/%m/%Y" and not day_first)]
    for f in formats:
        try:
            return datetime.strptime(value, f).date()
        except ValueError:
            pass
    raise ValueError(f"can't read the date {value!r}; use YYYY-MM-DD")


def _split_carrier(value: str) -> tuple[str, str]:
    m = re.match(r"\s*([^()]+?)\s*\(\s*([^()]*)\s*\)\s*$", value or "")
    return (m.group(1), m.group(2)) if m else ((value or "").strip(), "")


def parse_orders(text: str, day_first: bool = False) -> dict:
    """Parse a CSV export into expected deliveries. Returns {"rows", "errors", "format", "columns"}."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("the file is empty")
    cols, combined = _column_map(header)
    missing = [f for f in ("day", "supplier") if f not in cols]
    if missing:
        raise ValueError(f"no {' or '.join(missing)} column; the header row needs one (see the format on this page)")
    keys = {_key(h) for h in header}
    fmt = "amazon-business" if {"orderid", "sellername"} <= keys or combined is not None else (
        "shopfront" if all(_key(f) in keys for f in ("day", "supplier")) else "mapped")
    rows: list[Expected] = []
    errors: list[str] = []
    for line, rec in enumerate(reader, start=2):
        if not any(c.strip() for c in rec):
            continue
        get = lambda f: rec[cols[f]].strip() if f in cols and cols[f] < len(rec) else ""
        carrier, tracking = get("carrier"), get("tracking")
        if combined is not None and combined < len(rec) and not (carrier or tracking):
            carrier, tracking = _split_carrier(rec[combined])
        try:
            rows.append(make_expected(_day(get("day"), day_first), get("supplier"), get("window_start"),
                                      get("window_end"), order_ref=get("order_ref"), carrier=carrier,
                                      tracking=tracking, items=get("items"), amount=get("amount"),
                                      origin=f"csv:{fmt}"))
        except ValueError as exc:
            errors.append(f"line {line}: {exc}")
    # Amazon Business lists one row per item; fold items of the same order and day into one delivery
    merged: dict[tuple, Expected] = {}
    for r in rows:
        k = (r.order_ref, r.day) if r.order_ref else (r.id,)
        if k in merged:
            m = merged[k]
            m.items = "; ".join(x for x in (m.items, r.items) if x)[:200]
            if r.amount and r.amount not in m.amount:
                m.amount = ""  # several amounts for one order: don't pretend one of them is the total
        else:
            merged[k] = r
    return {"rows": list(merged.values()), "errors": errors, "format": fmt,
            "columns": {f: header[i] for f, i in cols.items()}}
