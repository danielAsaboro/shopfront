"""Drawn stills for demo door events: always captioned as drawings, never for a real Ring event, never in a zip."""

import io
import zipfile
from datetime import date, datetime

from app import illustrations as il
from app import main
from tests.test_pages import TZ, c, ring_courier, replayed_courier  # noqa: F401 - c is the page fixture

WEDNESDAY = date(2026, 10, 7)


def test_only_demo_events_without_a_frame_get_a_drawing():
    row = {"event_source": "synthetic", "type": "customer", "event_id": "syn-1"}
    assert il.url_for(row).startswith("/illustrations/customer")
    assert il.url_for({**row, "type": "doorbell_press"}) == "/illustrations/doorway.svg"  # a guess draws neutrally
    assert il.url_for({**row, "type": "unknown"}) == "/illustrations/doorway.svg"
    assert il.url_for({**row, "type": "after_hours"}) == "/illustrations/night.svg"
    assert il.url_for({**row, "type": "staff"}) == "/illustrations/staff.svg"
    assert il.url_for({"event_source": "ring-webhook-replay"}, "delivery") == "/illustrations/courier.svg"
    for real in ("ring", "ring-webhook", "ring-live-browser"):  # a real event with no frame shows no picture
        assert il.url_for({**row, "event_source": real}) is None
    assert il.url_for({**row, "frame_url": "/frames/x.jpg"}) is None  # a real frame takes the slot
    assert il.url_for(row) == il.url_for(dict(row))  # the same event always gets the same customer


def test_every_drawing_says_it_is_not_a_camera_frame():
    for name in il.files():
        svg = il.svg(name)
        assert svg.startswith("<svg") and svg.count(il.CAPTION) >= 2  # in the title and on the picture
        assert "Ring" not in svg and "Bedrock" not in svg and "frame." not in svg


def test_drawings_are_served_and_unknown_names_are_not(c):  # noqa: F811
    r = c.get("/illustrations/courier.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert c.get("/illustrations/customer-2.svg").status_code == 200
    assert c.get("/illustrations/nope.svg").status_code == 404


def test_desk_rows_show_a_captioned_still_for_demo_events_only(c):  # noqa: F811
    main.book.add(WEDNESDAY, "Hillside Mill", "10:00", "11:00", order_ref="PO-1041")
    replayed_courier(main.webhook_log, datetime(2026, 10, 7, 10, 12, tzinfo=TZ))
    page = c.get(f"/?date={WEDNESDAY}").text
    row = page.split("Hillside Mill</strong>")[1].split("</tr>")[0]
    assert 'src="/illustrations/courier.svg"' in row and "Illustration, not a camera frame" in row
    assert "<figcaption>Illustration</figcaption>" in row
    # the same order, matched to a courier Ring itself announced (no frame): no picture at all
    main.book.remove(main.book.for_day(WEDNESDAY)[0].id)
    main.webhook_log.path.unlink()
    main.book.add(WEDNESDAY, "Bay Mills", "10:00", "11:00")
    ring_courier(main.webhook_log, datetime(2026, 10, 7, 10, 12, tzinfo=TZ))
    row = c.get(f"/?date={WEDNESDAY}").text.split("Bay Mills</strong>")[1].split("</tr>")[0]
    assert "/illustrations/" not in row


def test_pack_strip_shows_the_door_around_the_scan_but_the_zip_holds_no_drawing(c):  # noqa: F811
    e = main.book.add(WEDNESDAY, "Dairy Co", "13:00", "16:00", order_ref="PO-9")
    q = f"date={WEDNESDAY}&order={e.id}&at=15:00&claim=business_closed"
    page = c.get(f"/dispute?{q}").text
    strip = page.split('class="stills"')[1].split("<table>")[0]
    assert strip.count("/illustrations/") >= 1 and strip.count("· illustration</figcaption>") >= 1
    assert "illustrations, not camera frames" in strip and "aren't in the zip" in strip
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/dispute.zip?{q}").content))
    assert not [n for n in z.namelist() if n.endswith(".svg") or "illustration" in n.lower()]
    assert "not camera frames, not evidence, and not in this zip" in z.read("README.txt").decode()


def test_every_page_has_an_inline_icon_and_favicon_ico_answers(c):  # noqa: F811
    page = c.get(f"/?date={WEDNESDAY}").text
    assert '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,' in page
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.text.startswith("<svg")


def test_phone_rows_keep_how_we_know(c):  # noqa: F811
    """At 390px the provenance column used to be hidden on delivery rows; it now stacks under the row."""
    page = c.get(f"/?date={WEDNESDAY}").text
    phone = page.split("@media (max-width: 560px)")[1].split("@media print")[0]
    assert ".receipts td.why, .log td.why { display: block;" in phone
