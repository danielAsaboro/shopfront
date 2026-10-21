"""Lost trading (round 4): when the owner marks an order perishable, a missed delivery puts the order's own value at
risk, not only the cited redelivery figure. The value is the order's as written, listed, never added up."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main
from app import checks as kc
from app import dispute
from app.deliveries import DeliveryBook
from app.models import ClassifiedVisit, DoorEvent, VisitType
from app.orders import parse_orders

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)
REMOTE = ("203.0.113.9", 5000)
EVIL = {"Origin": "https://evil.example", "Referer": "https://evil.example/x"}
SCAN = {"date": DAY.isoformat(), "attempt": "14:30", "claim": "business_closed", "carrier": "Northline Parcel",
        "tracking": "NP1"}


def busy(d):
    return [ClassifiedVisit(DoorEvent(f"e{m}", "d", datetime.combine(d, time(14, m), tzinfo=TZ), "motion", "ring"),
                            VisitType.CUSTOMER, 0.5, "why", "rules") for m in (2, 10, 50, 55)]


def dairy():
    return main.book.add(DAY, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042", carrier="Northline Parcel",
                         tracking="NP1", items="Butter 10kg; cream 12L", amount="$318.75")


def text(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script).*?</\1>", "", page, flags=re.S)))


def test_the_mark_is_the_owners_and_survives_a_reimport(tmp_path):
    book = DeliveryBook(tmp_path / "e.json")
    csv = "day,supplier,order_ref,amount\n2026-10-14,Valley Creamery,PO-1042,$318.75\n"
    book.import_rows(parse_orders(csv)["rows"])
    [e] = book.for_day(DAY)
    assert not e.perishable
    assert book.set_perishable(e.id, True).perishable
    book.import_rows(parse_orders(csv)["rows"])  # the same file again
    [again] = book.for_day(DAY)
    assert again.id == e.id and again.perishable
    assert not book.set_perishable(e.id, False).perishable


def test_marking_is_a_same_site_owner_post():
    e = dairy()
    assert TestClient(main.app, client=REMOTE).post(f"/deliveries/{e.id}/perishable", data={"on": "1"}).status_code == 403
    assert TestClient(main.app).post(f"/deliveries/{e.id}/perishable", data={"on": "1"}, headers=EVIL).status_code == 403
    assert not main.book.get(e.id).perishable
    r = TestClient(main.app).post(f"/deliveries/{e.id}/perishable", data={"on": "1", "back": "/?date=2026-10-14"},
                                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?date=2026-10-14" and main.book.get(e.id).perishable
    assert TestClient(main.app).post("/deliveries/nope/perishable", data={"on": "1"}).status_code == 404


def test_check_page_shows_the_order_value_at_risk_once_marked(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    e = dairy()
    c = TestClient(main.app)
    before = text(c.get("/", params=SCAN).text)
    assert "Perishable stock? Count $318.75 as at risk" in before and "Lost trading" not in before
    main.book.set_perishable(e.id, True)
    page = c.get("/", params=SCAN).text
    verdict = text(page.split('<section class="verdict')[1].split("</section>")[0])
    assert "Lost trading: PO-1042 is perishable, so all $318.75 is at risk if it isn't redelivered in time (the " \
           "order's own value, from your orders), not only the redelivery fee." in " ".join(verdict.split())
    assert "Loqate, Fixing Failed Deliveries 2021" in verdict  # the survey figure keeps its source
    assert "Butter 10kg; cream 12L · $318.75 · perishable" in text(page)


def test_kept_checks_carry_it_to_the_month_the_report_and_the_pack(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    e = dairy()
    main.book.set_perishable(e.id, True)
    c = TestClient(main.app)
    assert c.post("/checks", data={"date": DAY.isoformat(), "at": "14:30", "claim": "business_closed",
                                   "carrier": "Northline Parcel", "tracking": "NP1", "back": "/"},
                  follow_redirects=False).status_code == 303
    [k] = main.checks.all()
    assert k["perishable"] and k["outcome"] in dispute.DISPUTE
    assert kc.perishable_at_risk(main.checks.all(), DAY, dispute.DISPUTE) == ["PO-1042 ($318.75)"]
    desk = " ".join(text(c.get("/", params={"date": DAY.isoformat()}).text).split())
    assert "Lost trading: perishable stock behind those scans, PO-1042 ($318.75), at risk as well as the " \
           "redelivery fees (each order's own value)." in desk
    report = " ".join(text(c.get("/report", params={"month": "2026-10"}).text).split())
    assert "Perishable, so at risk in full (each order's own value): PO-1042 ($318.75)." in report
    pack = c.get("/api/dispute", params={"date": DAY.isoformat(), "at": "14:30", "claim": "business_closed",
                                         "tracking": "NP1"}).json()
    assert pack["order"]["perishable"] is True
    page = " ".join(text(c.get("/dispute", params={"date": DAY.isoformat(), "at": "14:30", "claim": "business_closed",
                                                    "tracking": "NP1"}).text).split())
    assert "$318.75 of perishable stock (lost trading if it spoils before a redelivery: the order's own value)" in page
