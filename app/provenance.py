"""Where a door label came from, said exactly, for pages, packs and drafts.

A door label can rest on nine different things, and they are not equally strong:

- `ring_alert`: Ring's own motion `sub_type: package_delivery` (a Smart Alert, pushed by webhook or in Event
  History). Ring says a courier came; Shopfront holds no frame of it unless it downloaded one.
- `ring_person`: Ring's own motion `sub_type: human` ("Human detected", Smart Alerts enabled; `motion.human` in Event
  History). Ring says a person was at the door, not who: never a courier, never proof there wasn't one.
- `replay`: an event from Shopfront's signed webhook replay feeder (`ring-webhook-replay`), not a Ring
  notification, whatever it was labelled.
- `bedrock_ring_frame`: Bedrock read the frame Shopfront downloaded from Ring for that event.
- `bedrock_live_frame`: Bedrock read a frame the owner's browser captured from a Ring live-view session
  (app/live.py). Shopfront can't tell those bytes from any other JPEG, so it is never called a Ring frame.
- `ring_playground`: a Ring Developer Playground scene (the Ring simulator, event source `ring-playground`) that
  nothing labelled: counted as a door visit, labelled `PLAYGROUND`, never as anyone in particular.
- `bedrock_playground_frame`: Bedrock read the Ring frame of a Playground scene. Ring's simulator, not the shop's
  door, whatever Bedrock saw in it.
- `rules`: the time-of-day rules guessed; nothing looked at the door.
- `synthetic`: Shopfront's demo generator made the event up; no camera recorded it.

Each kind has its own wording; nothing says "the frame" when there is no frame.
"""

from __future__ import annotations

RING_ALERT, RING_PERSON, REPLAY, BEDROCK_RING, BEDROCK_LIVE, RULES, SYNTHETIC = (
    "ring_alert", "ring_person", "replay", "bedrock_ring_frame", "bedrock_live_frame", "rules", "synthetic")
PLAYGROUND_KIND, BEDROCK_PLAYGROUND = "ring_playground", "bedrock_playground_frame"

# A Playground scene's event source and its label, exactly (app/briefing.py classify_day, docs/ring.md)
PLAYGROUND_SOURCE = "ring-playground"
PLAYGROUND = "Playground scene (Ring simulator)"


def kind(labelled_by: str | None, event_source: str | None, visit_type: str | None = None) -> str:
    """One of the nine kinds above. Anything that isn't Ring, Bedrock or synthetic/replayed data is a rules
    guess, the weakest reading."""
    src = event_source or ""
    if src == "synthetic":
        return SYNTHETIC
    if src == "ring-webhook-replay":
        return REPLAY
    if labelled_by == PLAYGROUND_SOURCE:
        return PLAYGROUND_KIND
    if labelled_by == "ring":
        return RING_PERSON if visit_type == "person" else RING_ALERT
    if labelled_by == "bedrock":
        return BEDROCK_LIVE if src.startswith("ring-live") else BEDROCK_PLAYGROUND if src == PLAYGROUND_SOURCE \
            else BEDROCK_RING
    return RULES


def of(row: dict | None) -> str:
    """The kind for a visit row (deliveries._visit_row, reconcile._visit, dispute._timeline)."""
    row = row or {}
    return kind(row.get("labelled_by"), row.get("event_source"), row.get("type"))


def how_known(labelled_by: str | None, event_source: str | None, has_frame: bool = False,
              visit_type: str | None = None) -> str:
    """What the label rests on, in the owner's words: 'Ring's package_delivery alert, no frame'."""
    k = kind(labelled_by, event_source, visit_type)
    person = labelled_by == "ring" and visit_type == "person"
    # Demo and replayed events: a few words, since the row's badge and the page's banner already say what
    # they are (round 4: "demo" fifteen times a page)
    if k == SYNTHETIC:
        return "Demo person alert, no camera" if person else "Demo event; guess from the time of day"
    if k == REPLAY:
        what = "person alert" if person else "package alert" if labelled_by == "ring" else "event"
        tail = "" if labelled_by == "ring" else "; guess from the time of day"
        return f"Replayed {what}, not from Ring, no frame{tail}"
    if k == RING_PERSON:
        return "Ring's person alert (Smart Alert sub_type human): someone was at the door, not who"
    if k == RING_ALERT:
        return "Ring's package_delivery alert" + ("; Shopfront holds its Ring frame" if has_frame else ", no frame")
    if k == PLAYGROUND_KIND:
        return f"{PLAYGROUND}: a Ring Developer Playground event, not the shop's door; nothing said who"
    if k == BEDROCK_PLAYGROUND:
        return f"Bedrock read the Ring frame of a {PLAYGROUND}, not the shop's door"
    if k == BEDROCK_LIVE:
        return "Bedrock read a frame captured in your browser from Ring live view"
    if k == BEDROCK_RING:
        return "Bedrock read the Ring frame"
    return "Guess from the time of day"


# The same, as the source of a statement in a pack or a draft: "(Ring's package_delivery alert)"
SAID_BY = {
    RING_ALERT: "Ring's package_delivery alert",
    RING_PERSON: "Ring's person alert",
    REPLAY: "a replayed Ring package_delivery alert, not a Ring notification",
    BEDROCK_RING: "Bedrock saw them in the Ring frame",
    BEDROCK_LIVE: "Bedrock saw them in a frame captured in the owner's browser from a Ring live-view session",
    RULES: "a guess from the time of day",
    SYNTHETIC: "a synthetic demo event, no camera",
    PLAYGROUND_KIND: f"a {PLAYGROUND}, not the shop's door; nothing said who",
    BEDROCK_PLAYGROUND: f"Bedrock saw them in the Ring frame of a {PLAYGROUND}, not the shop's door",
}

# Why a courier label can't say whose order it was, per kind
NOT_WHOSE = {
    RING_ALERT: "Ring's package_delivery alert says a courier came, not whose order it was",
    RING_PERSON: "Ring's person alert says someone was at the door, not who or whose order",
    REPLAY: "the replayed package_delivery alert says a courier came, not whose order it was",
    BEDROCK_RING: "the Ring frame shows a courier, not whose order it was",
    BEDROCK_LIVE: "the frame captured in your browser from Ring live view shows a courier, not whose order it was",
    RULES: "the time-of-day rules only guessed a courier, and can't say whose order it was",
    SYNTHETIC: "it's a synthetic demo event: no camera saw a courier, let alone whose order it was",
    PLAYGROUND_KIND: f"it's a {PLAYGROUND}: nothing said who it was, let alone whose order",
    BEDROCK_PLAYGROUND: f"it's a {PLAYGROUND}: the simulator's frame shows a courier, not the shop's door or whose order",
}

FRAME_LABEL = {BEDROCK_LIVE: "Frame captured in your browser from Ring live view",
               BEDROCK_PLAYGROUND: "Ring frame of a Playground scene", PLAYGROUND_KIND: "Ring frame of a Playground scene"}


def said_by(row: dict | None) -> str:
    return SAID_BY[of(row)]


def not_whose(row: dict | None) -> str:
    return NOT_WHOSE[of(row)]


def frame_label(row: dict | None) -> str:
    """Alt text and headings for a frame Shopfront holds: a Ring frame unless the browser captured it."""
    return FRAME_LABEL.get(of(row), "Ring frame")
