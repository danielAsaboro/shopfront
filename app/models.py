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
    """One front-door camera event (motion or doorbell press)."""

    id: str
    device_id: str
    timestamp: datetime  # timezone-aware, shop local time
    kind: str  # "motion" | "ding"
    source: str = "synthetic"  # "ring" for Ring Event History, "synthetic" for the demo generator
    ring_subtype: str | None = None  # e.g. "human" when the history query filtered on motion.human
    snapshot: bytes | None = None  # JPEG bytes when available
    # Synthetic events only: the generator's answer key. Classifiers never read it;
    # tests use it to measure how well the rules do.
    truth: VisitType | None = None


@dataclass
class ClassifiedVisit:
    event: DoorEvent
    visit_type: VisitType
    confidence: float
    reason: str = ""
    source: str = "rules"  # rules | bedrock | ring (Ring's own package_delivery Smart Alert)
    request_id: str | None = None  # Bedrock response RequestId, when Bedrock answered


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
    deliveries_while_closed: list[dict] = field(default_factory=list)  # couriers Ring or Bedrock saw, nobody in
    presses_while_closed: list[dict] = field(default_factory=list)  # rule-guessed "deliveries": presses, not couriers
    after_hours: list[dict] = field(default_factory=list)
    first_activity: str | None = None
    last_activity: str | None = None
    classifier: str = "rules"
    ring_source: str = "synthetic"
    headline: list[str] = field(default_factory=list)
    visits: list[dict] = field(default_factory=list)


# Labels that come from something that looked at the door (Bedrock on a Ring frame, or Ring's own
# computer vision behind a package_delivery alert), as opposed to a time-of-day guess.
SEEN_SOURCES = ("bedrock", "ring")
