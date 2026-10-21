"""Door frames are pictures of people: private files, a retention limit, and a way to delete one."""

import os
import time

from fastapi.testclient import TestClient

import app.main as main
from app.deliveries import FrameStore
from app.files import prune_older_than

JPEG = b"\xff\xd8\xff\xe0frame"


def test_saved_frames_are_private(tmp_path):
    store = FrameStore(tmp_path)
    store.save("evt-1", JPEG)
    assert oct(store.path("evt-1").stat().st_mode & 0o777) == "0o600"


def test_frames_older_than_the_limit_are_deleted(tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    old, new = d / "old.jpg", d / "new.jpg"
    old.write_bytes(JPEG)
    new.write_bytes(JPEG)
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    assert prune_older_than(d, 30) == 1
    assert not old.exists() and new.exists()
    assert prune_older_than(d, 0) == 0  # 0 = keep forever


def test_housekeeping_applies_the_limit_to_both_frame_folders(monkeypatch):
    for sub in ("frames", "live-frames"):
        (main.DATA / sub).mkdir(parents=True)
        f = main.DATA / sub / "x.jpg"
        f.write_bytes(JPEG)
        os.utime(f, (0, 0))
    monkeypatch.setattr(main, "_last_housekeeping", 0.0)
    main._housekeeping()
    assert not any((main.DATA / sub / "x.jpg").exists() for sub in ("frames", "live-frames"))


def test_owner_can_delete_a_frame_and_its_live_view_copies():
    main.frames.save("evt-9", JPEG)
    live = main.DATA / "live-frames"
    live.mkdir(parents=True)
    for n in ("live-abc.jpg", "live-abc-f1.jpg", "live-abc-f2.jpg", "live-abcd.jpg"):
        (live / n).write_bytes(JPEG)
    c = TestClient(main.app)
    assert c.post("/frames/evt-9.jpg/delete", json={}).json() == {"deleted": 1}
    assert c.get("/frames/evt-9.jpg").status_code == 404
    assert c.post("/frames/live-abc.jpg/delete", json={}).json() == {"deleted": 3}
    assert (live / "live-abcd.jpg").exists()
    assert c.post("/frames/..%2Fshop.json/delete", json={}).status_code == 404
    remote = TestClient(main.app, client=("203.0.113.9", 5000))
    assert remote.post("/frames/live-abcd.jpg/delete", json={}).status_code == 403


def test_token_files_are_created_private_not_chmod_ed_after(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    import httpx

    from app.ring import RingSource
    from app.ring.api import RingAuth

    def no_chmod(self, mode):
        raise AssertionError("written world-readable first, then chmod-ed")
    monkeypatch.setattr(Path, "chmod", no_chmod)
    src = RingSource(main.settings, tmp_path / "ring-access-token")
    src.save_token("  pasted  ")
    assert src.pasted_token() == "pasted" and oct((tmp_path / "ring-access-token").stat().st_mode & 0o777) == "0o600"

    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"access_token": "a", "refresh_token": "rt-2", "expires_in": 100})))
    rt = tmp_path / "rt.json"
    RingAuth(refresh_token="rt-1", client_id="c", client_secret="s", token_file=rt, http=http).token()
    assert json.loads(rt.read_text()) == {"refresh_token": "rt-2"} and oct(rt.stat().st_mode & 0o777) == "0o600"


def test_the_delete_button_form_goes_back_to_its_page():
    main.frames.save("evt-7", JPEG)
    r = TestClient(main.app).post("/frames/evt-7.jpg/delete", data={"back": "/?date=2026-10-07"},
                                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?date=2026-10-07&notice=")
    assert main.frames.path("evt-7") is None
    r = TestClient(main.app).post("/frames/x.jpg/delete", data={"back": "//evil.example/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?notice=")


def test_frame_delete_macro_renders_a_post_form():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    html = env.from_string('{% from "_frame_delete.html" import delete_frame %}{{ delete_frame("/frames/a.jpg", "/") }}').render()
    assert 'action="/frames/a.jpg/delete"' in html and 'method="post"' in html and "Delete frame" in html
