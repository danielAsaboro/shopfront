"""scripts/ring_proof.py, the guided live proof, run as --dry-run (MockTransport + a fake Bedrock).

The dry run proves the script's steps and its record format, nothing about the live API.
"""

import json
import zipfile
from pathlib import Path

import httpx

from scripts import ring_proof, verify_pack


def run_dry(tmp_path: Path, *extra: str) -> tuple[int, dict]:
    code = ring_proof.main(["--dry-run", "--out", str(tmp_path), *extra])
    [path] = tmp_path.glob("ring-proof-*.json")
    return code, json.loads(path.read_text())


def test_dry_run_walks_every_step_and_records_every_request_id(tmp_path, capsys):
    code, rec = run_dry(tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    for step in ("Step 1 of 7", "Step 2 of 7", "press a scene", "Step 3 of 7", "Step 4 of 7", "Step 5 of 7",
                 "Step 6 of 7", "Step 7 of 7"):
        assert step in out
    assert rec["dry_run"] is True and "not evidence" in rec["note"]
    assert rec["online"] is True
    assert rec["event"]["event_type"] == "on_demand"  # what a Playground scene files
    # a few seconds in, asked in whole seconds (was: exactly 5000 ms in, to the millisecond); 425 once, then the frame
    assert abs(rec["frame"]["offset_into_event_ms"] - 5000) <= 500 and rec["frame"]["attempts"] == 2
    asked = rec["frame"]["x_media_timestamp"]  # the dry world echoes the requested timestamp
    assert asked.isdigit() and int(asked) % 1000 == 0
    assert rec["token_issued_from"] == "when this script started" and rec["token_issued_utc"] <= rec["event"]["start_utc"]
    assert rec["bedrock"]["labelled_by"] == "bedrock" and rec["bedrock"]["request_id"] == "dry-run-bedrock-0001"
    assert all(c["x_request_id"] for c in rec["calls"])
    statuses = [(c["method"], c["status"]) for c in rec["calls"] if "image/download" in c["url"]]
    assert statuses == [("POST", 425), ("POST", 303)]


def test_one_run_captures_the_whole_chain_from_event_to_signed_pack(tmp_path):
    code, rec = run_dry(tmp_path)
    assert code == 0
    chain = rec["chain"]
    rids = {c["x_request_id"]: c for c in rec["calls"]}
    # Event History (on_demand) -> frame (whole-second at_timestamp) -> Bedrock -> check -> pack, each with its ID
    assert "/v1/history/" in rids[chain["event_history_x_request_id"]]["url"] and rec["event"]["event_type"] == "on_demand"
    assert rec["frame"]["whole_second"] and chain["frame_x_request_id"] == rec["frame"]["x_request_id"]
    assert chain["bedrock_request_id"] == rec["bedrock"]["request_id"] == "dry-run-bedrock-0001"
    check = rec["check"]
    assert check["claim"] == "business_closed" and check["playground_mode"] and check["outcome"] == "open_courier_came"
    assert check["cited"] == {"label": "delivery", "labelled_by": "bedrock", "event_source": "ring-playground",
                              "bedrock_request_id": "dry-run-bedrock-0001", "is_this_scene": True}
    assert "Playground scene (Ring simulator)" in check["text"] and check["event_history_x_request_ids"]
    assert all(r in rids for r in check["event_history_x_request_ids"])  # the check re-read the day, logged too
    assert sum(1 for c in rec["calls"] if "image/download" in c["url"]) == 2  # no second frame for the check
    # the pack: saved beside the dry-run record, signed, its frame is step 3's, and it verifies offline
    pack = rec["pack"]
    [zpath] = tmp_path.glob("ring-proof-pack-*.zip")
    body = zpath.read_bytes()
    assert ring_proof.hashlib.sha256(body).hexdigest() == pack["zip_sha256"] == chain["pack_zip_sha256"]
    assert pack["frame_is_step_3s"] and "ring-frame-1240.jpg" in pack["files"] and "manifest.sig" in pack["files"]
    assert pack["event_sources"] == ["ring-playground"] and "PLAYGROUND" in pack["watermark"]
    problems, _ = verify_pack.verify(body, kid=pack["key_id"])
    assert problems == []
    inner = json.loads(zipfile.ZipFile(zpath).read("pack.json"))
    assert inner["finding"]["outcome"] == "open_courier_came" and inner["playground"]


def test_without_bedrock_the_scene_is_a_playground_visit_and_still_checked(tmp_path):
    code, rec = run_dry(tmp_path, "--no-bedrock")
    assert code == 0 and rec["check"]["outcome"] == "door_activity_unverified"
    assert "a Playground scene (Ring simulator) at 12:40" in rec["check"]["text"]
    assert rec["check"]["cited"]["labelled_by"] == "ring-playground" and "bedrock_request_id" not in rec["chain"]
    assert rec["pack"]["frame_is_step_3s"] and rec["pack"]["zip_sha256"] == rec["chain"]["pack_zip_sha256"]


def test_dry_run_hashes_every_ring_id_and_keeps_no_presigned_query(tmp_path):
    _, rec = run_dry(tmp_path)
    text = json.dumps(rec)
    for raw in (ring_proof.DRY_DEVICE, ring_proof.DRY_ACCOUNT, "dry-run-event-1", "security_token", "dry-run-token"):
        assert raw not in text
    assert rec["device_id_hash"] == ring_proof.h(ring_proof.DRY_DEVICE)
    assert rec["account_id_hash"] == ring_proof.h(ring_proof.DRY_ACCOUNT)


def test_dry_run_never_writes_evidence_or_a_frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = ring_proof.main(["--dry-run"])  # no --out: a temporary folder, never ./evidence
    assert code == 0 and not (tmp_path / "evidence").exists()
    assert not list(tmp_path.rglob("*.jpg"))


def test_no_bedrock_skips_the_label(tmp_path):
    code, rec = run_dry(tmp_path, "--no-bedrock")
    assert code == 0 and "bedrock" not in rec


def test_no_new_event_is_named_not_guessed(tmp_path, monkeypatch):
    real = ring_proof.dry_world

    def never_filed(clock):
        handler, press, state = real(clock)

        def quiet(req: httpx.Request):
            if "/history/" in req.url.path:
                return httpx.Response(200, json={"data": [], "links": {}}, headers={"X-Request-ID": "rid-h"})
            return handler(req)
        return quiet, press, state

    monkeypatch.setattr(ring_proof, "dry_world", never_filed)
    code, rec = run_dry(tmp_path, "--wait", "30")
    assert code == 1 and rec["error"]["status"] == "NO_NEW_EVENT" and "frame" not in rec


def test_a_ring_refusal_is_recorded_with_its_code_and_request_id(tmp_path, monkeypatch):
    real = ring_proof.dry_world

    def forbidden(clock):
        handler, press, state = real(clock)

        def refuse(req: httpx.Request):
            if req.url.path.endswith("/image/download"):
                return httpx.Response(403, headers={"X-Request-ID": "rid-403"}, json={"errors": [{
                    "status": "403", "code": "TIME_RANGE_NOT_AUTHORIZED",
                    "detail": "Requested time range is not within authorized boundaries"}]})
            return handler(req)
        return refuse, press, state

    monkeypatch.setattr(ring_proof, "dry_world", forbidden)
    code, rec = run_dry(tmp_path)
    assert code == 1
    assert rec["error"] == {"status": "FORBIDDEN", "http_status": 403, "code": "TIME_RANGE_NOT_AUTHORIZED",
                            "request_id": "rid-403"}


def test_live_mode_without_a_token_says_how_to_get_one(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ring_proof, "_token", lambda: "")
    assert ring_proof.main(["--out", str(tmp_path)]) == 2
    assert "RING_ACCESS_TOKEN=<token>" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
