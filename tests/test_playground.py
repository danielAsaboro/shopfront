"""Ring Developer Playground mode: each scene (an on_demand event) is a door visit labelled exactly
"Playground scene (Ring simulator)", provenance ring-playground, through the check, the pack and the agent."""

import io
import json
import zipfile
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main
from app import agent, dispute, pages, provenance
from app import reconcile as rc
from app.bedrock_budget import HELD_REASON, Focus
from app.briefing import classify_day, playground_mode
from app.cache import ClassificationCache
from app.classify.bedrock import BedrockClassifier
from app.classify.rules import RulesClassifier
from app.models import DoorEvent, VisitType
from app.ring.api import RingApiClient, RingError, RingStatus
from tests.bedrock_fakes import label

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
HOURS = {i: (8, 18) for i in range(7)}
LABEL = "Playground scene (Ring simulator)"


def at(h, m=0, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=TZ)


def scene(h, m=0):
    return DoorEvent(id=f"od-{h:02d}{m:02d}", device_id="dev", timestamp=at(h, m), kind="on_demand", source="ring")


class Playground(RingApiClient):
    """A Playground token's Event History: on_demand events only, and their frames. No HTTP."""

    device_id = "dev"

    def __init__(self, events, mode="playground", frame=b"\xff\xd8jpeg"):
        self.tz = TZ
        self.auth = SimpleNamespace(mode=mode)
        self.events = events
        self.frame = frame

    def account_id(self):
        return None

    def events_for_day(self, day):
        return [e for e in self.events if e.timestamp.date() == day]

    def snapshot(self, e, retry_for=0.0):
        if self.frame is None:
            raise RingError(RingStatus.MEDIA_NOT_FOUND, "nothing recorded")
        return self.frame


class Bedrock:
    def __init__(self, kind="delivery"):
        self.kind, self.calls = kind, 0

    def converse(self, **kw):
        self.calls += 1
        return label(self.kind, 0.9, "a person holding a parcel", request_id=f"rid-{self.calls}")


def test_the_label_is_exact():
    assert provenance.PLAYGROUND == LABEL and provenance.PLAYGROUND_SOURCE == "ring-playground"


def test_playground_mode_from_the_token_or_the_setting():
    assert playground_mode(Playground([]))  # a fixed access token is what the Playground hands out
    assert not playground_mode(Playground([], mode="refresh"))
    assert playground_mode(Playground([], mode="refresh"), "1") and not playground_mode(Playground([]), "0")
    assert not playground_mode(SimpleNamespace(auth=SimpleNamespace(mode="playground")), "1")  # never synthetic


def test_scenes_are_door_visits_only_in_playground_mode():
    ring = Playground([scene(10, 5), scene(11, 30)])
    stats: dict = {}
    vs = classify_day(ring, RulesClassifier(), DAY, HOURS, stats=stats, playground=True)
    assert len(vs) == 2 and stats["playground_scenes"] == 2 and stats["ring_live_views"] == 0
    for v in vs:
        assert v.source == "ring-playground" and v.event.source == "ring-playground"
        assert v.visit_type == VisitType.UNKNOWN and not rc.confirmed(v)  # never a customer or courier by rules
        assert pages.visit_label(v.visit_type.value, False, v.event.source) == LABEL
        assert pages.badge(v.source, v.event.source) == "playground"
        assert pages.how(v.source, v.reason, v.event.source).startswith(LABEL)
    assert LABEL in pages.labelled_by_text(stats, "rules")
    # outside Playground mode an on_demand event stays a live view, never a visitor
    stats = {}
    assert classify_day(ring, RulesClassifier(), DAY, HOURS, stats=stats) == [] and stats["ring_live_views"] == 2


def test_only_bedrock_reading_the_frame_says_who(tmp_path):
    ring = Playground([scene(10, 5), scene(14, 0)])
    bedrock = Bedrock("delivery")
    clf = BedrockClassifier(region="us-east-1", model_id="m", client=bedrock)
    focus = Focus(TZ, lambda d: [], lambda: [at(10, 0)], lambda d: False)
    stats: dict = {}
    by = {v.event.id: v for v in classify_day(ring, clf, DAY, HOURS, ClassificationCache(tmp_path / "c.json"),
                                              stats=stats, focus=focus, playground=True)}
    seen, held = by["od-1005"], by["od-1400"]
    assert seen.source == "bedrock" and seen.visit_type == VisitType.DELIVERY and seen.request_id == "rid-1"
    assert seen.event.source == "ring-playground" and rc.confirmed(seen)
    assert pages.visit_label("delivery", True, "ring-playground") == f"delivery ({LABEL})"
    assert provenance.of({"labelled_by": "bedrock", "event_source": "ring-playground"}) == provenance.BEDROCK_PLAYGROUND
    assert agent.visit_provenance({"labelled_by": "bedrock", "event_source": "ring-playground"}) == "ring-playground"
    assert held.source == "ring-playground" and HELD_REASON in held.reason and bedrock.calls == 1
    assert stats["held_from_bedrock"] == 1


def test_no_frame_or_no_answer_is_never_the_rules_guess(tmp_path):
    for ring, bedrock in ((Playground([scene(12, 0)], frame=None), Bedrock()),
                          (Playground([scene(12, 0)]), Bedrock("not-a-type"))):
        clf = BedrockClassifier(region="us-east-1", model_id="m", client=bedrock)
        [v] = classify_day(ring, clf, DAY, HOURS, playground=True)
        assert v.source == "ring-playground" and v.visit_type == VisitType.UNKNOWN


def test_a_scene_flows_through_the_check_the_pack_and_the_agent(monkeypatch):
    ring = Playground([scene(12, 38)])
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring})())
    c = TestClient(main.app)
    check = c.get("/api/attempt", params={"at": "2026-10-14T12:40", "claim": "business_closed"}).json()
    assert check["outcome"] == rc.UNVERIFIED and f"a {LABEL} at 12:38" in check["text"]
    assert check["visit"]["event_source"] == "ring-playground" and check["visit"]["labelled_by"] == "ring-playground"
    assert check["activity"]["events"] == 1 and check["activity"]["customers"] == 0

    r = c.get("/dispute.zip", params={"date": "2026-10-14", "at": "12:40", "claim": "business_closed"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    pack = json.loads(z.read("pack.json"))
    assert pack["playground"] and dispute.PLAYGROUND_MARK in pack["watermark"]
    assert dispute.PLAYGROUND_LIMIT in pack["limits"] and pack["event_sources"] == ["ring-playground"]
    readme = z.read("README.txt").decode()
    assert "ring-playground" in readme and LABEL in readme
    for d in pack["drafts"]:
        assert d["body"].startswith(dispute.PLAYGROUND_MARK) and dispute.CAMERA_SHOWS not in d["body"]
    page = c.get("/dispute", params={"date": "2026-10-14", "at": "12:40", "claim": "business_closed"}).text
    assert "Ring Developer Playground scenes: don't send this pack as evidence" in page and LABEL in page

    row = check["visit"]
    assert agent.stamp(row)["provenance"] == "ring-playground" and LABEL in agent.stamp(row)["said_by"]
