"""Opening hours and clock times as owners type them."""

from datetime import date, time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.hours import normalize_clock, normalize_range, parse_hours, posted_hours
from app.ring.synthetic import SyntheticRingClient
from app.shop import hours_from_form

DAY = date(2026, 10, 7)  # a Wednesday


@pytest.mark.parametrize("typed,stored", [
    ("8-18", "8-18"), ("7:30-15", "7:30-15"), ("07:30-15:00", "7:30-15"), ("7am-3pm", "7-15"),
    ("7 AM - 3 PM", "7-15"), ("7.30am-3pm", "7:30-15"), ("12am-12pm", "0-12"), ("0-24", "0-24"),
    ("9–17", "9-17"), ("9 to 17", "9-17"), ("10:15-24:00", "10:15-24"),
])
def test_ranges_owners_type(typed, stored):
    assert normalize_range(typed) == stored


@pytest.mark.parametrize("typed,why", [
    ("19-8", "closes before it opens"), ("8-8", "closes before it opens"), ("8", "a range"),
    ("8-25", "isn't a time"), ("lunch", "a range"), ("7:75-9", "isn't a time"), ("13pm-14", "isn't a time"),
])
def test_bad_ranges_say_why_in_plain_words(typed, why):
    with pytest.raises(ValueError) as err:
        normalize_range(typed)
    assert why in str(err.value) and typed in str(err.value)


def test_form_errors_name_the_day():
    with pytest.raises(ValueError) as err:
        hours_from_form(["8-18", "8-18", "19-8", "8-18", "8-18", "9-16", ""])
    assert str(err.value).startswith("Wed:")
    assert hours_from_form(["7am-3pm"] * 6 + [""]) == "7-15,7-15,7-15,7-15,7-15,7-15,closed"


def test_minutes_and_midnight_closing():
    h = parse_hours("7:30-15,8-18,0-24,8-18,8-19,9-16,closed")
    assert posted_hours(h, date(2026, 10, 5)) == (time(7, 30), time(15))  # Monday
    lo, hi = posted_hours(h, DAY)  # Wednesday 0-24: open all day, including the last second
    assert lo == time(0) and lo <= time(23, 59, 59, 500000) < hi
    assert posted_hours(h, date(2026, 10, 11)) is None


def test_synthetic_day_for_a_shop_open_around_the_clock():
    ring = SyntheticRingClient(main.settings.tz, parse_hours(",".join(["0-24"] * 7)))
    assert ring.events_for_day(DAY)  # used to crash: hour must be in 0..23
    ring = SyntheticRingClient(main.settings.tz, parse_hours(",".join(["7:30-15:45"] * 7)))
    assert ring.events_for_day(DAY)


def test_whole_hour_days_generate_the_same_demo_as_before():
    # the demo data for ordinary hours mustn't change under the minute-precise parser
    ring = SyntheticRingClient(main.settings.tz, parse_hours(""))
    evs = ring.events_for_day(DAY)
    assert [e.timestamp.strftime("%H:%M:%S") for e in evs][:3] == ["01:24:00", "07:51:00", "08:14:07"]


@pytest.mark.parametrize("typed,stored", [("9", "09:00"), ("9:00", "09:00"), ("09:00", "09:00"),
                                          ("9:00 AM", "09:00"), ("9am", "09:00"), ("2:30 pm", "14:30"),
                                          ("12:15 AM", "00:15"), ("", "")])
def test_clock_times(typed, stored):
    assert normalize_clock(typed) == stored


def test_setup_keeps_what_the_owner_typed_when_hours_are_wrong():
    c = TestClient(main.app)
    form = {"name": "Shop", **{f"h{i}": "8-18" for i in range(7)}, "h2": "19-8"}
    r = c.post("/setup", data=form, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "Wed: " in page and 'value="19-8"' in page
