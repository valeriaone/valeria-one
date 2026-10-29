"""Help me set it up: helpers sign up and are approved by the admin; owners consent before anything is shared;
leads are recorded (first 5 free), never charged; owners rate."""
import re

import pytest

from _va import csrf, owner_session, refuse_network, shop
from valeria_one import db
from valeria_one.services import Invalid, NotFound
from valeria_one.services import helpers as HP
from valeria_one.services import mailer
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def helper(name="Ayuda Mérida", country="MX", city="Mérida", approve=True, langs=("es",)):
    with db.tx() as conn:
        h, token = HP.signup(conn, name=name, email=f"{name.split()[0].lower()}{len(name)}@help.example", country=country, city=city,
                             languages=list(langs))
        if approve:
            h = HP.approve(conn, h["id"], "user:admin")
    return h, token


def test_no_helpers_yet_the_request_waits_and_is_shared_with_nobody():
    biz, _, _ = shop()
    with db.tx() as conn:
        assert HP.matches(conn, biz, "es") == []
        with pytest.raises(Invalid):
            HP.request(conn, biz, "t", consent=False, name="Sol", city="Mérida", needs=["whatsapp"])
        lead = HP.request(conn, biz, "t", consent=True, name="Sol", city="Mérida", needs=["whatsapp", "hack"])
    assert lead["status"] == "waiting" and lead["helper_id"] is None and lead["shared"]["needs"] == ["whatsapp"]
    assert lead["consent_text"] == t("hp_consent", "en") and lead["consent_at"]


def test_only_approved_helpers_in_the_country_are_offered_best_first():
    biz, _, _ = shop()
    helper("Pendiente Uno", approve=False)
    far, _ = helper("Lejos Dos", city="Monterrey")
    near, _ = helper("Cerca Tres", city="Mérida")
    helper("Manila Four", country="PH", city="Manila", langs=("en", "fil"))
    with db.tx() as conn:
        got = HP.matches(conn, biz, "es", city="Mérida")
    assert [h["name"] for h in got] == ["Cerca Tres", "Lejos Dos"]


def test_consent_then_the_helper_sees_only_what_was_shared_and_first_5_are_free():
    biz, _, _ = shop()
    h, token = helper()
    with db.tx() as conn:
        HP.set_price(conn, h["id"], "user:admin", 15000)
        leads = [HP.request(conn, biz, "user:x", consent=True, name="Sol", city="Mérida", needs=["payments"], note="rápido",
                            helper_id=h["id"]) for _ in range(6)]
    assert [l["free"] for l in leads] == [True] * 5 + [False]
    assert [l["price_minor"] for l in leads] == [0] * 5 + [15000]                       # recorded only
    assert mailer.OUTBOX[-1]["to"] == h["email"] and "Sol" in mailer.OUTBOX[-1]["body"]
    with db.tx() as conn:
        mine = HP.leads_of_helper(conn, HP.by_token(conn, token)["id"])
        assert set(mine[0]["shared"]) == {"name", "business", "city", "needs", "note", "country", "language"}
        with pytest.raises(NotFound):
            HP.by_token(conn, "wrong")
        HP.decide(conn, h, leads[0]["id"], True)
        with pytest.raises(Invalid):
            HP.decide(conn, h, leads[0]["id"], False)
        with pytest.raises(Invalid):
            HP.rate(conn, biz["id"], "t", leads[1]["id"], 5)                             # not accepted yet
        HP.rate(conn, biz["id"], "t", leads[0]["id"], 4)
        assert HP.rating(conn, h["id"]) == {"stars": 4.0, "count": 1}
        assert HP.recorded(conn)["recorded_minor"] == 15000


def test_web_owner_flow_needs_consent_and_says_the_helper_may_charge(client):
    biz, _, _ = shop()
    h, _ = helper()
    owner_session(client, biz)
    for step in (0, 1, 2):
        r = client.get(f"/app/helpers?step={step}&needs=whatsapp")
        assert r.status_code == 200
    assert t("hp_free_note", "es") in r.text and "Ayuda Mérida" in r.text
    page = client.get(f"/app/helpers?step=3&needs=whatsapp&helper={h['id']}").text
    assert t("hp_consent", "es") in page
    tok = csrf(client, "/app/helpers?step=3")
    client.post("/app/helpers", data={"csrf": tok, "helper": str(h["id"]), "name": "Sol", "city": "Mérida", "need": "whatsapp"})
    with db.tx() as conn:
        assert HP.leads_of_business(conn, biz["id"]) == []                              # no tick: nothing shared
    client.post("/app/helpers", data={"csrf": tok, "helper": str(h["id"]), "name": "Sol", "city": "Mérida", "need": "whatsapp",
                                      "consent": "1"})
    with db.tx() as conn:
        assert len(HP.leads_of_business(conn, biz["id"])) == 1


def test_public_signup_and_dashboard(client):
    r = client.get("/helpers")
    assert r.status_code == 200
    tok = csrf(client, "/helpers")
    client.post("/helpers", data={"csrf": tok, "mode": "signup", "name": "Ana Ayuda", "email": "ana@help.example", "country": "MX",
                                  "city": "Mérida", "lang": "es"})
    link = re.search(r"https://one\.test/helpers/d/(\S+)", mailer.OUTBOX[-1]["body"]).group(1)
    page = client.get(f"/helpers/d/{link}").text
    assert "Ana Ayuda" in page and (t("hp_waiting_approval", "en") in page or t("hp_waiting_approval", "es") in page)
    assert client.get("/helpers/d/nope").status_code == 404
    client.post("/helpers", data={"csrf": tok, "mode": "link", "email": "nobody@x.example"})   # same answer: can't probe
    assert client.get("/app/server/helpers", follow_redirects=False).status_code in (303, 404)                # not the admin


def test_admin_approves(client):
    biz, _, _ = shop()
    uid = owner_session(client, biz)
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == uid).values(is_admin=True))
    h, _ = helper("Nuevo Ayudante", approve=False)
    assert "Nuevo Ayudante" in client.get("/app/server/helpers").text
    tok = csrf(client, "/app/server/helpers")
    client.post(f"/app/server/helpers/{h['id']}/approve", data={"csrf": tok})
    client.post(f"/app/server/helpers/{h['id']}/price", data={"csrf": tok, "price": "150", "currency": "MXN"})
    with db.tx() as conn:
        row = conn.execute(db.helpers.select().where(db.helpers.c.id == h["id"])).mappings().one()
    assert row["approved_at"] and row["lead_price_minor"] == 15000
