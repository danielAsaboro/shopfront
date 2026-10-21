"""Rule-based classifier: time of day + event kind only. Used when Bedrock is off or has no frame."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.classify.base import Classifier
from app.models import ClassifiedVisit, DoorEvent, VisitType

STAFF_BEFORE_OPEN = timedelta(minutes=75)
STAFF_AFTER_CLOSE = timedelta(minutes=60)


def _offset(t: time, ref: time) -> timedelta:
    return datetime.combine(datetime.min, t) - datetime.combine(datetime.min, ref)


class RulesClassifier(Classifier):
    name = "rules"

    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None,
                 note: str = "") -> ClassifiedVisit:
        def visit(kind: VisitType, conf: float, reason: str) -> ClassifiedVisit:
            return ClassifiedVisit(event, kind, conf, reason + (f" ({note})" if note else ""), self.name)

        t = event.timestamp.timetz().replace(tzinfo=None)
        if event.kind == "ding":
            # Who pressed it is unknown without a frame: a courier, a customer, a neighbour. Never "delivery".
            if open_hours and open_hours[0] <= t < open_hours[1]:
                return visit(VisitType.DOORBELL_PRESS, 0.5, "doorbell press during open hours")
            return visit(VisitType.DOORBELL_PRESS, 0.6, "doorbell press while closed")
        if open_hours is None:
            return visit(VisitType.AFTER_HOURS, 0.6, "motion on a closed day")
        open_t, close_t = open_hours
        if open_t <= t < close_t:
            return visit(VisitType.CUSTOMER, 0.5, "motion during open hours")
        if timedelta(0) < -_offset(t, open_t) <= STAFF_BEFORE_OPEN:
            return visit(VisitType.STAFF, 0.4, "motion shortly before opening")
        if timedelta(0) <= _offset(t, close_t) <= STAFF_AFTER_CLOSE:
            return visit(VisitType.STAFF, 0.4, "motion shortly after closing")
        return visit(VisitType.AFTER_HOURS, 0.6, "motion outside posted hours")
