"""Stills on the desk and the pack: a frame Shopfront holds, with its provenance, or nothing. Never a drawing."""

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

from app import main
from tests.test_pages import TZ, c, ring_courier, replayed_courier  # noqa: F401 - c is the page fixture

WEDNESDAY = date(2026, 10, 7)


def test_drawings_are_gone_and_their_route_with_them(c):  # noqa: F811
    """Round 3: a drawn "customer (guess)" on a dispute pack added doubt, not evidence. Shopfront draws nothing."""
    assert c.get("/illustrations/courier.svg").status_code == 404
    assert not (Path(main.__file__).parent / "illustrations.py").exists()


def test_desk_rows_show_no_drawings_only_real_frames(c):  # noqa: F811
    """Round 2: the 88px drawings beside each delivery row didn't read, so the desk drops them; a frame
    Shopfront holds still shows there."""
    main.book.add(WEDNESDAY, "Hillside Mill", "10:00", "11:00", order_ref="PO-1041")
    replayed_courier(main.webhook_log, datetime(2026, 10, 7, 10, 12, tzinfo=TZ))
    page = c.get(f"/?date={WEDNESDAY}").text
    row = page.split("Hillside Mill</strong>")[1].split("</tr>")[0]
    assert "/illustrations/" not in row and "Illustration" not in row
    assert "/illustrations/" not in page.split('id="dlv"')[1].split("</table>")[0]


def test_a_pack_without_a_frame_says_so_in_one_line_and_draws_nothing(c):  # noqa: F811
    e = main.book.add(WEDNESDAY, "Dairy Co", "13:00", "16:00", order_ref="PO-9")
    q = f"date={WEDNESDAY}&order={e.id}&at=15:00&claim=business_closed"
    page = c.get(f"/dispute?{q}").text
    assert page.count("No camera frame for this scan.") == 1
    assert "/illustrations/" not in page and "llustration" not in page and 'class="stills"' not in page
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/dispute.zip?{q}").content))
    assert not [n for n in z.namelist() if n.endswith(".svg") or "illustration" in n.lower()]
    assert "illustration" not in z.read("README.txt").decode().lower()
    assert "Camera frame: none for this scan" in z.read("pack.txt").decode()


def test_a_pack_shows_real_frames_around_the_scan_with_their_provenance(c):  # noqa: F811
    from app import pages
    from app.models import DoorEvent

    at = datetime(2026, 10, 7, 14, 50, tzinfo=TZ)
    e = DoorEvent("ring-evt-1", "dev", at, "motion", "ring")
    main.frames.save(e.id, b"\xff\xd8frame")
    p = {"claim": {"time": "2026-10-07 15:00"}, "frame": None,
         "timeline": [{"event_id": e.id, "time": "14:50:00", "frame_url": main.frames.url(e.id), "type": "customer",
                       "confirmed": True, "labelled_by": "bedrock", "event_source": "ring"},
                      {"event_id": "no-frame", "time": "14:59:00", "frame_url": None, "type": "customer",
                       "confirmed": False, "labelled_by": "rules", "event_source": "synthetic"}]}
    assert [t["event_id"] for t in pages.pack_stills(p)] == [e.id]  # a frame or nothing; never a drawing


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
