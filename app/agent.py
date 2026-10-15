"""Ask Shopfront: a Strands agent on Amazon Bedrock with tools over the shop's door data.

The agent can only see what the tools return (Ring door visits and their labels, the expected
deliveries and receipts, posted hours), and it is told to answer from those and nothing else.
Every tool call and every Bedrock RequestId is returned with the answer, so the page can show
exactly what the answer rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

SYSTEM = """You are Shopfront, the front-door assistant for {shop}, a small shop. Today is {today} ({weekday}).
You answer the owner's questions about what happened at the shop's door, using only the tools.
- Call tools for every fact. Never guess times, counts or suppliers.
- Labels "by bedrock" came from Amazon Bedrock looking at the Ring frame. Labels "by rules" are guesses
  from time of day and event kind; say so when an answer depends on one.
- Ring has no "door answered" signal. Say a courier was at the door, never that a parcel was received.
- When the owner quotes a carrier's "attempted delivery" time, call check_attempt and give its text; don't
  soften or strengthen it.
- When a visit has a frame_url, end your answer with it on its own line as: Frame: <frame_url>
- If the data doesn't answer the question, say what is missing in one sentence.
Answer in two or three short sentences, plain English, times as HH:MM."""


class AgentUnavailable(Exception):
    pass


@dataclass
class Trace:
    tools: list[dict] = field(default_factory=list)
    bedrock_request_ids: list[str] = field(default_factory=list)


class ShopTools:
    """Plain functions over the app's data; agent tools wrap these and record each call."""

    def __init__(self, visits_for_day: Callable[[date], list[dict]], receipts_for_day: Callable[[date], dict],
                 hours_for_day: Callable[[date], str], today: date,
                 attempt_checker: Callable[[datetime], dict] | None = None):
        self._visits, self._receipts, self._hours, self.today = visits_for_day, receipts_for_day, hours_for_day, today
        self._attempt = attempt_checker

    def _day(self, value: str) -> date:
        value = (value or "").strip().lower()
        if value in ("", "today"):
            return self.today
        if value == "yesterday":
            return self.today - timedelta(days=1)
        d = date.fromisoformat(value)
        if d > self.today:
            raise ValueError("that date is in the future")
        return d

    def door_visits(self, day: str) -> dict:
        d = self._day(day)
        return {"date": d.isoformat(), "posted_hours": self._hours(d), "visits": self._visits(d)}

    def deliveries(self, day: str) -> dict:
        d = self._day(day)
        return {"date": d.isoformat(), **self._receipts(d)}

    def check_attempt(self, day: str, at: str) -> dict:
        if not self._attempt:
            raise ValueError("attempt checks aren't available")
        d = self._day(day)
        t = datetime.strptime(at.strip(), "%H:%M").time()
        return self._attempt(datetime.combine(d, t))

    def week(self, end_day: str) -> list[dict]:
        end = self._day(end_day)
        out = []
        for i in range(6, -1, -1):
            d = end - timedelta(days=i)
            v = self._visits(d)
            out.append({"date": d.isoformat(), "weekday": d.strftime("%a"), "posted_hours": self._hours(d),
                        **{t: sum(1 for x in v if x["type"] == t)
                           for t in ("customer", "delivery", "staff", "after_hours")}})
        return out


def build_agent(tools: ShopTools, trace: Trace, shop: str, region: str, model_id: str):
    try:
        from strands import Agent, tool
        from strands.models import BedrockModel
    except ImportError as exc:  # pragma: no cover - requirements include it
        raise AgentUnavailable("strands-agents is not installed") from exc

    def recorded(name: str, fn, **args):
        trace.tools.append({"tool": name, "args": args})
        try:
            return fn(**args)
        except ValueError as exc:
            return {"error": str(exc)}

    @tool
    def door_visits(day: str) -> dict:
        """Every visit the Ring doorbell saw on one day, with its label, who labelled it, why, and a frame_url when Shopfront holds the Ring frame.

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("door_visits", tools.door_visits, day=day)

    @tool
    def deliveries(day: str) -> dict:
        """The deliveries the owner expected on one day, each matched to the courier the doorbell saw (or "not_seen"), plus couriers nobody expected.

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("deliveries", tools.deliveries, day=day)

    @tool
    def week(end_day: str) -> list:
        """Counts of customers, deliveries, staff and after-hours visits for the 7 days ending on end_day, with posted hours.

        Args:
            end_day: YYYY-MM-DD, "today" or "yesterday".
        """
        return recorded("week", tools.week, end_day=end_day)

    @tool
    def check_attempt(day: str, at: str) -> dict:
        """Check a carrier's "delivery attempted" time against the doorbell: was a courier at the door near then?

        Args:
            day: YYYY-MM-DD, "today" or "yesterday".
            at: the carrier's time, HH:MM in shop local time.
        """
        return recorded("check_attempt", tools.check_attempt, day=day, at=at)

    model = BedrockModel(model_id=model_id, region_name=region, temperature=0, max_tokens=400)

    def on_response(parsed=None, **_):
        rid = (parsed or {}).get("ResponseMetadata", {}).get("RequestId")
        if rid:
            trace.bedrock_request_ids.append(rid)

    model.client.meta.events.register("after-call.bedrock-runtime.*", on_response)
    today = tools.today
    return Agent(model=model, tools=[door_visits, deliveries, week, check_attempt], callback_handler=None,
                 system_prompt=SYSTEM.format(shop=shop, today=today.isoformat(), weekday=today.strftime("%A")))


def ask(question: str, tools: ShopTools, shop: str, region: str, model_id: str,
        agent_factory=build_agent) -> dict:
    question = question.strip()
    if not question:
        raise ValueError("ask a question")
    trace = Trace()
    started = datetime.now()
    agent = agent_factory(tools, trace, shop, region, model_id)
    try:
        answer = str(agent(question[:500])).strip()
    except AgentUnavailable:
        raise
    except Exception as exc:  # credentials, access to the model, throttling: name it, don't fake an answer
        raise AgentUnavailable(f"{type(exc).__name__}: {exc}"[:300]) from exc
    frame = next((ln.split(":", 1)[1].strip() for ln in answer.splitlines() if ln.startswith("Frame:")), None)
    text = "\n".join(ln for ln in answer.splitlines() if not ln.startswith("Frame:")).strip()
    return {"question": question, "answer": text, "frame_url": frame if frame and frame.startswith("/frames/") else None,
            "tools": trace.tools, "bedrock_request_ids": trace.bedrock_request_ids, "model": model_id,
            "seconds": round((datetime.now() - started).total_seconds(), 1)}
