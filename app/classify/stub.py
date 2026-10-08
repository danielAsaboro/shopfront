"""Rule-based classifier used when AWS credentials are not configured."""

from __future__ import annotations

from datetime import time

from app.classify.base import Classifier
from app.models import ClassifiedVisit, DoorEvent, VisitType


class StubClassifier(Classifier):
    name = "stub"

    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None) -> ClassifiedVisit:
        if event.hint is not None:  # mock data carries ground truth
            return ClassifiedVisit(event, event.hint, 0.99, "mock ground truth", self.name)

        t = event.timestamp.timetz().replace(tzinfo=None)
        if open_hours is None or not (open_hours[0] <= t < open_hours[1]):
            return ClassifiedVisit(event, VisitType.AFTER_HOURS, 0.6, "outside posted hours", self.name)
        if event.kind == "ding":
            return ClassifiedVisit(event, VisitType.DELIVERY, 0.6, "doorbell press in hours", self.name)
        return ClassifiedVisit(event, VisitType.CUSTOMER, 0.5, "motion in hours", self.name)
