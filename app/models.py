from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class VisitType(str, Enum):
    CUSTOMER = "customer"
    DELIVERY = "delivery"
    STAFF = "staff"
    AFTER_HOURS = "after_hours"
    UNKNOWN = "unknown"


@dataclass
class DoorEvent:
    """One front-door camera event (motion or doorbell press) from Ring."""

    id: str
    device_id: str
    timestamp: datetime  # timezone-aware, shop local time
    kind: str  # "motion" | "ding"
    answered: bool = False  # someone opened the door / answered the ding
    snapshot: bytes | None = None  # JPEG bytes when available
    snapshot_url: str | None = None
    # Only the mock client sets this: ground truth so the stub classifier is realistic.
    hint: VisitType | None = None


@dataclass
class ClassifiedVisit:
    event: DoorEvent
    visit_type: VisitType
    confidence: float
    reason: str = ""
    source: str = "stub"  # stub | bedrock


@dataclass
class Briefing:
    date: str
    shop_name: str
    total_visits: int
    customers: int
    customers_last_week: int
    hourly_customers: list[int] = field(default_factory=lambda: [0] * 24)
    hourly_customers_last_week: list[int] = field(default_factory=lambda: [0] * 24)
    busiest_hour: int | None = None
    busiest_hour_count: int = 0
    busiest_hour_last_week: int | None = None
    busiest_hour_last_week_count: int = 0
    deliveries: int = 0
    missed_deliveries: list[dict] = field(default_factory=list)
    after_hours: list[dict] = field(default_factory=list)
    opened_at: str | None = None
    closed_at: str | None = None
    classifier: str = "stub"
    ring_source: str = "mock"
    headline: list[str] = field(default_factory=list)
