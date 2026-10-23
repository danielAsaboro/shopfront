"""The welcome page (/welcome): what Shopfront is, its two calls to action, and the same chrome and honesty
rules as every other page. Synthetic events only; never Ring or Bedrock."""

import re

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import public_demo
from app.shop import Profile
from app.wording import accusing

REAL_CARRIERS = ("UPS", "FedEx", "Fed Ex", "DHL", "USPS", "Royal Mail", "Canada Post", "Purolator", "OnTrac",
                 "Amazon Logistics", "Evri", "Hermes", "DPD", "Yodel", "Parcelforce", "Australia Post", "La Poste",
                 "Deutsche Post", "GLS", "Lasership", "TNT")


@pytest.fixture
def c():
    return TestClient(main.app)


def text_of(page: str) -> str:
    """What a reader sees: the body without its style block, tags or entities' markup."""
    body = page.split("</style>")[-1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_welcome_is_a_page_with_both_calls_to_action(c):
    r = c.get("/welcome")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert '<a class="lp-btn amber" href="/">Open the receiving desk' in page
    assert '<a class="lp-btn pill" href="/#atth">Try a carrier scan' in page
    assert "<title>Welcome · Shopfront</title>" in page


def test_welcome_has_the_nav_and_the_demo_banner_like_every_page(c):
    main.profile.save(Profile("Rosa's Florist", "8-18,8-18,8-18,8-18,8-18,closed,closed"))
    page = c.get("/welcome").text
    assert 'nav aria-label="Shopfront"' in page and 'href="/setup"' in page and 'href="/"' in page
    for href in ("/", "/live", "/setup", "/connect"):
        assert f'<a href="{href}">' in page, href  # the four tabs, none marked current here
    assert "aria-current=\"page\"" not in page
    assert "Rosa&#39;s Florist" in page
    assert "Demo data: these door events were made up on this computer" in page
    assert '<span class="b-short">Demo data, not from Ring.' in page


def test_welcome_names_no_real_carrier_and_claims_nothing_it_lacks(c):
    words = text_of(c.get("/welcome").text)
    for name in REAL_CARRIERS:
        assert not re.search(rf"\b{re.escape(name)}\b", words, re.I), name
    assert "AgentCore" not in words
    assert accusing(words) == []
    # the hero's screens are example data, and say so
    assert "An illustration with example data, not a real shop" in words
    # every label names what it rests on
    for source in ("Ring alert", "Bedrock frame", "Rules guess"):
        assert source in words, source
    assert "never sends anything for you" in words


def test_welcome_is_owner_only_like_every_page(c):
    r = TestClient(main.app, client=("203.0.113.9", 5000)).get("/welcome")
    assert r.status_code == 403 and "<h1>Pair this device</h1>" in r.text
    assert "Open the receiving desk" not in r.text


def test_the_public_demo_shows_welcome_read_only_with_its_banner(tmp_path, monkeypatch):
    demo, orders = public_demo.install(main, tmp_path / "public-demo", setattr=monkeypatch.setattr)
    public_demo.tell_story(main, demo, orders)
    client = TestClient(public_demo.PublicDemoApp(main, demo), base_url="https://abc123example.lambda-url.us-east-1.on.aws",
                        follow_redirects=False)
    r = client.get("/welcome")
    assert r.status_code == 200 and r.text.count('id="public-demo"') == 1
    assert "Public demo of a made-up shop." in r.text
    assert client.post("/welcome").status_code == 403
