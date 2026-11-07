"""clean01 (v0.40.1): no jargon a customer or owner has to decode — the hour by city, plain pricing words, every trade findable."""
from valeria_one import packs
from valeria_one.web import _tz_place
from valeria_one.web.i18n import UI_LANGS, t



def test_the_hour_is_said_by_place_never_as_a_zone_id():
    assert _tz_place("America/Merida", "es") == "Hora de México (Mérida)"
    assert _tz_place("America/Merida", "en") == "Mexico (Mérida) Time"
    assert _tz_place("Nowhere/Strange_Place", "es") == "Strange Place"           # unknown: still readable


def test_the_booking_page_never_shows_america_merida(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    page = client.get("/b/decora-con-hugo", headers={"Accept-Language": "es-MX"}).text
    assert "America/Merida" not in page and "Mexico_City" not in page and "Hora de México" in page


def test_pricing_says_what_the_owner_gets_in_plain_words():
    for lang in UI_LANGS:
        line = t("plan_everything", lang).lower()
        assert "pipeline" not in line and "embudo" not in line and "funil" not in line and "inbox" not in line, lang


def test_barbershops_dentists_and_workshops_find_their_trade():
    name = lambda code, lang: packs.industry(code).name[lang]   # noqa: E731
    assert "barbería" in name("salon", "es") and "barbershop" in name("salon", "en")
    assert "dentista" in name("clinic", "es") and "dental" in name("clinic", "en")
    assert "talleres" in name("contractor", "es") and "repairs" in name("contractor", "en")
    for code in ("salon", "clinic", "contractor"):
        assert set(packs.industry(code).name) >= set(UI_LANGS) - {"en"} | {"en"}, code
