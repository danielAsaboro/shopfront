"""Small helpers the HTML pages share: shop-local times, plain-language labels and verdicts.

Kept out of the templates so they can be tested, and out of the data modules so a page's wording
never changes what the API returns.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import illustrations, provenance

# Who labelled a door event, in the owner's words (the API keeps the raw source names)
BY = {"bedrock": "Bedrock looked at the Ring frame", "ring": "Ring's package alert",
      "rules": "Guess from the time of day"}

# One line for each outcome of reconcile.check_attempt / check_closed_scan. The full reasoning
# (reconcile's text) is shown under it; an outcome this map doesn't know falls back to its first sentence.
VERDICT = {
    "open_no_courier": "You were open, and the doorbell saw no courier near the scan.",
    "open_door_busy": "You were open: your posted hours and a busy door say so.",
    "open_courier_came": "You were open, and a courier was at the door near the scan.",
    "open_by_hours_only": "Your hours say you were open, but the door recorded nothing to back that up.",
    "posted_closed": "Your posted hours say you were closed then, so the scan matches.",
    "courier_at_door": "The door agrees with the carrier: a courier was there.",
    "door_activity_unverified": "Something was at the door then, but nothing confirmed it was a courier.",
    "someone_not_a_courier": "Someone was at the door then, but not a courier.",
    "nothing_recorded": "The doorbell recorded nothing near that time.",
    "no_door_data": "Shopfront has no door events for that day.",
    "camera_offline": "The camera may not have been watching: Ring reported the doorbell offline then.",
    "door_silent": "Can't tell: the door recorded nothing around the scan.",
    "courier_came_later": "No courier near the scan, but one came to the door later that day.",
    "not_happened_yet": "That time hasn't happened yet.",
    "no_such_local_time": "That time didn't exist that day: the clocks went forward.",
}


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def local_time(value, tz: ZoneInfo, day: str | date | None = None) -> str:
    """'09:12' in the shop's time zone, with the date in front when it isn't `day`.
    Naive times are taken as shop-local already."""
    at = _parse(value)
    if at is None:
        return str(value or "")
    at = at.replace(tzinfo=tz) if at.tzinfo is None else at.astimezone(tz)
    same_day = day is None or at.date().isoformat() == str(day)
    return at.strftime("%H:%M") if same_day else at.strftime("%Y-%m-%d %H:%M")


def local_stamp(value, tz: ZoneInfo) -> str:
    """'2026-10-21 at 07:30' in the shop's time zone, for a 'made at' line."""
    at = _parse(value)
    if at is None:
        return str(value or "")
    at = at.replace(tzinfo=tz) if at.tzinfo is None else at.astimezone(tz)
    return f"{at:%Y-%m-%d} at {at:%H:%M}"


# Notes the classifier adds to a reason that how_known() already says up front
_SAID_ALREADY = (" (synthetic event has no frame)", " (replayed Ring event, no frame)")


def how(source: str | None, reason: str | None, event_source: str | None = None, frame_url: str | None = None) -> str:
    """'Ring's package_delivery alert, no frame' / 'Guess from the time of day: doorbell press during open
    hours': what the label rests on, exactly for its source (app/provenance.py), then the reason."""
    reason = reason or ""
    for note in _SAID_ALREADY:
        reason = reason.replace(note, "")
    if source == "ring" and reason.lower().startswith("ring smart alert"):
        reason = reason[len("Ring Smart Alert: package_delivery"):].strip(" ;:")  # the prefix says it
    if source not in ("ring", "bedrock", "rules"):  # e.g. a live session nothing labelled ("none")
        return f"{by(source)}: {reason}" if reason else by(source)
    known = provenance.how_known(source, event_source, bool(frame_url))
    return f"{known}: {reason}" if reason else known


# One short word for what a label rests on (app/provenance.py), for the badge on a desk row; the full sentence
# from how() sits behind it
BADGE = {provenance.RING_ALERT: "ring", provenance.REPLAY: "replay", provenance.BEDROCK_RING: "bedrock",
         provenance.BEDROCK_LIVE: "bedrock", provenance.RULES: "guess", provenance.SYNTHETIC: "demo"}


def badge(source: str | None, event_source: str | None = None) -> str:
    """'ring' / 'bedrock' / 'guess' / 'replay' / 'demo'."""
    if source not in ("ring", "bedrock", "rules") and event_source not in ("synthetic", "ring-webhook-replay"):
        return "guess"
    return BADGE[provenance.kind(source, event_source)]


def pack_stills(p: dict, limit: int = 2) -> list[dict]:
    """The door log rows to picture on a pack: at most `limit`, nearest the time in question (the scan, or the
    courier), the ones with a Ring frame first, and never the frame the pack already shows large."""
    def secs(t: str) -> int:
        h, m, *rest = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + (rest[0] if rest else 0)
    centre_at = (p.get("claim") or {}).get("time", "")[-5:] or (p.get("frame") or {}).get("time", "")
    if not centre_at:
        return []
    centre = secs(centre_at)
    shown = (p.get("frame") or {}).get("event_id")
    rows = [t for t in p.get("timeline") or [] if t["event_id"] != shown
            and (t.get("frame_url") or illustrations.url_for(t))]
    rows.sort(key=lambda t: (not t.get("frame_url"), abs(secs(t["time"]) - centre), t["time"]))
    return sorted(rows[:limit], key=lambda t: t["time"])


# A visit type in the owner's words, where the raw value isn't already one
TYPE_LABEL = {"doorbell_press": "doorbell press", "after_hours": "after hours"}


def visit_label(visit_type: str | None, confirmed: bool = True) -> str:
    """'customer', or 'doorbell press (guess)' when nothing looked at the door to say it."""
    t = str(visit_type or "")
    return TYPE_LABEL.get(t, t.replace("_", " ")) + ("" if confirmed else " (guess)")


def relative_day(value, today: date) -> str:
    """'today' / 'yesterday' / '' for a 'YYYY-MM-DD', so a morning visit knows which day it's looking at."""
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return ""
    return {0: "today", 1: "yesterday"}.get((today - d).days, "")


def long_date(value) -> str:
    """'Wednesday 14 October 2026' from '2026-10-14'."""
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{d:%A} {d.day} {d:%B %Y}"


def by(source: str | None) -> str:
    return BY.get(source or "", source or "")


def verdict(finding: dict | None) -> str:
    if not finding:
        return ""
    v = finding.get("visit") or {}
    if finding.get("outcome") == "delivered_after_scan" and v.get("time"):
        mins = v.get("minutes_after")
        return f"Delivered late: the courier came at {v['time']}" + (f", {mins} min after the scan." if mins else ".")
    if finding.get("outcome") in VERDICT:
        return VERDICT[finding["outcome"]]
    text = str(finding.get("text", ""))
    return text.split(". ")[0].rstrip(".") + "." if text else ""


CLAIM_LABEL = {"attempted": "Delivery attempted", "business_closed": "Business closed"}


def scan_lead(finding: dict | None, carrier: str = "") -> str | None:
    """'Business closed scan at 15:42 (Northline Parcel). You were open, and the doorbell saw no courier near
    the scan.': a checked scan and its answer, as one line for the top of the receiving desk."""
    if not finding or not finding.get("outcome"):
        return None
    label = CLAIM_LABEL.get(finding.get("claim", ""), "Carrier")
    when = str(finding.get("attempt", ""))[-5:]
    return f"{label} scan at {when}" + (f" ({carrier})" if carrier else "") + f". {verdict(finding)}"


# Shown where Ask can't run, so the owner sees what it does. Recorded from the fake agent in
# tests/test_ask_example.py (the same tools over the demo's door data), never from Bedrock: the page says so.
ASK_EXAMPLE = {
    "question": "Did the flour come yesterday?",
    "answer": "Yes. Ring's package alert put a courier at the door at 10:12, and Shopfront matched it to "
              "Hillside Mill's order PO-1041. Ring has no door-answered signal, so this doesn't show the sacks "
              "were handed over.",
    "tools": "deliveries(yesterday)",
}


def labelled_by_text(funnel: dict, classifier: str) -> str:
    """What actually labelled the day's door events, from the funnel counts (never from the
    classifier's name alone: 'bedrock' mode with no frames means Bedrock labelled nothing)."""
    if not funnel or not funnel.get("events"):
        return "Bedrock vision, rules when there is no frame" if classifier == "bedrock" else "time-of-day rules"
    parts = []
    if funnel.get("labelled_by_ring"):
        parts.append(f"{funnel['labelled_by_ring']} by Ring's package_delivery alert")
    ring_frames = funnel.get("reached_bedrock", 0) - funnel.get("bedrock_live", 0)
    if ring_frames:
        parts.append(f"{ring_frames} by Bedrock from a Ring frame")
    if funnel.get("bedrock_live"):
        parts.append(f"{funnel['bedrock_live']} by Bedrock from frames captured in your browser from Ring live view")
    if funnel.get("labelled_by_rules"):
        parts.append(f"{funnel['labelled_by_rules']} by time-of-day rules")
    if funnel.get("skipped_stage0"):
        parts.append(f"{funnel['skipped_stage0']} skipped (Ring said not a person)")
    if funnel.get("ring_live_views"):
        n = funnel["ring_live_views"]
        parts.append(f"{n} Ring live view{'' if n == 1 else 's'} (on_demand: the camera was opened), not visitors")
    return ", ".join(parts) or "none"


def desk_rows(r: dict) -> list[dict]:
    """Expected deliveries and couriers nobody listed, in one list ordered by the time on the row:
    the courier's time when seen, else the start of the window, else at the end."""
    rows = [{"kind": "expected", **x} for x in r.get("expected", [])]
    rows += [{"kind": "unexpected", "visit": u} for u in r.get("unexpected", [])]

    def key(x: dict) -> str:
        if x.get("visit"):
            return x["visit"]["time"]
        if x.get("status") == "possible" and x.get("possible"):
            return x["possible"][0]["time"]
        return x.get("window_start") or "99:99"
    return sorted(rows, key=key)


def plain_error(status: int, detail: str, path: str) -> dict:
    """An error page in the owner's words: a title, one sentence, and a way back.
    The raw status and detail stay available under Details."""
    d = (detail or "").lower()
    back = ("/", "Back to the receiving desk")
    if status == 404 and ("order" in d or "delivery" in d):
        title, text, back = "That order isn't here", "It may have been removed on Set up, or the link is old.", \
            ("/setup#orders", "See your orders")
    elif status == 404:
        title, text = "Page not found", "There's nothing at this address."
    elif "pack needs" in d or (path == "/dispute" and status == 400 and "must be" not in d):
        title = "A dispute pack needs something to dispute"
        text = ("Start from a carrier's scan (check it on the receiving desk) or from an order that never came, "
                "or that staff marked short or damaged.")
        back = ("/#atth", "Check a carrier's scan")
    elif "pick a date" in d or "date must be" in d or "use yyyy-mm-dd" in d:
        title, text = "That date doesn't look right", "Pick a day with the date picker, between 2000 and a year from today."
    elif "hh:mm" in d:
        title, text = "That time doesn't look right", "Write the time like 12:40, on the 24-hour clock."
    elif "future" in d or "hasn't happened" in d:
        title, text = "That time hasn't happened yet", "Shopfront can only check a scan against door events it has."
    elif "claim must be" in d:
        title, text = "Shopfront can't check that scan", "It checks \"Delivery attempted\" and \"Business closed\" scans."
    elif status == 403:
        title, text = "Not available here", (detail[:1].upper() + detail[1:] + ".") if detail else \
            "This page only opens for the shop's owner."
    elif status == 422:
        title, text = "Something in that request was missing", "Go back and fill in the form again."
    elif status >= 500:
        title, text = "Something went wrong on Shopfront's side", "Nothing was changed. Try again, and if it keeps " \
                                                                  "happening, check the terminal Shopfront runs in."
    else:
        title, text = "That didn't work", (detail[:1].upper() + detail[1:].rstrip(".") + ".") if detail else ""
    return {"status": status, "title": title, "text": text, "detail": detail, "back": back}
