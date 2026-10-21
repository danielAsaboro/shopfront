"""Ring's own person signal: motion sub_type `human` (API reference, Notifications > Motion Detection, Smart Alerts
enabled: "human, Human detected"; Event History > Event types: `motion.human`). It makes a door event a
"person (Ring)" where the time-of-day rules only guessed "customer", and the business-closed check cites it.
Local only: MockTransport, signed test webhooks and the synthetic generator."""

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import dispute, pages, provenance
from app import reconcile as rc
from app.briefing import classify_day
from app.classify.rules import RulesClassifier
from app.deliveries import FrameStore
from app.hours import parse_hours
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.ring.api import RingApiClient, RingAuth
from app.ring.synthetic import SyntheticRingClient
from app.ring.webhooks import WebhookLog, ring_person, sign

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 7)  # a Wednesday
OPEN = (time(8), time(18))
DEV = "ava1.ring.device.TEST"
KEY = "test-hmac-key"


def ms(h, m=0):
    return int(datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ).timestamp() * 1000)


def hist(event_type, start):
    return {"type": "history-events", "id": f"evt-{event_type}-{start}",
            "attributes": {"event_type": event_type, "is_third_party_reviewed": False, "start": start,
                           "end": start + 20000},
            "relationships": {"source": {"data": {"type": "devices", "id": DEV}}}}


def ring_client(main_items, human_items, calls=None):
    calls = [] if calls is None else calls

    def handler(req: httpx.Request):
        calls.append(req)
        kind = req.url.params.get("event_types")
        if kind == "motion.package_delivery":
            return httpx.Response(200, json={"data": [], "links": {}})
        if kind == "motion.human":
            return httpx.Response(200, json={"data": human_items, "links": {}})
        return httpx.Response(200, json={"data": main_items, "links": {}})
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return RingApiClient(auth=RingAuth(access_token="t", http=http), tz=TZ, device_id=DEV, http=http)


def at(hh, mm):
    return datetime.combine(DAY, time(hh, mm), tzinfo=TZ)


def visit(hh, mm, kind=VisitType.CUSTOMER, source="rules", sub=None, ring_event="motion", src="ring"):
    e = DoorEvent(f"ev-{hh:02d}{mm:02d}", "d", at(hh, mm), ring_event, src, ring_subtype=sub)
    return ClassifiedVisit(e, kind, 0.9, "why", source)


def person(hh, mm, src="ring"):
    return ring_person(visit(hh, mm, sub="human", src=src))


# ---- Event History: the documented composite filter --------------------------------------------

def test_history_asks_motion_human_and_makes_people_labelled_by_ring():
    walk, other = hist("motion", ms(11)), hist("motion", ms(12))
    calls: list = []
    ring = ring_client([walk, other, hist("ding", ms(13))], [walk], calls)
    events = ring.events_for_day(DAY)
    assert [e.ring_subtype for e in events] == ["human", None, None]
    assert any(c.url.params.get("event_types") == "motion.human" for c in calls)
    vs = classify_day(ring, RulesClassifier(), DAY, parse_hours(""))
    p = next(v for v in vs if v.event.id == walk["id"])
    assert (p.visit_type, p.source, p.reason) == (VisitType.PERSON, "ring", "Ring Smart Alert: human")
    plain = next(v for v in vs if v.event.id == other["id"])
    assert (plain.visit_type, plain.source) == (VisitType.CUSTOMER, "rules")  # still a guess, and says so


def test_an_ignored_human_filter_tags_nothing_like_package_delivery():
    """The same reliability check as package_delivery (app/ring/api.py): an answer holding every motion event
    of the day, or event types nobody asked for, means the filter was ignored on this doorbell."""
    a, b = hist("motion", ms(11)), hist("motion", ms(12))
    ring = ring_client([a, b], [a, b])
    assert [e.ring_subtype for e in ring.events_for_day(DAY)] == [None, None]
    assert "as if unfiltered" in ring.filter_notes[(DEV, "human")]
    stray = ring_client([a, b, hist("on_demand", ms(13))], [a, hist("on_demand", ms(13))])
    assert all(e.ring_subtype is None for e in stray.events_for_day(DAY))
    assert (DEV, "human") in stray.filter_notes


def test_bedrock_label_beats_rings_person_and_rules_staff_keeps_its_guess():
    seen = ClassifiedVisit(DoorEvent("e", "d", at(12, 0), "motion", "ring", "human"), VisitType.DELIVERY, 0.9,
                           "courier with a box", "bedrock", "rid")
    assert ring_person(seen) is seen
    staff = ring_person(visit(7, 30, VisitType.STAFF, sub="human"))
    assert staff.visit_type == VisitType.STAFF and staff.source == "rules" and "Ring's Smart Alert said a person" in \
        staff.reason
    ding = visit(12, 0, VisitType.DOORBELL_PRESS, ring_event="ding", sub="human")
    assert ring_person(ding) is ding  # sub_type is a motion attribute


# ---- Webhooks: the same signal pushed by Ring ----------------------------------------------------

def test_a_human_webhook_is_a_person_by_ring(tmp_path):
    log = WebhookLog(tmp_path / "w.jsonl")
    stamp = ms(14, 25)
    raw = json.dumps({"meta": {"version": "1.1", "time": "2026-10-07T21:25:00Z", "request_id": "r1",
                               "account_id": "acct"},
                      "data": {"id": f"dev_human_{stamp}", "type": "motion_detected",
                               "attributes": {"source": "dev", "source_type": "devices", "timestamp": stamp,
                                              "sub_type": "human"}}}).encode()
    assert log.handle(raw, sign(raw, KEY), KEY).value == "ACCEPTED"

    class Empty(SyntheticRingClient):
        def events_for_day(self, day):
            return []
    stats: dict = {}
    [v] = classify_day(Empty(TZ, parse_hours("")), RulesClassifier(), DAY, parse_hours(""), webhooks=log, stats=stats)
    assert (v.visit_type, v.source, v.event.source) == (VisitType.PERSON, "ring", "ring-webhook")
    assert stats["ring_people"] == 1 and pages.labelled_by_text(stats, "rules") == "1 as people by Ring's person alert"
    row = {"labelled_by": "ring", "event_source": "ring-webhook", "type": "person"}
    assert provenance.of(row) == provenance.RING_PERSON
    assert pages.visit_label("person", False, "ring-webhook") == "person (Ring)"
    assert pages.how("ring", v.reason, "ring-webhook").startswith("Ring's person alert")


# ---- The business-closed finding cites Ring's people, or says it has only motion counts --------

def test_closed_scan_cites_ring_people_around_the_scan():
    vs = [person(14, 0), person(14, 10), visit(14, 15), person(14, 45), visit(14, 50, VisitType.DOORBELL_PRESS,
                                                                              ring_event="ding")]
    r = rc.check_closed_scan(at(14, 30), vs, OPEN, now=at(20, 0))
    assert r["activity"]["people"] == 3 and r["activity"]["people_source"] == ["ring"]
    assert "(14:00–15:00; Ring classified 3 of these 5 door events as people)" in r["text"]
    assert r["outcome"] == rc.OPEN_NO_COURIER  # nothing within 10 minutes of 14:30


def test_a_person_near_the_scan_could_be_the_courier():
    """A courier is a person: Ring's person alert never rules a courier out (or in)."""
    vs = [person(14, 0), person(14, 25), visit(14, 50)]
    r = rc.check_closed_scan(at(14, 30), vs, OPEN, now=at(20, 0))
    assert r["outcome"] == rc.OPEN_DOOR_BUSY and "a person Ring detected at 14:25" in r["text"]
    assert [u["time"] for u in r["unidentified"]] == ["14:25"]
    only = rc.check_closed_scan(at(14, 30), [person(14, 28)], OPEN, now=at(20, 0))
    assert only["outcome"] == rc.UNVERIFIED


def test_without_person_alerts_the_finding_is_motion_counts():
    vs = [visit(14, 0), visit(14, 10), visit(14, 50)]
    r = rc.check_closed_scan(at(14, 30), vs, OPEN, now=at(20, 0))
    assert r["activity"]["people"] == 0 and "classified" not in r["text"]
    assert "recorded 3 motion events in the 30 minutes either side of the scan (14:00–15:00)" in r["text"]


def test_draft_states_rings_people_and_backs_otherwise(tmp_path):
    vs = [person(14, 0), person(14, 10), person(14, 50)]
    check = rc.check_closed_scan(at(14, 30), vs, OPEN, now=at(20, 0))
    p = dispute.build_pack("Shop", DAY, vs, FrameStore(tmp_path), "08:00–18:00", check, carrier="Northline")
    body = p["draft"]["body"]
    assert "Ring's person detection classified 3 of them as people" in body
    assert "Our front-door camera shows otherwise." in body


# ---- Demo: the generator stands in for Ring's alert, labelled demo -------------------------------

def test_demo_has_person_alerts_and_never_calls_them_rings():
    ring = SyntheticRingClient(TZ, parse_hours(""))
    events = ring.events_for_day(DAY)
    humans = [e for e in events if e.ring_subtype == "human"]
    assert humans and all(e.kind == "motion" and e.source == "synthetic" for e in humans)
    assert len(humans) < sum(1 for e in events if e.kind == "motion")  # some plain motion too
    # the alerts come from their own seed: the events are the same as without them
    assert [e.timestamp for e in events] == [e.timestamp for e in ring.events_for_day(DAY)]
    stats: dict = {}
    vs = classify_day(ring, RulesClassifier(), DAY, parse_hours(""), stats=stats)
    people = [v for v in vs if v.visit_type == VisitType.PERSON]
    footer = pages.labelled_by_text(stats, "rules")
    assert f"{len(people)} as people by the demo's stand-in for Ring's person alert" in footer
    assert "by Ring's person alert" not in footer.replace("stand-in for Ring's person alert", "")
    assert people and all(v.source == "ring" and v.event.source == "synthetic" for v in people)
    v = people[0]
    assert pages.visit_label("person", False, "synthetic") == "person (demo)"
    assert "Synthetic demo event standing in for Ring's person alert" in pages.how("ring", v.reason, "synthetic")
    scan = v.event.timestamp + timedelta(minutes=12)
    r = rc.check_closed_scan(scan, vs, OPEN, now=at(23, 0))
    assert "Shopfront's demo stand-in for Ring's person alert classified" in r["text"]
    assert "Ring classified" not in r["text"] and "Ring detected" not in r["text"]
    near = rc.check_closed_scan(v.event.timestamp + timedelta(minutes=2), vs, OPEN, now=at(23, 0))
    assert "Ring detected" not in near["text"]
    if v.event.timestamp.strftime("%H:%M") in near["text"]:
        assert f"a person (demo person alert) at {v.event.timestamp:%H:%M}" in near["text"]
