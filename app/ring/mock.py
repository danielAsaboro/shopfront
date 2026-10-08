"""Deterministic synthetic Ring events so the app runs with no credentials.

The same date always produces the same events, so "vs last week" comparisons are stable.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import DoorEvent, VisitType
from app.ring.base import RingClient

# (open, close) by weekday, Monday=0. None = closed.
HOURS: dict[int, tuple[int, int] | None] = {
    0: (8, 18), 1: (8, 18), 2: (8, 18), 3: (8, 18), 4: (8, 19),
    5: (9, 16), 6: None,
}

# Relative customer traffic per hour of the day (morning coffee + lunch peaks).
HOURLY_WEIGHT = {8: 6, 9: 8, 10: 5, 11: 6, 12: 10, 13: 8, 14: 4, 15: 4, 16: 5, 17: 6, 18: 3}


class MockRingClient(RingClient):
    name = "mock"

    def __init__(self, tz: ZoneInfo, device_id: str = "mock-doorbell-1"):
        self.tz = tz
        self.device_id = device_id

    def _rng(self, day: date) -> random.Random:
        seed = int(hashlib.sha256(day.isoformat().encode()).hexdigest()[:8], 16)
        return random.Random(seed)

    def _at(self, day: date, hour: int, minute: int, second: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute, second), tzinfo=self.tz)

    def events_for_day(self, day: date) -> list[DoorEvent]:
        rng = self._rng(day)
        hours = HOURS[day.weekday()]
        raw: list[tuple[datetime, VisitType, str, bool]] = []

        if hours:
            open_h, close_h = hours
            # Staff arrive before open and leave after close.
            arrive = self._at(day, open_h - 1, rng.randint(35, 55))
            leave = self._at(day, close_h, rng.randint(5, 25))
            raw.append((arrive, VisitType.STAFF, "motion", True))
            raw.append((leave, VisitType.STAFF, "motion", True))

            # Customers: busier on Fridays/Saturdays, with daily noise.
            day_factor = {4: 1.25, 5: 1.4}.get(day.weekday(), 1.0) * rng.uniform(0.8, 1.2)
            for h in range(open_h, close_h):
                n = max(0, round(rng.gauss(HOURLY_WEIGHT.get(h, 3) * day_factor, 1.5)))
                for _ in range(n):
                    raw.append((self._at(day, h, rng.randint(0, 59), rng.randint(0, 59)),
                                VisitType.CUSTOMER, "motion", True))

            # Deliveries: a few per day, some arrive when nobody answers the bell.
            for _ in range(rng.randint(2, 4)):
                h = rng.choice([open_h - 1, open_h, 10, 11, 14, 15, close_h])
                answered = rng.random() > 0.3 and open_h <= h < close_h
                raw.append((self._at(day, h, rng.randint(0, 59)), VisitType.DELIVERY, "ding", answered))
        else:
            # Closed day: a delivery attempt nobody answers.
            raw.append((self._at(day, 11, rng.randint(0, 59)), VisitType.DELIVERY, "ding", False))

        # After-hours motion: passers-by, late-night activity.
        for _ in range(rng.randint(0, 3)):
            h = rng.choice([0, 1, 2, 3, 22, 23])
            raw.append((self._at(day, h, rng.randint(0, 59)), VisitType.AFTER_HOURS, "motion", False))

        raw.sort(key=lambda r: r[0])
        return [
            DoorEvent(
                id=f"{day.isoformat()}-{i:04d}",
                device_id=self.device_id,
                timestamp=ts,
                kind=kind,
                answered=answered,
                hint=hint,
            )
            for i, (ts, hint, kind, answered) in enumerate(raw)
        ]


def previous_same_weekday(day: date) -> date:
    return day - timedelta(days=7)
