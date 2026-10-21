"""The owner's pages: shared header and banner, error pages, the receiving desk, setup and the dispute pack.
Synthetic events and fakes only; never Ring or Bedrock."""

import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import pages
from app.briefing import classify_day
from app.classify.bedrock import BedrockClassifier
from app.classify.rules import RulesClassifier
from app.deliveries import DeliveryBook, FrameStore
from app.hours import parse_hours
from app.ring import RingSource
from app.ring.api import RingApiClient, RingAuth
from app.ring.synthetic import SyntheticRingClient
from app.ring.webhooks import WebhookLog
from app.shop import Profile, ShopProfile

TZ = ZoneInfo("America/Los_Angeles")
SUNDAY = date(2026, 10, 11)  # closed; the synthetic courier rings at 11:xx
WEDNESDAY = date(2026, 10, 7)  # open 8-18
PAGES = ["/", "/setup", "/connect", "/live", f"/dispute?date={WEDNESDAY}&at=03:17&claim=attempted", "/no-such-page"]


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", RingSource(main.settings, tmp_path / "no-token"))
    monkeypatch.setattr(main, "classifier", RulesClassifier())
    monkeypatch.setattr(main, "profile", ShopProfile(tmp_path / "shop.json", "Corner Bakery", ""))
    monkeypatch.setattr(main, "book", DeliveryBook(tmp_path / "expected.json"))
    monkeypatch.setattr(main, "frames", FrameStore(tmp_path))
    monkeypatch.setattr(main, "webhook_log", WebhookLog(tmp_path / "hooks.jsonl"))
    monkeypatch.setattr(main, "_aws_creds_available", lambda region: False)
    return TestClient(main.app)


def ring_courier(log: WebhookLog, at: datetime, replayed: bool = False) -> None:
    """A row as the webhook log stores Ring's package_delivery Smart Alert: a courier Ring confirmed."""
    ms = int(at.timestamp() * 1000)
    log.path.parent.mkdir(parents=True, exist_ok=True)
    row = {"request_id": f"r-{ms}", "type": "motion_detected", "id": f"dev_{ms}", "device": "dev",
           "timestamp": ms, "sub_type": "package_delivery"}
    with log.path.open("a") as f:
        f.write(json.dumps({**row, **({"replayed": True} if replayed else {})}) + "\n")


def replayed_courier(log: WebhookLog, at: datetime) -> None:
    """The same, sent by Shopfront's demo feeder."""
    ring_courier(log, at, replayed=True)


def dead_ring(tmp_path):
    """A live Ring client whose token Ring refuses (401), behind a source that hands it out."""
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="expired")))
    ring = RingApiClient(auth=RingAuth(access_token="old", http=http), tz=TZ, device_id="dev", http=http)

    class Source:
        _key = ("live", "old", "")

        def get(self):
            return ring

        def pasted_token(self):
            return "old"
    return Source()


# ---- every page --------------------------------------------------------------------------------

def test_every_page_has_the_nav_banner_and_the_saved_shop_name(c):
    main.profile.save(Profile("Rosa's Florist", "8-18,8-18,8-18,8-18,8-18,closed,closed"))
    for path in PAGES:
        page = c.get(path).text
        assert 'nav aria-label="Shopfront"' in page and 'href="/setup"' in page and 'href="/"' in page, path
        assert "Demo data: these door events were made up on this computer" in page, path
        assert '<span class="b-short">Demo data, not from Ring.' in page, path  # one line on a phone
        assert "Rosa&#39;s Florist" in page and "Corner Bakery" not in page, path


def test_replays_are_named_in_the_banner(c):
    replayed_courier(main.webhook_log, datetime(2026, 10, 7, 13, 2, tzinfo=TZ))
    for path in ["/setup", "/live", f"/?date={WEDNESDAY}"]:
        assert "replayed by Shopfront's feeder" in c.get(path).text, path


def test_dispute_banner_prints():
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    assert re.search(r"@media print \{[^@]*\.banner \{[^}]*print-color-adjust: exact", css)


# ---- error pages -------------------------------------------------------------------------------

@pytest.mark.parametrize("path,code,said", [
    ("/no-such-page", 404, "Page not found"),
    ("/?date=0001-01-01", 400, "That date doesn&#39;t look right"),
    ("/?date=9999-12-31", 400, "That date doesn&#39;t look right"),
    ("/?date=nope", 400, "That date doesn&#39;t look right"),
    (f"/?date={WEDNESDAY}&attempt=late", 400, "That time doesn&#39;t look right"),
    ("/dispute", 400, "A dispute pack needs something to dispute"),
    (f"/dispute?date={WEDNESDAY}&order=exp-gone", 404, "That order isn&#39;t here"),
])
def test_errors_are_pages_in_plain_words(c, path, code, said):
    r = c.get(path)
    assert r.status_code == code and r.headers["content-type"].startswith("text/html")
    assert said in r.text and 'nav aria-label="Shopfront"' in r.text and "<details" in r.text


def test_a_form_missing_a_field_is_a_page_and_the_api_stays_json(c):
    r = c.post("/connect", data={})
    assert r.status_code == 422 and "Something in that request was missing" in r.text
    assert c.get("/api/briefing?date=0001-01-01").status_code == 400
    assert c.get("/api/briefing?date=0001-01-01").json()["detail"].startswith("pick a date")


def test_an_unexpected_failure_is_a_page(c, monkeypatch):
    monkeypatch.setattr(main, "_briefing", lambda *a, **k: 1 / 0)
    r = TestClient(main.app, raise_server_exceptions=False).get(f"/?date={WEDNESDAY}")
    assert r.status_code == 500 and "Something went wrong on Shopfront&#39;s side" in r.text


# ---- the receiving desk ------------------------------------------------------------------------

def test_first_run_panel_until_there_are_orders(c):
    page = c.get(f"/?date={WEDNESDAY}").text
    assert "What Shopfront does" in page and 'href="/setup#orders"' in page and 'href="#atth"' in page
    # one glance: what it does in one short line, then the next step as the page's one primary button
    panel = page.split('class="first-run"')[1].split("</section>")[0]
    assert '<p class="first">Check every delivery against your Ring doorbell.</p>' in panel
    assert '<a class="primary" href="/setup#orders">Add your orders</a>' in panel
    assert 'class="figures"' not in page  # "— / 0 / 07:55" means nothing before the first order
    main.book.add(WEDNESDAY, "Bay Mills")
    page = c.get(f"/?date={WEDNESDAY}").text
    assert "What Shopfront does" not in page and 'class="figures"' in page


def test_no_orders_empty_state_survives_couriers_nobody_listed(c):
    ring_courier(main.webhook_log, datetime(2026, 10, 11, 11, 40, tzinfo=TZ))
    page = c.get(f"/?date={SUNDAY}").text
    assert "Courier nobody listed" in page
    assert "No orders for this day" in page and 'href="/setup#orders">Import your orders' in page


def test_receiving_desk_leads_and_the_traffic_chart_is_folded(c):
    """Round 2: the scan check sits right under the lead, above the figures and the deliveries, so the story
    is on screen in the first 30 seconds on a phone too."""
    main.book.add(WEDNESDAY, "Bay Mills")
    page = c.get(f"/?date={WEDNESDAY}").text
    assert page.index('class="lead"') < page.index('id="atth"') < page.index('class="figures"') \
        < page.index('id="dlv"') < page.index("Customers by hour")
    assert re.search(r"<details class=\"block\">\s*<summary><h2>The rest of the day", page)


def test_staff_tap_undo_label_and_shop_local_time(c):
    e = main.book.add(SUNDAY, "Sunday bread")
    # stored tz-aware; a UTC time must still show the shop's 09:12, never the offset
    main.book.mark_received(e.id, "short", "2 crates", datetime(2026, 10, 11, 16, 12, tzinfo=timezone.utc))
    page = c.get(f"/?date={SUNDAY}").text
    assert "Staff marked short at 09:12: 2 crates" in page
    assert ">Undo</button>" in page and "built-in method" not in page
    pack = c.get(f"/dispute?date={SUNDAY}&order={e.id}").text
    assert "staff marked short at 09:12" in pack and "at 07:00" not in pack and "16:12" not in pack
    assert "unpacked it at 09:12" in c.get(f"/api/dispute?date={SUNDAY}&order={e.id}").json()["draft"]["body"]


def test_tapping_through_the_page(c):
    ring_courier(main.webhook_log, datetime(2026, 10, 11, 11, 40, tzinfo=TZ))
    e = main.book.add(SUNDAY, "Sunday bread")
    page = c.get(f"/?date={SUNDAY}").text  # Ring's courier matched: the full set of taps
    form = page.split(f'action="/deliveries/{e.id}/received"')[1].split("</form>")[0]
    assert all(f'value="{k}"' in form for k in ("ok", "short", "damaged")) and 'name="note"' in form
    r = c.post(f"/deliveries/{e.id}/received", data={"status": "ok"})
    assert r.status_code == 200 and "Staff marked all there at" in r.text and ">Undo</button>" in r.text
    r = c.post(f"/deliveries/{e.id}/received", data={"status": "clear"})
    assert 'name="status" value="ok"' in r.text


def test_rows_in_time_order_and_late_matches_in_warn(c):
    ring_courier(main.webhook_log, datetime(2026, 10, 11, 11, 40, tzinfo=TZ))
    main.book.add(SUNDAY, "Late flour", "06:00", "07:00")  # Ring's 11:40 courier is outside this window
    main.book.add(SUNDAY, "Evening veg", "19:00", "20:00")  # never seen: sorts by its window
    page = c.get(f"/?date={SUNDAY}").text
    rows = re.findall(r'<td class="t[^"]*">([^<]+)</td>', page.split('id="dlv"')[1].split('</table>')[0])
    assert rows == sorted(rows)
    assert re.search(r'<td class="t warn">11:40</td>', page) and "outside the window" in page


def test_a_rules_match_is_only_possible_but_staff_can_still_say_what_arrived(c):
    e = main.book.add(SUNDAY, "Sunday bread", "11:00", "12:00")  # the synthetic 11:28 press fits the window
    page = c.get(f"/?date={SUNDAY}").text
    assert "Possible courier, not confirmed by a camera frame" in page and "No courier seen" not in page
    assert "A doorbell press at 11:28 fits the window; no camera frame confirmed a courier." in page
    assert re.search(r'<td class="t muted">11:28</td>', page)  # muted, not the warn colour
    assert f'action="/deliveries/{e.id}/received"' in page  # the staff tap is the owner's own confirmation
    # round 2: the full All there / Short / Damaged set is for a confirmed courier; a guess gets one quiet link
    form = page.split(f'action="/deliveries/{e.id}/received"')[1].split("</form>")[0]
    assert 'value="ok" class="quiet"' in form and ">It arrived</button>" in form
    assert 'value="short"' not in form and 'value="damaged"' not in form and 'name="note"' not in form
    assert f"order={e.id}" not in page  # no dispute pack from a guess
    assert "0<small> confirmed</small>" in page and "1 possible, of 1 expected" in page  # not counted as seen


def test_an_order_not_due_yet_is_quiet(c):
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    main.book.add(tomorrow, "Night bakery", "06:00", "07:00")
    page = c.get(f"/?date={tomorrow}").text
    assert "Not due yet" in page and "No courier seen for Night bakery" not in page


def test_claim_row_fields_are_labelled_and_the_button_never_wraps(c):
    page = c.get(f"/?date={WEDNESDAY}").text
    form = page.split('id="atth"')[1].split("</form>")[0]
    for label in ("Scan", "At", "Carrier (optional)", "Tracking no. (optional)"):
        assert 'class="field' in form and f">{label}<" in form
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    assert "white-space: nowrap" in css.split("button, .primary, .secondary {")[1].split("}")[0]
    assert ".fields > button, .fields > .btn { flex: none; }" in css


def test_inputs_are_labelled(c):
    ring_courier(main.webhook_log, datetime(2026, 10, 11, 11, 40, tzinfo=TZ))  # the note input needs a courier
    main.book.add(SUNDAY, "Sunday bread")
    page = c.get(f"/?date={SUNDAY}").text
    assert re.search(r'<label class="field">Day<input type="date"', page)
    note = re.search(r'<input id="(note-[^"]+)" name="note"', page).group(1)
    assert f'<label class="sr" for="{note}">' in page


def test_footer_says_what_labelled_the_day(c):
    page = c.get(f"/?date={WEDNESDAY}").text
    footer = page.split('<footer class="proof">')[1]
    assert "Bedrock" not in footer.split("<details")[0] and "by time-of-day rules" in footer


def test_ask_unavailable_is_one_plain_sentence(c):
    page = c.get(f"/?date={WEDNESDAY}").text
    assert "Ask needs an AWS sign-in with Bedrock access; it isn't available on this machine." in page


def test_dashboard_degrades_when_the_ring_token_expires(c, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "source", dead_ring(tmp_path))
    day = datetime.now(TZ).date() - timedelta(days=1)
    main.book.add(day, "Bay Mills", "12:00", "15:00", order_ref="PO-1042")
    main.book.add(day, "Dairy Co", "07:00", "07:30", order_ref="PO-1043")
    replayed_courier(main.webhook_log, datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=13, minutes=2))
    r = c.get(f"/?date={day}&claim=business_closed&attempt=12:40")
    assert r.status_code == 200
    assert "Ring sign-in expired, so this is your orders" in r.text and "(TOKEN_REJECTED" in r.text
    assert 'href="/connect">reconnect Ring' in r.text and r.text.count('class="banner warn-bg"') == 2  # replay + Ring
    assert "PO-1042" in r.text and "13:02" in r.text  # the order and Ring's pushed courier still show
    assert "1 of 2 expected deliveries confirmed by Ring" in r.text and "No courier seen" not in r.text
    assert "Not confirmed: door history unavailable" in r.text and "order=" not in r.text  # no pack from a guess
    assert "Checking a scan needs the door history from Ring" in r.text and "Build the dispute pack" not in r.text
    # the other pages carry the notice too, and /setup says what Ring really said
    assert "Ring sign-in expired" in c.get("/live").text
    setup = c.get("/setup").text
    assert "Ring refused the saved sign-in (TOKEN_REJECTED)" in setup and "Connected to Ring" not in setup


# ---- setup -------------------------------------------------------------------------------------

def test_setup_is_one_orders_step_with_both_inputs(c):
    page = c.get("/setup").text
    orders = page.split('id="orders"')[1].split('id="watch"')[0]
    assert 'action="/orders/import"' in orders and 'action="/deliveries"' in orders
    assert page.count('class="n"') == 4


def test_setup_messages_land_next_to_their_section(c):
    r = c.post("/orders/import", files={"file": ("o.csv", "day,supplier\n2026-09-01,Bay Mills\n", "text/csv")})
    orders = r.text.split('id="orders"')[1]
    assert "Imported 1 new" in orders and 'role="status"' in orders
    assert "Imported 1 new" not in r.text.split('id="orders"')[0]
    form = {"name": "Corner Bakery", "currency": "$", **{f"h{i}": "8-18" for i in range(7)}}
    shop = c.post("/setup", data=form).text.split('id="shop"')[1].split('id="orders"')[0]
    assert "Saved." in shop


def test_setup_lists_every_order_day_and_removes_one(c):
    old = main.book.add(date(2026, 9, 1), "September flour")
    main.book.add(date(2026, 12, 24), "Christmas eggs")
    page = c.get("/setup").text
    assert "September flour" in page and "Christmas eggs" in page
    assert page.index("Christmas eggs") < page.index("September flour")  # newest first
    r = c.post(f"/deliveries/{old.id}/remove")
    assert "Removed September flour (2026-09-01)." in r.text and "September flour</td>" not in r.text


def test_setup_inputs_are_16px(c):
    css = (main.Path(main.__file__).parent / "templates" / "_style.html").read_text()
    assert "input, select, textarea { font: inherit; font-size: 16px;" in css
    assert "color-scheme: light dark" in css


# ---- the dispute pack --------------------------------------------------------------------------

def test_dispute_page_leads_with_a_verdict_and_hides_the_jargon(c):
    e = main.book.add(SUNDAY, "Sunday bread", order_ref="PO-7")
    main.book.mark_received(e.id, "damaged", "", datetime(2026, 10, 11, 9, 0, tzinfo=TZ))
    page = c.get(f"/dispute?date={SUNDAY}&order={e.id}").text
    assert '<a class="primary" href="/dispute.zip?' in page and "Back to the dashboard" in page
    assert "Back to the briefing" not in page
    visible = re.sub(r"<details.*?</details>", "", page, flags=re.S)
    for jargon in ("SHA-256", "X-Request-ID", "sub_type", "WHEP", "by rules:"):
        assert jargon not in visible, jargon
    scan = c.get(f"/dispute?date={WEDNESDAY}&at=03:17&claim=attempted").text
    # nothing within 30 minutes of 03:17 and no Ring status notice: the door can't speak for that time
    assert '<p class="first">Can&#39;t tell: the door recorded nothing around the scan.</p>' in scan
    # three short facts under the verdict; the full reasoning (and its caveats) moved under Details
    assert "Nothing recorded within 10 min of 03:17" in scan and "No courier seen" in scan
    details = re.search(r"<details class=\"tech\"><summary>Details: how Shopfront reached this</summary>(.*?)</details>",
                        scan, re.S).group(1)
    assert "Can&#39;t tell: the door recorded nothing around 03:17." in details
    assert "may not have been watching" in scan


def test_made_time_is_shop_local():
    assert pages.local_stamp("2026-10-21T07:30:00+00:00", TZ) == "2026-10-21 at 00:30"
    assert pages.local_time("2026-10-11T09:12-07:00", TZ, "2026-10-11") == "09:12"
    assert pages.local_time("2026-10-12T09:12-07:00", TZ, "2026-10-11") == "2026-10-12 09:12"


# ---- labels ------------------------------------------------------------------------------------

def test_a_replayed_webhook_without_a_frame_says_replayed_not_synthetic(tmp_path):
    class NoCall:
        def converse(self, **kw):
            raise AssertionError("must not call Bedrock without a frame")

    log = WebhookLog(tmp_path / "w.jsonl")
    at = datetime(2026, 10, 7, 13, 2, tzinfo=TZ)
    replayed_courier(log, at)
    rows = [json.loads(x) for x in log.path.read_text().splitlines()]
    rows[0]["sub_type"] = "human"  # goes on to the classifier instead of being labelled by Ring
    log.path.write_text(json.dumps(rows[0]) + "\n")
    ring = SyntheticRingClient(TZ, parse_hours(""))
    clf = BedrockClassifier(region="us-east-1", model_id="m", client=NoCall())
    vs = classify_day(ring, clf, WEDNESDAY, parse_hours(""), webhooks=log)
    replay = next(v for v in vs if v.event.source == "ring-webhook-replay")
    assert "replayed Ring event, no frame" in replay.reason and "synthetic" not in replay.reason


def test_late_courier_verdicts_read_as_one_line():
    late = {"outcome": "delivered_after_scan", "text": "The carrier scanned ...", "visit": {"time": "13:02",
                                                                                          "minutes_after": 22}}
    assert pages.verdict(late) == "Delivered late: the courier came at 13:02, 22 min after the scan."
    assert pages.verdict({"outcome": "courier_came_later", "text": "x"}).startswith("No courier near the scan, but")
    assert pages.verdict({"outcome": "something_new", "text": "First sentence. Second."}) == "First sentence."


def test_visit_label_marks_rule_guesses():
    assert pages.visit_label("doorbell_press", confirmed=False) == "doorbell press (guess)"
    assert pages.visit_label("delivery", confirmed=True) == "delivery"
    assert pages.visit_label("after_hours", confirmed=False) == "after hours (guess)"


def test_demo_pack_says_demo_once_on_the_page(c):
    """Round 2: the site banner and ONE watermark block; the subject gets "[DEMO]", the draft one line, and the
    'what this does not show' list doesn't repeat it."""
    e = main.book.add(date(2026, 10, 7), "Dairy Co", "05:00", "05:30", order_ref="PO-9")
    page = c.get(f"/dispute?date=2026-10-07&order={e.id}").text
    assert page.count('class="watermark"') == 1 and page.count("demo generator") == 1
    from app import dispute
    assert dispute.DEMO_LIMIT not in page
    subject = page.split('id="subject-supplier">')[1].split("</dd>")[0]
    assert subject.startswith("[DEMO] ") and subject.count("DEMO") == 1
    body = page.split('id="draft-supplier">')[1].split("</div>")[0]
    assert body.count("DEMO") == 1 and body.startswith("DEMO DATA, not from a camera: don&#39;t send this.")


def test_the_desk_says_which_day_it_shows():
    today = date(2026, 10, 21)
    assert pages.relative_day("2026-10-20", today) == "yesterday"
    assert pages.relative_day("2026-10-21", today) == "today" and pages.relative_day("2026-10-07", today) == ""


def test_the_desk_opens_on_today_once_today_has_orders(c):
    """The bakery owner at 7am adds today's flour order: the desk must open on today, not yesterday's door."""
    today = datetime.now(main.settings.tz).date()
    assert "Receiving desk · yesterday" in c.get("/").text
    main.book.add(today, "Hillside Mill", order_ref="PO-1")
    page = c.get("/").text
    assert "Receiving desk · today" in page and "Hillside Mill" in page


def test_a_scan_pack_page_lets_the_owner_pick_the_supplier_or_the_carrier(c):
    """Round 2: two drafts on the pack, to the supplier (receiving issue) and to the carrier (its scan)."""
    day = date(2026, 10, 20)
    main.book.add(day, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042", carrier="Northline Parcel")
    e = main.book.for_day(day)[0]
    page = c.get(f"/dispute?date={day}&at=14:30&claim=business_closed&order={e.id}&carrier=Northline+Parcel").text
    assert page.count('name="to"') == 2 and "Pick a draft" in page
    assert "To Valley Creamery, the supplier" in page and "To Northline Parcel, the carrier" in page
    assert 'id="draft-supplier">' in page and 'id="draft-carrier">' in page
    assert page.split('id="draft-carrier">')[1].split("</div>")[0].split("\n\n")[1].startswith("Hello Northline Parcel,")


def test_the_story_is_on_screen_first(c):
    """Round 2: a one-line value statement under the h1 on every desk, first run included; every order row
    has "Carrier says closed?" that pre-fills the scan form; the form doesn't jump past the verdict lead."""
    first = c.get(f"/?date={WEDNESDAY}").text
    line = '<p class="value">Carrier says you were closed? Your doorbell can show you were open.</p>'
    assert line in first
    main.book.add(WEDNESDAY, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042", carrier="Northline Parcel",
                  tracking="NP 44/71")
    page = c.get(f"/?date={WEDNESDAY}").text
    assert line in page and page.index(line) < page.index('id="atth"')
    href = f"/?date={WEDNESDAY}&amp;claim=business_closed&amp;carrier=Northline%20Parcel&amp;tracking=NP%2044/71#atth"
    assert f'<a class="claim-link" href="{href}"' in page and ">Carrier says closed?</a>" in page
    form = page.split('id="atth"')[1].split("</form>")[0]
    assert '<option value="business_closed" selected>' in form  # the story's scan is the default
    assert 'action="/#atth"' not in page and '<form method="get" action="/" class="fields scan-form"' in page
    pre = c.get(f"/?date={WEDNESDAY}&claim=business_closed&carrier=Northline+Parcel&tracking=NP4471").text
    form = pre.split('id="atth"')[1].split("</form>")[0]
    assert '<option value="business_closed" selected>' in form and 'value="Northline Parcel"' in form
    assert 'value="NP4471"' in form and "required autofocus" in form


def test_desk_rows_show_a_source_badge_with_the_sentence_behind_it(c):
    """Round 2: "Replayed Ring package_delivery alert from Shopfront's feeder…" was the third column of every
    row. Now a one-word badge, the full sentence in a tap-to-open details and its title."""
    assert [pages.badge(*a) for a in (("ring", "ring"), ("ring", "ring-webhook-replay"), ("bedrock", "ring"),
                                      ("bedrock", "ring-live-browser"), ("rules", "ring"), ("rules", "synthetic"),
                                      ("none", "ring-live"))] == ["ring", "replay", "bedrock", "bedrock", "guess",
                                                                  "demo", "guess"]
    e = main.book.add(SUNDAY, "Sunday bread", "11:00", "12:00")  # the synthetic 11:28 press fits the window
    page = c.get(f"/?date={SUNDAY}").text
    row = page.split(f'action="/deliveries/{e.id}/received"')[0].rsplit("<tr>", 1)[1] + \
        page.split(f'action="/deliveries/{e.id}/received"')[1].split("</tr>")[0]
    assert '<span class="badge b-demo">demo</span>' in row
    assert '<details class="src"><summary title="Demo event; guess from the time of day' in row


def test_live_door_is_in_the_nav_only_with_ring_connected(c, tmp_path, monkeypatch):
    """Round 2: on demo data "Live door" led to a page with nothing to watch. It shows with Ring connected;
    without, the Ring link reads "Connect Ring", and /live itself says how, plainly."""
    page = c.get(f"/?date={WEDNESDAY}").text
    nav = page.split('nav aria-label="Shopfront"')[1].split("</nav>")[0]
    assert 'href="/live"' not in nav and ">Connect Ring</a>" in nav
    live = c.get("/live").text
    assert "Live view streams your own Ring doorbell, so it needs Ring connected" in live and 'aria-current="page">Live door' in live
    monkeypatch.setattr(main, "_chrome", lambda r: {**chrome(r), "ring_mode": "ring"})
    nav = c.get(f"/?date={WEDNESDAY}").text.split('nav aria-label="Shopfront"')[1].split("</nav>")[0]
    assert 'href="/live">Live door</a>' in nav and 'href="/connect">Ring</a>' in nav


chrome = main._chrome


def test_fresh_install_shows_no_ask_example_about_orders_it_doesnt_have(c):
    page = c.get(f"/?date={WEDNESDAY}").text  # no orders yet
    assert "Ask needs an AWS sign-in with Bedrock access; it isn't available on this machine." in page
    assert "PO-1041" not in page and "ask-example" not in page.split("</style>")[-1]
    main.book.add(WEDNESDAY, "Bay Mills")
    assert 'class="ask-example"' in c.get(f"/?date={WEDNESDAY}").text


def test_impact_is_on_screen_with_a_sourced_default(c):
    """Round 2: no cost set yet. The week line uses the survey average and links its source; a disputed scan
    says what's at stake; /setup shows the default as the placeholder and names where it comes from."""
    from app.deliveries import COST_SOURCE
    main.book.add(SUNDAY, "Evening veg", "19:00", "20:00")  # never seen: a missed delivery this week
    page = c.get(f"/?date={SUNDAY}").text
    assert "the survey average for a failed delivery (not your own number yet)" in page
    assert f'href="{COST_SOURCE["url"]}"' in page and 'href="/setup#shop">Use your own number</a>' in page
    setup = c.get("/setup").text
    assert 'placeholder="17.20"' in setup and "Left empty, Shopfront uses $17.20" in setup
    assert f'href="{COST_SOURCE["url"]}"' in setup
    main.book.add(WEDNESDAY, "Valley Creamery", "13:00", "16:00", order_ref="PO-1042", tracking="NP1", amount="$318.75")
    # find a time the door disputes, so the scan is the headline
    for hhmm in ("14:30", "12:30", "11:00", "16:30"):
        page = c.get(f"/?date={WEDNESDAY}&claim=business_closed&attempt={hhmm}&tracking=NP1").text
        if 'class="reason stake"' in page:
            break
    stake = page.split('class="reason stake">')[1].split("</p>")[0]
    assert stake.startswith("At stake: PO-1042 from Valley Creamery, $318.75 of stock, and about $17.20 for the "
                            "redelivery (survey average")
