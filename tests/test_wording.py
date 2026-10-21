"""No draft Shopfront writes accuses anyone: every outcome, every recipient, every data source."""

import itertools
from dataclasses import replace
from datetime import date, datetime

import pytest

from app import dispute, wording
from app import reconcile as rc
from app.deliveries import Expected, FrameStore, receipts
from tests.test_dispute_pack import LA, NOW, OPEN, confirmed_lunch, pitch_day, visit

DAY = date(2026, 10, 14)
HOURS = "08:00–18:00"
ORDER = Expected("exp-1042", DAY.isoformat(), "Northside Flour", "12:00", "14:00", order_ref="PO-1042",
                 carrier="Swift Parcel", tracking="SP123", items="10 x 25kg flour", amount="412.50")
WIDE = replace(ORDER, id="exp-wide", window_start="08:00", window_end="18:00")
PAPER = Expected("exp-p", DAY.isoformat(), "Pinecrest Paper Goods")

# (name, claim, scan, visits, the outcome the door gives[, Ring's device_online/offline notices])
ONLINE = [{"at": datetime(2026, 10, 14, 7, 55, tzinfo=LA), "online": True, "replayed": False}]
OFFLINE = ONLINE + [{"at": datetime(2026, 10, 14, 12, 35, tzinfo=LA), "online": False, "replayed": False},
                    {"at": datetime(2026, 10, 14, 13, 5, tzinfo=LA), "online": True, "replayed": False}]
SCANS = [
    ("courier at door", rc.ATTEMPTED, (12, 40), lambda: [visit(12, 42, "delivery", "bedrock", eid="c")],
     rc.COURIER_AT_DOOR),
    ("unverified attempt", rc.ATTEMPTED, (12, 40), lambda: [visit(12, 41, "delivery", "rules", ring_event="ding")],
     rc.UNVERIFIED),
    ("someone not a courier", rc.ATTEMPTED, (12, 40), lambda: [visit(12, 45, "customer", "bedrock")],
     rc.SOMEONE_NOT_COURIER),
    ("nothing recorded", rc.ATTEMPTED, (12, 40), lambda: [visit(12, 15, "customer", "bedrock")], rc.NOTHING_RECORDED),
    ("door silent, attempt", rc.ATTEMPTED, (12, 40), lambda: [visit(9, 0, "customer", "bedrock")], rc.NO_COVERAGE),
    ("camera offline, attempt", rc.ATTEMPTED, (12, 40), lambda: [visit(12, 15, "customer", "bedrock")],
     rc.CAMERA_OFFLINE, OFFLINE),
    ("no door data, attempt", rc.ATTEMPTED, (12, 40), lambda: [], rc.NO_DOOR_DATA),
    ("courier later, attempt", rc.ATTEMPTED, (12, 40), pitch_day, rc.COURIER_LATER),
    ("open, no courier", rc.BUSINESS_CLOSED, (12, 40), confirmed_lunch, rc.OPEN_NO_COURIER),
    ("open, rules-only lunch", rc.BUSINESS_CLOSED, (12, 40), lambda: [visit(12, m) for m in (13, 20, 59)],
     rc.OPEN_NO_COURIER),
    ("open, courier came", rc.BUSINESS_CLOSED, (12, 40),
     lambda: confirmed_lunch() + [visit(12, 44, "delivery", "bedrock", eid="c")], rc.OPEN_COURIER_CAME),
    ("open by hours only", rc.BUSINESS_CLOSED, (12, 40), lambda: [visit(9, 0)], rc.OPEN_BY_HOURS_ONLY, ONLINE),
    ("door silent, closed scan", rc.BUSINESS_CLOSED, (12, 40), lambda: [visit(9, 0)], rc.NO_COVERAGE),
    ("camera offline, closed scan", rc.BUSINESS_CLOSED, (12, 40), confirmed_lunch, rc.CAMERA_OFFLINE, OFFLINE),
    ("posted closed", rc.BUSINESS_CLOSED, (19, 30), confirmed_lunch, rc.POSTED_CLOSED),
    ("unverified closed scan", rc.BUSINESS_CLOSED, (12, 40),
     lambda: [visit(12, 41, "delivery", "rules", ring_event="ding")], rc.UNVERIFIED),
    ("no door data, closed scan", rc.BUSINESS_CLOSED, (12, 40), lambda: [], rc.NO_DOOR_DATA),
    # the pitch day: busy lunch, rule-guessed motion near 12:40, Ring's courier at 13:02. The claim is "closed",
    # so a busy door answers it; the order's own courier later makes it a late-delivery note
    ("busy door, courier later, closed scan", rc.BUSINESS_CLOSED, (12, 40), pitch_day, rc.OPEN_DOOR_BUSY),
    ("busy door, unidentified press, closed scan", rc.BUSINESS_CLOSED, (12, 40),
     lambda: [visit(12, 20), visit(12, 41, "delivery", "rules", ring_event="ding"), visit(13, 5)], rc.OPEN_DOOR_BUSY),
    ("quiet door, courier later, closed scan", rc.BUSINESS_CLOSED, (12, 40),
     lambda: [visit(9, 0), visit(15, 2, "delivery", "ring", src="ring-webhook", eid="pkg")], rc.COURIER_LATER),
]


def _check(claim, hh, mm, vs, status=None):
    when = datetime(2026, 10, 14, hh, mm, tzinfo=LA)
    if claim == rc.BUSINESS_CLOSED:
        return rc.check_closed_scan(when, vs, OPEN, now=NOW, status=status)
    return rc.check_attempt(when, vs, now=NOW, status=status)


SOURCES = ["ring", "ring-webhook", "synthetic", "ring-webhook-replay", "ring-live-browser"]
ORDERS = [None, ORDER, WIDE]
CARRIERS = ["", "Swift Parcel"]


def _with_source(vs, src):
    for v in vs:
        v.event.source = src
    return vs


def _pack(visits, check=None, order=None, carrier="", others=(), frames=None):
    frames = frames or FrameStore.__new__(FrameStore)
    if not hasattr(frames, "dirs"):
        frames.dirs = []
    receipt = None
    if order:
        rows = receipts([order, *others], visits, OPEN, frames, now=NOW)["expected"]
        receipt = next(x for x in rows if x["id"] == order.id)
    return dispute.build_pack("Corner Bakery", DAY, visits, frames, HOURS, check, order, receipt, carrier,
                              order.tracking if order else "", tz=LA)


def _texts(p):
    out = [p.get("no_draft_reason") or "", (p.get("finding") or {}).get("text", ""), dispute.as_text(p)]
    for d in p["drafts"]:  # the supplier's and the carrier's: the owner may send either
        out += [d["subject"], d["body"]]
    return out


def scan_cases():
    for (name, claim, (hh, mm), make, outcome, *status), src, order, carrier, also in itertools.product(
            SCANS, SOURCES, ORDERS, CARRIERS, [(), (PAPER,)]):
        yield pytest.param(claim, hh, mm, make, outcome, status[0] if status else None, src, order, carrier, also,
                           id=f"{name}-{src}-{order.id if order else 'no order'}-{carrier or 'no carrier'}"
                              f"{'-also fits' if also else ''}")


@pytest.mark.parametrize("claim,hh,mm,make,outcome,status,src,order,carrier,also", list(scan_cases()))
def test_no_scan_draft_accuses(claim, hh, mm, make, outcome, status, src, order, carrier, also):
    vs = _with_source(make(), src)
    check = _check(claim, hh, mm, vs, status)
    assert check["outcome"] == outcome
    p = _pack(vs, check, order, carrier, also)
    for text in _texts(p):
        assert wording.accusing(text) == [], text


def test_every_outcome_is_covered():
    """The parametrisation above reaches every outcome the checks and the pack can give, drafts included."""
    seen, drafts = set(), set()
    for _, claim, (hh, mm), make, _outcome, *status in SCANS:
        for order in ORDERS:
            vs = make()
            p = _pack(vs, _check(claim, hh, mm, vs, status[0] if status else None), order)
            seen.add(p["finding"]["outcome"])
            if p["draft"]:
                drafts.add(p["finding"]["outcome"])
    every = {v for k, v in vars(rc).items() if k.isupper() and isinstance(v, str) and v.islower() and "_" in v
             and v not in rc.REFUSED and v not in (rc.ATTEMPTED, rc.BUSINESS_CLOSED)}
    assert every | {dispute.DELIVERED_AFTER_SCAN} == seen
    assert drafts == (dispute.DISPUTE - {rc.COURIER_LATER}) | {dispute.DELIVERED_AFTER_SCAN}


ORDER_CASES = [  # (name, visits, staff tap, door status)
    ("short", lambda: [visit(12, 30, "delivery", "bedrock", eid="c")], ("short", "2 sacks missing"), "seen"),
    ("damaged", lambda: [visit(12, 30, "delivery", "bedrock", eid="c")], ("damaged", "torn bag"), "seen"),
    ("damaged, no note", lambda: [visit(12, 30, "delivery", "bedrock", eid="c")], ("damaged", ""), "seen"),
    ("not seen, nothing recorded", lambda: [visit(9, 0, "customer", "bedrock")], None, "not_seen"),
    ("not seen, all looked at", lambda: [visit(12, 30, "customer", "bedrock")], None, "not_seen"),
    ("not seen, a guess in the window", lambda: [visit(12, 30)], None, "not_seen"),
    ("possible", lambda: [visit(12, 30, "delivery", "rules", ring_event="ding")], None, "possible"),
    ("seen, nothing wrong", lambda: [visit(12, 30, "delivery", "ring", eid="c")], None, "seen"),
]


@pytest.mark.parametrize("src", SOURCES)
@pytest.mark.parametrize("name,make,tap,status", ORDER_CASES, ids=[c[0] for c in ORDER_CASES])
def test_no_order_draft_accuses(name, make, tap, status, src):
    order = replace(ORDER, received={"status": tap[0], "note": tap[1], "at": "2026-10-14T13:05:00-07:00"}
                    if tap else None)
    p = _pack(_with_source(make(), src), order=order)
    assert p["order"]["door_status"] == status
    for text in _texts(p):
        assert wording.accusing(text) == [], text


def test_an_accusing_staff_note_stays_in_the_pack_but_not_the_draft():
    order = replace(ORDER, received={"status": "short", "note": "they lied about the count, it's fraud",
                                     "at": "2026-10-14T13:05:00-07:00"})
    p = _pack([visit(12, 30, "delivery", "bedrock", eid="c")], order=order)
    assert wording.accusing(p["draft"]["body"]) == [] and "staff note is in the attached pack" in p["draft"]["body"]
    assert p["order"]["received"]["note"].startswith("they lied")  # the owner's own words, kept in the pack


@pytest.mark.parametrize("text", [
    "lied", "LIE", "Lies", "a liar", "fraud", "Fraudulent", "fake", "FAKED", "falsified", "dishonest", "cheat",
    "cheating", "l1ed", "1ie", "|ie", "fr@ud", "fr4ud", "f4ke", "ch3at", "d1shonest",
    "fаke",            # Cyrillic а
    "сheat",           # Cyrillic с
    "frαud",           # Greek α
    "ｆｒａｕｄ",  # full-width
    "li​e",            # zero-width space inside
    "lïe", "fáke",  # accents, precomposed and combining
    "the scan was fаkеd.",
])
def test_lookalikes_are_caught(text):
    assert wording.accusing(text), text


@pytest.mark.parametrize("text", [
    "Hello Northside Flour, please ask your supplier", "Hillside Mill", "Valley Creamery", "earlier", "reliable",
    "belief", "applied", "the delivery window", "Pinecrest Paper Goods", "client", "PO-1041", "NP4471002958",
    "Fakenham Road", "cheddar", "Lyon", "lieu", "relied",
])
def test_ordinary_words_pass(text):
    assert wording.accusing(text) == [], text


def test_a_scan_pack_offers_the_supplier_and_the_carrier_a_draft():
    """Round 2: the owner picks who to write to. With the shop's order, the supplier's draft (a receiving
    issue) comes first and the carrier's (its own scan) second; with no order, only the carrier's."""
    for _, claim, (hh, mm), make, _outcome, *status in SCANS:
        for order in ORDERS:
            vs = make()
            p = _pack(vs, _check(claim, hh, mm, vs, status[0] if status else None), order, "Swift Parcel")
            if not p["drafts"]:
                assert p["draft"] is None
                continue
            keys = [d["key"] for d in p["drafts"]]
            assert keys == (["supplier", "carrier"] if order else ["carrier"]), (claim, hh, mm, order)
            assert p["draft"] is p["drafts"][0]
            carrier = p["drafts"][-1]
            assert carrier["to"] == "Swift Parcel" and carrier["body"].startswith("Hello Swift Parcel,")
            assert carrier["label"] == "To Swift Parcel, the carrier"
            if order:
                assert p["drafts"][0]["to"] == order.supplier
