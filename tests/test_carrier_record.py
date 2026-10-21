"""Kept scan checks and the carrier record they add up to (app/checks.py), the desk's month of money, and
the demo's seeded history and filled-in business-closed check. Round 3 audit: "make the B2B edge visible".
Local only."""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import checks as kc
from app import dispute
from app import reconcile as rc
from app.models import ClassifiedVisit, DoorEvent, VisitType
from scripts import demo

TZ = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 14)  # a Wednesday
OPEN = (time(8), time(18))
REMOTE = ("203.0.113.9", 5000)
EVIL = {"Origin": "https://evil.example", "Referer": "https://evil.example/x"}


def at(d, hh, mm):
    return datetime.combine(d, time(hh, mm), tzinfo=TZ)


def busy(d):
    """A door in use around 14:30, nothing that looked at it near the scan."""
    return [ClassifiedVisit(DoorEvent(f"e{m}", "d", at(d, 14, m), "motion", "synthetic"), VisitType.CUSTOMER, 0.5,
                            "why", "rules") for m in (2, 10, 50, 55)]


def closed_check(d, hh=14, mm=30, hours=OPEN):
    return rc.check_closed_scan(at(d, hh, mm), busy(d), hours, now=at(date(2026, 10, 31), 12, 0))


# ---- the book ---------------------------------------------------------------------------------------------

def test_keep_replaces_the_same_scan_and_remove_deletes_it(tmp_path):
    book = kc.CheckBook(tmp_path / "checks.json")
    a = book.keep(closed_check(DAY), "Northline Parcel", "NP1")
    again = book.keep(closed_check(DAY), " northline parcel ", "NP1")  # same scan: replaced, not counted twice
    assert len(book.all()) == 1 and again["id"] == a["id"]
    assert a["posted_open"] and a["outcome"] in dispute.DISPUTE and a["day"] == "2026-10-14" and a["at"] == "14:30"
    assert book.remove(a["id"])["id"] == a["id"] and book.all() == []
    assert book.remove("nope") is None
    refused = rc.check_closed_scan(at(DAY, 14, 30), [], OPEN, now=at(DAY, 9, 0))
    with pytest.raises(ValueError):
        book.keep(refused, "X", "")
    assert (tmp_path / "checks.json").stat().st_mode & 0o077 == 0  # the owner's records: owner-only file


# ---- the record -------------------------------------------------------------------------------------------

def test_record_says_how_often_and_whether_during_posted_hours(tmp_path):
    book = kc.CheckBook(tmp_path / "c.json")
    for d in (date(2026, 10, 2), date(2026, 10, 9), DAY):
        book.keep(closed_check(d), "Northline Parcel", f"NP{d.day}")
    book.keep(closed_check(date(2026, 9, 30)), "Northline Parcel", "NP-sep")  # last month: not counted
    book.keep(closed_check(DAY, 7, 0), "Swift Parcel", "SP1")  # before opening: the scan matched the hours
    [north, swift] = kc.carrier_record(book.all(), DAY)
    assert north["text"] == "Northline Parcel scanned you closed 3 times in October, every time during posted hours."
    assert swift["text"] == "Swift Parcel scanned you closed once in October, never during posted hours."
    assert north["demo"] == 0 and len(north["checks"]) == 3


def test_demo_checks_are_counted_as_demo(tmp_path):
    book = kc.CheckBook(tmp_path / "c.json")
    book.keep(closed_check(date(2026, 10, 9)), "Northline Parcel", "NP1", demo=True)
    book.keep(closed_check(DAY), "Northline Parcel", "NP2")
    [r] = kc.carrier_record(book.all(), DAY)
    assert r["text"].startswith("Northline Parcel scanned you closed twice in October")
    assert r["demo"] == 1 and not r["all_demo"]


def test_month_money_uses_the_cost_it_is_given_and_never_order_amounts(tmp_path):
    book = kc.CheckBook(tmp_path / "c.json")
    book.keep(closed_check(DAY), "Northline Parcel", "NP1")
    book.keep(closed_check(DAY, 14, 31), "Northline Parcel", "NP2")
    m = kc.month_money(book.all(), DAY, 5, 17.2, "$", True, dispute.DISPUTE)
    assert (m["disputed"], m["at_stake"], m["confirmed"], m["confirmed_value"], m["default"]) == \
        (2, "$34.40", 5, "$86.00", True)
    assert kc.month_money([], DAY, 0, 17.2, "$", True, dispute.DISPUTE) is None
    assert kc.month_money(book.all(), DAY, None, None, "¥", False, dispute.DISPUTE)["at_stake"] is None


# ---- the app: keep, show, remove; owner-only and same-site -------------------------------------------------

def form(**kw):
    return {"date": "2026-10-14", "at": "14:30", "claim": "business_closed", "carrier": "Northline Parcel",
            "tracking": "NP4471002958", "back": "/?date=2026-10-14", **kw}


def test_keep_recomputes_the_check_and_the_desk_shows_the_record(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    main.book.add(DAY, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042", carrier="Northline Parcel",
                  tracking="NP4471002958", amount="$318.75")
    c = TestClient(main.app)
    r = c.post("/checks", data=form(outcome="posted_closed"), follow_redirects=False)  # a forged outcome is ignored
    assert r.status_code == 303 and r.headers["location"] == "/?date=2026-10-14#record"
    [k] = main.checks.all()
    assert k["outcome"] == rc.OPEN_NO_COURIER and k["order_ref"] == "PO-1042" and k["amount"] == "$318.75"
    assert k["demo"]  # the synthetic source: demo data, and the record says so
    desk = c.get("/", params={"date": "2026-10-14"}).text
    assert "Carrier record, October 2026" in desk
    assert "Northline Parcel scanned you closed once in October, during posted hours." in desk
    assert "1 disputed scan, about $17.20 of redeliveries at stake (all demo)" in desk
    assert "the survey average" in desk  # the sourced default, said as such
    page = c.get("/", params={"date": "2026-10-14", "attempt": "14:30", "claim": "business_closed",
                              "carrier": "Northline Parcel", "tracking": "NP4471002958"}).text
    assert "Kept in Northline Parcel&#39;s record." in page and f'/checks/{k["id"]}/delete' in page
    assert c.post(f"/checks/{k['id']}/delete", data={"back": "/"}, follow_redirects=False).status_code == 303
    assert main.checks.all() == []
    assert "Carrier record" not in c.get("/", params={"date": "2026-10-14"}).text


def test_an_unkept_check_offers_keep_and_a_refused_one_does_not(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    c = TestClient(main.app)
    page = c.get("/", params={"date": "2026-10-14", "attempt": "14:30", "claim": "business_closed",
                              "carrier": "Northline Parcel"}).text
    assert "Keep in Northline Parcel&#39;s record" in page and 'action="/checks"' in page
    future = date.today().replace(year=date.today().year + 1)
    assert c.post("/checks", data=form(date=future.isoformat())).status_code == 400
    assert main.checks.all() == []


def test_outsiders_and_other_sites_cannot_keep_or_remove(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    row = main.checks.keep(closed_check(DAY), "Northline Parcel", "NP1")
    outsider = TestClient(main.app, client=REMOTE)
    assert outsider.post("/checks", data=form()).status_code == 403
    assert outsider.post(f"/checks/{row['id']}/delete", data={}).status_code == 403
    owner = TestClient(main.app)
    assert owner.post("/checks", data=form(at="15:00"), headers=EVIL).status_code == 403
    assert owner.post(f"/checks/{row['id']}/delete", data={}, headers=EVIL).status_code == 403
    assert [r["id"] for r in main.checks.all()] == [row["id"]]


def test_back_is_only_a_local_path(monkeypatch):
    monkeypatch.setattr(main, "_visits", lambda d: busy(d))
    r = TestClient(main.app).post("/checks", data=form(back="//evil.example/x"), follow_redirects=False)
    assert r.headers["location"] == "/#record"


# ---- the demo: two earlier checks, and the desk offers the demo's scan ------------------------------------

def test_demo_seeds_two_earlier_checks_this_month_marked_demo(tmp_path):
    day = date(2026, 10, 20)
    kept = demo.seed_past_checks(tmp_path, day, TZ)
    assert len(kept) == 2 and all(k["demo"] and k["carrier"] == demo.CARRIER for k in kept)
    assert all(k["day"] < day.isoformat() and k["day"][:7] == "2026-10" for k in kept)
    assert all(k["posted_open"] and k["outcome"] in dispute.DISPUTE for k in kept)
    [r] = kc.carrier_record(kc.CheckBook(tmp_path / "checks.json").all(), day)
    assert r["text"] == f"{demo.CARRIER} scanned you closed twice in October, every time during posted hours."
    assert r["all_demo"]


def test_desk_offers_the_demo_scan_already_filled_in_only_on_a_demo_install():
    orders = demo.dated_orders(demo.SAMPLE.read_text(), DAY)
    c = TestClient(main.app)
    assert "Try the business-closed check" not in c.get("/", params={"date": DAY.isoformat()}).text
    main.DATA.mkdir(parents=True, exist_ok=True)
    scan = demo.note_demo_scan(main.DATA, DAY, orders)
    assert (main.DATA / demo.MARKER).exists()
    desk = c.get("/", params={"date": DAY.isoformat()}).text
    button = desk.split('class="try-demo', 1)[1].split("</form>", 1)[0]
    assert "Try the business-closed check" in button
    for k, v in scan.items():
        assert f'name="{k}" value="{v}"' in button
    assert scan == {"date": DAY.isoformat(), "carrier": demo.CARRIER, "tracking": "NP4471002958",
                    "attempt": demo.SCAN_AT, "claim": "business_closed"}
    # once the scan is checked, the verdict card is the top; the button doesn't repeat it
    shown = c.get("/", params={k: v for k, v in scan.items()}).text
    assert 'class="try-demo' not in shown
    (main.DATA / demo.MARKER).unlink()  # the same file without the demo's marker: not a demo install
    assert "Try the business-closed check" not in c.get("/", params={"date": DAY.isoformat()}).text
    json.loads((main.DATA / "demo-scan.json").read_text())  # still there, just not offered


def test_demo_prints_the_check_url_first():
    """What scripts/demo.py prints: the check's link before anything else, then the story."""
    import inspect
    printed = inspect.getsource(demo.main).split('print(f"""', 1)[1]
    assert printed.lstrip().startswith("Start here, the business-closed check:\n   {check_url}")
    assert printed.index("{check_url}") < printed.index("{desk}") < printed.index("{pack_url}")
