"""The B2B edge as the desk's hero: each carrier's pattern this month near the top, linking to every kept check,
and /report?month=YYYY-MM, the month on one printable page for a carrier's account manager or a supplier (round 4).
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main
from app import checks as kc
from app import dispute, wording
from app import reconcile as rc
from app.models import ClassifiedVisit, DoorEvent, VisitType

TZ = ZoneInfo("America/Los_Angeles")
OPEN = (time(8), time(18))
REMOTE = ("203.0.113.9", 5000)


def at(d, hh, mm):
    return datetime.combine(d, time(hh, mm), tzinfo=TZ)


def busy(d, src="ring"):
    return [ClassifiedVisit(DoorEvent(f"e{d.day}-{m}", "d", at(d, 14, m), "motion", src), VisitType.CUSTOMER, 0.5,
                            "why", "rules") for m in (2, 10, 50, 55)]


def keep_three(book, src="ring"):
    for d in (date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 14)):
        check = rc.check_closed_scan(at(d, 14, 30), busy(d, src), OPEN, now=at(date(2026, 10, 31), 12, 0))
        book.keep(check, "Northline Parcel", f"NP{d.day}", demo=src == "synthetic")
    book.keep(rc.check_closed_scan(at(date(2026, 10, 9), 19, 30), busy(date(2026, 10, 9)), OPEN,
                                   now=at(date(2026, 10, 31), 12, 0)), "Swift Parcel", "SP1")


def test_each_carrier_gets_one_pattern_line_with_what_is_at_stake(tmp_path):
    book = kc.CheckBook(tmp_path / "c.json")
    keep_three(book)
    north, swift = kc.carrier_record(book.all(), date(2026, 10, 1), 17.2, "$", dispute.DISPUTE)
    assert north["headline"] == ('Northline Parcel: 3 "business closed" scans in October, all during posted hours, '
                                 "about $51.60 at stake")
    assert swift["headline"] == 'Swift Parcel: 1 "business closed" scan in October, none during posted hours'
    assert swift["at_stake"] is None  # the shop's own hours agree with the scan: nothing to dispute
    assert kc.carrier_record(book.all(), date(2026, 10, 1))[0]["headline"].endswith("all during posted hours")


def test_report_lines_are_third_person_and_never_accuse():
    for line in kc.REPORT_LINE.values():
        assert not wording.accusing(line) and "you" not in line.lower().split()
    assert set(kc.REPORT_LINE) >= set(dispute.DISPUTE) | {rc.POSTED_CLOSED, rc.UNVERIFIED}


def test_desk_leads_with_the_record_linking_to_every_check_and_the_report(monkeypatch):
    main.book.add(date(2026, 10, 14), "Valley Creamery", "13:00", "16:00", order_ref="PO-1042")
    keep_three(main.checks)
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    page = TestClient(main.app).get("/", params={"date": "2026-10-14"}).text
    record = page.split('<section class="block record"')[1].split("</section>")[0]
    assert 'Northline Parcel: 3 &#34;business closed&#34; scans in October, all during posted hours' in record \
        or 'Northline Parcel: 3 "business closed" scans in October, all during posted hours' in record
    links = re.findall(r'<a href="(/\?[^"]+)">', record)
    assert len(links) == 4 and all("attempt=" in x and "claim=business_closed" in x for x in links)
    assert 'href="/report?month=2026-10"' in record
    assert page.index('<section class="block record"') < page.index('<section class="lead">')  # near the top


def test_report_is_one_printable_page_of_the_month(monkeypatch):
    keep_three(main.checks, "synthetic")
    c = TestClient(main.app)
    r = c.get("/report", params={"month": "2026-10"})
    assert r.status_code == 200
    page = r.text
    text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script).*?</\1>", "", page, flags=re.S)))
    assert "Carrier scans, October 2026" in text
    assert 'Northline Parcel: 3 "business closed" scans in October, all during posted hours, about $51.60 at stake.' \
        in text
    assert text.count("Open by posted hours; the door was in use, and no courier was recorded near the scan") == 3
    assert "Demo data: 3 of these checks were made on Shopfront's demo door. Don't send this report." in text
    assert "What this does not show" in text and dispute.LIMITS[0] in text
    assert "@page { size: A4;" in page and 'onclick="window.print()"' in page
    assert not wording.accusing(text)
    empty = c.get("/report", params={"month": "2026-09"}).text
    assert "No carrier scans were checked and kept in September 2026." in empty
    assert c.get("/report", params={"month": "2026-13"}).status_code == 400


def test_report_is_the_owners_only():
    keep_three(main.checks)
    assert TestClient(main.app, client=REMOTE).get("/report", params={"month": "2026-10"}).status_code == 403
