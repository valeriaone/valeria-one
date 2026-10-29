"""Valeria by chat: only the owner's registered numbers are commands; ask first, undo, stop; never nag; 24/7 answers."""
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from _va import (LANGS, NOW, Net, actions, appt, customer, due_now, gw_in, link_owner, owner_session, refuse_network, shop, tg_in,
                 with_ai)
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import ai as AI
from valeria_one.services import answers as ANS
from valeria_one.services import briefing as BR
from valeria_one.services import commands as CMD
from valeria_one.services import owner as OW
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture
def daytime(monkeypatch):
    monkeypatch.setattr(OW, "OPEN_FROM", 0)
    monkeypatch.setattr(OW, "OPEN_UNTIL", 24)
    monkeypatch.setattr(BR, "MORNING_UNTIL", 24)


def count(table, **where):
    with db.tx() as conn:
        q = select(func.count()).select_from(table)
        for k, v in where.items():
            q = q.where(table.c[k] == v)
        return conn.execute(q).scalar()


# ---------------------------------------------------------------- who is trusted
def test_pairing_code_links_the_phone_and_its_messages_are_commands():
    biz, tg, gw = shop()
    n = link_owner(biz, tg, "900")
    assert n["role"] == "owner" and n["channel"] == "telegram" and n["pair_code"] is None
    start = datetime.combine(datetime.now(ZoneInfo(biz["timezone"])).date(), time(23, 30), ZoneInfo(biz["timezone"]))
    appt(biz, customer(biz, gw, "Ana"), start)
    net = Net()
    st, out = tg_in(tg, "900", "hoy", net=net)
    assert out == ["answered"] and st["owner_ids"] and not st.get("conversation_ids")
    assert "23:30" in net.last()
    assert count(db.contacts, business_id=biz["id"]) == 1                     # Ana only: the owner never became a customer
    assert count(db.conversations, business_id=biz["id"]) == 0


def test_only_registered_numbers_are_trusted():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        stale = OW.start_pairing(conn, biz, "t", now=NOW() - timedelta(hours=2))       # expired code
    st, out = tg_in(tg, "555", "quién me debe")                                        # a stranger
    assert not st.get("owner_ids") and st.get("conversation_ids") and out == []
    st, _ = tg_in(tg, "556", f"{stale['pair_code']} hoy")                              # an expired code links nothing
    assert not st.get("owner_ids") and st.get("conversation_ids")
    st, _ = tg_in(tg, "557", "V-000000 hoy")                                           # a made-up code: a customer
    assert not st.get("owner_ids")
    with db.tx() as conn:
        assert OW.match(conn, biz["id"], "telegram", "555") is None
        assert OW.match(conn, biz["id"], "telegram", "900")


def test_typed_phone_is_trusted_on_every_phone_channel_of_this_business_only():
    biz, tg, gw = shop()
    other, _, _ = shop(name="Otra")
    with db.tx() as conn:
        OW.add_phone(conn, biz, "t", phone="+52 999 123 4567")
        assert OW.match(conn, biz["id"], "whatsapp", "+529991234567")["role"] == "owner"
        assert OW.match(conn, biz["id"], "sms", "+529991234567")
        assert OW.match(conn, biz["id"], "telegram", "+529991234567") is None          # a phone isn't a Telegram chat id
        assert OW.match(conn, other["id"], "whatsapp", "+529991234567") is None         # another business: a customer there
        with pytest.raises(Invalid):
            OW.add_phone(conn, biz, "t", phone="12")


def test_replayed_message_is_answered_once():
    from valeria_one.channels import HookRequest
    from valeria_one.services import autoreply
    from valeria_one.services import inbox as X
    from _va import raw_acc, tg_update, TG_SECRET
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    body = tg_update("900", "hoy")
    req = HookRequest("POST", "", {"x-telegram-bot-api-secret-token": TG_SECRET}, {}, body)
    st1 = X.receive(raw_acc(tg["id"]), req)
    st2 = X.receive(raw_acc(tg["id"]), req)
    assert len(st1["owner_ids"]) == 1 and not st2.get("owner_ids") and st2["duplicates"] == 1
    net = Net()
    assert autoreply.respond_owner(biz["id"], st1["owner_ids"][0], http=net.http) == "answered"
    assert autoreply.respond_owner(biz["id"], st1["owner_ids"][0], http=net.http) == "duplicate"
    assert len(net.tg) == 1


def test_staff_get_only_what_their_permissions_allow():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    link_owner(biz, tg, "901", role="staff", perms=["view"])
    net = Net()
    tg_in(tg, "901", "quién me debe", net=net)
    assert net.last() == t("va_not_allowed", "es")
    tg_in(tg, "901", "valeria, sigue", net=net)                                         # resume is the owner's
    assert net.last() == t("va_not_allowed", "es")
    tg_in(tg, "901", "hoy", net=net)
    assert net.last() != t("va_not_allowed", "es")
    with db.tx() as conn:
        n = OW.match(conn, biz["id"], "telegram", "901")
        OW.set_perms(conn, biz["id"], "t", n["id"], ["view", "money"])
    tg_in(tg, "901", "quién me debe", net=net)
    assert net.last() == t("va_nobody_owes", "es")


# ---------------------------------------------------------------- ask first, undo, stop
def test_reply_to_a_customer_asks_first_then_goes_after_the_undo_minute():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    ana = customer(biz, gw, "Ana", who="ana-1")
    gw_in(gw, "ana-1", "¿tienen lugar hoy?")
    net = Net()
    tg_in(tg, "900", "responde a Ana: sí, te espero a las 5", net=net)
    assert "te espero a las 5" in net.last() and not net.gw                            # a question, nothing sent
    q = actions(biz, kind="reply")[-1]
    assert q["status"] == "asked" and q["contact_id"] == ana["id"]
    tg_in(tg, "900", "1", net=net)
    assert net.last() == t("va_queued", "es")
    assert OW.run_due(http=net.http) == {"done": 0, "failed": 0}                       # still inside the undo minute
    due_now(biz)
    assert OW.run_due(http=net.http) == {"done": 1, "failed": 0}
    assert net.gw[-1]["body"] == "sí, te espero a las 5" and net.gw[-1]["to"] == "ana-1"
    assert OW.run_due(http=net.http) == {"done": 0, "failed": 0}                       # never twice


def test_undo_stops_what_has_not_gone_out():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    customer(biz, gw, "Ana", who="ana-1")
    gw_in(gw, "ana-1", "hola")
    net = Net()
    tg_in(tg, "900", "responde a Ana: ya voy", net=net)
    tg_in(tg, "900", "sí", net=net)
    tg_in(tg, "900", "deshacer", net=net)
    assert net.last() == t("va_undone", "es").format(n=1)
    due_now(biz)
    assert OW.run_due(http=net.http)["done"] == 0 and not net.gw
    assert actions(biz, kind="reply")[-1]["status"] == "undone"
    tg_in(tg, "900", "undo", net=net)
    assert net.last() == t("va_nothing_to_undo", "es")


def test_owner_can_change_her_words_before_saying_yes():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    customer(biz, gw, "Ana", who="ana-1")
    gw_in(gw, "ana-1", "hola")
    net = Net()
    tg_in(tg, "900", "responde a Ana", net=net)
    assert net.last() == t("va_q_reply_what", "es").format(name="Ana")
    tg_in(tg, "900", "Claro, mañana a las 10", net=net)                                 # the words she asked for
    assert "Claro, mañana a las 10" in net.last()
    tg_in(tg, "900", "1", net=net)
    due_now(biz)
    OW.run_due(http=net.http)
    assert net.gw[-1]["body"] == "Claro, mañana a las 10"


def test_granted_kind_goes_without_a_question_and_money_never_can_be_granted():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    customer(biz, gw, "Ana", who="ana-1")
    gw_in(gw, "ana-1", "hola")
    with db.tx() as conn:
        OW.grant(conn, biz, "t", "reply", True)
        for kind in ("paylink", "pay_reminder", "promo", "slowday", "holiday", "booking_cancel"):
            with pytest.raises(Invalid):
                OW.grant(conn, biz, "t", kind, True)
    net = Net()
    tg_in(tg, "900", "responde a Ana: listo", net=net)
    assert net.last() == t("va_queued", "es")
    assert actions(biz, kind="reply")[-1]["status"] == "queued"
    with db.tx() as conn:
        OW.grant(conn, biz, "t", "reply", False)                                        # taken back
    tg_in(tg, "900", "responde a Ana: otra cosa", net=net)
    assert actions(biz, kind="reply")[-1]["status"] == "asked"


def test_stop_pauses_her_own_actions_and_the_ai_until_resume():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="auto")
        OW.grant(conn, biz, "t", "loyalty", True)
        OW.ask(conn, biz, "loyalty", "x", {"text": "hola"}, contact_id=customer(biz, gw, "Ana", who="ana-1")["id"])   # queued alone
    assert actions(biz, kind="loyalty")[-1]["status"] == "queued"
    net = Net(ai_text="¡Hola!")
    tg_in(tg, "900", "Valeria, para", net=net)
    assert net.last().startswith(t("va_stopped", "es"))
    assert actions(biz, kind="loyalty")[-1]["status"] == "undone"
    _, res = gw_in(gw, "ana-1", "¿precio?", net=net)
    assert res == ["paused"] and not net.ai_calls and not net.gw
    with db.tx() as conn:
        assert not OW.alone(OW.settings(conn, biz["id"]), "loyalty")
    tg_in(tg, "900", "sigue", net=net)
    assert net.last() == t("va_resumed", "es")
    _, res = gw_in(gw, "ana-1", "¿precio?", net=net)
    assert res == ["replied"]


# ---------------------------------------------------------------- understanding
def test_not_sure_gives_three_taps_and_a_tap_works():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    net = Net()
    tg_in(tg, "900", "zxqv blorp", net=net)
    assert t("va_unsure", "es") in net.last()
    assert "1) " in net.last() and "3) " in net.last()
    tg_in(tg, "900", "1", net=net)                                                      # 1 = Today
    assert net.last().startswith(t("va_today_head", "es").split("{")[0])


def test_the_business_own_ai_reads_any_mix_and_rules_are_the_fallback():
    biz, tg, gw = shop()
    biz = with_ai(biz)
    link_owner(biz, tg, "900")
    net = Net(ai_text=lambda p, j: json.dumps({"intent": "tomorrow"}))
    tg_in(tg, "900", "oye what's up para el día after today?", net=net)
    assert net.last().startswith(t("va_tomorrow_head", "es").split("{")[0])
    assert net.ai_calls and "AIza" not in json.dumps(net.ai_calls[-1])                 # the key travels in a header only
    busy = Net(ai_text=None)                                                            # the AI is busy: the rules answer
    tg_in(tg, "900", "¿quién me debe?", net=busy)
    assert busy.last() == t("va_nobody_owes", "es")


@pytest.mark.parametrize("text,intent", [
    ("today", "today"), ("hoy", "today"), ("hoje", "today"), ("aujourd'hui", "today"), ("heute", "today"), ("ngayon", "today"),
    ("hari ini", "today"), ("आज", "today"), ("اليوم", "today"), ("今日", "today"), ("今天", "today"),
    ("who owes me?", "owes"), ("¿quién me debe?", "owes"), ("quem me deve", "owes"), ("qui me doit ?", "owes"),
    ("wer schuldet mir", "owes"), ("sino ang may utang", "owes"), ("siapa yang berutang", "owes"), ("किसका बकाया है", "owes"),
    ("من عليه دين لم يدفع", "owes"), ("未払いは？", "owes"), ("谁欠款", "owes"),
    ("stop", "stop"), ("Valeria, para", "stop"), ("pare", "stop"), ("arrête", "stop"), ("stopp", "stop"), ("tigil", "stop"),
    ("berhenti", "stop"), ("रुको", "stop"), ("توقف", "stop"), ("止めて", "stop"), ("停止", "stop"),
    ("undo", "undo"), ("deshacer", "undo"), ("desfazer", "undo"), ("rückgängig", "undo"), ("bawiin", "undo"), ("urungkan", "undo"),
    ("वापस लो", "undo"), ("تراجع", "undo"), ("元に戻す", "undo"), ("撤销", "undo"),
    ("what did you do today?", "log"), ("¿qué hiciste hoy?", "log"), ("ask yesterday's customers for a review", "review"),
    ("who hasn't come back?", "winback"), ("¿quiénes no han vuelto?", "winback"), ("send Juan a payment link for 500", "paylink"),
    ("move Luis to 4pm", "move"), ("cancela a Luis", "cancel"), ("block 2 to 4", "block"), ("anything new?", "new"),
    ("¿algo nuevo?", "new"), ("más formal", "style"), ("sin emojis", "style"), ("cerramos a las 6 el sábado", "hours"),
])
def test_keyword_rules_in_every_language(text, intent):
    assert CMD.rules(text)["intent"] == intent


# ---------------------------------------------------------------- never nag
def test_one_question_at_a_time_and_quiet_after_two_unanswered(daytime):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    ana = customer(biz, gw, "Ana")
    with db.tx() as conn:
        for i in range(3):
            OW.ask(conn, biz, "loyalty", f"pregunta {i}", {"text": "x"}, contact_id=ana["id"])
    net = Net()
    assert OW.prompt_due(http=net.http)["asked"] == 1
    assert OW.prompt_due(http=net.http)["asked"] == 0                                   # the first is still open
    with db.tx() as conn:                                                               # a day passes, unanswered
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.prompted_at.isnot(None))
                     .values(prompted_at=NOW() - timedelta(hours=25)))
    assert OW.prompt_due(http=net.http)["asked"] == 1                                   # second one: now she goes quiet
    with db.tx() as conn:
        s = OW.settings(conn, biz["id"])
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.prompted_at.isnot(None))
                     .values(prompted_at=NOW() - timedelta(hours=25)))
    assert s["unanswered"] == 2 and s["quiet_until"] > NOW()
    assert OW.prompt_due(http=net.http)["asked"] == 0                                   # quiet until tomorrow
    tg_in(tg, "900", "hoy", net=net)                                                    # the owner writes: she may speak again
    with db.tx() as conn:
        s = OW.settings(conn, biz["id"])
    assert s["unanswered"] == 0 and s["quiet_until"] is None
    assert OW.prompt_due(http=net.http)["asked"] == 1


def test_she_never_speaks_first_at_night(monkeypatch):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        OW.ask(conn, biz, "promo", "¿promo?", {"mode": "winback"})
    monkeypatch.setattr(OW, "OPEN_FROM", 25)                                            # never "day"
    assert OW.prompt_due(http=Net().http)["asked"] == 0


def test_morning_summary_once_a_day_with_one_tap_answers(daytime):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    tz = ZoneInfo(biz["timezone"])
    appt(biz, customer(biz, gw, "Ana"), datetime.combine(datetime.now(tz).date(), time(23, 45), tz))
    net = Net()
    assert BR.round_(http=net.http)["morning"] == 1
    assert t("va_morning", "es") in net.last() and "23:45" in net.last() and "1) " in net.last()
    assert BR.round_(http=net.http)["morning"] == 0                                     # once a day
    tg_in(tg, "900", "2", net=net)                                                      # 2 = tomorrow
    assert net.last().startswith(t("va_tomorrow_head", "es").split("{")[0])


def test_what_did_you_do_today_is_honest():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    net = Net()
    tg_in(tg, "900", "¿qué hiciste hoy?", net=net)
    assert t("va_log_nothing", "es") in net.last()
    customer(biz, gw, "Ana", who="ana-1")
    gw_in(gw, "ana-1", "hola")
    tg_in(tg, "900", "responde a Ana: hola!", net=net)
    tg_in(tg, "900", "1", net=net)
    due_now(biz)
    OW.run_due(http=net.http)
    tg_in(tg, "900", "what did you do today?", net=net)
    assert "Ana" in net.last()


# ---------------------------------------------------------------- 24/7 answers in their style
def test_rule_answers_use_only_the_owners_facts_day_or_night():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        OW.save_settings(conn, biz, "t", answers=True)
        from valeria_one.services import style
        style.save(conn, biz, "t", {"emoji": "none", "signoff": "— Sol"})
    customer(biz, gw, "Ana", who="ana-1")
    net = Net()
    _, res = gw_in(gw, "ana-1", "¿cuánto cuesta el corte?", net=net)                    # whatever the hour
    assert res == ["answers:replied"]
    body = net.gw[-1]["body"]
    assert "Corte" in body and "200" in body and body.endswith("— Sol")
    _, res = gw_in(gw, "ana-1", "¿hacen uñas acrílicas?", net=net)                       # not in her facts
    assert res == ["answers:asked_owner"] and net.gw[-1]["body"].startswith(t("va_ans_later", "es").split("!")[0])
    q = actions(biz, kind="reply")[-1]
    assert q["status"] == "asked" and "uñas" in q["summary"]
    n = len(net.gw)
    gw_in(gw, "ana-1", "¿y pestañas?", net=net)                                          # "we'll get back" once per 12 h
    assert len(net.gw) == n


def test_rule_answers_are_off_by_default_and_hours_unknown_means_ask():
    biz, tg, gw = shop(booking=False)
    customer(biz, gw, "Ana", who="ana-1")
    net = Net()
    _, res = gw_in(gw, "ana-1", "¿a qué hora abren?", net=net)
    assert res == ["off"] and not net.gw
    with db.tx() as conn:
        OW.save_settings(conn, biz, "t", answers=True)
    _, res = gw_in(gw, "ana-1", "¿a qué hora abren?", net=net)                           # no hours entered: never invented
    assert res == ["answers:asked_owner"]


def test_ai_handoff_asks_the_owner():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="auto")
    customer(biz, gw, "Ana", who="ana-1")
    net = Net(ai_text="Una persona te responde pronto. " + AI.HANDOFF)
    _, res = gw_in(gw, "ana-1", "quiero hablar con alguien", net=net)
    assert res == ["handed_off"]
    assert actions(biz, kind="reply")[-1]["status"] == "asked"
    with db.tx() as conn:
        style_line = AI._system(conn, biz, "", "es")
    assert "Write the way the owner does" in style_line


def test_answer_topics_in_languages():
    for txt, topic in (("how much is it", "price"), ("¿cuánto cuesta?", "price"), ("magkano po", "price"), ("berapa harganya", "price"),
                       ("いくらですか", "price"), ("多少钱", "price"), ("كم السعر", "price"), ("where are you", "location"),
                       ("¿dónde están?", "location"), ("what time do you open", "hours"), ("quiero una cita", "booking")):
        assert ANS.topic(txt) == topic, txt


# ---------------------------------------------------------------- the web chat + screens
def test_web_chat_and_page(client):
    biz, tg, gw = shop()
    owner_session(client, biz)
    r = client.get("/app/valeria")
    assert r.status_code == 200 and "Valeria" in r.text
    from _va import csrf
    tok = csrf(client, "/app/valeria")
    r = client.post("/app/valeria/say", data={"csrf": tok, "text": "¿quién me debe?"}, follow_redirects=True)
    assert r.status_code == 200 and t("va_nobody_owes", "es") in r.text
    r = client.post("/app/valeria/stop", data={"csrf": tok}, follow_redirects=True)
    with db.tx() as conn:
        assert OW.paused(conn, biz["id"])
    client.post("/app/valeria/resume", data={"csrf": tok})
    with db.tx() as conn:
        assert not OW.paused(conn, biz["id"])
    r = client.post("/app/valeria/pair", data={"csrf": tok}, follow_redirects=True)
    assert "V-" in r.text
    for step in (1, 2, 3, 4):
        assert client.get(f"/app/valeria/setup?step={step}").status_code == 200
    r = client.post("/app/valeria/tone", data={"csrf": tok, "tone": "formal", "next": "/app/valeria/setup?step=3"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("step=3")
    assert client.get("/app").status_code == 200


def test_staff_on_the_web_cannot_change_settings(client):
    biz, tg, gw = shop()
    owner_session(client, biz, role="staff")
    assert client.get("/app/valeria").status_code == 200
    from _va import csrf
    tok = csrf(client, "/app/valeria")
    client.post("/app/valeria/autonomy", data={"csrf": tok, "kind": "reply", "on": "1"})
    with db.tx() as conn:
        assert not OW.settings(conn, biz["id"])["autonomy"]


def test_every_new_word_is_in_all_11_languages():
    import pathlib
    import re
    base = pathlib.Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.split("_")[0] in ("va", "ly", "hp", "vn")] + [
        "valeria", "nx_valeria", "nx_helpers", "blk_assistant", "blk_assistant_d", "blk_loyalty", "blk_loyalty_d"]
    assert len(new) > 300
    same_ok = {"valeria", "va_card_title", "va_need_whatsapp", "hp_need_whatsapp"}
    for lg in LANGS:
        for k in new:
            assert k in files[lg], (lg, k)
            ph = sorted(re.findall(r"\{[a-z_]+\}", files["en"][k]))
            assert sorted(re.findall(r"\{[a-z_]+\}", files[lg][k])) == ph, (lg, k)
        if lg != "en":
            copied = [k for k in new if files[lg][k] == files["en"][k] and k not in same_ok and re.search(r"[a-z]{4}", files["en"][k])]
            assert len(copied) <= 12, (lg, copied)                                     # proper nouns only (WhatsApp, Google…)
