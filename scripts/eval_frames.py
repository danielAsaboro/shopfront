"""Measure the Bedrock frame classifier against labels people wrote, not against our own guesses.

Three steps, each resumable:

  1. sheet   Write a blank labelling sheet for a folder of frames (shuffled, no model output shown).
             Give a copy to each labeller. Labels: courier, customer, staff, nobody, other.
  2. run     Ask Bedrock about every frame once and keep each raw response (RequestId, latency,
             token usage, the model's own text) in a JSON-lines file.
  3. score   Read two labellers' sheets and the run file, and write a report: agreement between the
             labellers (Cohen's kappa), then a confusion matrix, per-class precision and recall,
             accuracy with a 95% interval, a per-session view, latency, cost, and every mistake.

Frames come from Shopfront's live view (`.data/live-frames/live-<id>-f<N>.jpg`, one group per Ring
session) or any folder of JPEGs. Files named `<group>__<anything>.jpg` are grouped by `<group>`.

    python -m scripts.eval_frames sheet --frames .data/live-frames --out eval/labels-daniel.csv
    python -m scripts.eval_frames run   --frames .data/live-frames --out eval/bedrock-run.jsonl
    python -m scripts.eval_frames score --a eval/labels-daniel.csv --b eval/labels-second.csv \\
        --run eval/bedrock-run.jsonl --out eval/report.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

CLASSES = ["courier", "customer", "staff", "nobody", "other"]
# Bedrock answers in visit types; a frame shows a person (or nobody). after_hours is about the clock, not
# the picture, so the run passes all-day hours and anything still called after_hours counts as "other".
FROM_MODEL = {"delivery": "courier", "customer": "customer", "staff": "staff", "unknown": "nobody",
              "after_hours": "other"}
ALL_DAY = (time(0, 0), time(23, 59, 59))


def frame_files(folder: Path) -> list[Path]:
    files = sorted(p for p in folder.glob("*.jpg") if p.is_file())
    # live view also saves each session's first frame as live-<id>.jpg; that's a copy of -f1
    return [p for p in files if not re.fullmatch(r"live-[0-9a-f]{12}", p.stem)]


def group_of(name: str) -> str:
    stem = Path(name).stem
    if "__" in stem:
        return stem.split("__", 1)[0]
    m = re.match(r"(live-[0-9a-f]{12})-f\d+$", stem)
    return m.group(1) if m else stem


# --- metrics (plain Python, so the numbers can be checked by hand) ---------------------------------

def cohen_kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def confusion(truth: list[str], pred: list[str]) -> dict[str, Counter]:
    m = {t: Counter() for t in CLASSES}
    for t, p in zip(truth, pred):
        m[t][p] += 1
    return m


def per_class(truth: list[str], pred: list[str]) -> dict[str, dict]:
    out = {}
    for c in CLASSES:
        tp = sum(t == c and p == c for t, p in zip(truth, pred))
        fp = sum(t != c and p == c for t, p in zip(truth, pred))
        fn = sum(t == c and p != c for t, p in zip(truth, pred))
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        out[c] = {"support": tp + fn, "predicted": tp + fp, "precision": prec, "recall": rec}
    return out


def percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


# --- steps ------------------------------------------------------------------------------------------

def read_labels(path: Path) -> dict[str, str]:
    out = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            lab = (row.get("label") or "").strip().lower()
            if lab:
                if lab not in CLASSES:
                    raise SystemExit(f"{path}: {row['frame']} has label {lab!r}; use one of {', '.join(CLASSES)}")
                out[row["frame"]] = lab
    return out


def cmd_sheet(frames: Path, out: Path, seed: int) -> None:
    files = [p.name for p in frame_files(frames)]
    random.Random(seed).shuffle(files)  # so a labeller doesn't read a session as one story
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "label", "note"])
        for name in files:
            w.writerow([name, "", ""])
    print(f"wrote {len(files)} rows to {out}. Labels: {', '.join(CLASSES)}. Open each frame from {frames}.")


def cmd_run(frames: Path, out: Path, region: str, model_id: str, classifier=None) -> None:
    from app.classify.bedrock import BedrockClassifier
    from app.models import DoorEvent

    done = set()
    if out.exists():
        done = {json.loads(x)["frame"] for x in out.read_text().splitlines() if x.strip()}
    raw = out.with_name(out.stem + "-raw.jsonl")
    clf = classifier or BedrockClassifier(region=region, model_id=model_id, record_path=raw)
    out.parent.mkdir(parents=True, exist_ok=True)
    todo = [p for p in frame_files(frames) if p.name not in done]
    for i, p in enumerate(todo, 1):
        ev = DoorEvent(id=p.name, device_id="eval", timestamp=datetime.now(ZoneInfo("UTC")), kind="live_view",
                       source="eval", snapshot=p.read_bytes())
        v = clf.classify(ev, ALL_DAY, no_frame_reason="empty frame")
        row = {"frame": p.name, "group": group_of(p.name), "model_label": v.visit_type.value,
               "label": FROM_MODEL.get(v.visit_type.value, "other") if v.source == "bedrock" else None,
               "confidence": v.confidence, "reason": v.reason, "source": v.source, "request_id": v.request_id}
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[{i}/{len(todo)}] {p.name}: {row['label'] or 'NOT LABELLED (' + v.reason + ')'}", file=sys.stderr)


def score(a: dict[str, str], b: dict[str, str] | None, run: list[dict], raw: list[dict],
          price_in: float, price_out: float) -> dict:
    both = sorted(set(a) & set(b)) if b is not None else sorted(a)
    kappa = cohen_kappa([a[f] for f in both], [b[f] for f in both]) if b is not None else None
    disagreed = [f for f in both if b is not None and a[f] != b[f]]
    truth = {f: a[f] for f in both if f not in disagreed}
    by_frame = {r["frame"]: r for r in run}
    failed = [f for f in truth if f in by_frame and by_frame[f]["label"] is None]
    scored = [f for f in truth if f in by_frame and by_frame[f]["label"] is not None]
    t = [truth[f] for f in scored]
    p = [by_frame[f]["label"] for f in scored]
    correct = sum(x == y for x, y in zip(t, p))
    groups: dict[str, list[str]] = {}
    for f in scored:
        groups.setdefault(group_of(f), []).append(f)
    group_rows = []
    for g, fs in sorted(groups.items()):
        gt = Counter(truth[f] for f in fs).most_common(1)[0][0]
        gp = Counter(by_frame[f]["label"] for f in fs).most_common(1)[0][0]
        group_rows.append({"group": g, "frames": len(fs), "truth": gt, "predicted": gp,
                           "frames_correct": sum(truth[f] == by_frame[f]["label"] for f in fs)})
    lat = [r["latency_ms"] for r in raw if r.get("latency_ms") is not None]
    tin = sum((r.get("usage") or {}).get("inputTokens", 0) for r in raw)
    tout = sum((r.get("usage") or {}).get("outputTokens", 0) for r in raw)
    calls = sum(1 for r in raw if r.get("request_id"))
    cost = (tin * price_in + tout * price_out) / 1e6
    majority = Counter(t).most_common(1)[0] if t else ("-", 0)
    return {
        "labelled_by_a": len(a), "labelled_by_both": len(both) if b is not None else None, "kappa": kappa,
        "disagreed": disagreed, "scored": len(scored), "not_labelled_by_bedrock": failed,
        "correct": correct, "accuracy": correct / len(scored) if scored else None,
        "accuracy_ci": wilson(correct, len(scored)), "majority_class": majority[0],
        "majority_baseline": majority[1] / len(scored) if scored else None,
        "per_class": per_class(t, p), "confusion": {k: dict(v) for k, v in confusion(t, p).items()},
        "groups": group_rows,
        "errors": [{"frame": f, "truth": truth[f], "predicted": by_frame[f]["label"],
                    "reason": by_frame[f]["reason"], "request_id": by_frame[f]["request_id"]}
                   for f in scored if truth[f] != by_frame[f]["label"]],
        "calls": calls, "request_ids": [r["request_id"] for r in raw if r.get("request_id")],
        "latency_p50_ms": percentile(lat, 0.5), "latency_p95_ms": percentile(lat, 0.95),
        "input_tokens": tin, "output_tokens": tout, "cost_usd": cost,
        "cost_per_frame_usd": cost / calls if calls else None,
        "models": sorted({r.get("model_id") for r in raw if r.get("model_id")}),
    }


def _pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.0f}%"


def report_md(s: dict, price_in: float, price_out: float) -> str:
    lo, hi = s["accuracy_ci"]
    L = ["# Bedrock frame classifier: evaluation", "",
         f"Model: {', '.join(s['models']) or 'unknown'}. Bedrock calls with a RequestId: {s['calls']}.", ""]
    if s["kappa"] is not None:
        L += [f"**Labeller agreement:** {s['labelled_by_both']} frames labelled by both people, Cohen's kappa "
              f"{s['kappa']:.2f}. {len(s['disagreed'])} frames where they disagreed are left out of the scores "
              "below.", ""]
    else:
        L += ["**Only one labeller.** These numbers are one person's labels against the model; get a second "
              "person to label the same sheet before quoting them.", ""]
    L += [f"**Accuracy:** {s['correct']} of {s['scored']} frames ({_pct(s['accuracy'])}, 95% interval "
          f"{_pct(lo)} to {_pct(hi)}). Always answering \"{s['majority_class']}\" would score "
          f"{_pct(s['majority_baseline'])}.", ""]
    pc = s["per_class"]["courier"]
    L += [f"**Courier, the label receipts depend on:** precision {_pct(pc['precision'])} "
          f"({pc['predicted']} frames called courier), recall {_pct(pc['recall'])} ({pc['support']} real courier "
          "frames).", ""]
    L += ["## Per class", "", "| Class | Real frames | Called this | Precision | Recall |", "|---|---|---|---|---|"]
    for c in CLASSES:
        r = s["per_class"][c]
        L.append(f"| {c} | {r['support']} | {r['predicted']} | {_pct(r['precision'])} | {_pct(r['recall'])} |")
    L += ["", "## Confusion matrix (rows: people's label, columns: Bedrock)", "",
          "| | " + " | ".join(CLASSES) + " |", "|---" * (len(CLASSES) + 1) + "|"]
    for c in CLASSES:
        L.append(f"| **{c}** | " + " | ".join(str(s["confusion"][c].get(p, 0)) for p in CLASSES) + " |")
    L += ["", "## Per session", "",
          "Frames from one Ring session show the same scene, so they are not independent. This is the "
          "honest sample size.", "", "| Session | Frames | People say | Bedrock says (most frames) | Frames right |",
          "|---|---|---|---|---|"]
    for g in s["groups"]:
        L.append(f"| {g['group']} | {g['frames']} | {g['truth']} | {g['predicted']} | {g['frames_correct']} |")
    L += ["", "## Speed and cost", "",
          f"- Bedrock latency (its own `latencyMs`): p50 {s['latency_p50_ms'] or 'n/a'} ms, p95 "
          f"{s['latency_p95_ms'] or 'n/a'} ms.",
          f"- Tokens: {s['input_tokens']} in, {s['output_tokens']} out. At ${price_in}/M in and ${price_out}/M "
          f"out, ${s['cost_usd']:.4f} in total, ${(s['cost_per_frame_usd'] or 0):.5f} per frame. Check the "
          "prices against the current Bedrock price list before quoting.", ""]
    if s["not_labelled_by_bedrock"]:
        L += [f"Frames Bedrock didn't label (call failed or no frame): {', '.join(s['not_labelled_by_bedrock'])}", ""]
    L += ["## Every mistake", "", "| Frame | People | Bedrock | Bedrock's reason | RequestId |", "|---|---|---|---|---|"]
    for e in s["errors"]:
        L.append(f"| {e['frame']} | {e['truth']} | {e['predicted']} | {e['reason']} | {e['request_id'] or ''} |")
    if s["disagreed"]:
        L += ["", f"Frames the two labellers disagreed on: {', '.join(s['disagreed'])}"]
    return "\n".join(L) + "\n"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("sheet")
    s1.add_argument("--frames", type=Path, required=True)
    s1.add_argument("--out", type=Path, required=True)
    s1.add_argument("--seed", type=int, default=7)
    s2 = sub.add_parser("run")
    s2.add_argument("--frames", type=Path, required=True)
    s2.add_argument("--out", type=Path, required=True)
    s3 = sub.add_parser("score")
    s3.add_argument("--a", type=Path, required=True)
    s3.add_argument("--b", type=Path)
    s3.add_argument("--run", type=Path, required=True)
    s3.add_argument("--out", type=Path, required=True)
    s3.add_argument("--price-in", type=float, default=1.0, help="USD per million input tokens")
    s3.add_argument("--price-out", type=float, default=5.0, help="USD per million output tokens")
    a = ap.parse_args(argv)
    if a.cmd == "sheet":
        cmd_sheet(a.frames, a.out, a.seed)
    elif a.cmd == "run":
        from app.config import Settings
        st = Settings()
        cmd_run(a.frames, a.out, st.aws_region, st.bedrock_model_id)
    else:
        s = score(read_labels(a.a), read_labels(a.b) if a.b else None, _jsonl(a.run),
                  _jsonl(a.run.with_name(a.run.stem + "-raw.jsonl")), a.price_in, a.price_out)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(report_md(s, a.price_in, a.price_out))
        a.out.with_suffix(".json").write_text(json.dumps(s, indent=1, default=str))
        print(f"wrote {a.out} and {a.out.with_suffix('.json')}")


if __name__ == "__main__":
    main()
