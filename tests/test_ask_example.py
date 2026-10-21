"""Ask Shopfront when Bedrock can't answer: no question box that can only fail, and an example answer that is
honestly what the fake agent produces over the same tools (never presented as live)."""

from datetime import date, datetime

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi.testclient import TestClient

from app import main, pages
from app.ask_probe import AskProbe
from app.classify.rules import RulesClassifier
from app.deliveries import DeliveryBook, FrameStore
from app.ring import RingSource
from app.ring.webhooks import WebhookLog
from app.shop import ShopProfile
from tests.test_pages import ring_courier

DAY = date(2026, 10, 7)  # a Wednesday, open 8-18


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "profile", ShopProfile(tmp_path / "shop.json", "Corner Bakery", ""))
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "expected.json"))
    monkeypatch.setattr(main, "frames", FrameStore(tmp_path))
    monkeypatch.setattr(main, "webhook_log", WebhookLog(tmp_path / "hooks.jsonl"))
    monkeypatch.setattr(main, "_aws_creds_available", lambda region: True)
    return TestClient(main.app)


def _error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def raising(exc):
    calls = []

    def call(region, model_id):
        calls.append(1)
        raise exc
    call.calls = calls
    return call


def test_probe_reads_how_bedrock_refuses_a_blank_message():
    ok = raising(_error("ValidationException", "The text field in the ContentBlock object at messages.0.content.0 is blank."))
    p = AskProbe("us-east-1", "m", call=ok)
    assert p.state() is None and p.state() is None and len(ok.calls) == 1  # checked once, then kept
    blocked = _error("403", 'request blocked: no rule allows host "bedrock-runtime.us-east-1.amazonaws.com"')
    assert AskProbe("r", "m", call=raising(blocked)).state() == "BEDROCK_UNREACHABLE"
    assert AskProbe("r", "m", call=raising(NoCredentialsError())).state() == "NO_AWS_SIGN_IN"
    no_model = _error("ValidationException", "The provided model identifier is invalid.")
    assert AskProbe("r", "m", call=raising(no_model)).state() == "NO_MODEL_ACCESS"
    assert AskProbe("r", "m", call=lambda r, m: None).state() is None
    p.mark("BEDROCK_BUSY")  # passes in a minute: keep offering the box
    assert p.state() is None
    p.mark("BEDROCK_UNREACHABLE")
    assert p.state() == "BEDROCK_UNREACHABLE"


def test_no_question_box_when_bedrock_is_unreachable(c, monkeypatch):
    monkeypatch.setattr(main, "ask_probe", AskProbe("r", "m", call=raising(_error("403", "request blocked"))))
    main.book.add(date(2026, 10, 7), "Bay Mills")  # the example is about orders; a fresh install has none
    page = c.get("/?date=2026-10-07").text
    assert 'id="ask"' not in page and 'name="q"' not in page
    assert "Ask needs an AWS sign-in with Bedrock access" in page
    assert "Example answer, recorded from the test fakes, not live" in page
    assert pages.ASK_EXAMPLE["answer"].replace("'", "&#39;") in page and "BEDROCK_UNREACHABLE" in page


def test_question_box_when_bedrock_answers(c, monkeypatch):
    ok = _error("ValidationException", "The text field is blank.")
    monkeypatch.setattr(main, "ask_probe", AskProbe("r", "m", call=raising(ok)))
    page = c.get("/?date=2026-10-07").text
    assert 'id="ask"' in page and "Example answer" not in page


def test_a_lasting_failure_takes_the_box_away(c, monkeypatch):
    monkeypatch.setattr(main, "ask_probe", AskProbe("r", "m", call=lambda r, m: None))
    assert 'id="ask"' in c.get("/?date=2026-10-07").text

    def broken(*a):
        def run(q):
            raise _error("403", "request blocked: no rule allows host")
        return run
    monkeypatch.setattr(main, "agent_factory", broken)
    assert c.post("/api/ask", json={"question": "did the flour come?"}).json()["reason"] == "BEDROCK_UNREACHABLE"
    assert 'id="ask"' not in c.get("/?date=2026-10-07").text


def test_the_example_is_what_the_fake_agent_answers_from_the_tools(c, monkeypatch):
    """The recording behind pages.ASK_EXAMPLE: the demo's flour order and Ring's 10:12 package alert, the fake
    agent reading the real deliveries tool, the answer built only from what the tool returned."""
    main.book.add(DAY, "Hillside Mill", "10:00", "11:00", order_ref="PO-1041")
    ring_courier(main.webhook_log, datetime(2026, 10, 7, 10, 12, tzinfo=main.settings.tz))

    def factory(tools, trace, shop, region, model_id):
        def run(question):
            trace.tools.append({"tool": "deliveries", "args": {"day": "yesterday"}})
            x = next(r for r in tools.deliveries("yesterday")["expected"] if r["visit"])
            assert x["visit"]["labelled_by"] == "ring"
            return (f"Yes. Ring's package alert put a courier at the door at {x['visit']['time']}, and Shopfront "
                    f"matched it to {x['supplier']}'s order {x['order_ref']}. Ring has no door-answered signal, so "
                    f"this doesn't show the sacks were handed over.")
        return run
    monkeypatch.setattr(main, "agent_factory", factory)
    monkeypatch.setattr(main, "_agent_tools", lambda today, real=main._agent_tools: real(date(2026, 10, 8)))
    d = c.post("/api/ask", json={"question": pages.ASK_EXAMPLE["question"]}).json()
    assert d["answer"] == pages.ASK_EXAMPLE["answer"]
    assert pages.ASK_EXAMPLE["tools"] == "deliveries(" + d["tools"][0]["args"]["day"] + ")"
