"""Turn a day of classified door events into the owner's daily briefing."""

from __future__ import annotations

from datetime import UTC, date, time, timedelta

from app.cache import ClassificationCache
from app.classify.base import Classifier
from app.classify.bedrock import BedrockClassifier
from app.deliveries import is_possible_courier, is_seen_courier, staff_in
from app.hours import posted_hours
from app.models import Briefing, ClassifiedVisit, DoorEvent, VisitType
from app.ring.api import RingApiClient, RingError, RingStatus
from app.ring.base import RingClient
from app.ring.webhooks import ring_alert, ring_person, stage0_route

SAME_EVENT = timedelta(seconds=5)  # a webhook and its Event History record name the same press or motion


def _door_events(ring: RingClient, day: date, stage0: dict | None) -> list:
    history = ring.events_for_day(day)
    if not stage0:
        return history
    pushed = stage0["events"] + [v.event for v in stage0["ring_labelled"]] + stage0["skipped"]

    def dup(e):  # instants, not wall-clock readings, so a press at 01:30 PDT isn't the one at 01:30 PST
        return any(p.kind == e.kind and abs(p.timestamp.astimezone(UTC) - e.timestamp.astimezone(UTC)) <= SAME_EVENT
                   for p in pushed)
    return [e for e in history if not dup(e)] + stage0["events"]  # the webhook copy carries the sub_type


def classify_day(ring: RingClient, classifier: Classifier, day: date,
                 hours: dict[int, tuple[int, int] | None],
                 cache: ClassificationCache | None = None, live=None, frames=None,
                 webhooks=None, stats: dict | None = None) -> list[ClassifiedVisit]:
    """Every door visit that day, labelled. Stages follow Ring's CV guidelines: Ring's motion sub_type
    first (free; from a webhook or from the Event History composite filter), then the rules or Bedrock. Pass `stats` to get the funnel counts back."""
    open_hours = posted_hours(hours, day)
    tz = getattr(ring, "tz", None)
    stage0 = webhooks.day(day, tz) if webhooks is not None and tz is not None else None
    visits: list[ClassifiedVisit] = []
    if live is not None and isinstance(ring, RingApiClient):  # live-view sessions from the same Ring source
        visits.extend(live.visits_for_day(day))
    events: list[DoorEvent] = []
    skipped: list[DoorEvent] = []
    ring_labelled: list[ClassifiedVisit] = list((stage0 or {}).get("ring_labelled", []))
    live_views: list[DoorEvent] = []
    for e in _door_events(ring, day, stage0):  # Event History events get the same stage 0 as webhooks
        route = stage0_route(e)
        if e.kind == "on_demand":
            # API reference, Event History > Event types: on_demand is "User or partner initiated a live view
            # session". Someone opened the camera; it says nothing about who was at the door, so it is counted
            # as a live view and never becomes a visitor (or reaches a classifier).
            live_views.append(e)
        elif route == "skip":
            skipped.append(e)
        elif route == "ring":
            ring_labelled.append(ring_alert(e))
        else:
            events.append(e)
    for v in ring_labelled:
        if frames is not None and isinstance(ring, RingApiClient) and not frames.path(v.event.id):
            try:  # keep Ring's frame as the receipt; no Bedrock call, Ring already said what it was
                frames.save(v.event.id, ring.snapshot(v.event))
            except RingError as exc:
                v.reason += f" ({no_ring_frame(exc)})"
        visits.append(v)
    for e in events:
        if not isinstance(classifier, BedrockClassifier):
            visits.append(ring_person(classifier.classify(e, open_hours)))
            continue
        hit = cache.get(e.id, classifier.model_id) if cache else None
        if hit:
            visits.append(ClassifiedVisit(e, VisitType(hit["type"]), hit["confidence"], hit["reason"],
                                          "bedrock", hit.get("request_id")))
            continue
        no_frame = NO_FRAME.get(e.source, f"{e.source} event, no frame")
        if isinstance(ring, RingApiClient):
            try:
                e.snapshot = ring.snapshot(e)
            except RingError as exc:
                no_frame = no_ring_frame(exc)
        v = classifier.classify(e, open_hours, no_frame_reason=no_frame)
        if frames is not None and e.snapshot and v.source == "bedrock":
            frames.save(e.id, e.snapshot)  # keep the frame Bedrock saw, as the visit's receipt
        e.snapshot = None  # don't keep images in memory
        if v.source == "bedrock" and cache:
            cache.put(e.id, {"type": v.visit_type.value, "confidence": v.confidence, "reason": v.reason,
                             "model": classifier.model_id, "request_id": v.request_id})
        visits.append(ring_person(v, no_frame))  # Bedrock's label stands; a rules fallback defers to Ring's alert
    if stats is not None:
        stats.update(funnel(visits, {"skipped": list((stage0 or {}).get("skipped", [])) + skipped,
                                     "live_views": live_views}))
    return sorted(visits, key=lambda v: v.event.timestamp)


def no_ring_frame(exc: RingError) -> str:
    """Why there is no Ring frame, in a few words for a visit's reason."""
    if exc.status == RingStatus.BEFORE_TOKEN:
        return "no Ring frame: the event is from before the Playground token was issued"
    return f"no Ring frame: {exc.status.value}"


NO_FRAME = {  # why an event reached the classifier without a Ring frame, by where the event came from
    "synthetic": "synthetic event has no frame",
    "ring-webhook-replay": "replayed Ring event, no frame",
    "ring-webhook": "Ring webhook event, no frame downloaded",
    "ring": "no Ring frame downloaded",
}


def funnel(visits: list[ClassifiedVisit], stage0: dict | None) -> dict:
    """How far each door event got: the instrumentation Ring's CV guidelines ask for."""
    skipped = len((stage0 or {}).get("skipped", []))
    live_views = len((stage0 or {}).get("live_views", []))
    return {"events": len(visits) + skipped + live_views, "skipped_stage0": skipped, "ring_live_views": live_views,
            "labelled_by_ring": sum(1 for v in visits if v.source == "ring"),
            "ring_people": sum(1 for v in visits if v.source == "ring" and v.visit_type == VisitType.PERSON),
            "demo_people": sum(1 for v in visits if v.source == "ring" and v.visit_type == VisitType.PERSON
                               and v.event.source in ("synthetic", "ring-webhook-replay")),
            "replayed_alerts": sum(1 for v in visits if v.source == "ring" and v.visit_type != VisitType.PERSON
                                   and v.event.source == "ring-webhook-replay"),
            "reached_bedrock": sum(1 for v in visits if v.source == "bedrock"),
            "bedrock_live": sum(1 for v in visits if v.source == "bedrock" and v.event.source.startswith("ring-live")),
            "labelled_by_rules": sum(1 for v in visits if v.source == "rules"),
            "webhook_events": sum(1 for v in visits if v.event.source.startswith("ring-webhook")) + skipped,
            "replayed": any(v.event.source == "ring-webhook-replay" for v in visits)
            or any(e.source == "ring-webhook-replay" for e in (stage0 or {}).get("skipped", []))}


def _hourly(visits: list[ClassifiedVisit]) -> list[int]:
    counts = [0] * 24
    for v in visits:  # people Ring detected count with customers: during open hours they are the door's traffic
        if v.visit_type in (VisitType.CUSTOMER, VisitType.PERSON):
            counts[v.event.timestamp.hour] += 1
    return counts


def _peak(counts: list[int]) -> tuple[int | None, int]:
    best = max(range(24), key=lambda h: counts[h])
    return (best, counts[best]) if counts[best] else (None, 0)


def _fmt_hour(h: int | None) -> str:
    return "—" if h is None else f"{h:02d}:00–{(h + 1) % 24:02d}:00"


def _is_open(v: ClassifiedVisit, open_hours: tuple[time, time] | None) -> bool:
    t = v.event.timestamp.timetz().replace(tzinfo=None)
    return open_hours is not None and open_hours[0] <= t < open_hours[1]


def build_briefing(ring: RingClient, classifier: Classifier, day: date, shop_name: str,
                   hours: dict[int, tuple[int, int] | None],
                   cache: ClassificationCache | None = None, live=None, frames=None, webhooks=None,
                   stats: dict | None = None) -> Briefing:
    today = classify_day(ring, classifier, day, hours, cache, live, frames, webhooks, stats)
    last_week = classify_day(ring, classifier, day - timedelta(days=7), hours, cache, live, frames, webhooks)
    open_hours = posted_hours(hours, day)

    hourly, hourly_lw = _hourly(today), _hourly(last_week)
    peak_h, peak_n = _peak(hourly)
    peak_lw_h, peak_lw_n = _peak(hourly_lw)

    deliveries = [v for v in today if v.visit_type == VisitType.DELIVERY]
    # Closed and nobody in. Staff at the door (in early to open up, or still there) means it wasn't missed.
    # Couriers Ring or Bedrock saw, and doorbell presses nothing looked at (listed as presses, never couriers).
    closed = [v for v in today if (is_seen_courier(v) or is_possible_courier(v)) and not _is_open(v, open_hours)
              and not staff_in(v.event.timestamp, today, open_hours)]

    def row(v: ClassifiedVisit, what: str) -> dict:
        return {"time": v.event.timestamp.strftime("%H:%M"), "id": v.event.id, "reason": v.reason, "what": what,
                "labelled_by": v.source, "confirmed": is_seen_courier(v), "ring_event": v.event.kind,
                "event_source": v.event.source}
    # Only a courier Ring or Bedrock saw is a courier; a doorbell press nothing looked at stays a press
    closed_deliveries = [row(v, "Courier at the door while closed") for v in closed if is_seen_courier(v)]
    presses = [row(v, "Doorbell pressed while closed" if v.event.kind == "ding" else "Door event while closed")
               for v in closed if not is_seen_courier(v)]
    after = [{"time": v.event.timestamp.strftime("%H:%M"), "id": v.event.id, "reason": v.reason}
             for v in today if v.visit_type == VisitType.AFTER_HOURS]
    staff = [v for v in today if v.visit_type == VisitType.STAFF]

    b = Briefing(
        date=day.isoformat(),
        shop_name=shop_name,
        total_visits=len(today),
        customers=sum(hourly),
        customers_last_week=sum(hourly_lw),
        hourly_customers=hourly,
        hourly_customers_last_week=hourly_lw,
        busiest_hour=peak_h,
        busiest_hour_count=peak_n,
        busiest_hour_last_week=peak_lw_h,
        busiest_hour_last_week_count=peak_lw_n,
        deliveries=len(deliveries),
        deliveries_while_closed=closed_deliveries,
        presses_while_closed=presses,
        after_hours=after,
        first_activity=staff[0].event.timestamp.strftime("%H:%M") if staff else None,
        last_activity=staff[-1].event.timestamp.strftime("%H:%M") if len(staff) > 1 else None,
        classifier=classifier.name,
        ring_source=ring.name,
        visits=[{"id": v.event.id, "time": v.event.timestamp.strftime("%H:%M"), "kind": v.event.kind,
                 "type": v.visit_type.value, "confidence": v.confidence, "reason": v.reason,
                 "classifier": v.source, "event_source": v.event.source} for v in today],
    )
    b.headline = _headline(b, open_hours, has_last_week=bool(last_week))
    return b


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _headline(b: Briefing, hours: tuple[time, time] | None, has_last_week: bool) -> list[str]:
    lines: list[str] = []
    if b.deliveries_while_closed:
        times = ", ".join(m["time"] for m in b.deliveries_while_closed)
        n = len(b.deliveries_while_closed)
        lines.append(f"{n} courier{'' if n == 1 else 's'} at the door while the shop was closed, at {times}.")
    if b.presses_while_closed:  # a guess from the clock: say what Ring recorded, not who it was
        times = ", ".join(m["time"] for m in b.presses_while_closed)
        lines.append(f"Doorbell pressed while closed, at {times}; no camera frame confirmed who it was.")
    if b.first_activity:
        lines.append(f"Staff activity at the door from {b.first_activity}"
                     + (f" until {b.last_activity}." if b.last_activity else "."))
    if hours is None and not b.customers:
        lines.append("The shop was closed.")
    elif not has_last_week:
        lines.append(f"{b.customers} customers came in. No history for the same day last week yet.")
    else:
        diff = b.customers - b.customers_last_week
        pct = f" ({diff / b.customers_last_week:+.0%})" if b.customers_last_week else ""
        lines.append(f"{b.customers} customers came in, {abs(diff)} "
                     f"{'more' if diff >= 0 else 'fewer'} than the same day last week{pct}.")
    if b.busiest_hour is not None:
        same = b.busiest_hour == b.busiest_hour_last_week
        lines.append(f"Busiest window was {_fmt_hour(b.busiest_hour)} with {b.busiest_hour_count} customers"
                     + (", same as last week." if same else
                        f"; last week it was {_fmt_hour(b.busiest_hour_last_week)}." if b.busiest_hour_last_week
                        is not None else "."))
    if b.after_hours:
        lines.append(f"{_n(len(b.after_hours), 'after-hours motion event')} to review.")
    return lines
