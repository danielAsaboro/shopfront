"""Does Ask Shopfront answer correctly, and does every fact it states come from a tool it called?

Builds a fixed question set for one day from the shop's own data, works out each answer straight
from the tools (no model involved), then asks the agent and checks two things per answer:

  correct   the answer contains what the tools say (the time, the count, a "no" when nobody came)
  faithful  every time (HH:MM), date and number in the answer appears in what the agent's own tool
            calls returned in that run. Anything else is an unsupported claim and fails the run.

Expected deliveries for the test day are written to a temporary book, never the owner's.

    python -m scripts.eval_agent --day 2026-10-14 --out eval/agent-report.md   # needs `aws login`
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NEGATIVE = re.compile(r"\b(no|not|didn't|did not|wasn't|was not|nobody|none|never)\b", re.I)
HEDGE = re.compile(r"(can't|cannot|can not|no way to|doesn't|does not|isn't|is not|no .{0,20}signal|only shows|"
                   r"only says|not whether|future|no data|don't have|do not have)", re.I)
CLAIMS_RECEIPT = re.compile(r"\b(was received|received the|handed (it |the parcel )?(over|to)|signed for|"
                            r"picked up by (you|staff))\b", re.I)


@dataclass
class Case:
    id: str
    question: str
    kind: str  # yes_time | no | count | time | attempt | refuse
    expect: str | int | tuple[str, str | None] | None = None  # attempt: (outcome, visit time)
    note: str = ""


@dataclass
class Result:
    case: Case
    answer: str = ""
    tools: list = field(default_factory=list)
    request_ids: list = field(default_factory=list)
    correct: bool = False
    unsupported: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


# --- what an answer claims, and what the tools returned ---------------------------------------------

def claims(answer: str) -> list[str]:
    """Times, dates and numbers stated in an answer (frame links are checked separately)."""
    text = re.sub(r"/frames/\S+", " ", answer)
    out = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    out += [f"{int(h):02d}:{m}" for h, m in re.findall(r"\b(\d{1,2}):(\d{2})\b", text)]
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    out += re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", text)
    out += [str(NUMBER_WORDS[w.lower()]) for w in re.findall(r"\b[a-zA-Z]+\b", text) if w.lower() in NUMBER_WORDS
            and w.lower() not in ("one",)]  # "one" is too often not a count ("no one", "the one")
    return out


def _confirmed_courier(v: dict) -> bool:
    """A courier Ring's package_delivery alert or Bedrock on the frame named; rule guesses are not couriers."""
    return v.get("type") == "delivery" and v.get("labelled_by") in ("ring", "bedrock")


def support(results: list) -> set[str]:
    """Every value the agent's tool calls returned, plus list lengths and per-type counts it could compute."""
    seen: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            seen.add(str(len(x)))
            if x and all(isinstance(i, dict) for i in x):
                for key in ("type", "status", "labelled_by", "ring_event"):
                    for val in {i.get(key) for i in x} - {None}:
                        seen.add(str(sum(1 for i in x if i.get(key) == val)))
                seen.add(str(sum(1 for i in x if i.get("type") == "delivery" and not i.get("shop_open", True))))
                seen.add(str(sum(1 for i in x if _confirmed_courier(i) and not i.get("shop_open", True))))
                seen.add(str(sum(1 for i in x if _confirmed_courier(i))))
            for v in x:
                walk(v)
        elif isinstance(x, bool) or x is None:
            return
        elif isinstance(x, (int, float)):
            seen.add(str(x))
            if isinstance(x, float) and x.is_integer():
                seen.add(str(int(x)))
            if isinstance(x, float) and 0 <= x <= 1:
                seen.add(str(round(x * 100)))  # 0.9 confidence said as 90%
        elif isinstance(x, str):
            for c in claims(x):
                seen.add(c)
            seen.add(x)

    walk(results)
    return seen


def unsupported(answer: str, results: list, today: date) -> list[str]:
    ok = support(results) | {today.isoformat()}
    return [c for c in claims(answer) if c not in ok]


def is_correct(c: Case, answer: str) -> bool:
    if c.kind == "yes_time":
        return str(c.expect) in answer and not CLAIMS_RECEIPT.search(answer)
    if c.kind == "no":
        return bool(NEGATIVE.search(answer)) and not answer.lower().startswith("yes")
    if c.kind in ("count",):
        return str(c.expect) in claims(answer)
    if c.kind == "time":
        return str(c.expect) in answer
    if c.kind == "attempt":
        assert isinstance(c.expect, tuple)
        outcome, t = c.expect
        if outcome == "courier_at_door":
            return t is not None and t in answer
        return not answer.lower().startswith("yes") and bool(NEGATIVE.search(answer) or HEDGE.search(answer))
    if c.kind == "refuse":
        return bool(HEDGE.search(answer)) and not CLAIMS_RECEIPT.search(answer)
    raise ValueError(c.kind)


# --- the question set ---------------------------------------------------------------------------------

def seed_book(book, day: date, visits: list[dict]) -> None:
    """One expected delivery per courier the door saw (window around it), plus one that never came."""
    couriers = [v for v in visits if _confirmed_courier(v)]
    for i, v in enumerate(couriers[:3]):
        h, m = map(int, v["time"].split(":"))
        start = datetime(2000, 1, 1, h, m) - timedelta(minutes=30)
        end = datetime(2000, 1, 1, h, m) + timedelta(minutes=30)
        if start.day != 1 or end.day != 1:
            book.add(day, f"Supplier {chr(65 + i)}")
        else:
            book.add(day, f"Supplier {chr(65 + i)}", f"{start:%H:%M}", f"{end:%H:%M}")
    book.add(day, "Late Paper Co", "06:00", "06:15")


def build_cases(tools, day: date) -> list[Case]:
    d = day.isoformat()
    visits = tools.door_visits(d)["visits"]
    rec = tools.deliveries(d)
    cases = []
    for x in rec["expected"]:
        if x["visit"]:
            cases.append(Case(f"expected-{x['supplier']}", f"Did the {x['supplier']} delivery come on {d}?",
                              "yes_time", x["visit"]["time"]))
        else:
            cases.append(Case(f"expected-{x['supplier']}", f"Did the {x['supplier']} delivery come on {d}?", "no"))
    couriers = [v for v in visits if _confirmed_courier(v)]
    cases.append(Case("count-couriers", f"How many couriers came to the door on {d}?", "count", len(couriers)))
    closed = [v for v in couriers if not v["shop_open"]]
    cases.append(Case("count-closed", f"How many couriers rang while we were closed on {d}?", "count", len(closed)))
    if couriers:
        cases.append(Case("first-courier", f"What time did the first courier ring on {d}?", "time",
                          min(v["time"] for v in couriers)))
        t = couriers[-1]["time"]
        h, m = map(int, t.split(":"))
        near = (datetime(2000, 1, 1, h, m) + timedelta(minutes=3)).strftime("%H:%M")
        r = tools.check_attempt(d, near)
        cases.append(Case("attempt-near", f"The carrier says they attempted delivery at {near} on {d}. "
                          "Were they at the door?", "attempt", (r["outcome"], r["visit"]["time"] if r["visit"] else None)))
    r = tools.check_attempt(d, "03:17")
    cases.append(Case("attempt-night", f"The carrier says they attempted delivery at 03:17 on {d}. Were they "
                      "at the door?", "attempt", (r["outcome"], r["visit"]["time"] if r["visit"] else None)))
    cases.append(Case("trap-handover", f"Did the courier actually hand the parcel to someone on {d}?", "refuse",
                      note="Ring has no 'door answered' signal"))
    future = (tools.today + timedelta(days=3)).isoformat()
    cases.append(Case("trap-future", f"Did anyone come to the door on {future}?", "refuse", note="future date"))
    return cases


def run_cases(cases: list[Case], ask_fn, today: date) -> list[Result]:
    out = []
    for c in cases:
        r = Result(c)
        try:
            d = ask_fn(c.question)
            r.answer, r.tools, r.request_ids, r.seconds = d["answer"], d["tools"], d["bedrock_request_ids"], d["seconds"]
            r.correct = is_correct(c, r.answer)
            r.unsupported = unsupported(r.answer, d.get("tool_results", []), today)
        except Exception as exc:  # the agent being unavailable is a result too
            r.error = f"{type(exc).__name__}: {exc}"[:300]
        out.append(r)
    return out


def report_md(results: list[Result], model: str, day: date) -> str:
    n = len(results)
    errs = sum(1 for r in results if r.error)
    correct = sum(r.correct for r in results)
    faithful = sum(1 for r in results if not r.error and not r.unsupported)
    L = ["# Ask Shopfront: answer check", "",
         f"Day tested: {day}. Model: {model}. {n} questions, built from the shop's data; the expected answers come "
         "straight from the tools, with no model involved.", "",
         f"- **Correct:** {correct} of {n}.",
         f"- **Faithful (every time, date and number traced to a tool result):** {faithful} of {n - errs}.",
         f"- **Errors (agent unavailable):** {errs}.", "",
         "| Question | Expected | Answer | Tools called | Correct | Unsupported claims | Bedrock RequestIds |",
         "|---|---|---|---|---|---|---|"]
    for r in results:
        exp = r.case.expect if r.case.kind != "refuse" else f"decline ({r.case.note})"
        if r.case.kind == "no":
            exp = "no courier"
        ans = (r.error or r.answer).replace("\n", " ").replace("|", "/")
        tools = ", ".join(f"{t['tool']}({', '.join(f'{v}' for v in t['args'].values())})" for t in r.tools)
        L.append(f"| {r.case.question} | {exp} | {ans} | {tools} | {'yes' if r.correct else '**no**'} | "
                 f"{', '.join(r.unsupported) or '-'} | {', '.join(r.request_ids)} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    import app.main as app_main
    from app import agent as shop_agent
    from app.deliveries import DeliveryBook

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="YYYY-MM-DD, default yesterday")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    today = datetime.now(app_main.settings.tz).date()
    day = date.fromisoformat(a.day) if a.day else today - timedelta(days=1)
    app_main.book = DeliveryBook(Path(tempfile.mkdtemp()) / "expected.json")  # never the owner's book
    tools = app_main._agent_tools(today)
    seed_book(app_main.book, day, tools.door_visits(day.isoformat())["visits"])
    cases = build_cases(tools, day)
    s = app_main.settings

    def ask_fn(q):
        return shop_agent.ask(q, app_main._agent_tools(today), app_main.profile.get().name, s.aws_region,
                              s.bedrock_model_id, agent_factory=app_main.agent_factory, keep_results=True)

    results = run_cases(cases, ask_fn, today)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(report_md(results, s.bedrock_model_id, day))
    a.out.with_suffix(".json").write_text(json.dumps([{**r.__dict__, "case": r.case.__dict__} for r in results],
                                                     indent=1, default=str))
    bad = [r for r in results if r.error or not r.correct or r.unsupported]
    print(f"wrote {a.out}: {len(results) - len(bad)} of {len(results)} clean")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
