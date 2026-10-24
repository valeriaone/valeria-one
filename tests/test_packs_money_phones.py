import pytest

from valeria_one import money, packs, phones


def test_every_pack_loads_and_is_consistent():
    cs, inds = packs.countries(), packs.industries()
    assert len(cs) >= 12 and len(inds) >= 9
    for p in cs.values():
        assert p.languages and p.channels and p.currency
        assert p.reviewed_by_counsel is False          # legal text is a starting point until a lawyer signs off
    for i in inds.values():
        assert i.stages[0].outcome == "open"
        assert any(s.outcome == "won" for s in i.stages) and any(s.outcome == "lost" for s in i.stages)


def test_unknown_pack_is_a_clear_error():
    with pytest.raises(KeyError):
        packs.country("XX")
    with pytest.raises(KeyError):
        packs.industry("nope")


def test_text_falls_back_to_english():
    assert packs.text({"en": "Won", "es": "Ganado"}, "es") == "Ganado"
    assert packs.text({"en": "Won", "es": "Ganado"}, "es_MX") == "Ganado"
    assert packs.text({"en": "Won"}, "ja") == "Won"


@pytest.mark.parametrize("code,amount,inc,net,tax,gross", [
    ("MX", 24900, False, 24900, 3984, 28884),      # 249 + IVA = 288.84 (same figure as valai.mx)
    ("MX", 28884, True, 24900, 3984, 28884),
    ("GB", 1200, True, 1000, 200, 1200),
    ("JP", 1000, False, 1000, 100, 1100),          # yen: no decimals
    ("NG", 10000, False, 10000, 750, 10750),
])
def test_fixed_tax(code, amount, inc, net, tax, gross):
    p = money.price(amount, packs.country(code), includes_tax=inc)
    assert (p.net_minor, p.tax_minor, p.gross_minor) == (net, tax, gross)
    assert p.net_minor + p.tax_minor == p.gross_minor


def test_external_tax_is_not_invented():
    p = money.price(2900, packs.country("US"))
    assert p.net_minor == 2900 and p.tax_minor is None and p.gross_minor is None


def test_money_rejects_floats_and_negatives():
    with pytest.raises(TypeError):
        money.price(249.0, packs.country("MX"))
    with pytest.raises(ValueError):
        money.price(-1, packs.country("MX"))
    with pytest.raises(ValueError):
        money.to_minor("1.5", "JPY")
    assert money.to_minor("249.50", "MXN") == 24950


def test_money_format_per_locale():
    assert money.fmt(24900, "MXN", "es_MX") == "$249.00"
    assert money.fmt(1100, "JPY", "ja_JP").endswith("1,100")
    assert "19,99" in money.fmt(1999, "EUR", "de_DE")


@pytest.mark.parametrize("raw,country,want", [
    ("999 123 4567", "MX", "+529991234567"),
    ("+52 1 999 123 4567", "MX", "+529991234567"),       # legacy mobile 1 (WhatsApp still sends it)
    ("(951) 434-3352", "US", "+19514343352"),
    ("0917 123 4567", "PH", "+639171234567"),
    ("0044 20 7946 0958", "MX", "+442079460958"),
])
def test_phones(raw, country, want):
    assert phones.e164(raw, packs.country(country)) == want


@pytest.mark.parametrize("raw", ["", "abc", "123", "+1 555 0100"])
def test_bad_phones(raw):
    with pytest.raises(phones.BadPhone):
        phones.e164(raw, packs.country("US"))
