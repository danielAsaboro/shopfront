"""A slow Ask (a Bedrock agent turn) or Ring call must not hold up Ring's webhook, which has 5 s.

Runs the real app under uvicorn on a loopback port, so the event loop is the one production uses.
"""

import json
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn

import app.main as main
from app.ring.webhooks import sign

KEY = "test-hmac-key"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key=KEY))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def _webhook_seconds(base: str) -> float:
    raw = json.dumps({"meta": {"version": "1.1", "request_id": f"r-{time.time()}"},
                      "data": {"id": "d_button_press_1", "type": "button_press",
                               "attributes": {"source": "d", "timestamp": int(time.time() * 1000)}}}).encode()
    with httpx.Client(trust_env=False, timeout=10) as c:
        t0 = time.perf_counter()
        r = c.post(f"{base}/webhooks/ring", content=raw, headers={"X-Signature": sign(raw, KEY)})
        assert r.status_code == 200
        return time.perf_counter() - t0


def _in_background(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    time.sleep(0.4)  # let the slow call get going
    return t


def test_webhook_answers_while_the_agent_is_thinking(server, monkeypatch):
    def slow_factory(tools, trace, shop, region, model_id):
        def run(q):
            time.sleep(3)  # a blocking Strands/Bedrock turn
            return "ok"
        return run
    monkeypatch.setattr(main, "agent_factory", slow_factory)
    asked = {}

    def ask():
        with httpx.Client(trust_env=False, timeout=10) as c:
            asked["r"] = c.post(f"{server}/api/ask", json={"question": "anything?"})
    t = _in_background(ask)
    assert _webhook_seconds(server) < 1.0
    t.join(10)
    assert asked["r"].status_code == 200 and asked["r"].json()["answer"] == "ok"


def test_webhook_answers_while_live_view_waits_on_ring(server, monkeypatch):
    class SlowRing(main.RingApiClient):
        def __init__(self):
            self.last_request_id = None
            self._device_id = "d"

        def whep_start(self, offer):
            time.sleep(3)  # a slow Ring WHEP answer
            return "v=0\r\n", "https://api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s"

    ring = SlowRing()
    monkeypatch.setattr(main, "source", type("Src", (), {"get": lambda self: ring})())

    def start():
        with httpx.Client(trust_env=False, timeout=10) as c:
            c.post(f"{server}/api/live/start", content="v=0\r\n", headers={"Content-Type": "application/sdp"})
    t = _in_background(start)
    assert _webhook_seconds(server) < 1.0
    t.join(10)


def test_ask_with_a_broken_body_is_a_bad_question():
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    for body in ("not json", "[1, 2]"):
        r = c.post("/api/ask", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["error"] == "BAD_QUESTION"
