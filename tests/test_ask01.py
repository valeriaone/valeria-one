"""ask01 (v0.39.0): once, Today asks the owner «anything else you want me to do, or something special in your business?»"""
import re

from sqlalchemy import select

from valeria_one import db

from _va import owner_session
from conftest import make_business


def tok(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)


def asks(biz):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.owner_asks).where(db.owner_asks.c.business_id == biz["id"])).mappings()]


def test_the_owner_is_asked_once_and_the_answer_is_kept_with_the_trade(client):
    biz, _ = make_business("MX", "contractor", name="Taller Hugo")
    owner_session(client, biz)
    assert 'id="ask"' in client.get("/app").text
    r = client.post("/app/asks", data={"csrf": tok(client), "do": "send", "text": "  También entrego en Cancún y Playa  "}, follow_redirects=False)
    assert r.headers["location"] == "/app"
    got = asks(biz)
    assert len(got) == 1 and got[0]["text"] == "También entrego en Cancún y Playa" and got[0]["industry"] == "contractor"
    assert got[0]["status"] == "new" and got[0]["user_id"] is not None
    assert 'id="ask"' not in client.get("/app").text                 # asked once


def test_not_now_is_remembered_and_an_empty_answer_is_refused(client):
    biz, _ = make_business("MX", "salon", name="Salón Lupita")
    owner_session(client, biz)
    client.post("/app/asks", data={"csrf": tok(client), "do": "send", "text": "   "})
    assert asks(biz) == [] and 'id="ask"' in client.get("/app").text
    client.post("/app/asks", data={"csrf": tok(client), "do": "later"})
    got = asks(biz)
    assert len(got) == 1 and got[0]["status"] == "later" and got[0]["text"] == ""
    assert 'id="ask"' not in client.get("/app").text


def test_only_the_owner_is_asked(client):
    biz, _ = make_business("MX", "shop", name="Tienda")
    owner_session(client, biz, role="staff")
    assert 'id="ask"' not in client.get("/app").text
    client.post("/app/asks", data={"csrf": tok(client), "do": "send", "text": "hola"})
    assert asks(biz) == []


def test_the_question_speaks_every_language():
    from valeria_one.web.i18n import UI_LANGS, t
    for lang in UI_LANGS:
        for k in ("ask_title", "ask_sub", "ask_send", "ask_later_btn", "ask_thanks", "ask_later", "ask_empty", "ask_ph"):
            assert t(k, lang) != k, (lang, k)
