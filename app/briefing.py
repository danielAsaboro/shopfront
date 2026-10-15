"""Turn a day of classified door events into the owner's daily briefing."""

from __future__ import annotations

from datetime import date, time, timedelta

from app.cache import ClassificationCache
from app.classify.base import Classifier
from app.classify.bedrock import BedrockClassifier
from app.hours import posted_hours
from app.models import Briefing, ClassifiedVisit, VisitType
from app.ring.api import RingApiClient, RingError
from app.ring.base import RingClient


def classify_day(ring: RingClient, classifier: Classifier, day: date,
                 hours: dict[int, tuple[int, int] | None],
                 cache: ClassificationCache | None = None, live=None, frames=None) -> list[ClassifiedVisit]:
    open_hours = posted_hours(hours, day)
    visits: list[ClassifiedVisit] = []
    if live is not None and isinstance(ring, RingApiClient):  # live-view sessions from the same Ring source
        visits.extend(live.visits_for_day(day))
    for e in ring.events_for_day(day):
        if not isinstance(classifier, BedrockClassifier):
            visits.append(classifier.classify(e, open_hours))
            continue
        hit = cache.get(e.id, classifier.model_id) if cache else None
        if hit:
            visits.append(ClassifiedVisit(e, VisitType(hit["type"]), hit["confidence"], hit["reason"],
                                          "bedrock", hit.get("request_id")))
            continue
        no_frame = "synthetic event has no frame"
        if isinstance(ring, RingApiClient):
            try:
                e.snapshot = ring.snapshot(e)
            except RingError as exc:
                no_frame = f"no Ring frame: {exc.status.value}"
        v = classifier.classify(e, open_hours, no_frame_reason=no_frame)
        if frames is not None and e.snapshot and v.source == "bedrock":
            frames.save(e.id, e.snapshot)  # keep the frame Bedrock saw, as the visit's receipt
        e.snapshot = None  # don't keep images in memory
        if v.source == "bedrock" and cache:
            cache.put(e.id, {"type": v.visit_type.value, "confidence": v.confidence, "reason": v.reason,
                             "model": classifier.model_id, "request_id": v.request_id})
        visits.append(v)
    return sorted(visits, key=lambda v: v.event.timestamp)


def _hourly(visits: list[ClassifiedVisit]) -> list[int]:
    counts = [0] * 24
    for v in visits:
        if v.visit_type == VisitType.CUSTOMER:
            counts[v.event.timestamp.hour] += 1
    return counts


def _peak(counts: list[int]) -> tuple[int | None, int]:
    best = max(range(24), key=lambda h: counts[h])
    return (best, counts[best]) if counts[best] else (None, 0)


def _fmt_hour(h: int | None) -> str:
    return "—" if h is None else f"{h:02d}:00–{(h + 1) % 24:02d}:00"


def _is_open(v: ClassifiedVisit, open_hours: tuple[time, time] | None) -> bool:
    t = v.event.timestamp.timetz().replace(tzinfo=None)
    return bool(open_hours) and open_hours[0] <= t < open_hours[1]


def build_briefing(ring: RingClient, classifier: Classifier, day: date, shop_name: str,
                   hours: dict[int, tuple[int, int] | None],
                   cache: ClassificationCache | None = None, live=None, frames=None) -> Briefing:
    today = classify_day(ring, classifier, day, hours, cache, live, frames)
    last_week = classify_day(ring, classifier, day - timedelta(days=7), hours, cache, live, frames)
    open_hours = posted_hours(hours, day)

    hourly, hourly_lw = _hourly(today), _hourly(last_week)
    peak_h, peak_n = _peak(hourly)
    peak_lw_h, peak_lw_n = _peak(hourly_lw)

    deliveries = [v for v in today if v.visit_type == VisitType.DELIVERY]
    closed_deliveries = [{"time": v.event.timestamp.strftime("%H:%M"), "id": v.event.id, "reason": v.reason}
                         for v in deliveries if not _is_open(v, open_hours)]
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
        lines.append(f"{_n(len(b.deliveries_while_closed), 'delivery attempt')} while the shop was closed, at {times}.")
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
