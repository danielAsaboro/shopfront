"""Deterministic synthetic door events so the app runs with no Ring account.

Not Ring data. The same date always produces the same events, so "vs last week" comparisons
are stable. Each event carries `truth` (the generator's answer key) for tests only.

Like a doorbell with Ring's Smart Alerts on, some motion by people carries `ring_subtype="human"` (API
reference, Notifications > Motion Detection: sub_type `human`, "Human detected"), so the demo shows the path
Ring's own person signal takes. They are drawn from a separate seed, so the events themselves don't change, and
they stay `source="synthetic"`: every page and pack names them demo data, never Ring's alerts.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.models import DoorEvent, VisitType
from app.ring.base import RingClient

# Share of people's motion events that the demo marks the way Ring's Smart Alert would ("human"); the rest stay
# plain motion, as from a doorbell with Smart Alerts off or a person Ring didn't classify
HUMAN_SHARE = {VisitType.CUSTOMER: 0.7, VisitType.STAFF: 0.7, VisitType.AFTER_HOURS: 0.4}

# Relative customer traffic per hour of the day (morning coffee + lunch peaks).
HOURLY_WEIGHT = {8: 6, 9: 8, 10: 5, 11: 6, 12: 10, 13: 8, 14: 4, 15: 4, 16: 5, 17: 6, 18: 3}


class SyntheticRingClient(RingClient):
    name = "synthetic"

    def __init__(self, tz: ZoneInfo, hours: dict[int, tuple[int, int] | None],
                 device_id: str = "synthetic-doorbell-1"):
        self.tz = tz
        self.hours = hours
        self.device_id = device_id

    def _rng(self, day: date) -> random.Random:
        seed = int(hashlib.sha256(day.isoformat().encode()).hexdigest()[:8], 16)
        return random.Random(seed)

    def _at(self, day: date, hour: int, minute: int, second: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute, second), tzinfo=self.tz)

    def _at_min(self, day: date, minutes: int) -> datetime:
        minutes = max(0, min(minutes, 24 * 60 - 1))
        return self._at(day, minutes // 60, minutes % 60)

    def events_for_day(self, day: date) -> list[DoorEvent]:
        rng = self._rng(day)
        hours = self.hours[day.weekday()]
        raw: list[tuple[datetime, VisitType, str]] = []

        if hours:
            open_m, close_m = hours  # minutes since midnight (app.hours)
            open_h, close_h = open_m // 60, -(-close_m // 60)
            # Staff arrive before open and leave after close.
            raw.append((self._at_min(day, (open_h - 1) * 60 + rng.randint(35, 55)), VisitType.STAFF, "motion"))
            raw.append((self._at_min(day, close_m + rng.randint(5, 25)), VisitType.STAFF, "motion"))

            # Customers: busier on Fridays/Saturdays, with daily noise.
            day_factor = {4: 1.25, 5: 1.4}.get(day.weekday(), 1.0) * rng.uniform(0.8, 1.2)
            for h in range(open_h, close_h):
                n = max(0, round(rng.gauss(HOURLY_WEIGHT.get(h, 3) * day_factor, 1.5)))
                for _ in range(n):
                    ts = self._at(day, h, rng.randint(0, 59), rng.randint(0, 59))
                    if open_m <= ts.hour * 60 + ts.minute < close_m:
                        raw.append((ts, VisitType.CUSTOMER, "motion"))

            # Deliveries ring the bell; some arrive before open or after close.
            for _ in range(rng.randint(2, 4)):
                h = rng.choice([open_h - 1, open_h, 10, 11, 14, 15, close_h % 24])
                raw.append((self._at(day, max(0, min(h, 23)), rng.randint(0, 59)), VisitType.DELIVERY, "ding"))
        else:
            # Closed day: a courier rings anyway.
            raw.append((self._at(day, 11, rng.randint(0, 59)), VisitType.DELIVERY, "ding"))

        # After-hours motion: passers-by, late-night activity.
        for _ in range(rng.randint(0, 3)):
            h = rng.choice([0, 1, 2, 3, 22, 23])
            raw.append((self._at(day, h, rng.randint(0, 59)), VisitType.AFTER_HOURS, "motion"))

        now = datetime.now(self.tz)
        raw = sorted((r for r in raw if r[0] <= now), key=lambda r: r[0])  # no events from the future
        alerts = random.Random(int(hashlib.sha256(f"{day.isoformat()}:smart-alert".encode()).hexdigest()[:8], 16))
        return [DoorEvent(id=f"syn-{day.isoformat()}-{i:04d}", device_id=self.device_id, timestamp=ts,
                          kind=kind, source="synthetic", truth=truth,
                          ring_subtype="human" if kind == "motion" and alerts.random() < HUMAN_SHARE.get(truth, 0)
                          else None)
                for i, (ts, truth, kind) in enumerate(raw)]
