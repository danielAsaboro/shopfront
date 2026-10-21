"""JSON stores under concurrent requests and after a crash."""

import json
import threading
from datetime import date

from app.cache import ClassificationCache
from app.deliveries import DeliveryBook


def _together(n, fn):
    ts = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_cache_puts_from_many_threads_all_land(tmp_path):
    c = ClassificationCache(tmp_path / "c.json")
    errors = []

    def put(i):
        try:
            for j in range(20):
                c.put(f"e{i}-{j}", {"type": "customer", "model": "m"})
        except Exception as exc:  # the shared .tmp name used to collide: FileNotFoundError on replace
            errors.append(exc)
    _together(8, put)
    assert not errors
    assert len(json.loads((tmp_path / "c.json").read_text())) == 160


def test_a_corrupt_cache_is_set_aside_not_fatal(tmp_path, caplog):
    p = tmp_path / "classifications.json"
    p.write_text('{"e1": {"type": "deliv')
    c = ClassificationCache(p)  # used to raise JSONDecodeError at app import
    assert c.get("e1", "m") is None
    assert list(tmp_path.glob("classifications.json.corrupt-*")) and "set aside" in caplog.text
    c.put("e2", {"type": "customer", "model": "m"})
    assert ClassificationCache(p).get("e2", "m")


def test_delivery_book_adds_from_many_threads_all_land(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    _together(10, lambda i: book.add(date(2026, 10, 7), f"Supplier {i}"))
    assert len(book.for_day(date(2026, 10, 7))) == 10
