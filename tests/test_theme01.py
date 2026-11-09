"""theme01 (v0.38.0): the office wears its trade's look, and WhatsApp leads the day on Today."""
import re
from pathlib import Path

from valeria_one import packs
from valeria_one.web import TRADE_FONTS

from _va import owner_session
from conftest import make_business

CSS = (Path(__file__).resolve().parent.parent / "valeria_one/web/static/trades.css").read_text()


def test_every_trade_but_generic_has_a_look_in_dark_and_light_and_a_face():
    codes = set(packs.industries()) - {"generic"}
    assert codes == set(TRADE_FONTS)
    for k in codes:
        assert f':root[data-trade="{k}"]:not([data-theme="light"])' in CSS and f':root[data-trade="{k}"][data-theme="light"]' in CSS, k
        assert f':root[data-trade="{k}"]{{--display:' in CSS, k


def test_the_office_wears_its_trade(client):
    biz, _ = make_business("MX", "contractor", name="Taller Hugo")
    owner_session(client, biz)
    page = client.get("/app").text
    assert 'data-trade="contractor"' in page and "/static/trades.css" in page and "family=Fraunces" in page
    assert client.get("/static/trades.css").status_code == 200


def test_generic_keeps_valeria_ones_own_look(client):
    biz, _ = make_business("US", "generic", name="Plain Co")
    owner_session(client, biz)
    page = client.get("/app").text
    assert "data-trade=" not in page and "/static/trades.css" not in page


def test_whatsapp_leads_the_day_and_asks_to_connect_when_it_is_not(client):
    biz, _ = make_business("MX", "contractor", name="Taller Hugo")
    owner_session(client, biz)
    client.post("/lang/es", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)})
    page = client.get("/app").text
    assert 'id="wa-day"' in page and "WhatsApp hoy" in page and 'href="/app/channels"' in page and "Conectar WhatsApp" in page


def test_a_demo_shows_its_whatsapp_day(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    en = {"Accept-Language": "en-US,en;q=0.9"}
    client.get("/try/decora-con-hugo", headers=en)
    page = client.get("/app", headers=en).text
    assert 'data-trade="design"' in page and "WhatsApp hoy" in page and "Esperando respuesta: " in page and ">demo<" in page
    assert "Todo sale de tu propio número." in page


def test_the_website_never_says_she_posts_by_herself_before_facebook_is_connected():
    """truth01 (v0.39.1): Meta connect is off until its keys are set — the home page says «you publish it with one tap, or she
    publishes it herself once your Facebook is connected», in every language."""
    import json
    world = Path(__file__).resolve().parent.parent / "valeria_one/web/world"
    for f in world.glob("*.json"):
        line = json.loads(f.read_text())["bento"][6][2]
        assert "Facebook" in line, f.name
    assert "one tap" in json.loads((world / "en.json").read_text())["bento"][6][2]
    assert "con un toque" in json.loads((world / "es.json").read_text())["bento"][6][2]
