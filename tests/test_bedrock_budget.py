"""Bedrock only where it matters (app/bedrock_budget.py), and what it cost."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main
from app import bedrock_budget as bb
from app.briefing import classify_day
from app.cache import ClassificationCache
from app.classify.bedrock import BedrockClassifier
from app.deliveries import make_expected
from app.models import DoorEvent
from app.ring.api import RingApiClient
from tests.bedrock_fakes import label

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
HOURS = {i: (8, 18) for i in range(7)}


def at(h, m=0, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def ev(h, m=0, kind="motion", d=DAY):
    return DoorEvent(id=f"e-{d}-{h:02d}{m:02d}", device_id="dev", timestamp=at(h, m, d), kind=kind, source="ring")


class FakeRing(RingApiClient):
    """Event History and frames, no HTTP."""

    def __init__(self, events):
        self.tz = TZ
        self.events = events
        self.frames_asked: list[str] = []

    def events_for_day(self, day):
        return [e for e in self.events if e.timestamp.date() == day]

    def snapshot(self, e):
        self.frames_asked.append(e.id)
        return b"\xff\xd8jpeg"


class FakeBedrock:
    def __init__(self):
        self.calls = 0

    def converse(self, **kw):
        self.calls += 1
        return label("delivery", 0.9, "courier with a parcel", request_id=f"rid-{self.calls}")


def order(start=None, end=None, ref="PO-1", d=DAY):
    return make_expected(d, "Bay Mills", start or "", end or "", order_ref=ref)


# --- windows -------------------------------------------------------------------------------------------
def test_windows_cover_orders_scans_and_owner_requests():
    spans = bb.windows(DAY, [order("10:00", "11:00")], [at(12, 40)], TZ)
    assert (at(9, 50), at(11, 10)) == spans[0][:2] and "PO-1's delivery window 10:00–11:00" in spans[0][2]
    assert (at(12, 30), at(12, 50)) == spans[1][:2] and "12:40 scan" in spans[1][2]
    whole = bb.windows(DAY, [order()], [], TZ)[0]
    assert whole[:2] == (at(0), at(0, d=DAY + timedelta(days=1))) and "no window" in whole[2]
    assert bb.windows(DAY, [], [], TZ) == []
    assert "owner asked" in bb.windows(DAY, [], [], TZ, asked=True)[0][2]
    # a scan five minutes after midnight reaches back into the day before
    late = bb.windows(DAY, [], [at(0, 5, DAY + timedelta(days=1))], TZ)
    assert late and late[0][0] == at(23, 55)


def test_focus_says_why_or_none():
    f = bb.Focus(TZ, lambda d: [order("10:00", "11:00")] if d == DAY else [], lambda: [at(15, 0)], lambda d: False)
    assert "window" in f(ev(9, 55)) and "scan" in f(ev(15, 9))
    assert f(ev(9, 49)) is None and f(ev(13, 0)) is None and f(ev(10, 30, d=DAY - timedelta(days=7))) is None


# --- classify_day ---------------------------------------------------------------------------------------
def test_only_focused_events_reach_bedrock_and_the_rest_cost_no_frame(tmp_path):
    ring = FakeRing([ev(7, 30), ev(10, 15), ev(12, 38), ev(14, 0, "ding"), ev(16, 45)])
    bedrock = FakeBedrock()
    clf = BedrockClassifier(region="us-east-1", model_id="m", client=bedrock, record_path=tmp_path / "calls.jsonl")
    focus = bb.Focus(TZ, lambda d: [order("10:00", "11:00")], lambda: [at(12, 40)], lambda d: False)
    stats: dict = {}
    vs = classify_day(ring, clf, DAY, HOURS, ClassificationCache(tmp_path / "c.json"), stats=stats, focus=focus)
    by = {v.event.id: v for v in vs}
    assert by["e-2026-10-14-1015"].source == "bedrock" and by["e-2026-10-14-1238"].source == "bedrock"
    for held in ("0730", "1400", "1645"):
        v = by[f"e-2026-10-14-{held}"]
        assert v.source == "rules" and bb.HELD_REASON in v.reason
    assert bedrock.calls == 2 and sorted(ring.frames_asked) == ["e-2026-10-14-1015", "e-2026-10-14-1238"]
    assert stats["reached_bedrock"] == 2 and stats["held_from_bedrock"] == 3
    rows = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert {r["event_day"] for r in rows} == {"2026-10-14"} and all(r["usage"]["inputTokens"] for r in rows)


def test_without_focus_every_event_may_go_and_cached_labels_are_kept(tmp_path):
    ring = FakeRing([ev(7, 30), ev(16, 45)])
    bedrock = FakeBedrock()
    clf = BedrockClassifier(region="us-east-1", model_id="m", client=bedrock)
    cache = ClassificationCache(tmp_path / "c.json")
    classify_day(ring, clf, DAY, HOURS, cache)
    assert bedrock.calls == 2
    nothing = bb.Focus(TZ, lambda d: [], lambda: [], lambda d: False)
    vs = classify_day(ring, clf, DAY, HOURS, cache, focus=nothing)  # already paid for: still Bedrock's labels
    assert bedrock.calls == 2 and all(v.source == "bedrock" for v in vs)


# --- cost ---------------------------------------------------------------------------------------------
def test_day_cost_sums_converse_usage_by_event_day(tmp_path):
    p = tmp_path / "calls.jsonl"
    rows = [{"event_day": "2026-10-14", "usage": {"inputTokens": 1_500, "outputTokens": 60}},
            {"event_day": "2026-10-14", "usage": {"inputTokens": 1_500, "outputTokens": 40}},
            {"event_day": "2026-10-13", "usage": {"inputTokens": 9_999, "outputTokens": 999}},
            {"at": "2026-10-14T19:00:00+00:00", "usage": {"inputTokens": 1_000, "outputTokens": 0}},  # older row
            {"event_day": "2026-10-14", "error": "ThrottlingException"}]  # a failed call: counted, no tokens
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    c = bb.day_cost(p, DAY, 1.0, 5.0, TZ)
    assert c == {"calls": 4, "input_tokens": 4_000, "output_tokens": 100, "usd": round(4_000 / 1e6 + 500 / 1e6, 4)}
    line = bb.cost_line(c, verified=False)
    assert "4 calls" in line and "4,000 tokens in" in line and "price not verified" in line
    assert "price not verified" not in bb.cost_line(c, verified=True)
    assert "no frame-labelling calls" in bb.cost_line(bb.day_cost(p, date(2026, 1, 1), 1, 5, TZ), False)


# --- the app ------------------------------------------------------------------------------------------
def test_footer_shows_the_cost_and_the_owner_can_ask_for_the_rest(monkeypatch):
    calls = main.DATA / "bedrock-calls.jsonl"
    calls.parent.mkdir(parents=True, exist_ok=True)
    calls.write_text(json.dumps({"event_day": "2026-10-14", "usage": {"inputTokens": 2000, "outputTokens": 80}}) + "\n")
    c = TestClient(main.app)
    page = c.get("/", params={"date": "2026-10-14"}).text
    assert "Bedrock: 1 call, 2,000 tokens in, 80 out, about $0.0024 (price not verified" in page

    r = c.post("/bedrock/label", data={"date": "2026-10-14"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?date=2026-10-14#events"
    assert main.label_requests.asked(DAY)
    evil = c.post("/bedrock/label", data={"date": "2026-10-15"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and not main.label_requests.asked(date(2026, 10, 15))


def test_checking_a_scan_focuses_bedrock_on_it(monkeypatch):
    seen = []

    def spy(ring, clf, day, hours, *a, focus=None, **kw):  # asked while the request runs, as classify_day does
        seen.append((bool(focus and focus(ev(12, 45))), bool(focus and focus(ev(9, 0)))))
        return []
    monkeypatch.setattr(main, "classify_day", spy)
    TestClient(main.app).get("/api/attempt", params={"at": "2026-10-14T12:40", "claim": "business_closed"})
    assert seen and all(near and not far for near, far in seen)
