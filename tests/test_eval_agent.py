"""The agent answer check: question set built from shop data, and the scorer catches wrong and
unsupported answers. Fake agents only, never Bedrock."""

from datetime import date

from app import agent as shop_agent
from app.deliveries import DeliveryBook
from scripts import eval_agent as ea

DAY = date(2026, 10, 7)


def test_claims_pulls_times_dates_and_numbers():
    c = ea.claims("Two couriers came, at 9:05 and 19:30 on 2026-10-07; 3 rang.\nFrame: /frames/ev-1930.jpg")
    assert sorted(c) == sorted(["2026-10-07", "09:05", "19:30", "3", "2"])
    assert ea.claims("No one came.") == []


def test_support_counts_what_the_agent_could_compute():
    s = ea.support([{"visits": [{"type": "delivery", "time": "09:05", "shop_open": True},
                                {"type": "delivery", "time": "19:30", "shop_open": False},
                                {"type": "customer", "time": "12:00", "shop_open": True}]}])
    assert {"09:05", "19:30", "3", "2", "1"} <= s


def test_unsupported_catches_an_invented_time():
    results = [{"visits": [{"type": "delivery", "time": "09:05"}]}]
    assert ea.unsupported("A courier rang at 09:05.", results, DAY) == []
    assert ea.unsupported("A courier rang at 09:15.", results, DAY) == ["09:15"]


def _with_a_ring_courier(main, monkeypatch):
    """The synthetic day, plus one courier Ring's package_delivery alert named: rule-guessed presses are not
    couriers, so without it the day has nothing to ask "did it come?" about."""
    from datetime import datetime, time

    from app.models import ClassifiedVisit, DoorEvent, VisitType
    synthetic = main._visits

    def visits(d):
        out = synthetic(d)
        if d == DAY:
            e = DoorEvent("ring-pkg", "d", datetime.combine(d, time(10, 15), tzinfo=main.settings.tz), "motion",
                          "ring-webhook", "package_delivery")
            out = sorted(out + [ClassifiedVisit(e, VisitType.DELIVERY, 1.0, "Ring Smart Alert: package_delivery",
                                                "ring")], key=lambda v: v.event.timestamp)
        return out
    monkeypatch.setattr(main, "_visits", visits)


def _cases_and_tools(isolated_main):
    tools = isolated_main._agent_tools(date(2026, 10, 8))
    ea.seed_book(isolated_main.book, DAY, tools.door_visits(DAY.isoformat())["visits"])
    return ea.build_cases(tools, DAY), tools


def _fake(answers):
    """An agent that calls door_visits and deliveries for the day, then gives a canned answer."""
    def factory(tools, trace, shop, region, model_id):
        def run(q):
            for name, fn in (("door_visits", tools.door_visits), ("deliveries", tools.deliveries)):
                trace.tools.append({"tool": name, "args": {"day": DAY.isoformat()}})
                trace.results.append(fn(DAY.isoformat()))
            if "attempted" in q:
                at = q.split("delivery at ")[1][:5]
                trace.tools.append({"tool": "check_attempt", "args": {"day": DAY.isoformat(), "at": at}})
                trace.results.append(tools.check_attempt(DAY.isoformat(), at))
            trace.bedrock_request_ids.append("rid-x")
            return answers(q)
        return run
    return factory


def _honest(cases):
    by_q = {}
    for c in cases:
        if c.kind == "yes_time":
            by_q[c.question] = f"Yes, a courier was at the door at {c.expect}."
        elif c.kind == "no":
            by_q[c.question] = "No courier was seen for that delivery."
        elif c.kind == "count":
            by_q[c.question] = f"{c.expect} couriers."
        elif c.kind == "time":
            by_q[c.question] = f"The first courier rang at {c.expect}."
        elif c.kind == "attempt":
            o, t = c.expect
            by_q[c.question] = f"Yes, a courier was at the door at {t}." if o == "courier_at_door" else \
                "The doorbell recorded nothing near then, which doesn't prove nobody came."
        else:
            by_q[c.question] = "I can't tell: Ring has no door-answered signal."
    return by_q


def test_honest_agent_scores_clean(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "e.json"))
    _with_a_ring_courier(main, monkeypatch)
    cases, tools = _cases_and_tools(main)
    assert {c.kind for c in cases} >= {"yes_time", "no", "count", "attempt", "refuse"}
    honest = _honest(cases)
    ask = lambda q: shop_agent.ask(q, tools, "Shop", "r", "m", agent_factory=_fake(honest.get), keep_results=True)
    results = ea.run_cases(cases, ask, date(2026, 10, 8))
    assert [r.case.id for r in results if not r.correct or r.unsupported] == []
    assert "**Correct:** {0} of {0}".format(len(cases)) in ea.report_md(results, "m", DAY)


def test_a_wrong_or_invented_answer_is_caught(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "e.json"))
    _with_a_ring_courier(main, monkeypatch)
    cases, tools = _cases_and_tools(main)
    lying = {c.question: "Yes, the courier handed the parcel to your staff at 04:44." for c in cases}
    ask = lambda q: shop_agent.ask(q, tools, "Shop", "r", "m", agent_factory=_fake(lying.get), keep_results=True)
    results = ea.run_cases(cases, ask, date(2026, 10, 8))
    assert all("04:44" in r.unsupported for r in results)
    assert not any(r.correct for r in results if r.case.kind in ("refuse", "no", "yes_time"))
