from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import time

from app.models import ClassifiedVisit, DoorEvent


class Classifier(ABC):
    name: str = "base"

    @abstractmethod
    def classify(self, event: DoorEvent, open_hours: tuple[time, time] | None) -> ClassifiedVisit:
        ...
