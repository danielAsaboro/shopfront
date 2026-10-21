"""Ask Shopfront's tools hand the model facts with where each came from, so a guess can't pass as a sighting."""

from datetime import date, datetime

from app import agent as shop_agent
from app.agent import PROVENANCE, SYSTEM, ShopTools, unstamped

DAY = date(2026, 10, 7)


def row(t, labelled_by, source, kind="motion", type_="delivery"):
    return {"time": t, "ring_event": kind, "event_source": source, "type": type_, "labelled_by": labelled_by,
            "confidence": 0.8, "reason": "r", "shop_open": True, "frame_url": None}


VISITS = [
    row("10:12", "ring", "ring-webhook"),                      # Ring's own package_delivery alert
    row("10:30", "ring", "ring"),                               # the same alert from Event History
    row("11:00", "bedrock", "ring", type_="customer"),          # Bedrock read the Ring frame
    row("11:20", "bedrock", "ring-live-abc", type_="customer"),  # Bedrock read a browser-captured live frame
    row("12:00", "rules", "ring", kind="ding"),                 # a guess about a real Ring press
    row("12:40", "ring", "ring-webhook-replay"),                # replayed by Shopfront's feeder
    row("13:00", "rules", "synthetic", type_="customer"),       # made up by the demo generator
    row("13:30", "crystal_ball", "ring"),                       # nobody knows: a guess, never a sighting
]
WANT = ["ring", "ring", "bedrock", "bedrock", "rules", "replay", "synthetic", "rules"]


def tools(visits=VISITS):
    receipts = {"expected": [{"supplier": "Hillside Mill", "order_ref": "PO-1042", "status": "seen",
                              "visit": visits[0], "possible": [visits[4]]}],
                "unexpected": [visits[2]], "unconfirmed": [visits[4]], "seen": 1, "total": 1}
    attempt = {"outcome": "courier_came_later", "text": "t", "visit": visits[5], "activity": {"motion": 3}}
    pack = {"finding": {"text": "f", "visit": visits[6]}, "order": {"supplier": "Hillside Mill"},
            "frame": None, "draft": None, "no_draft_reason": "demo", "limits": [], "pack_url": "/dispute"}
    return ShopTools(lambda d: [dict(v) for v in visits], lambda d: receipts, lambda d: "08:00-18:00", DAY,
                     attempt_checker=lambda *a: attempt, pack_builder=lambda *a: pack)


def test_every_visit_says_where_it_came_from_and_unknown_is_a_guess():
    out = tools().door_visits("2026-10-07")
    assert [v["provenance"] for v in out["visits"]] == WANT
    assert all(v["said_by"] for v in out["visits"])
    assert out["visits"][4]["said_by"] == "a guess from the time of day"
    assert out["visits"][5]["said_by"].startswith("a replayed Ring package_delivery alert, not a Ring notification")
    assert out["visits"][6]["said_by"] == "a synthetic demo event, no camera"
    assert out["posted_hours_provenance"] == "owner"
    assert unstamped(out) == []


def test_every_tool_result_carries_provenance_all_the_way_down():
    t = tools()
    results = [t.door_visits("today"), t.deliveries("2026-10-07"), t.week("2026-10-07"),
               t.check_attempt("2026-10-07", "12:40", "business_closed"),
               t.dispute_pack("2026-10-07", at="12:40", claim="business_closed")]
    for r in results:
        assert unstamped(r) == [], r
    deliveries, week, attempt, pack = results[1:]
    assert deliveries["expected"][0]["provenance"] == "owner"  # the order is the owner's
    assert deliveries["expected"][0]["visit"]["provenance"] == "ring"  # the courier is Ring's
    assert deliveries["expected"][0]["possible"][0]["provenance"] == "rules"
    assert deliveries["unconfirmed"][0]["provenance"] == "rules"
    assert attempt["visit"]["provenance"] == "replay" and attempt["scan_provenance"] == "owner"
    assert pack["finding"]["visit"]["provenance"] == "synthetic" and pack["order"]["provenance"] == "owner"
    day = week[-1]
    assert day["delivery"] == 5 and day["by_provenance"]["delivery"] == {"ring": 2, "rules": 2, "replay": 1}
    assert day["by_provenance"]["customer"] == {"bedrock": 2, "synthetic": 1}
    assert set().union(*(d["by_provenance"][t] for d in week for t in d["by_provenance"])) <= set(PROVENANCE)


def test_the_real_tools_over_demo_data_never_present_synthetic_events_as_seen():  # conftest isolates main
    import app.main as main

    t = main._agent_tools(datetime.now(main.settings.tz).date())
    out = t.door_visits("2026-10-07")
    assert out["visits"] and unstamped(out) == []
    assert {v["provenance"] for v in out["visits"]} == {"synthetic"}
    assert unstamped(t.week("2026-10-07")) == [] and unstamped(t.deliveries("2026-10-07")) == []


def test_the_agent_is_told_what_each_provenance_means():
    for p in ("\"ring\"", "\"bedrock\"", "\"rules\"", "\"replay\"", "\"synthetic\"", "\"owner\""):
        assert p in SYSTEM
    assert "never state" in SYSTEM and "a guess" in SYSTEM


def test_a_tool_error_is_not_a_fact():
    assert shop_agent.stamp({"error": "that date is in the future"}) == {"error": "that date is in the future"}
