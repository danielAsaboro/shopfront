"""Event History composite filters (event_types=motion.package_delivery) that Ring may not apply.

API reference, Event History > Event type filtering: dotted values filter "by event type AND detection
subtype". Another team logged the Developer Playground answering a motion.vehicle query with on_demand
events anyway. A package_delivery tag makes a motion event a courier "by Ring" with no model call, the
app's strongest label, so a filter that looks ignored must tag nothing. MockTransport, documented shapes.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.ring.api import RingApiClient, RingAuth

TZ = ZoneInfo("America/Los_Angeles")
DEV = "ava1.ring.device.TEST"
DAY = date(2026, 10, 7)


def ms(h, m=0, d=DAY):
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp() * 1000)


def hist(event_type, start):
    return {"type": "history-events", "id": f"evt-{event_type}-{start}",
            "attributes": {"event_type": event_type, "is_third_party_reviewed": False,
                           "start": start, "end": start + 20000},
            "relationships": {"source": {"data": {"type": "devices", "id": DEV}}}}


def ring_with(by_filter: dict, calls: list, event_types: str = "ding,motion,on_demand") -> RingApiClient:
    def handler(req: httpx.Request):
        calls.append(req.url.params.get("event_types"))
        return httpx.Response(200, json={"data": by_filter.get(req.url.params.get("event_types"), []), "links": {}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, device_id=DEV,
                         event_types=event_types, http=http)


def subtypes(events):
    return [(e.kind, e.ring_subtype) for e in events]


def test_a_filtered_answer_with_live_views_in_it_tags_nothing_and_says_why(caplog):
    walk, pkg, live = hist("motion", ms(11)), hist("motion", ms(10, 12)), hist("on_demand", ms(9))
    calls: list = []
    ring = ring_with({"ding,motion,on_demand": [walk, pkg, live],
                      "motion.package_delivery": [pkg, live]}, calls)  # Ring ignored the filter's type
    with caplog.at_level(logging.WARNING, logger="app.ring.api"):
        events = ring.events_for_day(DAY)
    assert subtypes(events) == [("on_demand", None), ("motion", None), ("motion", None)]
    assert "on_demand" in ring.filter_notes[(DEV, "package_delivery")]
    assert "looks ignored" in caplog.text and "on_demand" in caplog.text
    ring.events_for_day(date(2026, 10, 6))  # unreliable for this device: not asked again
    assert calls.count("motion.package_delivery") == 1


def test_a_filtered_answer_equal_to_every_motion_event_tags_nothing():
    a, b = hist("motion", ms(10)), hist("motion", ms(11))
    ring = ring_with({"ding,motion,on_demand": [b, a], "motion.package_delivery": [b, a]}, [])
    assert subtypes(ring.events_for_day(DAY)) == [("motion", None), ("motion", None)]
    assert "all 2 of the day's motion events" in ring.filter_notes[(DEV, "package_delivery")]


def test_untagged_events_go_to_the_classifier_not_to_ring_couriers():
    from app.briefing import classify_day
    from app.hours import parse_hours

    a, b = hist("motion", ms(10)), hist("motion", ms(11))
    ring = ring_with({"ding,motion,on_demand": [b, a], "motion.package_delivery": [b, a]}, [])
    seen: list = []

    class Recorder:
        name = "recorder"

        def classify(self, event, open_hours, **kw):
            from app.models import ClassifiedVisit, VisitType
            seen.append(event.id)
            return ClassifiedVisit(event, VisitType.UNKNOWN, 0.1, "recorded", "rules")

    visits = classify_day(ring, Recorder(), DAY, parse_hours(""))
    assert all(v.source != "ring" for v in visits) and sorted(seen) == sorted([a["id"], b["id"]])


def test_a_real_subset_is_still_tagged():
    walk, pkg = hist("motion", ms(11)), hist("motion", ms(10, 12))
    ring = ring_with({"ding,motion,on_demand": [walk, pkg], "motion.package_delivery": [pkg]}, [])
    assert subtypes(ring.events_for_day(DAY)) == [("motion", "package_delivery"), ("motion", None)]
    assert ring.filter_notes == {}


def test_one_motion_event_matching_the_filter_is_tagged():
    pkg = hist("motion", ms(10, 12))
    ring = ring_with({"ding,motion,on_demand": [pkg], "motion.package_delivery": [pkg]}, [])
    assert subtypes(ring.events_for_day(DAY)) == [("motion", "package_delivery")]


def test_a_single_composite_query_that_returns_unasked_types_tags_nothing():
    walk, live = hist("motion", ms(11)), hist("on_demand", ms(9))
    earlier = hist("motion", ms(11, 0, date(2026, 10, 6)))  # the day before: nothing unasked in it
    calls: list = []
    ring = ring_with({"ding,motion.human": [walk, live, earlier]}, calls, event_types="ding,motion.human")
    assert subtypes(ring.events_for_day(DAY)) == [("on_demand", None), ("motion", None)]
    assert (DEV, "human") in ring.filter_notes
    assert subtypes(ring.events_for_day(date(2026, 10, 6))) == [("motion", None)]  # still distrusted
    assert len(calls) == 2  # one query a day, as before
