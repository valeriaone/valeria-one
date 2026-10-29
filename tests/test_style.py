"""Talks like them: one question on day one, learned from the owner's edits, used in every draft, changed by chat."""
import json

import pytest

from _va import Net, csrf, customer, gw_in, link_owner, owner_session, refuse_network, shop, tg_in, with_ai
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import ai as AI
from valeria_one.services import posts as PO
from valeria_one.services import style as STY
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def test_day_one_question_sets_the_tone_and_emoji():
    biz, tg, gw = shop()
    with db.tx() as conn:
        assert STY.get(conn, biz["id"])["set"] is False
        p = STY.set_tone(conn, biz, "t", "formal")
        assert p["tone"] == "formal" and p["emoji"] == "none" and p["set"]
        assert STY.set_tone(conn, biz, "t", "casual")["emoji"] == "lots"
        with pytest.raises(Invalid):
            STY.set_tone(conn, biz, "t", "rude")
        with pytest.raises(Invalid):
            STY.save(conn, biz, "t", {"emoji": "tons"})


def test_every_ai_draft_carries_the_style():
    biz, tg, gw = shop()
    biz = with_ai(biz)
    with db.tx() as conn:
        STY.save(conn, biz, "t", {"tone": "formal", "emoji": "none", "signoff": "Atentamente, Sol", "manners": "usted"})
        system = AI._system(conn, biz, "", "es")
    assert "polite and formal" in system and "never use emoji" in system and "Atentamente, Sol" in system and "usted" in system
    net = Net(ai_text=json.dumps({"facebook": "Buenas tardes. Le esperamos."}))
    PO.draft(biz, "t", kind="idea", idea="nuevo horario", nets=["facebook"], http=net.http)
    assert "Atentamente, Sol" in net.ai_calls[-1]["systemInstruction"]["parts"][0]["text"]


def test_fixed_messages_follow_the_style_without_ai():
    p = {**STY.DEFAULT, "emoji": "none", "signoff": "— Sol"}
    assert STY.apply(p, "¡Hola! 🎉 Tu visita 5 ✨") == "¡Hola! Tu visita 5\n— Sol"
    assert STY.apply(p, "Gracias\n— Sol") == "Gracias\n— Sol"                         # never twice
    assert STY.apply({**STY.DEFAULT}, "Hola 🎉") == "Hola 🎉"


def test_learns_from_edits_without_ai():
    biz, tg, gw = shop()
    with db.tx() as conn:
        assert STY.record(conn, biz["id"], "reply", "Hola", "Hola") is False           # nothing changed: nothing to learn
        for d, s in (("¡Hola! 😊 Sí tenemos lugar", "Hola, sí tenemos lugar.\nSaludos, Ana"),
                     ("¡Claro! 🎉 Te esperamos", "Claro, lo esperamos a usted.\nSaludos, Ana"),
                     ("Gracias 🙌", "Gracias por escribir.\nSaludos, Ana")):
            assert STY.record(conn, biz["id"], "reply", d, s)
    assert STY.learn_due()["learned"] == 1
    with db.tx() as conn:
        p = STY.get(conn, biz["id"])
    assert p["emoji"] == "none" and p["signoff"] == "Saludos, Ana" and p["tone"] == "formal"
    assert STY.learn_due()["learned"] == 0                                              # once a day, and the edits are used


def test_learns_with_the_business_own_ai():
    biz, tg, gw = shop()
    biz = with_ai(biz)
    with db.tx() as conn:
        for i in range(3):
            STY.record(conn, biz["id"], "post", f"Draft {i} 😊", f"Sent {i} po")
    net = Net(ai_text=json.dumps({"tone": "friendly", "emoji": "none", "manners": "says po", "dialect": "Taglish", "tone_bad": 1}))
    assert STY.learn(biz, net.http)["dialect"] == "Taglish"
    with db.tx() as conn:
        assert STY.get(conn, biz["id"])["manners"] == "says po"


def test_owner_changes_and_reads_it_by_chat():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    net = Net()
    tg_in(tg, "900", "más formal", net=net)
    assert net.last().startswith(t("va_style_changed", "es"))
    tg_in(tg, "900", "sin emojis", net=net)
    with db.tx() as conn:
        p = STY.get(conn, biz["id"])
    assert p["tone"] == "formal" and p["emoji"] == "none"
    tg_in(tg, "900", "¿cómo sueno?", net=net)
    assert t("va_style_tone_formal", "es") in net.last() and t("va_style_emoji_none", "es") in net.last()


def test_change_detection_casual_is_not_formal():
    assert STY.change_from("more informal please") == {"tone": "casual"}
    assert STY.change_from("be more formal") == {"tone": "formal"}
    assert STY.change_from("no emojis") == {"emoji": "none"}
    assert STY.change_from("hola") == {}


def test_inbox_edit_of_an_ai_draft_is_learned(client):
    biz, tg, gw = shop()
    customer(biz, gw, "Ana", who="ana-1")
    st, _ = gw_in(gw, "ana-1", "hola")
    owner_session(client, biz)
    conv = st["conversation_ids"][0]
    tok = csrf(client, f"/app/inbox?c={conv}")
    client.post(f"/app/inbox/{conv}/send", data={"csrf": tok, "body": "Hola Ana, claro.", "ai_draft": "¡Hola Ana! 😊 Claro que sí."})
    with db.tx() as conn:
        rows = conn.execute(db.style_samples.select().where(db.style_samples.c.business_id == biz["id"])).mappings().all()
    assert [(r["kind"], r["sent"]) for r in rows] == [("reply", "Hola Ana, claro.")]


def test_style_page_saves(client):
    biz, tg, gw = shop()
    owner_session(client, biz)
    tok = csrf(client, "/app/valeria")
    client.post("/app/valeria/style", data={"csrf": tok, "emoji": "none", "greeting": "Buen día", "signoff": "— Sol"})
    with db.tx() as conn:
        p = STY.get(conn, biz["id"])
    assert p["greeting"] == "Buen día" and p["signoff"] == "— Sol" and p["emoji"] == "none"
