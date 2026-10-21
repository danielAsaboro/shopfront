"""Orders and invoices in: the CSV shapes owners actually export."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.deliveries import DeliveryBook
from app.orders import parse_orders

DAY = date(2026, 10, 7)


def amount(csv):
    return parse_orders(csv)["rows"][0].amount


def test_one_amount_per_order_is_kept_as_written():
    assert amount("day,supplier,order_ref,amount\n2026-10-07,Acme,PO-1,$212.40\n") == "$212.40"
    assert amount("day,supplier,order_ref,amount\n2026-10-07,Acme,PO-1,$212.40\n2026-10-07,Acme,PO-1,\n") == "$212.40"


@pytest.mark.parametrize("a,b", [("$120", "$12"), ("$10.00", "$10.00"), ("$212.40", "$12.40")])
def test_several_line_amounts_are_not_a_total(a, b):
    # docs/receiving-desk.md: Shopfront never sums; several amounts for one order leave it blank
    assert amount(f"day,supplier,order_ref,amount\n2026-10-07,Acme,PO-1,{a}\n2026-10-07,Acme,PO-1,{b}\n") == ""


def test_day_first_works_with_two_digit_years():
    csv = "day,supplier\n05/10/26,X\n"
    assert parse_orders(csv, day_first=True)["rows"][0].day == "2026-10-05"
    assert parse_orders(csv)["rows"][0].day == "2026-05-10"


def test_the_delivery_date_wins_over_a_plain_or_order_date():
    assert parse_orders("Date,Vendor,Expected Delivery Date\n2026-10-01,X,2026-10-07\n")["rows"][0].day == "2026-10-07"
    out = parse_orders("Order Date,Vendor\n2026-10-01,X\n")  # only an order date: use it, and say so
    assert out["rows"][0].day == "2026-10-01" and out["columns"]["day"] == "Order Date"


@pytest.mark.parametrize("typed", ["9:00", "9:00 AM", "9am", "09:00"])
def test_windows_as_people_write_them(typed):
    r = parse_orders(f"day,supplier,window_start,window_end\n2026-10-07,X,{typed},11:30 am\n")["rows"][0]
    assert (r.window_start, r.window_end) == ("09:00", "11:30")


@pytest.mark.parametrize("sep", [";", "\t", "|"])
def test_other_delimiters(sep):
    out = parse_orders(f"day{sep}supplier{sep}amount\n2026-10-07{sep}Bay Mills{sep}12,50 €\n")
    assert out["rows"][0].supplier == "Bay Mills" and out["rows"][0].amount == "12,50 €"


def test_reimport_without_order_numbers_does_not_duplicate(tmp_path):
    csv = "day,supplier,items,amount\n2026-10-07,Smithers Oasis,Floral foam x2 boxes,$64.10\n"
    book = DeliveryBook(tmp_path / "e.json")
    assert book.import_rows(parse_orders(csv)["rows"]) == {"added": 1, "updated": 0}
    assert book.import_rows(parse_orders(csv)["rows"]) == {"added": 0, "updated": 1}
    assert len(book.for_day(DAY)) == 1


def test_import_banner_says_where_dates_came_from():
    r = TestClient(main.app).post("/orders/import", data={"csv": "Order Date;Vendor\n2026-10-07;X\n"},
                                  follow_redirects=False)
    assert "Order%20Date" in r.headers["location"] and main.book.for_day(DAY)[0].supplier == "X"


def test_manual_delivery_window_reads_am_pm():
    c = TestClient(main.app)
    c.post("/deliveries", data={"day": "2026-10-07", "supplier": "Flour", "window_start": "9:00 AM",
                                "window_end": "11am"}, follow_redirects=False)
    e = main.book.for_day(DAY)[0]
    assert (e.window_start, e.window_end) == ("09:00", "11:00")
