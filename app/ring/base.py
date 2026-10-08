from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.models import DoorEvent


class RingClient(ABC):
    """Source of front-door events for one shop on one day."""

    name: str = "base"

    @abstractmethod
    def events_for_day(self, day: date) -> list[DoorEvent]:
        """All door events for the given local date, oldest first."""
