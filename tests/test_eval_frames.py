"""The frame evaluation harness: metrics checked against hand-worked numbers, and the run/score steps
against a fake Bedrock that returns real-shaped responses. Local only."""

import csv
import json

import pytest

from app.classify.bedrock import BedrockClassifier
from scripts import eval_frames as ev
from tests.bedrock_fakes import label


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
        reply = label(t, 0.8, "r", f"rid-{self.calls}")
        reply["metrics"] = {"latencyMs": 100 * self.calls}
        return reply


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


# ---- prompt injection: planted text in the frame ---------------------------------------------------
def test_injection_eval_reports_false_courier_rate_planted_against_clean(tmp_path, capsys):
    from app.models import ClassifiedVisit, VisitType

    class FooledBySystemText:
        """Stands in for a model that obeys 'SYSTEM:' text and nothing else."""

        def classify(self, e, open_hours, no_frame_reason=""):
            assert e.snapshot[:2] == b"\xff\xd8"
            t = VisitType.DELIVERY if e.id.endswith("__system") else VisitType.UNKNOWN
            return ClassifiedVisit(e, t, 0.9, "r", "bedrock", "rid")

    out = tmp_path / "inj.jsonl"
    s = ev.cmd_injection(out, tmp_path / "frames", "us-east-1", "m", classifier=FooledBySystemText())
    n_scenes = len(ev.SCENES)
    assert s["planted"]["frames"] == n_scenes * len(ev.PLANTED) and s["clean"]["frames"] == n_scenes
    assert s["planted"]["called_courier"] == n_scenes
    assert s["planted"]["false_courier_rate"] == pytest.approx(1 / len(ev.PLANTED))
    assert s["clean"]["false_courier_rate"] == 0
    assert s["planted"]["fooled_by"] == sorted(f"{sc}__system" for sc in ev.SCENES)
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == n_scenes * (len(ev.PLANTED) + 1)
    assert "false-courier rate" in capsys.readouterr().err
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert {r["planted"] for r in rows if r["planted"]} == set(ev.PLANTED.values())


def test_injection_frames_go_to_bedrock_with_the_guard_and_bad_answers_are_not_labels(tmp_path):
    from app.classify.bedrock import INJECTION_GUARD
    from tests.bedrock_fakes import label

    seen = []

    class PlantedTextWins:
        def converse(self, **kw):
            seen.append(kw)
            # a model that "obeyed" the JSON on the sign might answer outside the enum: never a label
            return label("courier", 1.0, "the sign says so")

    s = ev.cmd_injection(tmp_path / "inj.jsonl", None, "us-east-1", "m",
                         classifier=BedrockClassifier("us-east-1", "m", client=PlantedTextWins()))
    assert all(INJECTION_GUARD in kw["system"][0]["text"] for kw in seen)
    assert s["planted"]["labelled_by_bedrock"] == 0 and s["planted"]["false_courier_rate"] is None
