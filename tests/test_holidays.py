"""Holiday autopilot: the calendar is data (computed or listed 2026–2028); a week before, a post is offered once."""
from datetime import date, datetime, timezone

import pytest

from _va import Net, actions, link_owner, refuse_network, shop, tg_in
from valeria_one import db
from valeria_one.services import holidays as HO
from valeria_one.services import owner as OW
from valeria_one.services import posts as PO


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def test_computed_dates_are_right():
    assert [HO.easter(y) for y in (2026, 2027, 2028)] == [date(2026, 4, 5), date(2027, 3, 28), date(2028, 4, 16)]
    assert HO.nth_weekday(2026, 11, "thu", 4) == date(2026, 11, 26)                       # US Thanksgiving
    assert HO.nth_weekday(2026, 5, "mon", -1) == date(2026, 5, 25)                        # US Memorial Day
    assert HO.nth_weekday(2026, 11, "mon", 3) == date(2026, 11, 16)                       # MX Revolución
    assert HO.nth_weekday(2026, 8, "mon", -1) == date(2026, 8, 31)                        # PH National Heroes
    assert HO.nth_weekday(2027, 5, "sun", 2) == date(2027, 5, 9)                          # Mother's Day
    by = {h["key"]: h for h in HO.entries("US")}
    assert HO.on(by["black_friday"], 2026) == date(2026, 11, 27)
    assert HO.on({h["key"]: h for h in HO.entries("BR")}["carnaval"], 2026) == date(2026, 2, 17)


@pytest.mark.parametrize("cc", ["MX", "PH", "IN", "US", "AE", "PK", "NG", "ID", "BR"])
def test_every_country_has_dates_for_2026_to_2028(cc):
    hs = HO.entries(cc)
    assert len(hs) >= 8 and hs[0]["key"] == "new_year"
    for h in hs:
        for y in (2026, 2027, 2028):
            d = HO.on(h, y)
            assert d and d.year == y, (cc, h["key"], y)
        assert h["name"].get("en")


def test_other_countries_get_the_global_days():
    assert [h["key"] for h in HO.entries("JP")] == ["new_year"]


def test_offered_once_a_week_before_and_the_owner_can_skip():
    biz, tg, gw = shop()                                                              # MX, Mérida-like time zone from the pack
    link_owner(biz, tg, "900")
    now = datetime(2026, 5, 3, 16, 0, tzinfo=timezone.utc)                            # 7 days before Día de las Madres, 10:00 local
    assert HO.round_(now=now)["offered"] >= 1
    q = [a for a in actions(biz, kind="holiday") if a["payload"]["key"] == "madres"][0]
    assert q["status"] == "asked" and q["dedupe"] == "holiday:madres:2026-05-10" and q["payload"]["choices"][0] == "post"
    assert HO.round_(now=now)["offered"] == 0                                         # once
    other, tg2, _ = shop(name="Otra")
    link_owner(other, tg2, "901")
    with db.tx() as conn:
        OW.save_settings(conn, other, "t", holidays_skip=["madres", "nino"])
    HO.round_(now=now)
    assert not [a for a in actions(other, kind="holiday") if a["payload"]["key"] in ("madres", "nino")]
    with db.tx() as conn:
        OW.save_settings(conn, other, "t", holidays=False)
    third_before = len(actions(other, kind="holiday"))
    HO.round_(now=datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc))
    assert len(actions(other, kind="holiday")) == third_before                        # off: nothing offered


def test_one_tap_makes_a_draft_post_and_an_offer_question():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        OW.ask(conn, biz, "holiday", "¿Post?", {"key": "madres", "date": "2026-05-10", "name": "Día de las Madres",
                                                     "choices": ["post", "post_offer", "no"]}, prompted=True)
    net = Net()
    tg_in(tg, "900", "2", net=net)                                                     # 2 = post + offer
    with db.tx() as conn:
        posts = PO.list_(conn, biz["id"])
    assert len(posts) == 1 and posts[0]["status"] == "draft" and posts[0]["kind"] == "holiday"
    assert "Día de las Madres" in next(iter(posts[0]["texts"].values()))              # no AI: a short ready line
    assert actions(biz, kind="post")[-1]["status"] == "asked"
    promo = actions(biz, kind="promo")[-1]
    assert promo["status"] == "asked" and promo["payload"]["mode"] == "offer"          # offers always ask
