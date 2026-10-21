"""The dispute pack: what the door saw, put together for the owner to send.

One pack answers one question: a carrier's scan ("business closed", "attempted") or an order that
never reached the door, or one the staff marked short or damaged. It holds the order it was for,
the carrier's claim and Shopfront's check of it, the door timeline around that time, the frame with
its SHA-256 and where it came from, the posted hours, and what the evidence does *not* show.

The drafted message is a fixed template filled from those facts, so every sentence in it traces
to a field in the pack, and only camera-confirmed facts (Ring's package_delivery alert, Bedrock on the
frame, or Ring recording an event at all) are stated as facts. A label the time-of-day rules guessed
is never written as a fact: when one sits where the dispute needs certainty, there is no draft.

It goes to whoever the shop can actually ask: the supplier when the pack is about one of the shop's
orders (the shipper is the carrier's customer, not the shop), the carrier, by the name the owner
typed, only when there is no order. When any event that day is Shopfront's demo data or a replayed
webhook, the pack, the draft and the zip README say so in their first line.

Shopfront never sends it: the owner reads it, edits it and sends it from their own email. When the
door agrees with the carrier there is no draft, and the pack says why.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta, tzinfo

from app import provenance, wording
from app import reconcile as rc
from app.deliveries import STATUS_TEXT, Expected, FrameStore
from app.live import frame_provenance
from app.models import SEEN_SOURCES, ClassifiedVisit

WINDOW = timedelta(minutes=30)  # timeline shown either side of the time in question

LIMITS = [
    "Ring has no \"door answered\" signal: a courier at the door is not proof a parcel was handed over.",
    "Motion detection can miss people, so \"nothing recorded\" supports an account but doesn't prove it.",
    ("Labels \"by rules\" are guesses from the time of day and the kind of event, not from a frame. The "
     "draft never states them as facts."),
]
REPLAY_LIMIT = ("Some events came from Shopfront's webhook replay feeder (marked ring-webhook-replay), "
                "not from a Ring notification.")
DEMO_LIMIT = ("Some events are synthetic: Shopfront's demo generator made them (marked synthetic). No camera "
              "recorded them.")
NO_STATUS_LIMIT = ("Shopfront has no Ring device_online/device_offline notices for this doorbell around the scan, "
                   "so it can show the camera was recording when it recorded events, not that it never went offline "
                   "between them.")
DEMO = "DEMO DATA, not from a camera"
REPLAYED = "REPLAYED RING EVENTS, not live Ring notifications"
SUBJECT_TAG = "[DEMO] "  # one short tag on the draft's subject; the body's first line says what it means


def draft_warning(demo: bool, replayed: bool) -> str:
    """The one line at the top of a draft built on demo or replayed data. The pack page's watermark and the
    zip README carry the full explanation; the draft only has to stop it being sent."""
    if demo and replayed:
        return f"{DEMO}, with replayed Ring events: don't send this."
    if demo:
        return f"{DEMO}: don't send this."
    return f"{REPLAYED}: don't send this as evidence."


# Where a frame came from, by the source of the event it belongs to
PROVENANCE = {
    "ring": "downloaded from Ring (image download for this event)",
    "ring-webhook": "downloaded from Ring (image download for this event)",
    "ring-webhook-replay": "downloaded from Ring for a replayed webhook event",
}  # live-view frames: app.live.frame_provenance(), never "downloaded from Ring"

# The finding when the order's own courier came after the carrier's scan
DELIVERED_AFTER_SCAN = "delivered_after_scan"

# Outcomes where the door disagrees with the carrier, so a pack (and maybe a draft) is worth making
DISPUTE = {rc.OPEN_NO_COURIER, rc.OPEN_COURIER_CAME, rc.NOTHING_RECORDED, rc.SOMEONE_NOT_COURIER, rc.COURIER_LATER}

NO_DRAFT = {
    rc.COURIER_AT_DOOR: "The door agrees with the carrier: a courier was there near that time.",
    rc.POSTED_CLOSED: "Your own posted hours say the shop was closed then.",
    rc.OPEN_BY_HOURS_ONLY: "The door recorded nothing around the scan, so only your posted hours back you up. "
                           "That's too thin to send as proof.",
    rc.UNVERIFIED: "Something was at the door near that time, but no camera frame confirmed who it was, so "
                   "Shopfront can't say a courier didn't come. Check the timeline before disputing.",
    rc.NO_DOOR_DATA: "Shopfront has no door events for that day.",
    rc.CAMERA_OFFLINE: "Ring reported the doorbell offline around that time, so the door can't show whether a "
                       "courier came. Ask the carrier for its proof of the attempt instead.",
    rc.NO_COVERAGE: "The door recorded nothing around that time and nothing shows the camera was watching, so "
                    "there's nothing to send as proof.",
    rc.COURIER_LATER: "A courier was at the door later that day, but Shopfront can't tell whose delivery it was. "
                      "Enter the tracking number of the order it was for, and the pack can draft a late-delivery "
                      "note to that supplier.",
}


def _sha256(path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def _hhmm(iso: str, tz: tzinfo | None = None) -> str:
    """"2026-10-07T10:45-07:00" or "2026-10-07 10:45" -> "10:45" on the shop's clock. An aware time in
    another offset (say UTC) is converted to `tz` first; a naive one is already shop-local."""
    at = datetime.fromisoformat(iso)
    return (at.astimezone(tz) if tz and at.tzinfo else at).strftime("%H:%M")


def _timeline(visits: list[ClassifiedVisit], centre: datetime | None, frames: FrameStore) -> list[dict]:
    rows = visits if centre is None else [v for v in visits if abs(rc.gap(v.event.timestamp, centre)) <= WINDOW]
    return [{"time": v.event.timestamp.strftime("%H:%M:%S"), "ring_event": v.event.kind,
             "ring_subtype": v.event.ring_subtype, "event_id": v.event.id, "event_source": v.event.source,
             "type": v.visit_type.value, "labelled_by": v.source, "confirmed": rc.confirmed(v), "reason": v.reason,
             "bedrock_request_id": v.request_id, "frame_url": frames.url(v.event.id)} for v in rows]


def _order(e: Expected | None, receipt: dict | None, tz: tzinfo | None = None) -> dict | None:
    if not e:
        return None
    rec = dict(e.received) if e.received else None
    if rec and rec.get("at"):
        rec["time"] = _hhmm(rec["at"], tz)
    return {"id": e.id, "supplier": e.supplier, "order_ref": e.order_ref, "carrier": e.carrier,
            "tracking": e.tracking, "items": e.items, "amount": e.amount, "day": e.day,
            "window": f"{e.window_start}–{e.window_end}" if e.window_start else "any time",
            "window_start": e.window_start, "window_end": e.window_end, "origin": e.origin,
            "received": rec,
            "door_status": receipt["status"] if receipt else None,
            "door_status_text": STATUS_TEXT.get(receipt["status"]) if receipt else None,
            "also_fits": receipt.get("also_fits", []) if receipt else [],
            "possible": receipt.get("possible", []) if receipt else [],
            "unidentified": possible_text(receipt.get("possible", [])) if receipt else "",
            "courier_visit": receipt.get("visit") if receipt else None}


def possible_text(possible: list[dict]) -> str:
    """"a doorbell press at 14:36" / "doorbell presses at 10:09 and 10:53": the door events in an order's
    window that only the time-of-day rules called a courier. What Ring recorded, never who it was."""
    if not possible:
        return ""
    presses = all(x.get("ring_event") == "ding" for x in possible)
    times = [x["time"] for x in possible]
    when = times[0] if len(times) == 1 else ", ".join(times[:-1]) + " and " + times[-1]
    if len(times) == 1:
        return f"{'a doorbell press' if presses else 'a door event'} at {when}"
    return f"{'doorbell presses' if presses else 'door events'} at {when}"


def _in_order_window(v: ClassifiedVisit, o: dict) -> bool:
    if v.event.timestamp.date().isoformat() != o["day"]:
        return False
    if not o["window_start"]:
        return True
    return o["window_start"] <= v.event.timestamp.strftime("%H:%M") <= o["window_end"]


def _late(check: dict, order: dict | None, visits: list[ClassifiedVisit]) -> dict | None:
    """The order's own courier (matched by the receipts), seen by Ring or Bedrock after the carrier's time."""
    v = (order or {}).get("courier_visit")
    if not check or not order or not v or check["outcome"] != rc.COURIER_LATER:
        return None
    when = next((x.event.timestamp for x in visits if x.event.id == v["event_id"]), None)
    scan = datetime.fromisoformat(check["attempt_at"])
    if when is None or v.get("labelled_by") not in SEEN_SOURCES or rc.gap(when, scan) <= rc.TOLERANCE:
        return None
    mins = round(rc.gap(when, scan).total_seconds() / 60)
    return {**v, "minutes_after": mins}


def _finding(check: dict, late: dict | None, order: dict | None) -> dict:
    f = {"outcome": check["outcome"], "text": check["text"], "activity": check.get("activity"),
         "visit": check.get("visit")}
    if not late or not order:
        return f
    what = "scanned \"business closed\"" if check["claim"] == rc.BUSINESS_CLOSED else "recorded a delivery attempt"
    hours = f" Your posted hours had you open ({check['posted_hours']}), so the scan doesn't match them." \
        if check["claim"] == rc.BUSINESS_CLOSED else ""
    whose = f" It could also have been {' or '.join(order['also_fits'])}'s courier: " \
            f"{provenance.not_whose(late)}." if order["also_fits"] else ""
    text = (f"The carrier {what} at {check['attempt'][-5:]}, then the courier was at the door at {late['time']}, "
            f"{late['minutes_after']} min later ({provenance.said_by(late)}). Shopfront "
            f"matched that courier to {order['order_ref'] or order['supplier']}.{hours}{whose}")
    return {**f, "outcome": DELIVERED_AFTER_SCAN, "text": text, "visit": late, "door_outcome": check["outcome"]}


def build_pack(shop: str, day: date, visits: list[ClassifiedVisit], frames: FrameStore, posted_hours: str,
               check: dict | None = None, order: Expected | None = None, receipt: dict | None = None,
               carrier: str = "", tracking: str = "", tz: tzinfo | None = None,
               made_at: datetime | None = None) -> dict:
    """`check` is a result of reconcile.check_attempt or check_closed_scan; `receipt` is the order's row
    from deliveries.receipts; `tz` is the shop's zone; `made_at` pins the "made" time so the zip downloaded
    from a pack page has the manifest digest that page showed. Give a check, an order, or both. Raises ValueError
    when the check was refused (a time that hasn't happened, or never existed)."""
    if check is None and order is None:
        raise ValueError("a pack needs a carrier scan to check or an order")
    if check and check["outcome"] in rc.REFUSED:
        raise ValueError(check["text"])
    tz = tz or (visits[0].event.timestamp.tzinfo if visits else None) or UTC
    carrier = carrier or (order.carrier if order else "")
    tracking = tracking or (order.tracking if order else "")
    o = _order(order, receipt, tz)
    centre = datetime.fromisoformat(check["attempt_at"]) if check else None
    if centre is None and receipt and receipt.get("visit"):  # the courier who came for this order
        centre = next((v.event.timestamp for v in visits if v.event.id == receipt["visit"]["event_id"]), None)
    timeline = _timeline(visits, centre, frames) if centre else []
    late = _late(check, o, visits) if check else None
    finding = _finding(check, late, o) if check else None
    cited = (finding or {}).get("visit") or (receipt or {}).get("visit")
    frame = None
    if cited:
        path = frames.path(cited["event_id"])
        src = cited.get("event_source", "")
        live = frame_provenance(cited["event_id"]) or (
            frame_provenance("live-") if src.startswith("ring-live") else None)  # bytes from the owner's browser
        frame = {"url": frames.url(cited["event_id"]), "sha256": _sha256(path), "event_id": cited["event_id"],
                 "bedrock_request_id": cited.get("bedrock_request_id"), "labelled_by": cited.get("labelled_by"),
                 "time": cited["time"], "event_source": src,
                 "provenance": live or PROVENANCE.get(src, f"from a {src} event"),
                 "label": "Live-view frame" if live else "Ring frame", "live_view": bool(live)} if path else None
    sources = sorted({v.event.source for v in visits})
    cited_sources = sorted({t["event_source"] for t in timeline} | ({cited["event_source"]} if cited and
                                                                     cited.get("event_source") else set()))
    demo = "synthetic" in sources or "synthetic" in cited_sources
    replayed = any("replay" in s for s in sources + cited_sources)
    marks = [m for m, on in ((DEMO, demo), (REPLAYED, replayed)) if on]
    cov = (check or {}).get("coverage")
    limits = LIMITS + ([DEMO_LIMIT] if demo else []) + ([REPLAY_LIMIT] if replayed else []) + (
        [NO_STATUS_LIMIT] if cov and not cov.get("status_notices") else [])
    pack = {
        "shop": shop, "day": day.isoformat(), "posted_hours": posted_hours,
        "made_at": (made_at or datetime.now(tz)).astimezone(tz).isoformat(timespec="seconds"),
        "watermark": " · ".join(marks) or None, "demo_data": demo, "replayed": replayed,
        "claim": {"kind": check["claim"], "label": rc.CLAIMS[check["claim"]], "time": check["attempt"],
                  "at": check["attempt_at"], "carrier": carrier, "tracking": tracking} if check else None,
        "finding": finding, "coverage": cov,
        "tolerance_min": check["tolerance_min"] if check else None,
        "order": o, "frame": frame, "timeline": timeline,
        "timeline_window_min": int(WINDOW.total_seconds() // 60) if centre else None,
        "door_events_that_day": len(visits),
        "seen_by_vision_that_day": sum(1 for v in visits if v.source in SEEN_SOURCES),
        "event_sources": sources, "cited_sources": cited_sources, "limits": limits,
    }
    d, pack["no_draft_reason"] = draft(pack, visits)
    if d and marks:
        d["subject"] = SUBJECT_TAG + d["subject"]
        d["body"] = draft_warning(demo, replayed) + "\n\n" + d["body"]
    pack["draft"] = d
    return pack


def _order_line(o: dict | None) -> str:
    if not o:
        return ""
    ref = f"order {o['order_ref']}" if o["order_ref"] else "our order"
    what = f" ({o['items']})" if o["items"] else ""
    return f"This concerns {ref} from {o['supplier']}{what}, due {o['day']}, {o['window']}."


def _supported(p: dict) -> bool:
    """Whether a camera-confirmed event backs the shop's side: "Our front-door camera shows otherwise"."""
    f = p["finding"]
    if f["outcome"] in (rc.OPEN_COURIER_CAME, rc.SOMEONE_NOT_COURIER):
        return True
    return f["outcome"] == rc.OPEN_NO_COURIER and bool((f.get("activity") or {}).get("seen"))


def _facts(p: dict) -> str:
    """The finding in the shop's own voice. Same facts as the finding, nothing a rule guessed."""
    f = p["finding"]
    a, v = f.get("activity") or {}, (f.get("visit") or {})
    tol = p.get("tolerance_min", 10)
    if f["outcome"] in (rc.OPEN_NO_COURIER, rc.OPEN_COURIER_CAME):
        kinds = _kinds_text(a)
        conf = (f", {a['customers']} of them confirmed from the camera frame as "
                f"{'a customer' if a['customers'] == 1 else 'customers'}") if a.get("customers") else ""
        line = f"Our posted hours that day were {p['posted_hours']}, and our doorbell camera recorded {kinds} " \
               f"between {a['from']} and {a['to']}{conf}."
        if f["outcome"] == rc.OPEN_COURIER_CAME:
            return line + f" It also shows a courier at our door at {v['time']} ({provenance.said_by(v)}), " \
                          f"during those hours."
        if not a.get("near"):
            return line + f" It recorded nothing at our door within {tol} minutes of the scan."
        return line + (f" {'The visit' if a['near'] == 1 else 'Every visit'} it recorded within {tol} minutes of "
                       f"the scan was not a courier, judged from the camera frame.")
    if f["outcome"] == rc.NOTHING_RECORDED:
        return f"Our doorbell camera recorded nothing at our door within {tol} minutes of that time."
    if f["outcome"] == rc.SOMEONE_NOT_COURIER:
        return f"The visit our doorbell camera recorded nearest that time, at {v['time']}, was not a courier, " \
               f"judged from the camera frame."
    return f["text"]


def _kinds_text(a: dict) -> str:
    """"5 motion events and 1 doorbell press", from the activity counts."""
    parts = [f"{a['motion']} motion event{'' if a['motion'] == 1 else 's'}" if a.get("motion") else "",
             f"{a['presses']} doorbell press{'' if a['presses'] == 1 else 'es'}" if a.get("presses") else ""]
    other = a.get("events", 0) - a.get("motion", 0) - a.get("presses", 0)
    parts.append(f"{other} live-view visit{'' if other == 1 else 's'}" if other > 0 else "")
    parts = [x for x in parts if x]
    return " and ".join(parts) if parts else f"{a.get('events', 0)} door events"


def _evidence(p: dict) -> str:
    if p["frame"] and p["frame"].get("live_view"):
        return (f"I've attached a frame {p['frame']['provenance']}, and the door log for that time.")
    return "I've attached the doorbell frame and the door log for that time." if p["frame"] else \
        "I've attached the door log for that time."


def _late_note(p: dict, sign: str) -> dict:
    c, f, o = p["claim"], p["finding"], p["order"]
    v = f["visit"]
    who = c["carrier"] or "Your carrier"
    ask = c["carrier"] or "your carrier"
    track = f" (tracking {c['tracking']})" if c["tracking"] else ""
    if c["kind"] == rc.BUSINESS_CLOSED:
        scan = f"{who} scanned it \"business closed\" at {c['time'][-5:]} on {p['day']}{track}, during our posted " \
               f"hours ({p['posted_hours']})."
        so = (f"So the order reached us late, after a \"business closed\" scan that doesn't match our hours. Please "
              f"ask {ask} to correct that scan on the shipment's record, so it isn't counted against us, and note "
              f"the late delivery.")
        subject = f"Late delivery after a \"business closed\" scan, {o['order_ref'] or o['day']}"
    else:
        scan = f"{who} recorded a delivery attempt at {c['time'][-5:]} on {p['day']}{track}."
        so = (f"So the order reached us {v['minutes_after']} minutes after the recorded attempt. Please note the "
              f"late delivery, and ask {ask} to review the attempt scan if it affects anything on your side.")
        subject = f"Late delivery after an attempted-delivery scan, {o['order_ref'] or o['day']}"
    seen = (f"Our doorbell camera then recorded the courier at our door at {v['time']} "
            f"({provenance.said_by(v)}), {v['minutes_after']} minutes after that scan.")
    if o["also_fits"]:
        seen += f" ({provenance.not_whose(v)[:1].upper()}{provenance.not_whose(v)[1:]}: it could also have been " \
                f"for {' or '.join(o['also_fits'])}.)"
    body = "\n\n".join([f"Hello {o['supplier']},", _order_line(o), scan + " " + seen, so + " " + _evidence(p)]) + sign
    return {"to": o["supplier"], "subject": subject, "body": body}


def _scan_dispute(p: dict, sign: str) -> dict:
    c, o = p["claim"], p["order"]
    ref = c["tracking"] or (o or {}).get("order_ref") or ""
    otherwise = " Our front-door camera shows otherwise." if _supported(p) else ""
    if o:  # the shop's order: the supplier shipped it, so the supplier is who can take it up with the carrier
        who, ask = c["carrier"] or "Your carrier", c["carrier"] or "your carrier"
        what = "scanned it \"business closed\"" if c["kind"] == rc.BUSINESS_CLOSED else "recorded a delivery attempt"
        track = f" (tracking {c['tracking']})" if c["tracking"] else ""
        parts = [f"Hello {o['supplier']},", _order_line(o),
                 f"{who} {what} at {c['time'][-5:]} on {p['day']}{track}.{otherwise}", _facts(p) + _also_recorded(o),
                 f"{_evidence(p)} Please ask {ask} to correct the scan and arrange redelivery, or tell us what happened."]
        to = o["supplier"]
    else:  # no order: the carrier's own scan, to the carrier by the name the owner typed
        what = "marked our delivery \"business closed\"" if c["kind"] == rc.BUSINESS_CLOSED else \
            "recorded a delivery attempt"
        parts = [f"Hello {c['carrier']}," if c["carrier"] else "Hello,",
                 f"Your tracking{f' for {ref}' if ref else ''} {what} at {c['time'][-5:]} on {p['day']}.{otherwise}",
                 _facts(p), f"{_evidence(p)} Please correct the scan and redeliver, or tell us what happened."]
        to = c["carrier"]
    subject = f"{'Business closed' if c['kind'] == rc.BUSINESS_CLOSED else 'Missed delivery'} scan, " \
              f"{p['day']} {c['time'][-5:]}" + (f", {ref}" if ref else "")
    return {"to": to, "subject": subject, "body": "\n\n".join(x for x in parts if x) + sign}


def _note_for_draft(note: str) -> str:
    """The staff note, quoted in the draft unless it accuses someone (app/wording.py): the draft states what
    arrived, and the note stays in the pack for the owner to word themselves."""
    if not note:
        return "."
    if wording.accusing(note):
        return ". (Our staff note is in the attached pack.)"
    return f": {note}."


def _also_recorded(o: dict) -> str:
    """The order's own unidentified door events, said plainly so the draft doesn't hide what the pack shows."""
    if not o.get("unidentified"):
        return ""
    span = "during the delivery window" if o.get("window_start") else "that day"
    return f" Our doorbell also recorded {o['unidentified']} {span}, which no camera frame identified."


def _unlooked_in_window(o: dict, visits: list[ClassifiedVisit]) -> list[ClassifiedVisit]:
    return [v for v in visits if _in_order_window(v, o) and not rc.confirmed(v)]


def draft(p: dict, visits: list[ClassifiedVisit] | None = None) -> tuple[dict | None, str | None]:
    """A message for the owner to edit and send, or (None, why there isn't one)."""
    c, f, o = p["claim"], p["finding"], p["order"]
    sign = f"\n\n{p['shop']}"
    if c and f:
        if f["outcome"] == DELIVERED_AFTER_SCAN:
            return _late_note(p, sign), None
        if f["outcome"] not in DISPUTE or f["outcome"] == rc.COURIER_LATER:
            return None, NO_DRAFT.get(f["outcome"], "")
        return _scan_dispute(p, sign), None
    if o:
        rec = o.get("received")
        if rec and rec["status"] in ("short", "damaged"):
            v = o.get("courier_visit")
            seen = f"Our doorbell saw the courier at {v['time']}." if v else ""
            body = "\n\n".join(x for x in [
                f"Hello {o['supplier']},",
                (_order_line(o) + " " + seen).strip(),
                f"When we unpacked it at {rec['time']}, it was {rec['status']}" + _note_for_draft(rec["note"]),
                _evidence(p) + " Please send the missing items or a credit note.",
            ] if x) + sign
            return {"to": o["supplier"], "subject": f"{rec['status'].title()} delivery, {o['order_ref'] or o['day']}",
                    "body": body}, None
        if o["door_status"] == "possible":
            times = ", ".join(x["time"] for x in o["possible"])
            return None, (f"A doorbell press at {times} might have been the courier, and no camera frame "
                          f"confirmed who it was. Check the timeline before asking the supplier.")
        if o["door_status"] == "expected":
            return None, "This delivery isn't due yet."
        if o["door_status"] == "not_seen":
            span = f"between {o['window_start']} and {o['window_end']}" if o["window_start"] else "that day"
            guessed = _unlooked_in_window(o, visits or [])
            if guessed:
                times = ", ".join(v.event.timestamp.strftime("%H:%M") for v in guessed[:5])
                return None, (f"The doorbell recorded door events {span} that no camera frame looked at ({times}); "
                              f"any of them could have been the courier. Check the timeline before asking the "
                              f"supplier.")
            looked = [v for v in (visits or []) if _in_order_window(v, o)]
            seen_line = (f"Our doorbell camera recorded nothing at our door {span}, the delivery window." if not looked
                         else f"Every one of the {len(looked)} visits our doorbell camera recorded {span} was "
                              f"judged from the camera frame as not a courier.")
            body = "\n\n".join([
                f"Hello {o['supplier']},",
                _order_line(o),
                seen_line + " Motion detection can miss people, so please check your proof of delivery and tell us "
                            "where it went, or redeliver.",
            ]) + sign
            return {"to": o["supplier"], "subject": f"Delivery not received, {o['order_ref'] or o['day']}",
                    "body": body}, None
        return None, "The door saw a courier for this order and nobody has marked it short or damaged."
    return None, ""


def as_text(p: dict) -> str:
    lines = ([p["watermark"], ""] if p.get("watermark") else []) + [
        f"Shopfront dispute pack · {p['shop']} · {p['day']}", f"Made {p['made_at']}", ""]
    if p["claim"]:
        c = p["claim"]
        lines += [f"Carrier claim: {c['label']} at {c['time']}" + (f" · {c['carrier']}" if c["carrier"] else "")
                  + (f" · tracking {c['tracking']}" if c["tracking"] else ""), f"Finding: {p['finding']['text']}", ""]
    if p["order"]:
        o = p["order"]
        lines += [f"Order: {o['order_ref'] or '(no ref)'} · {o['supplier']} · due {o['day']} {o['window']}"
                  + (f" · {o['items']}" if o["items"] else "") + (f" · {o['amount']}" if o["amount"] else ""),
                  f"At the door: {o['door_status_text'] or '—'}"
                  + (f" ({o['unidentified']}, not identified)" if o.get("unidentified") else "")
                  + (f" · staff marked {o['received']['status']} at {o['received']['time']}" if o["received"] else ""),
                  ""]
    lines.append(f"Posted hours that day: {p['posted_hours']}")
    if p.get("coverage"):
        lines.append(f"Camera around the scan: {p['coverage']['text']}")
    if p["frame"]:
        f = p["frame"]
        lines.append(f"{f['label']}: {f['time']} · event {f['event_id']} · {f['provenance']} · SHA-256 {f['sha256']}"
                     + (f" · Bedrock RequestId {f['bedrock_request_id']}" if f["bedrock_request_id"] else ""))
    if p["timeline"]:
        lines += ["", f"Door log, {p['timeline_window_min']} min either side:"]
        lines += [f"  {t['time']}  {t['ring_event']:<9} {t['type']:<12} by {t['labelled_by']:<8} "
                  f"{t['event_source']:<20} {t['reason']}" for t in p["timeline"]]
    lines += ["", "What this does not show:"] + [f"  - {x}" for x in p["limits"]]
    if p["draft"]:
        d = p["draft"]
        lines += ["", "Draft message (not sent; edit before sending)", f"To: {d['to']}", f"Subject: {d['subject']}",
                  "", d["body"]]
    elif p["no_draft_reason"]:
        lines += ["", f"No draft: {p['no_draft_reason']}"]
    return "\n".join(lines) + "\n"


def _readme(p: dict, frame_name: str | None) -> str:
    lines = ([p["watermark"], ""] if p.get("watermark") else []) + [
        f"Shopfront dispute pack for {p['shop']}, {p['day']}. Made {p['made_at']}.", "",
        "pack.txt   the pack to read: the claim, the finding, the door log, the limits and the draft",
        "pack.json  the same facts as data, every event with its source and who labelled it",
        f"{MANIFEST}  the SHA-256 of every other file; check them with: sha256sum -c {MANIFEST}",
        f"           (the SHA-256 of {MANIFEST} itself is shown under Details on the pack page)",
    ]
    if frame_name and p["frame"]:
        lines.append(f"{frame_name}  the frame, {p['frame']['provenance']} (SHA-256 in pack.txt)")
    lines += ["", f"Door events came from: {', '.join(p['event_sources']) or 'none'}."]
    if p["demo_data"]:
        lines.append("synthetic = Shopfront's demo generator. No camera recorded these events.")
    if p["replayed"]:
        lines.append("ring-webhook-replay = Shopfront's webhook replay feeder, not a live Ring notification.")
    if p["demo_data"] or p["replayed"]:
        lines += ["", "Why this pack says DEMO: " + " ".join(
            ([DEMO_LIMIT] if p["demo_data"] else []) + ([REPLAY_LIMIT] if p["replayed"] else [])),
            "The pack page shows this once, in its watermark; the draft's subject carries [DEMO] and its first "
            "line says not to send it. A pack built from your own Ring doorbell has none of these marks."]
    if p["demo_data"] or p["replayed"]:
        lines += ["", "The pack page shows drawn illustrations for demo door events. They are not camera frames, "
                  "not evidence, and not in this zip."]
    lines += ["", "Shopfront doesn't send anything. Read the draft, edit it, and send it yourself."]
    return "\n".join(lines) + "\n"


MANIFEST = "manifest.sha256"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed entry times: the same pack always zips to the same bytes


def zip_files(p: dict, frames: FrameStore) -> list[tuple[str, bytes]]:
    """Every file in the pack's zip except the manifest, in order, as (name, bytes)."""
    name = None
    if p["frame"]:
        prefix = "live-view-frame" if p["frame"].get("live_view") else "ring-frame"
        name = f"{prefix}-{p['frame']['time'].replace(':', '')}.jpg"
    files = [("README.txt", _readme(p, name).encode()), ("pack.txt", as_text(p).encode()),
             ("pack.json", json.dumps(p, indent=1).encode())]
    path = frames.path(p["frame"]["event_id"]) if p["frame"] and name else None
    if path and name:
        files.append((name, path.read_bytes()))
    return files


def manifest(files: list[tuple[str, bytes]]) -> str:
    """`sha256sum -c` format: one "<hex>  <name>" line per file, so anyone can check nothing in the zip was
    changed after Shopfront made it."""
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files)


def manifest_digest(p: dict, frames: FrameStore) -> str:
    """The SHA-256 of the zip's manifest.sha256: the one number the pack page shows for the whole zip."""
    return hashlib.sha256(manifest(zip_files(p, frames)).encode()).hexdigest()


def as_zip(p: dict, frames: FrameStore) -> bytes:
    buf = io.BytesIO()
    files = zip_files(p, frames)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files + [(MANIFEST, manifest(files).encode())]:
            z.writestr(zipfile.ZipInfo(name, _ZIP_TIME), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()
