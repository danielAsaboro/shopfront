"""The frame evaluation harness: metrics checked against hand-worked numbers, and the run/score steps
against a fake Bedrock that returns real-shaped responses. Local only."""

import csv
import json

import pytest

from app.classify.bedrock import BedrockClassifier
from scripts import eval_frames as ev


def test_kappa_matches_a_hand_worked_example():
    # 50 items: both say yes 20, both no 15, a-yes/b-no 5, a-no/b-yes 10. po=.7, pe=.5*.6+.5*.4=.5, kappa=.4
    a = ["y"] * 20 + ["n"] * 15 + ["y"] * 5 + ["n"] * 10
    b = ["y"] * 20 + ["n"] * 15 + ["n"] * 5 + ["y"] * 10
    assert ev.cohen_kappa(a, b) == pytest.approx(0.4)


def test_wilson_interval_and_percentiles():
    lo, hi = ev.wilson(8, 10)
    assert lo == pytest.approx(0.490, abs=0.001) and hi == pytest.approx(0.943, abs=0.001)
    assert ev.percentile([100, 200, 300, 400], 0.5) == 250 and ev.percentile([], 0.5) is None


def test_per_class_precision_recall():
    pc = ev.per_class(["courier", "courier", "customer"], ["courier", "customer", "courier"])
    assert pc["courier"]["precision"] == 0.5 and pc["courier"]["recall"] == 0.5
    assert pc["staff"]["precision"] is None


def test_groups_and_first_frame_copies():
    assert ev.group_of("live-0123456789ab-f3.jpg") == "live-0123456789ab"
    assert ev.group_of("porch1__a.jpg") == "porch1"


def _frames(tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    for name in ["live-aaaaaaaaaaaa.jpg", "live-aaaaaaaaaaaa-f1.jpg", "live-aaaaaaaaaaaa-f2.jpg",
                 "live-bbbbbbbbbbbb-f1.jpg", "phone__courier1.jpg"]:
        (d / name).write_bytes(b"\xff\xd8" + name.encode())
    return d


class FakeBedrock:
    """Answers like the Converse API: courier for live-a, customer otherwise."""

    def __init__(self):
        self.calls = 0

    def converse(self, **kw):
        self.calls += 1
        img = kw["messages"][0]["content"][0]["image"]["source"]["bytes"]
        t = "delivery" if b"aaaa" in img else "customer"
        return {"output": {"message": {"content": [{"text": json.dumps({"type": t, "confidence": 0.8, "reason": "r"})}]}},
                "ResponseMetadata": {"RequestId": f"rid-{self.calls}"}, "metrics": {"latencyMs": 100 * self.calls},
                "usage": {"inputTokens": 1000, "outputTokens": 40}, "stopReason": "end_turn"}


def test_sheet_run_score_end_to_end(tmp_path):
    frames = _frames(tmp_path)
    sheet = tmp_path / "a.csv"
    ev.cmd_sheet(frames, sheet, seed=1)
    names = [r["frame"] for r in csv.DictReader(sheet.open())]
    assert sorted(names) == ["live-aaaaaaaaaaaa-f1.jpg", "live-aaaaaaaaaaaa-f2.jpg", "live-bbbbbbbbbbbb-f1.jpg",
                             "phone__courier1.jpg"]  # the first-frame copy is not labelled twice

    truth_a = {"live-aaaaaaaaaaaa-f1.jpg": "courier", "live-aaaaaaaaaaaa-f2.jpg": "courier",
               "live-bbbbbbbbbbbb-f1.jpg": "nobody", "phone__courier1.jpg": "courier"}
    truth_b = {**truth_a, "live-bbbbbbbbbbbb-f1.jpg": "customer"}  # the second person disagrees on one
    for path, truth in [(tmp_path / "a.csv", truth_a), (tmp_path / "b.csv", truth_b)]:
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frame", "label", "note"])
            w.writerows([k, v, ""] for k, v in truth.items())

    run = tmp_path / "run.jsonl"
    fake = FakeBedrock()
    clf = BedrockClassifier("us-east-1", "model-x", client=fake, record_path=tmp_path / "run-raw.jsonl")
    ev.cmd_run(frames, run, "us-east-1", "model-x", classifier=clf)
    ev.cmd_run(frames, run, "us-east-1", "model-x", classifier=clf)  # resumable: no second round of calls
    assert fake.calls == 4

    s = ev.score(ev.read_labels(tmp_path / "a.csv"), ev.read_labels(tmp_path / "b.csv"),
                 ev._jsonl(run), ev._jsonl(tmp_path / "run-raw.jsonl"), 1.0, 5.0)
    assert s["disagreed"] == ["live-bbbbbbbbbbbb-f1.jpg"] and s["scored"] == 3
    assert s["correct"] == 2 and s["per_class"]["courier"]["recall"] == pytest.approx(2 / 3)
    assert s["per_class"]["courier"]["precision"] == 1.0
    assert s["errors"][0]["frame"] == "phone__courier1.jpg" and s["errors"][0]["predicted"] == "customer"
    assert s["calls"] == 4 and s["latency_p95_ms"] == pytest.approx(385)
    assert s["cost_usd"] == pytest.approx((4000 * 1 + 160 * 5) / 1e6)
    md = ev.report_md(s, 1.0, 5.0)
    assert "Cohen's kappa" in md and "| phone__courier1.jpg | courier | customer |" in md


def test_bad_label_is_refused(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("frame,label,note\nf.jpg,parcel,\n")
    with pytest.raises(SystemExit):
        ev.read_labels(p)


def test_bedrock_call_failure_is_recorded_not_hidden(tmp_path):
    class Boom:
        def converse(self, **kw):
            raise RuntimeError("ThrottlingException")

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import DoorEvent

    raw = tmp_path / "raw.jsonl"
    e = DoorEvent("f.jpg", "d", datetime.now(ZoneInfo("UTC")), "live_view", "eval", snapshot=b"\xff\xd8x")
    v = BedrockClassifier("us-east-1", "m", client=Boom(), record_path=raw).classify(e, ev.ALL_DAY)
    assert v.source == "rules" and "Bedrock error" in v.reason
    assert "ThrottlingException" in json.loads(raw.read_text())["error"]
