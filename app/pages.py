"""Small helpers the HTML pages share: shop-local times, plain-language labels and verdicts.

Kept out of the templates so they can be tested, and out of the data modules so a page's wording
never changes what the API returns.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import provenance

# The page icon: a shop door with its doorbell, inline on every page (and at /favicon.ico)
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><style>.b{fill:#1d1d1f}.f{fill:#f7f6f3}'
           '@media (prefers-color-scheme: dark){.b{fill:#ececee}.f{fill:#151517}}</style>'
           '<rect width="32" height="32" rx="7" class="b"/><rect x="9" y="6" width="12" height="21" rx="1.5" class="f"/>'
           '<rect x="11.5" y="8.5" width="7" height="8" rx="1" class="b"/><circle cx="25" cy="16" r="2" class="f"/></svg>')

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
    person = source == "ring" and reason.startswith("Ring Smart Alert: human")
    if source == "ring" and reason.lower().startswith("ring smart alert"):
        reason = reason.split(":", 1)[1].strip().removeprefix("human").removeprefix("package_delivery").strip(" ;:")
    if source not in ("ring", "bedrock", "rules", provenance.PLAYGROUND_SOURCE):  # e.g. a live session nothing labelled
        return f"{by(source)}: {reason}" if reason else by(source)
    known = provenance.how_known(source, event_source, bool(frame_url), "person" if person else None)
    return f"{known}: {reason}" if reason else known


def log_how(t: dict) -> str:
    """A pack's door log row, "How we know": the same words on the page, in pack.txt and in pack.json, so no copy
    of the pack credits Ring for a demo or replayed event. A demo event the rules guessed reads as the guess it is
    (the pack's watermark already says the day is demo data); a demo person alert says it was the demo's."""
    es = t.get("event_source")
    return how(t.get("labelled_by"), t.get("reason"), None if es == "synthetic" and t.get("type") != "person" else es,
               t.get("frame_url"))


# One short word for what a label rests on (app/provenance.py), for the badge on a desk row; the full sentence
# from how() sits behind it
BADGE = {provenance.RING_ALERT: "ring", provenance.RING_PERSON: "ring", provenance.REPLAY: "replay", provenance.BEDROCK_RING: "bedrock",
         provenance.BEDROCK_LIVE: "bedrock", provenance.RULES: "guess", provenance.SYNTHETIC: "demo",
         provenance.PLAYGROUND_KIND: "playground", provenance.BEDROCK_PLAYGROUND: "playground"}


def badge(source: str | None, event_source: str | None = None) -> str:
    """'ring' / 'bedrock' / 'guess' / 'replay' / 'demo' / 'playground'."""
    if source not in ("ring", "bedrock", "rules", provenance.PLAYGROUND_SOURCE) and \
            event_source not in ("synthetic", "ring-webhook-replay"):
        return "guess"
    return BADGE[provenance.kind(source, event_source)]


def pack_stills(p: dict, limit: int = 2) -> list[dict]:
    """The door log rows with a frame Shopfront holds (a Ring frame, or one captured from live view), to picture on
    a pack: at most `limit`, nearest the time in question (the scan, or the courier), never the frame the pack
    already shows large. Nothing is drawn: an event with no frame has no picture."""
    def secs(t: str) -> int:
        h, m, *rest = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + (rest[0] if rest else 0)
    centre_at = (p.get("claim") or {}).get("time", "")[-5:] or (p.get("frame") or {}).get("time", "")
    if not centre_at:
        return []
    centre = secs(centre_at)
    shown = (p.get("frame") or {}).get("event_id")
    rows = [t for t in p.get("timeline") or [] if t["event_id"] != shown and t.get("frame_url")]
    rows.sort(key=lambda t: (abs(secs(t["time"]) - centre), t["time"]))
    return sorted(rows[:limit], key=lambda t: t["time"])


# A visit type in the owner's words, where the raw value isn't already one
TYPE_LABEL = {"doorbell_press": "doorbell press", "after_hours": "after hours"}


def visit_label(visit_type: str | None, confirmed: bool = True, event_source: str | None = None) -> str:
    """'customer', or 'doorbell press (guess)' when nothing looked at the door to say it, or 'person (Ring)' when
    Ring's own Smart Alert said a person ('person' on demo or replayed events, whose row says where they came from:
    never Ring's). A Ring Developer Playground scene nothing labelled is exactly "Playground scene (Ring
    simulator)"; one Bedrock labelled says what Bedrock saw, then that it was a Playground scene."""
    t = str(visit_type or "")
    if event_source == provenance.PLAYGROUND_SOURCE:
        return f"{TYPE_LABEL.get(t, t.replace('_', ' '))} ({provenance.PLAYGROUND})" if confirmed \
            else provenance.PLAYGROUND
    if t == "person":
        return "person" if event_source in ("synthetic", "ring-webhook-replay") else "person (Ring)"
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
    # the tool calls behind the answer, each with what the tool returned that the answer rests on
    "trace": [{"call": 'deliveries(day="yesterday")',
               "returned": "Hillside Mill, order PO-1041, due 10:00–11:00: courier at 10:12, labelled by Ring's "
                           "package_delivery alert (labelled_by: ring)"}],
}


def labelled_by_text(funnel: dict, classifier: str) -> str:
    """What actually labelled the day's door events, from the funnel counts (never from the
    classifier's name alone: 'bedrock' mode with no frames means Bedrock labelled nothing)."""
    if not funnel or not funnel.get("events"):
        return "Bedrock vision, rules when there is no frame" if classifier == "bedrock" else "time-of-day rules"
    parts = []
    alerts = funnel.get("labelled_by_ring", 0) - funnel.get("ring_people", 0)
    replayed = min(alerts, funnel.get("replayed_alerts", 0))  # Shopfront's feeder, never Ring's own alert
    if alerts - replayed:
        parts.append(f"{alerts - replayed} by Ring's package_delivery alert")
    if replayed:
        parts.append(f"{replayed} by a replayed package alert")
    if funnel.get("ring_people"):
        demo = funnel.get("demo_people", 0)
        real = funnel["ring_people"] - demo
        if real:
            parts.append(f"{real} as people by Ring's person alert")
        if demo:
            parts.append(f"{demo} as people by demo person alerts")
    ring_frames = funnel.get("reached_bedrock", 0) - funnel.get("bedrock_live", 0)
    if ring_frames:
        parts.append(f"{ring_frames} by Bedrock from a Ring frame")
    if funnel.get("bedrock_live"):
        parts.append(f"{funnel['bedrock_live']} by Bedrock from frames captured in your browser from Ring live view")
    if funnel.get("labelled_by_rules"):
        parts.append(f"{funnel['labelled_by_rules']} by time-of-day rules")
    if funnel.get("skipped_stage0"):
        parts.append(f"{funnel['skipped_stage0']} skipped (Ring said not a person)")
    if funnel.get("playground_scenes"):
        n = funnel["playground_scenes"]
        parts.append(f"{n} as \"{provenance.PLAYGROUND}\" (Ring Developer Playground on_demand "
                     f"event{'' if n == 1 else 's'}, not the shop's door)")
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


# ---- The verdict as three short facts, and the door around the scan as a picture ---------------------------

# What a label rests on, in two or three words, for a fact line: "Courier at 10:12 (Ring alert)"
SHORT_BY = {provenance.RING_ALERT: "Ring alert", provenance.RING_PERSON: "Ring person alert",
            provenance.REPLAY: "replayed alert", provenance.BEDROCK_RING: "Bedrock, Ring frame",
            provenance.BEDROCK_LIVE: "Bedrock, live-view frame", provenance.RULES: "guess",
            provenance.SYNTHETIC: "demo", provenance.PLAYGROUND_KIND: "Playground scene",
            provenance.BEDROCK_PLAYGROUND: "Bedrock, Playground frame"}


def _mins(hhmm: str) -> int | None:
    """Minutes since midnight from 'HH:MM', 'HH:MM:SS' or 'YYYY-MM-DD HH:MM'."""
    try:
        h, m, *_ = (int(x) for x in str(hhmm).strip().split(" ")[-1].split(":"))
    except ValueError:
        return None
    return h * 60 + m


def _clock(m: float) -> str:
    m = int(round(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _person(row: dict) -> bool:
    return row.get("type") == "person" or (row.get("ring_event") == "motion" and row.get("ring_subtype") == "human")


def _people(rows: list[dict]) -> str:
    """', 4 people (Ring)' when every person alert came from the shop's Ring camera; ', 4 person alerts' when any
    came from the demo or a replay (the verdict's chip says which), never credited to Ring."""
    people = [r for r in rows if _person(r)]
    n = len(people)
    if not n:
        return ""
    if all(r.get("event_source") in ("ring", "ring-webhook") for r in people):
        return f", {n} {'person' if n == 1 else 'people'} (Ring)"
    return f", {n} person alert{'' if n == 1 else 's'}"


def _events(n: int) -> str:
    return f"{n} door event{'' if n == 1 else 's'}"


def _camera_fact(cov: dict | None, at: str) -> dict | None:
    if not cov:
        return None
    st = cov.get("state")
    if st == "online_by_status":
        return {"ok": True, "text": f"Camera online around {at} ({'demo status checks' if cov.get('demo') else 'Ring status'})"}
    if st == "door_events":
        return {"ok": True, "text": f"Camera recording around {at}"}
    if st == "online_by_ring":
        return {"ok": True, "text": f"Camera online around {at} (Ring notice)"}
    if st == "offline":
        return {"ok": False, "text": f"Camera offline around {at} (Ring)"}
    return {"ok": False, "text": f"Nothing shows the camera was watching at {at}"}


def _courier(v: dict, later: bool = False) -> str:
    said = SHORT_BY[provenance.of(v)]
    mins = v.get("minutes_after")
    if later or mins:
        return f"Courier came later, at {v['time']}" + (f", {mins} min after the scan" if mins else "") + f" ({said})"
    return f"Courier at the door at {v['time']} ({said})"


def _no_courier_frame(door: list[dict]) -> dict:
    """The courier fact when nothing Ring or Bedrock labelled says who came: a neutral note ("ok": None), never a
    cross. Round 7: "✗ Courier not identified" read as "no proof anyone came", when it only means no camera
    frame shows the courier."""
    held = any(r.get("frame_url") and not r.get("confirmed") for r in door)
    return {"ok": None, "text": "No frame confirmed the courier" if held else "No camera frame of the courier"}


def _sides(door: list[dict], scan: int | None) -> str:
    """" (2 before the scan, 3 after)" or " (all after the scan)": which side of the scan the door was used on, so
    "a busy door" never stands for use on one side only. The times are in the strip and under Details."""
    ms = [m for r in door if (m := _mins(r.get("time", ""))) is not None]
    if scan is None or not ms:
        return ""
    b = sum(1 for m in ms if m < scan)
    if not b:
        return " (all after the scan)"
    if b == len(ms):
        return " (all before the scan)"
    return f" ({b} before the scan, {len(ms) - b} after)"


def verdict_facts(c: dict | None) -> list[dict]:
    """Three short facts under a scan's one-line verdict, each {"ok": True/False/None, "text": ...}: the posted
    hours, the door around the scan, and the courier (or the camera, when that is what decides it). None is a
    neutral note, shown after the ticks and crosses. The full reasoning stays in reconcile's text, which the page
    keeps under Details. Nothing a rule guessed is stated as a fact."""
    if not c or not c.get("outcome") or c["outcome"] in ("not_happened_yet", "no_such_local_time"):
        return []
    out, at = c["outcome"], str(c.get("attempt", ""))[-5:]
    door, a, v = c.get("door") or [], c.get("activity") or {}, c.get("visit") or {}
    tol = c.get("tolerance_min") or 10
    scan = _mins(at)
    near = [r for r in door if scan is not None and _mins(r.get("time", "")) is not None
            and abs(_mins(r["time"]) - scan) <= tol]
    facts: list[dict | None] = []
    if c.get("claim") == "business_closed":
        hours = c.get("posted_hours") or "closed"
        facts.append({"ok": False, "text": f"Closed by your posted hours ({hours})"} if out == "posted_closed"
                     else {"ok": True, "text": f"Open {hours}"})
        if out == "no_door_data":
            return facts + [{"ok": False, "text": "No door events that day"}]
        lo, hi = a.get("from", ""), a.get("to", "")
        n = a.get("events", len(door))
        facts.append({"ok": True, "text": f"{_events(n)} {lo}–{hi}{'' if out == 'posted_closed' else _sides(door, scan)}"
                                          f"{_people(door)}"} if n else
                     {"ok": False, "text": f"No door events {lo}–{hi}"})
        if out == "posted_closed":
            return facts
        if out in ("open_courier_came", "delivered_after_scan", "courier_came_later") and v:
            facts.append({"ok": True, "text": _courier(v, later=out != "open_courier_came")})
        elif out in ("camera_offline", "door_silent"):
            facts.append(_camera_fact(c.get("coverage"), at))
        elif out == "open_no_courier":
            facts.append({"ok": False, "text": f"No courier within {tol} min of the scan" +
                          (" (checked from the frame)" if a.get("near") else "")})
        else:
            facts.append(_no_courier_frame(near))
        return _neutral_last(facts)
    # "delivery attempted": was anyone near the time, who, and was the camera watching
    if out == "no_door_data":
        return [{"ok": False, "text": "No door events that day"}]
    facts.append({"ok": True, "text": f"{_events(len(near))} within {tol} min of {at}{_people(near)}"} if near else
                 {"ok": False, "text": f"Nothing recorded within {tol} min of {at}"})
    if out in ("courier_at_door", "courier_came_later", "delivered_after_scan") and v:
        facts.append({"ok": True, "text": _courier(v, later=out != "courier_at_door")})
    elif out == "someone_not_a_courier" and v:
        facts.append({"ok": False, "text": f"Not a courier: {visit_label(v.get('type'))} at {v['time']} "
                                           f"({SHORT_BY[provenance.of(v)]})"})
    elif out == "door_activity_unverified":
        facts.append(_no_courier_frame(near))
    else:
        facts.append({"ok": False, "text": "No courier seen"})
    facts.append(_camera_fact(c.get("coverage"), at))
    return _neutral_last(facts)


def _neutral_last(facts: list[dict | None]) -> list[dict]:
    """The ticks and crosses first, then any neutral note, so a note never reads as a pass or a fail."""
    out = [f for f in facts if f]
    return [f for f in out if f["ok"] is not None] + [f for f in out if f["ok"] is None]


def answer(c: dict | None) -> dict:
    """A checked scan's answer as one plain sentence and at most one supporting line: {"sentence", "support"}.
    Round 6: the demo's answer was ~90 words. The full reasoning stays in reconcile's text (the API's `text`,
    and under Details on the page). A refused check has no facts, so no supporting line."""
    if not c or not c.get("outcome"):
        return {"sentence": "", "support": ""}
    facts = verdict_facts(c)
    return {"sentence": verdict(c), "support": " · ".join(f["text"] for f in facts) + "." if facts else ""}


def pack_check(p: dict) -> dict | None:
    """A pack's finding in the shape verdict_facts() and door_strip() read (a reconcile check)."""
    f, c = p.get("finding"), p.get("claim")
    if not f or not c:
        return None
    return {**f, "claim": c["kind"], "attempt": c["time"], "posted_hours": p.get("posted_hours"),
            "coverage": p.get("coverage"), "tolerance_min": p.get("tolerance_min"), "door": p.get("timeline") or []}


def chip(sources, day_source: str | None = None) -> str:
    """One word for where a check's door data came from: 'demo', 'replay', 'playground' or 'ring'. With no door
    events around the scan there is nothing to credit: '' (no chip), or 'demo' when the day itself came from the
    demo generator (`day_source`, the briefing's ring_source). Round 7: an empty set read "ring" on the demo."""
    s = set(sources or [])
    if "synthetic" in s or (not s and day_source == "synthetic"):
        return "demo"
    if provenance.PLAYGROUND_SOURCE in s:
        return "playground"
    if any("replay" in x for x in s):
        return "replay"
    return "ring" if s else ""


STRIP_KIND = {"courier": "courier, confirmed", "person": "person alert", "seen": "seen in a frame",
              "guess": "not identified"}


def _strip_kind(r: dict) -> str:
    if r.get("type") == "delivery" and r.get("confirmed"):
        return "courier"
    if _person(r):
        return "person"
    return "seen" if r.get("confirmed") else "guess"


def _strip_label(r: dict) -> str:
    src = r.get("event_source")
    kind = _strip_kind(r)
    if kind == "courier":
        what = f"courier ({SHORT_BY[provenance.of(r)]})"
    elif kind == "person":
        what = "person (" + ("demo alert" if src == "synthetic" else "replayed alert" if "replay" in (src or "")
                             else "Ring alert") + ")"
    elif kind == "seen":
        what = f"{visit_label(r.get('type'))} (seen in a frame)"
    elif src == provenance.PLAYGROUND_SOURCE:
        what = provenance.PLAYGROUND
    else:
        what = {"ding": "doorbell press", "motion": "motion"}.get(r.get("ring_event"), "door event") + ", not identified"
    return f"{str(r.get('time', ''))[:5]} {what}"


def door_strip(c: dict | None, hours: str | None = None, order: dict | None = None,
               frame: dict | None = None) -> dict | None:
    """The door around a scan as a picture: a 60-minute track centred on the carrier's scan (the same 30 minutes
    either side that the finding, its text and the pack's door log call "around the scan", labelled so), the posted hours
    shaded, one mark per door event by what it rests on, the scan as its own tick, the 10 minutes that count
    as "near" bracketed, the order's delivery window when it overlaps, and a frame thumbnail when Shopfront
    holds one. Positions are percentages, so the SVG fits any width with readable text. `text` is the same
    picture in words, for screen readers and print."""
    if not c or not c.get("attempt"):
        return None
    scan = _mins(c["attempt"])
    if scan is None:
        return None
    reach, tol = 30, c.get("tolerance_min") or 10
    lo, hi = scan - reach, scan + reach

    def pct(m: float) -> float:
        return round(max(0.0, min(100.0, (m - lo) / (hi - lo) * 100)), 2)

    hours = hours or c.get("posted_hours") or ""
    band = None
    if hours and hours != "closed" and "–" in hours:
        o, cl = (_mins(x) for x in hours.split("–", 1))
        if o is not None and cl is not None and o < hi and cl > lo:
            band = {"x": pct(o), "w": round(pct(cl) - pct(o), 2), "label": f"Open {hours}"}
    win = None
    if order and order.get("window_start") and order.get("window_end"):
        ws, we = _mins(order["window_start"]), _mins(order["window_end"])
        if ws is not None and we is not None and ws < hi and we > lo:
            win = {"x": pct(ws), "w": round(pct(we) - pct(ws), 2),
                   "label": f"{order.get('order_ref') or 'Order'} due {order['window_start']}–{order['window_end']}"}
    dots = []
    for r in c.get("door") or []:
        m = _mins(r.get("time", ""))
        if m is None or not lo <= m <= hi:
            continue
        dots.append({"x": pct(m), "kind": _strip_kind(r), "label": _strip_label(r)})
    ticks = [{"x": pct(m), "label": _clock(m)} for m in range(lo, hi + 1, 15)]
    kinds = [k for k in STRIP_KIND if any(d["kind"] == k for d in dots)]
    words = (f"Door around the scan, {reach} minutes either side: {_clock(lo)} to {_clock(hi)}. "
             + ((f"Posted hours {hours} cover " + ("all of it. " if band["w"] >= 99.9 else "the shaded part. "))
                if band else "Posted hours: closed that day. " if hours == "closed" else
                f"All of it is outside the posted hours ({hours}). " if hours else "")
             + f"Carrier's scan at {_clock(scan)}; {tol} minutes either side bracketed as near the scan. "
             + (f"{win['label']}. " if win else "")
             + (f"{len(dots)} door event{'' if len(dots) == 1 else 's'}: " + "; ".join(d["label"] for d in dots) + "."
                if dots else "No door events in this hour."))
    return {"scan": pct(scan), "scan_label": f"Scan {_clock(scan)}", "near": {"x": pct(scan - tol),
            "w": round(pct(scan + tol) - pct(scan - tol), 2), "label": f"near ±{tol}"},
            "around": f"around ±{reach} min",
            "band": band, "window": win, "dots": dots, "ticks": ticks, "kinds": kinds,
            "kind_text": STRIP_KIND, "text": words, "frame": frame}
