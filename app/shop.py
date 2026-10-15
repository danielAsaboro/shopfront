"""The owner's shop profile: name, posted hours and what a missed delivery costs them.

Set on /setup and saved to `.data/shop.json`. Until then the .env values apply. The cost is the
owner's own number; Shopfront never supplies an industry average.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.hours import parse_hours

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Profile:
    name: str
    hours: str  # SHOP_HOURS format, Monday first
    redelivery_cost: float | None = None  # the owner's cost of one missed delivery
    currency: str = "$"
    saved: bool = False  # True once the owner has been through /setup


class ShopProfile:
    def __init__(self, path: Path, default_name: str, default_hours: str):
        self.path = path
        self._default = Profile(default_name, default_hours)

    def get(self) -> Profile:
        if not self.path.exists():
            return Profile(**asdict(self._default))
        data = json.loads(self.path.read_text())
        return Profile(**{**asdict(self._default), **data, "saved": True})

    def hours(self) -> dict[int, tuple[int, int] | None]:
        return parse_hours(self.get().hours)

    def save(self, p: Profile) -> None:
        parse_hours(p.hours)  # refuse hours that can't be read back
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in asdict(p).items() if k != "saved"}, indent=1))
        tmp.replace(self.path)


def hours_from_form(values: list[str]) -> str:
    """Seven form fields like '8-18' or 'closed' (blank means closed) into SHOP_HOURS format."""
    if len(values) != 7:
        raise ValueError("need 7 days of hours")
    out = []
    for v in values:
        v = v.strip().lower().replace(" ", "").replace("–", "-")
        out.append(v if v and v != "closed" else "closed")
    spec = ",".join(out)
    parse_hours(spec)
    return spec


def hours_for_form(spec: str) -> list[str]:
    return [p.strip() for p in spec.split(",")]
