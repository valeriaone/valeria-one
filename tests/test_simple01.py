"""simple01 (v0.40.0): money first on the home page, «free · no card · what's next» at the yes moment, a simpler office."""
import re

from valeria_one.services.leads import channel_name
from valeria_one.web import world

from _va import owner_session
from conftest import make_business


def test_the_home_page_leads_with_money_in_spanish_and_english(client, monkeypatch):
    from valeria_one.web import marketing
    monkeypatch.setattr(marketing, "on", lambda: True)
    es = client.get(world.HOMES["es"]).text
    assert "Yo atiendo a tus clientes. Tú ves llegar el dinero." in es and "sin tarjeta" in es and "código abierto" not in es
    for lang in world.HOMES:                                    # every language: money first, free + no card, no «open source»
        w = world.words(lang, free=True, price="$0", free_word=world.copy(lang)["free_word"], n_languages=11)
        assert "{" not in w["hero_k"] and world.copy(lang)["free_word"] in w["hero_k"] and "AGPL" not in w["hero_k"], lang
    en = world.words("en", free=True, price="$0", free_word="Free", n_languages=11)
    assert en["h1"] == "I take care of your customers. You watch the money come in." and en["hero_k"] == "Free · no card · 11 languages"


def test_sign_up_says_free_no_card_and_what_happens_next(client):
    page = client.get("/signup", headers={"Accept-Language": "es-MX"}).text
    assert "su-next" in page and "Sin contraseñas." in page


def test_the_menu_keeps_core_plus_the_trades_five_and_folds_the_rest(client):
    biz, _ = make_business("MX", "contractor", name="Taller")
    owner_session(client, biz)
    page = client.get("/app").text
    nav = page.split('<nav class="nav">')[1].split("</nav>")[0]
    top = nav.split('<details class="nav-more"')[0]
    # trade01 (v0.65.0): core + the trade's own 5 stay in view (was Today + 5); settings and what the owner adds fold under «More»
    assert re.findall(r'<a href="([^"]+)"', top) == ["/app", "/app/valeria", "/app/connect", "/app/inbox", "/app/contacts",
                                                     "/app/pipeline", "/app/projects", "/app/calendar", "/app/invoices", "/app/website"]
    assert 'class="nav-more"' in nav and "/app/settings" in nav.split('<details class="nav-more"')[1]
    assert '<details class="nav-more" open' not in nav                  # on Today, «More» stays closed


def test_the_setup_card_shows_one_number_while_valeria_still_asks(client):
    biz, _ = make_business("MX", "salon", name="Salón")
    owner_session(client, biz)
    page = client.get("/app").text
    if 'id="onboard"' in page:
        card = page.split('id="onboard"')[1].split("</a>")[0]
        assert not ("pill" in card and "preguntas" in card and re.search(r"\d+ (of|de) \d+.*\d+ (of|de) \d+", card, re.S))


def test_a_demo_never_shows_the_word_sandbox_on_a_send_button():
    assert channel_name("sandbox") == "WhatsApp · demo" and channel_name("whatsapp") == "WhatsApp"
