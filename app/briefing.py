"""Turn a day of classified door events into the owner's daily briefing."""

from __future__ import annotations

from datetime import date, time, timedelta

from app.classify.base import Classifier
from app.models import Briefing, ClassifiedVisit, VisitType
from app.ring.base import RingClient
from app.ring.mock import HOURS


def posted_hours(day: date) -> tuple[time, time] | None:
    h = HOURS[day.weekday()]
    return (time(h[0]), time(h[1])) if h else None


def classify_day(ring: RingClient, classifier: Classifier, day: date) -> list[ClassifiedVisit]:
    hours = posted_hours(day)
    return [classifier.classify(e, hours) for e in ring.events_for_day(day)]


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


def build_briefing(ring: RingClient, classifier: Classifier, day: date, shop_name: str) -> Briefing:
    today = classify_day(ring, classifier, day)
    last_week = classify_day(ring, classifier, day - timedelta(days=7))

    hourly, hourly_lw = _hourly(today), _hourly(last_week)
    peak_h, peak_n = _peak(hourly)
    peak_lw_h, peak_lw_n = _peak(hourly_lw)

    deliveries = [v for v in today if v.visit_type == VisitType.DELIVERY]
    missed = [{"time": v.event.timestamp.strftime("%H:%M"), "id": v.event.id}
              for v in deliveries if not v.event.answered]
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
        missed_deliveries=missed,
        after_hours=after,
        opened_at=staff[0].event.timestamp.strftime("%H:%M") if staff else None,
        closed_at=staff[-1].event.timestamp.strftime("%H:%M") if len(staff) > 1 else None,
        classifier=classifier.name,
        ring_source=ring.name,
    )
    b.headline = _headline(b, posted_hours(day))
    return b


def _headline(b: Briefing, hours: tuple[time, time] | None) -> list[str]:
    lines: list[str] = []
    if hours is None and not b.customers:
        lines.append("Shop was closed today.")
    else:
        diff = b.customers - b.customers_last_week
        pct = f" ({diff / b.customers_last_week:+.0%})" if b.customers_last_week else ""
        lines.append(f"{b.customers} customers came in, {abs(diff)} "
                     f"{'more' if diff >= 0 else 'fewer'} than last week{pct}.")
    if b.busiest_hour is not None:
        same = b.busiest_hour == b.busiest_hour_last_week
        lines.append(f"Busiest window was {_fmt_hour(b.busiest_hour)} with {b.busiest_hour_count} customers"
                     + (" — same as last week." if same else
                        f"; last week it was {_fmt_hour(b.busiest_hour_last_week)}."))
    if b.missed_deliveries:
        times = ", ".join(m["time"] for m in b.missed_deliveries)
        lines.append(f"{len(b.missed_deliveries)} delivery attempt(s) went unanswered at {times}.")
    if b.opened_at:
        lines.append(f"Door first opened by staff at {b.opened_at}"
                     + (f", last staff exit at {b.closed_at}." if b.closed_at else "."))
    if b.after_hours:
        lines.append(f"{len(b.after_hours)} after-hours motion event(s) to review.")
    return lines
