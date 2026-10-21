"""Demo data is said once where it matters, not fifteen times a page (round 4).

The site banner says it on every page, except the pack and the monthly report, whose own watermark says it instead
(round 8: one page-level notice per page); each row or section carries one chip; the draft keeps its [DEMO]
subject tag, which travels with the copied message. The zip's README keeps the full explanation.
"""

from __future__ import annotations

import html
import io
import json
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
    # watermark block (2), camera line (1), "Door that day" (1), the shown draft's subject tag (1)
    assert outside_rows <= 5, outside_rows
    assert "DEMO DATA, not from a camera" not in vis  # round 8: no second notice inside the draft
    assert vis.count("don't send this") + vis.count("don&#39;t send this") == 1


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


def test_the_pack_page_warns_once_in_its_watermark_not_the_banner_too(demo_client):
    """Round 7: the pack page showed the site banner and the watermark back to back. The watermark (which also
    prints) stays, with the banner's way out; the desk keeps its banner."""
    client, q = demo_client
    page = client.get("/dispute", params={**q, "at": demo.SCAN_AT, "claim": "business_closed"}).text
    assert 'class="watermark"' in page and 'class="banner' not in page
    assert "Demo data: don't send this pack as evidence." in page
    assert '<a class="no-print" href="/connect">Connect Ring</a> to use your doorbell.' in page
    assert 'class="banner warn-bg"' in client.get("/", params={"date": q["date"]}).text


def test_the_pack_never_credits_ring_for_the_days_demo_or_replayed_alerts(demo_client):
    """Round 7: "Door that day: 70 events, 1 confirmed from a Ring frame or Ring's own alert" on the demo pack,
    where the one was Shopfront's replayed alert."""
    client, q = demo_client
    page = client.get("/dispute", params={**q, "at": demo.SCAN_AT, "claim": "business_closed"}).text
    door = page.split("<dt>Door that day</dt>")[1].split("</dd>")[0]
    assert "Ring frame or Ring" not in door and "the door log says which" in door


@pytest.mark.parametrize("scan", [demo.SCAN_AT, demo.PACKAGE_AT])
def test_the_pack_text_and_json_say_what_the_page_says_about_demo_and_replayed_rows(demo_client, scan):
    """Round 7: pack.txt's door log printed "by ring ... Ring Smart Alert: human" for the demo's made-up person
    alerts (and the replayed package alert), where the page said "Demo person alert, no camera". Every copy of
    the pack (page, pack.txt, README, pack.json) now takes its words from the same provenance labels."""
    client, q = demo_client
    params = {**q, "at": scan, "claim": "business_closed"}
    z = zipfile.ZipFile(io.BytesIO(client.get("/dispute.zip", params=params).content))
    text, readme = z.read("pack.txt").decode(), z.read("README.txt").decode()
    p = json.loads(z.read("pack.json"))
    rows = [t for t in p["timeline"] if t["event_source"] in ("synthetic", "ring-webhook-replay")]
    assert rows and any(t["type"] == "person" for t in p["timeline"])
    if scan == demo.PACKAGE_AT:
        assert any(t["event_source"] == "ring-webhook-replay" for t in rows)
    for copy in (text, readme):
        assert "Ring Smart Alert" not in copy and "by ring" not in copy.lower()
    log = text.split("Door log")[1].split("What this does not show")[0]
    page = client.get("/dispute", params=params).text
    for t in rows:
        assert "Ring Smart Alert" not in t["reason"] and t["reason"] == t["how"]
        assert t["provenance"] in ("synthetic", "replay")
        assert f"{t['time']}  {t['ring_event']:<9} {t['label']:<24} {t['how']}" in log  # the page's own words
        assert f">{t['label']}</td><td class=\"why\">{html.escape(t['how'])}</td>" in page
    persons = [t for t in rows if t["type"] == "person" and t["event_source"] == "synthetic"]
    assert all(t["how"] == "Demo person alert, no camera" for t in persons)


def test_the_monthly_report_warns_once_in_its_watermark_not_the_banner_too(demo_client):
    """Round 7: /report showed the site banner and its own demo block. The watermark (which prints) stays, with the
    banner's way out."""
    from app import reconcile as rc
    from app.hours import parse_hours, posted_hours

    client, q = demo_client
    day = date.fromisoformat(q["date"])
    at = demo._scan_at(day, TZ)
    visits = [v for v in demo._door(day, TZ) if abs(rc.gap(v.event.timestamp, at)) <= rc.ACTIVITY]
    main.checks.keep(rc.check_closed_scan(at, visits, posted_hours(parse_hours(demo.SHOP_HOURS), day)), demo.CARRIER,
                     q["tracking"], demo=True)
    page = client.get("/report", params={"month": q["date"][:7]}).text
    assert 'class="watermark"' in page and 'class="banner' not in page
    assert "Don't send this report." in page and 'href="/connect">Connect Ring</a>' in page
    assert len(re.findall(r"demo", visible_text(page), re.I)) <= 2
