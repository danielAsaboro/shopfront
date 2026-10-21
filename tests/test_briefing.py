from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import main
from app.briefing import build_briefing, classify_day
from app.classify.bedrock import BedrockClassifier
from app.classify.rules import RulesClassifier
from app.hours import parse_hours
from app.models import VisitType
from app.ring.synthetic import SyntheticRingClient

TZ = ZoneInfo("America/Los_Angeles")
HOURS = parse_hours("")
RING = SyntheticRingClient(TZ, HOURS)


def test_synthetic_is_deterministic():
    a = [e.timestamp for e in RING.events_for_day(date(2026, 10, 7))]
    assert a == [e.timestamp for e in RING.events_for_day(date(2026, 10, 7))] and a


def test_rules_never_read_the_answer_key():
    e = RING.events_for_day(date(2026, 10, 7))[0]
    e.truth = VisitType.DELIVERY
    assert RulesClassifier().classify(e, None).reason != "mock ground truth"


def test_rules_agree_with_the_synthetic_generator():
    """Regression check only. The generator and the rules encode the same assumptions, so agreement
    here is circular and says nothing about accuracy on real Ring footage."""
    rules, total, right, per = RulesClassifier(), 0, 0, {}
    for i in range(14):
        day = date(2026, 9, 28) + timedelta(days=i)
        for v in classify_day(RING, rules, day, HOURS):
            if v.source != "rules":  # the demo's stand-in for Ring's person alert labelled it, not the rules
                continue
            total += 1
            ok = v.visit_type == v.event.truth
            right += ok
            t = per.setdefault(v.event.truth.value, [0, 0])
            t[0] += ok
            t[1] += 1
    print(f"rules vs generator (circular) {right}/{total} = {right / total:.0%}", {k: f"{a}/{b}" for k, (a, b) in per.items()})
    assert right / total > 0.8
    assert per["staff"][0] / per["staff"][1] > 0.9


def test_weekday_briefing():
    b = build_briefing(RING, RulesClassifier(), date(2026, 10, 7), "Test Shop", HOURS)
    assert b.customers > 0 and b.customers_last_week > 0 and b.busiest_hour is not None
    assert b.first_activity and b.first_activity < "08:00"
    assert sum(b.hourly_customers) == b.customers and b.headline and b.ring_source == "synthetic"


def test_sunday_press_is_flagged_while_closed_as_a_press():
    """Was: a rules-labelled press counted as a delivery attempt while closed. The rules only know a button
    was pressed, so it's flagged as a press, and only a Ring/Bedrock courier counts as a courier."""
    b = build_briefing(RING, RulesClassifier(), date(2026, 10, 11), "Test Shop", HOURS)
    assert b.customers == 0 and b.deliveries_while_closed == [] and len(b.presses_while_closed) == 1
    assert b.headline[0].startswith("Doorbell pressed while closed")


def test_bedrock_without_a_frame_says_so():
    class NoCall:
        def converse(self, **kw):
            raise AssertionError("must not call Bedrock without a frame")

    clf = BedrockClassifier(region="us-east-1", model_id="m", client=NoCall())
    v = classify_day(RING, clf, date(2026, 10, 7), HOURS)[0]
    assert v.source == "rules" and "synthetic event has no frame" in v.reason


def test_bedrock_reply_is_parsed_with_request_id():
    from tests.bedrock_fakes import label

    class Fake:
        def converse(self, **kw):
            assert kw["messages"][0]["content"][0]["image"]["source"]["bytes"] == b"jpg"
            assert kw["toolConfig"]["toolChoice"] == {"tool": {"name": "label_door_frame"}}
            return label("delivery", 0.9, "parcel", "rid")

    e = RING.events_for_day(date(2026, 10, 7))[0]
    e.snapshot = b"jpg"
    v = BedrockClassifier(region="us-east-1", model_id="m", client=Fake()).classify(e, None)
    assert (v.visit_type, v.source, v.request_id) == (VisitType.DELIVERY, "bedrock", "rid")


def test_dashboard_and_api_render(monkeypatch, tmp_path):
    from app.ring import RingSource

    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    c = TestClient(main.app)
    page = c.get("/?date=2026-10-07").text
    assert "Demo data: these door events were made up" in page and "Every door event" in page
    assert c.get("/api/briefing?date=2026-10-07").json()["customers"] > 0
    assert c.get("/api/events?date=2026-10-07").json()
    assert c.get("/?date=nope").status_code == 400


def test_live_mode_without_token_fails_closed(monkeypatch, tmp_path):
    from dataclasses import replace

    from app.ring import RingSource

    monkeypatch.setattr(main, "source", RingSource(replace(main.settings, ring_mode="live"), tmp_path / "t"))
    c = TestClient(main.app)
    r = c.get("/")
    assert r.status_code == 503 and "NOT_CONFIGURED" in r.text and "Synthetic" not in r.text
    assert c.get("/api/briefing").json()["error"] == "NOT_CONFIGURED"


def test_pasted_token_switches_to_live_and_forget_switches_back(monkeypatch, tmp_path):
    from app.ring import RingSource
    from app.ring.api import RingApiClient
    from app.ring.synthetic import SyntheticRingClient

    src = RingSource(main.settings, tmp_path / "ring-access-token")
    monkeypatch.setattr(main, "source", src)
    c = TestClient(main.app)
    assert "Playground access token" in c.get("/connect").text
    assert isinstance(src.get(), SyntheticRingClient)
    r = c.post("/connect", data={"token": "  pasted-token  "}, follow_redirects=False)
    assert r.status_code == 303 and src.pasted_token() == "pasted-token"
    assert oct((tmp_path / "ring-access-token").stat().st_mode & 0o777) == "0o600"
    ring = src.get()
    assert isinstance(ring, RingApiClient) and ring.auth.token() == "pasted-token"
    assert "pasted-token" not in c.get("/connect").text
    c.post("/connect/forget", follow_redirects=False)
    assert isinstance(src.get(), SyntheticRingClient)


def test_connect_refuses_remote_clients():
    c = TestClient(main.app, client=("203.0.113.9", 5000))
    assert c.get("/connect").status_code == 403
    assert c.post("/connect", data={"token": "x"}).status_code == 403


# ---- closed-time doorbell: a guess stays a guess, staff in means not missed ----------------------

def _v(hh, mm, kind, vt, source, src="ring", day=date(2026, 10, 7)):
    from datetime import datetime

    from app.models import ClassifiedVisit, DoorEvent
    e = DoorEvent(f"{kind}-{hh:02d}{mm:02d}", "d", datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ), kind, src)
    return ClassifiedVisit(e, vt, 0.9, "why", source)


class _Fixed:
    """A Ring source that returns a fixed day; the classifier labels by a lookup."""
    name, tz = "fixed", TZ

    def __init__(self, visits):
        self.visits = visits

    def events_for_day(self, day):
        return [v.event for v in self.visits if v.event.timestamp.date() == day]


class _Lookup:
    name = "lookup"

    def __init__(self, visits):
        self.by_id = {v.event.id: v for v in visits}

    def classify(self, e, open_hours, **kw):
        return self.by_id[e.id]


def _brief(visits, day=date(2026, 10, 7)):
    return build_briefing(_Fixed(visits), _Lookup(visits), day, "Test Shop", HOURS)


def test_rules_press_while_closed_is_a_doorbell_press_not_a_missed_delivery():
    b = _brief([_v(19, 30, "ding", VisitType.DELIVERY, "rules")])
    assert b.deliveries_while_closed == [] and len(b.presses_while_closed) == 1
    assert b.presses_while_closed[0]["what"] == "Doorbell pressed while closed"
    line = next(x for x in b.headline if "19:30" in x)
    assert line.startswith("Doorbell pressed while closed, at 19:30") and "courier" not in line.split(";")[0]
    assert "delivery attempt" not in " ".join(b.headline) and "missed" not in " ".join(b.headline)


def test_confirmed_courier_while_closed_is_flagged():
    b = _brief([_v(19, 30, "motion", VisitType.DELIVERY, "ring")])
    assert [m["time"] for m in b.deliveries_while_closed] == ["19:30"] and b.presses_while_closed == []
    assert b.deliveries_while_closed[0]["what"] == "Courier at the door while closed"
    assert "1 courier at the door while the shop was closed, at 19:30." in b.headline


def test_staff_at_the_door_before_opening_suppresses_the_closed_flag():
    """The bakery: staff in from 07:45, a courier at 07:47, opening at 08:00."""
    b = _brief([_v(7, 45, "motion", VisitType.STAFF, "bedrock"), _v(7, 47, "ding", VisitType.DELIVERY, "bedrock")])
    assert b.deliveries_while_closed == [] and b.presses_while_closed == []
    assert not any("closed" in x for x in b.headline)


def test_bedrock_without_a_frame_names_a_replayed_webhook_as_replayed():
    from app.classify.bedrock import BedrockClassifier

    class NoCall:
        def converse(self, **kw):
            raise AssertionError("must not call Bedrock without a frame")

    vs = [_v(9, 0, "ding", VisitType.DELIVERY, "rules", src="ring-webhook-replay"),
          _v(9, 5, "ding", VisitType.DELIVERY, "rules", src="ring-webhook")]
    clf = BedrockClassifier(region="us-east-1", model_id="m", client=NoCall())
    out = classify_day(_Fixed(vs), clf, date(2026, 10, 7), HOURS)
    reasons = {v.event.source: v.reason for v in out}
    assert "replayed Ring event, no frame" in reasons["ring-webhook-replay"]
    assert "Ring webhook event, no frame downloaded" in reasons["ring-webhook"]
    assert not any("synthetic" in r for r in reasons.values())
