"""Demo data is said once where it matters, not fifteen times a page (round 4).

The site banner says it on every page; each row or section carries one chip; the pack keeps its one watermark
block and the draft its one line (plus the [DEMO] subject tag). The zip's README keeps the full explanation.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import replace
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import dispute, main
from app.orders import parse_orders
from scripts import demo

TZ = ZoneInfo("America/Los_Angeles")


def visible_text(page: str) -> str:
    """What a reader sees before opening anything: no closed <details>, scripts, styles, SVG descriptions or
    text that only prints."""
    for pat in (r"<script.*?</script>", r"<style.*?</style>", r"<details.*?</details>", r"<desc.*?</desc>",
                r"<title.*?</title>", r'<p class="strip-words">.*?</p>', r'<div class="one-draft" data-key="carrier".*?</div>\s*</div>'):
        page = re.sub(pat, "", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


@pytest.fixture
def demo_client(monkeypatch):
    day = demo.demo_day(date(2026, 10, 21), usable=lambda d: demo.has_scan_slot(d, TZ))
    monkeypatch.setattr(main, "settings", replace(main.settings, ring_hmac_key="demo-key"))
    text = demo.dated_orders(demo.SAMPLE.read_text(), day)
    main.book.import_rows(parse_orders(text)["rows"])
    client = TestClient(main.app)
    demo.feed_package_alert(client, day, TZ, "demo-key")
    order = next(r for r in parse_orders(text)["rows"] if r.order_ref == demo.SCAN_ORDER)
    q = {"date": day.isoformat(), "carrier": demo.CARRIER, "tracking": order.tracking}
    return client, q


def test_the_pack_says_demo_in_its_watermark_its_draft_and_once_per_row(demo_client):
    client, q = demo_client
    page = client.get("/dispute", params={**q, "at": demo.SCAN_AT, "claim": "business_closed"}).text
    vis = visible_text(page)
    rows = re.findall(r'<table class="log">(.*?)</table>', page, re.S)[0]
    per_row = [len(re.findall(r"demo|synthetic", r, re.I)) for r in rows.split("</tr>")[1:-1]]
    assert max(per_row) <= 1  # one per door-log row at most
    outside_rows = len(re.findall(r"demo|synthetic", visible_text(page.replace(rows, "")), re.I))
    # banner (2), watermark block (2), camera line (1), the shown draft's subject tag and first line (2)
    assert outside_rows <= 7, outside_rows
    assert vis.count("DEMO DATA, not from a camera") == 1


def test_the_check_page_says_it_in_the_banner_and_one_chip_per_section(demo_client):
    client, q = demo_client
    page = client.get("/", params={**q, "attempt": demo.SCAN_AT, "claim": "business_closed"}).text
    verdict = re.search(r'<section class="verdict.*?</section>', page, re.S).group(0)
    assert len(re.findall(r"demo", visible_text(verdict), re.I)) == 1  # the chip
    assert "person alerts" in verdict and "(Ring)" not in verdict  # never credited to Ring


def test_the_zip_readme_keeps_the_full_explanation(demo_client):
    client, q = demo_client
    z = zipfile.ZipFile(io.BytesIO(client.get("/dispute.zip", params={**q, "at": demo.SCAN_AT,
                                                                     "claim": "business_closed"}).content))
    readme = z.read("README.txt").decode()
    assert readme.startswith(dispute.DEMO + " · " + dispute.REPLAYED)
    assert dispute.DEMO_LIMIT in readme and dispute.REPLAY_LIMIT in readme
    assert "synthetic = Shopfront's demo generator. No camera recorded these events." in readme


def test_a_scan_with_no_door_events_near_it_never_shows_a_ring_chip(demo_client):
    """Round 7: a 19:30 scan on the synthetic demo, with no door events around it, showed a RING chip."""
    client, q = demo_client
    page = client.get("/", params={**q, "attempt": "19:30", "claim": "business_closed"}).text
    verdict = re.search(r'<section class="verdict.*?</section>', page, re.S).group(0)
    assert "b-ring" not in verdict and ">ring<" not in verdict
    assert '<span class="badge b-demo"' in verdict  # the day came from the demo generator
