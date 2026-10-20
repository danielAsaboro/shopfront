"""The dispute pack: what the door saw, put together for the owner to send.

One pack answers one question: a carrier's scan ("business closed", "attempted") or an order that
never reached the door, or one the staff marked short or damaged. It holds the order it was for,
the carrier's claim and Shopfront's check of it, the door timeline around that time, the Ring
frame with its SHA-256, the posted hours, and what the evidence does *not* show.

The drafted message is a fixed template filled from those facts, so every sentence in it traces
to a field in the pack. Shopfront never sends it: the owner reads it, edits it and sends it from
their own email. When the door agrees with the carrier there is no draft, and the pack says why.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timedelta

from app.deliveries import STATUS_TEXT, Expected, FrameStore
from app.models import SEEN_SOURCES, ClassifiedVisit
from app import reconcile as rc

WINDOW = timedelta(minutes=30)  # timeline shown either side of the time in question

LIMITS = [
    "Ring has no \"door answered\" signal: a courier at the door is not proof a parcel was handed over.",
    "Motion detection can miss people, so \"nothing recorded\" supports an account but doesn't prove it.",
    "Labels \"by rules\" are guesses from the time of day and the kind of event, not from a frame.",
]
REPLAY_LIMIT = ("Some events came from Shopfront's webhook replay feeder (marked ring-webhook-replay), "
                "not from a Ring notification.")

# Outcomes where the door disagrees with the carrier, so a message is worth drafting
DISPUTE = {rc.OPEN_NO_COURIER, rc.OPEN_COURIER_CAME, rc.NOTHING_RECORDED, rc.SOMEONE_NOT_COURIER}


def _sha256(path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def _timeline(visits: list[ClassifiedVisit], centre: datetime | None, frames: FrameStore) -> list[dict]:
    rows = visits if centre is None else [v for v in visits if abs(v.event.timestamp - centre) <= WINDOW]
    return [{"time": v.event.timestamp.strftime("%H:%M:%S"), "ring_event": v.event.kind,
             "ring_subtype": v.event.ring_subtype, "event_id": v.event.id, "event_source": v.event.source,
             "type": v.visit_type.value, "labelled_by": v.source, "reason": v.reason,
             "bedrock_request_id": v.request_id, "frame_url": frames.url(v.event.id)} for v in rows]


def _order(e: Expected | None, receipt: dict | None) -> dict | None:
    if not e:
        return None
    return {"id": e.id, "supplier": e.supplier, "order_ref": e.order_ref, "carrier": e.carrier,
            "tracking": e.tracking, "items": e.items, "amount": e.amount, "day": e.day,
            "window": f"{e.window_start}–{e.window_end}" if e.window_start else "any time", "origin": e.origin,
            "received": e.received,
            "door_status": receipt["status"] if receipt else None,
            "door_status_text": STATUS_TEXT.get(receipt["status"]) if receipt else None,
            "courier_visit": receipt.get("visit") if receipt else None}


def build_pack(shop: str, day: date, visits: list[ClassifiedVisit], frames: FrameStore, posted_hours: str,
               check: dict | None = None, order: Expected | None = None, receipt: dict | None = None,
               carrier: str = "", tracking: str = "") -> dict:
    """`check` is a result of reconcile.check_attempt or check_closed_scan; `receipt` is the order's row
    from deliveries.receipts. Give a check, an order, or both."""
    if check is None and order is None:
        raise ValueError("a pack needs a carrier scan to check or an order")
    carrier = carrier or (order.carrier if order else "")
    tracking = tracking or (order.tracking if order else "")
    centre = datetime.strptime(check["attempt"], "%Y-%m-%d %H:%M").replace(
        tzinfo=visits[0].event.timestamp.tzinfo if visits else None) if check else None
    if centre is None and receipt and receipt.get("visit"):  # the courier who came for this order
        centre = next((v.event.timestamp for v in visits if v.event.id == receipt["visit"]["event_id"]), None)
    timeline = _timeline(visits, centre, frames) if centre else []
    cited = (check or {}).get("visit") or (receipt or {}).get("visit")
    frame = None
    if cited:
        path = frames.path(cited["event_id"])
        frame = {"url": frames.url(cited["event_id"]), "sha256": _sha256(path), "event_id": cited["event_id"],
                 "bedrock_request_id": cited.get("bedrock_request_id"), "labelled_by": cited.get("labelled_by"),
                 "time": cited["time"]} if path else None
    sources = sorted({v.event.source for v in visits})
    limits = LIMITS + ([REPLAY_LIMIT] if "ring-webhook-replay" in sources else [])
    pack = {
        "shop": shop, "day": day.isoformat(), "posted_hours": posted_hours,
        "made_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "claim": {"kind": check["claim"], "label": rc.CLAIMS[check["claim"]], "time": check["attempt"],
                  "carrier": carrier, "tracking": tracking} if check else None,
        "finding": {"outcome": check["outcome"], "text": check["text"], "activity": check.get("activity"),
                    "visit": check.get("visit")} if check else None,
        "tolerance_min": check["tolerance_min"] if check else None,
        "order": _order(order, receipt), "frame": frame, "timeline": timeline,
        "timeline_window_min": int(WINDOW.total_seconds() // 60) if centre else None,
        "door_events_that_day": len(visits),
        "seen_by_vision_that_day": sum(1 for v in visits if v.source in SEEN_SOURCES),
        "event_sources": sources, "limits": limits,
    }
    pack["draft"], pack["no_draft_reason"] = draft(pack)
    return pack


def _order_line(o: dict | None) -> str:
    if not o:
        return ""
    ref = f"order {o['order_ref']}" if o["order_ref"] else "our order"
    what = f" ({o['items']})" if o["items"] else ""
    return f"This concerns {ref} from {o['supplier']}{what}, due {o['day']}, {o['window']}."


def _carrier_facts(p: dict) -> str:
    """The finding in the shop's own voice, to the carrier. Same facts as the finding, nothing added."""
    c, f = p["claim"], p["finding"]
    a, v = f.get("activity") or {}, (f.get("visit") or {})
    tol = p.get("tolerance_min", 10)
    if f["outcome"] in (rc.OPEN_NO_COURIER, rc.OPEN_COURIER_CAME):
        busy = f"our doorbell camera recorded {a['events']} door events between {a['from']} and {a['to']}" + (
            f", {a['customers']} of them customers" if a.get("customers") else "")
        open_line = f"We were open: our posted hours that day were {p['posted_hours']}, and {busy}."
        if f["outcome"] == rc.OPEN_COURIER_CAME:
            return open_line + f" It also shows a courier at our door at {v['time']}, while we were open."
        return open_line + f" It recorded no courier at our door within {tol} minutes of your scan."
    if f["outcome"] == rc.NOTHING_RECORDED:
        return f"Our doorbell camera recorded nothing at our door within {tol} minutes of that time."
    if f["outcome"] == rc.SOMEONE_NOT_COURIER:
        return f"The visit our doorbell camera recorded nearest that time, at {v['time']}, was not a courier."
    return f["text"]


def draft(p: dict) -> tuple[dict | None, str | None]:
    """A message for the owner to edit and send, or (None, why there isn't one)."""
    c, f, o = p["claim"], p["finding"], p["order"]
    ref = (c or {}).get("tracking") or (o or {}).get("order_ref") or ""
    sign = f"\n\n{p['shop']}"
    evidence = "I've attached the doorbell frame and the door log for that time." if p["frame"] else \
        "I've attached the door log for that time."
    if c and f:
        if f["outcome"] not in DISPUTE:
            return None, {"courier_at_door": "The door agrees with the carrier: a courier was there near that time.",
                          "posted_closed": "Your own posted hours say the shop was closed then.",
                          "open_by_hours_only": "The door recorded nothing around the scan, so only your posted "
                                                "hours back you up. That's too thin to send as proof.",
                          "door_activity_unverified": "Something was at the door near that time, but no frame "
                                                      "confirmed who. Check the timeline before disputing.",
                          "no_door_data": "Shopfront has no door events for that day."}.get(f["outcome"], "")
        to = c["carrier"] or "the carrier"
        what = "marked our delivery \"business closed\"" if c["kind"] == rc.BUSINESS_CLOSED else \
            "recorded a delivery attempt"
        body = "\n\n".join(x for x in [
            f"Hello {to},",
            f"Your tracking{f' for {ref}' if ref else ''} {what} at {c['time'][-5:]} on {p['day']}. "
            f"Our front-door camera shows otherwise.",
            _carrier_facts(p),
            _order_line(o),
            evidence + " Please correct the scan and redeliver, or tell us what happened.",
        ] if x) + sign
        subject = f"{'Business closed' if c['kind'] == rc.BUSINESS_CLOSED else 'Missed delivery'} scan, " \
                  f"{p['day']} {c['time'][-5:]}" + (f", {ref}" if ref else "")
        return {"to": to, "subject": subject, "body": body}, None
    if o:
        rec = o.get("received")
        if rec and rec["status"] in ("short", "damaged"):
            v = o.get("courier_visit")
            seen = f"Our doorbell saw the courier at {v['time']}." if v else ""
            body = "\n\n".join(x for x in [
                f"Hello {o['supplier']},",
                _order_line(o) + " " + seen,
                f"When we unpacked it at {rec['at'][-5:]}, it was {rec['status']}" + (f": {rec['note']}." if rec["note"]
                                                                                     else "."),
                evidence + " Please send the missing items or a credit note.",
            ] if x) + sign
            return {"to": o["supplier"], "subject": f"{rec['status'].title()} delivery, {o['order_ref'] or o['day']}",
                    "body": body}, None
        if o["door_status"] == "not_seen":
            body = "\n\n".join([
                f"Hello {o['supplier']},",
                _order_line(o),
                f"Our front-door camera recorded {p['door_events_that_day']} door events that day and no courier "
                f"for this order. Motion detection can miss people, so please check your proof of delivery and "
                f"tell us where it went, or redeliver.",
            ]) + sign
            return {"to": o["supplier"], "subject": f"Delivery not received, {o['order_ref'] or o['day']}",
                    "body": body}, None
        return None, "The door saw a courier for this order and nobody has marked it short or damaged."
    return None, ""


def as_text(p: dict) -> str:
    lines = [f"Shopfront dispute pack · {p['shop']} · {p['day']}", f"Made {p['made_at']}", ""]
    if p["claim"]:
        c = p["claim"]
        lines += [f"Carrier claim: {c['label']} at {c['time']}" + (f" · {c['carrier']}" if c["carrier"] else "")
                  + (f" · tracking {c['tracking']}" if c["tracking"] else ""), f"Finding: {p['finding']['text']}", ""]
    if p["order"]:
        o = p["order"]
        lines += [f"Order: {o['order_ref'] or '(no ref)'} · {o['supplier']} · due {o['day']} {o['window']}"
                  + (f" · {o['items']}" if o["items"] else "") + (f" · {o['amount']}" if o["amount"] else ""),
                  f"At the door: {o['door_status_text'] or '—'}"
                  + (f" · staff marked {o['received']['status']} at {o['received']['at']}" if o["received"] else ""), ""]
    lines.append(f"Posted hours that day: {p['posted_hours']}")
    if p["frame"]:
        f = p["frame"]
        lines.append(f"Ring frame: {f['time']} · event {f['event_id']} · SHA-256 {f['sha256']}"
                     + (f" · Bedrock RequestId {f['bedrock_request_id']}" if f["bedrock_request_id"] else ""))
    if p["timeline"]:
        lines += ["", f"Door log, {p['timeline_window_min']} min either side:"]
        lines += [f"  {t['time']}  {t['ring_event']:<9} {t['type']:<12} by {t['labelled_by']:<8} {t['reason']}"
                  for t in p["timeline"]]
    lines += ["", "What this does not show:"] + [f"  - {x}" for x in p["limits"]]
    if p["draft"]:
        d = p["draft"]
        lines += ["", "Draft message (not sent; edit before sending)", f"To: {d['to']}", f"Subject: {d['subject']}",
                  "", d["body"]]
    elif p["no_draft_reason"]:
        lines += ["", f"No draft: {p['no_draft_reason']}"]
    return "\n".join(lines) + "\n"


def as_zip(p: dict, frames: FrameStore) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.txt", as_text(p))
        z.writestr("pack.json", json.dumps(p, indent=1))
        if p["frame"]:
            path = frames.path(p["frame"]["event_id"])
            if path:
                z.write(path, f"ring-frame-{p['frame']['time'].replace(':', '')}.jpg")
    return buf.getvalue()
