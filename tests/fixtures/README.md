There are no fixture files here. The Ring-shaped responses the tests use are built inline, in
[tests/test_ring_api.py](../test_ring_api.py) (Event History pages, device lists, the 303 frame
download, token refresh, error statuses, served by an httpx MockTransport) and
[tests/test_webhooks.py](../test_webhooks.py) (v1.1 webhook payloads). Their shapes come from Ring's
public API documentation (https://developer.amazon.com/docs/ring/api-documentation.html, read
2026-10-14) with placeholder IDs. None of them was captured from a live Ring account.

Live captures go in `evidence/` and are written by `scripts/ring_proof.py`; see
[evidence/README.md](../../evidence/README.md).
