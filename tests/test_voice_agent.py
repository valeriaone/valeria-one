"""ghl01 (v0.18): the phone agent on the business's OWN Twilio number with its OWN AI key — dormant until both are there,
every webhook signature-checked, answers only from the business's facts, books, takes messages, puts callers through,
and turns anything it doesn't know into a call-back task and a word to the owner. Twilio and the AI are fakes."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from _va import ALL_WEEK, refuse_network
from conftest import make_business
from valeria_one import db
from valeria_one.channels.twilio_sms import signature
from valeria_one.services import Invalid
from valeria_one.services import ai as AI
from valeria_one.services import booking as BK
from valeria_one.services import inbox as X
from valeria_one.services import voice_agent as VA

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
TOKEN = "twilio-token"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def setup(key=True, enabled=True, **agent):
    biz, _ = make_business("MX", "salon", name="Salón Luz")
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="sms", name="Número", cfg={"account_sid": "AC9", "auth_token": TOKEN,
                                                                     "from_number": "+525599990000"})
        hook = conn.execute(select(db.channel_accounts.c.hook_id).where(db.channel_accounts.c.business_id == biz["id"])).scalar()
        BK.save_settings(conn, biz, "t", enabled=True, hours=ALL_WEEK, notice_minutes=60)
        BK.add_service(conn, biz, "t", name="Corte", minutes=30, price_minor=25000)
        if key:
            AI.save(conn, biz, "t", provider="gemini", api_key="own-key", mode="suggest")
        if enabled:
            VA.save(conn, biz, "t", enabled=True, **agent)
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    return biz, hook


def post(client, hook, path, params, signed=True):
    url = f"https://one.test/hooks/{hook}{path}"
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        h["X-Twilio-Signature"] = signature(TOKEN, url, params)
    return client.post(f"/hooks/{hook}{path}", content=urlencode(params), headers=h)


def call(sid="CA1", caller="+525511112233"):
    return {"CallSid": sid, "From": caller, "To": "+525599990000"}


def says(xml):
    return " ".join(re.findall(r"<Say[^>]*>(.*?)</Say>", xml))


def fake_ai(monkeypatch, answers):
    """answers: heard -> JSON the AI gives back (the router, and the question-answerer)."""
    seen = []

    def ai(biz, system, user, http=None):
        seen.append((system, user))
        if "FACTS:" in system:
            return json.dumps(answers.get(("q", user), {"known": False, "answer": ""}))
        return json.dumps(answers.get(user, {"intent": "question", "question": user}))
    monkeypatch.setattr(VA, "_ai", ai)
    return seen


def the_call():
    with db.tx() as conn:
        return dict(conn.execute(select(db.calls)).mappings().one())


# ---------------------------------------------------------------- dormant until the business's own accounts are there
def test_dormant_without_the_ai_key_and_it_cannot_be_switched_on(client):
    biz, hook = setup(key=False, enabled=False)
    with db.tx() as conn:
        assert VA.missing(conn, biz) == ["ai"]
        with pytest.raises(Invalid):
            VA.save(conn, biz, "t", enabled=True)
    r = post(client, hook, "/voice", call())
    assert r.status_code == 200 and "<Gather" not in r.text                                    # the old way: text-back
    assert client.get("/app/voice-agent").status_code in (200, 303)


def test_every_webhook_is_signed(client):
    biz, hook = setup()
    for path in ("/voice", "/voice/agent", "/voice/agent/recording"):
        r = post(client, hook, path, call(), signed=False)
        assert r.status_code == 403, path
    assert client.post("/hooks/nope/voice/agent", content=b"", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 404


# ---------------------------------------------------------------- a call
def test_greets_with_the_recording_notice_and_listens(client, monkeypatch):
    biz, hook = setup(record=True, greeting="Hola, gracias por llamar a Salón Luz.")
    fake_ai(monkeypatch, {})
    started = []
    monkeypatch.setattr(VA, "_record", lambda cfg, sid, base, http=None: started.append(sid))
    xml = post(client, hook, "/voice", call()).text
    assert '<Gather input="speech"' in xml and f'action="https://one.test/hooks/{hook}/voice/agent"' in xml and 'language="es-MX"' in xml
    s = says(xml)
    assert s.index(VA._t("vc_recorded", "es")) < s.index("Hola, gracias por llamar")              # announced first
    assert started == ["CA1"]
    c = the_call()
    assert c["status"] == "live" and c["contact_id"] and c["turns"][0]["who"] == "agent"


def test_prices_and_hours_come_from_the_businesss_own_facts(client, monkeypatch):
    biz, hook = setup()
    fake_ai(monkeypatch, {"¿cuánto cuesta el corte?": {"intent": "price"}, "¿a qué hora abren?": {"intent": "hours"}})
    post(client, hook, "/voice", call())
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "¿cuánto cuesta el corte?"}).text
    assert "Corte" in says(xml) and "250" in says(xml) and "<Gather" in xml
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "¿a qué hora abren?"}).text
    assert "08:00" in says(xml)


def test_unknown_becomes_a_call_back_task_and_a_word_to_the_owner(client, monkeypatch):
    biz, hook = setup()
    fake_ai(monkeypatch, {("q", "¿tienen estacionamiento?"): {"known": False, "answer": ""},
                          ("q", "¿hacen descuento a estudiantes?"): {"known": True, "answer": "Sí, 45% de descuento."}})
    post(client, hook, "/voice", call())
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "¿tienen estacionamiento?"}).text
    assert VA._t("vc_callback", "es") in says(xml)
    # the AI claims to know, but "45" is in none of the facts: never said
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "¿hacen descuento a estudiantes?"}).text
    assert "45%" not in says(xml) and VA._t("vc_callback", "es") in says(xml)
    with db.tx() as conn:
        tasks = [dict(r) for r in conn.execute(select(db.tasks)).mappings()]
        notes = [dict(r) for r in conn.execute(select(db.assistant_actions)).mappings()]
    assert len(tasks) == 2 and "+525511112233" in tasks[0]["title"] and tasks[0]["contact_id"]
    assert {n["kind"] for n in notes} == {"notice"} and all(n["status"] == "queued" for n in notes)
    assert VA.grounded("Abrimos a las 08:00", "hours 08:00-20:00") and not VA.grounded("Cuesta 99", "price 250")


def test_books_an_appointment_in_two_turns(client, monkeypatch):
    biz, hook = setup()
    fake_ai(monkeypatch, {"quiero una cita para corte": {"intent": "book", "service": "Corte"}})
    post(client, hook, "/voice", call())
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "quiero una cita para corte"}).text
    st = the_call()["state"]
    assert st["mode"] == "book_slot" and len(st["slots"]) == 2 and st["said"][0] in says(xml)
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "la primera"}).text
    c = the_call()
    assert c["appointment_id"] and st["said"][0] in says(xml)
    with db.tx() as conn:
        ap = dict(conn.execute(select(db.appointments)).mappings().one())
    assert ap["title"] == "Corte" and ap["contact_id"] == c["contact_id"]
    assert ap["starts_at"] == datetime.fromisoformat(st["slots"][0])


def test_takes_a_message_puts_through_and_hangs_up_with_a_summary(client, monkeypatch):
    biz, hook = setup(transfer_to="55 1234 5678")
    fake_ai(monkeypatch, {"quiero dejar un recado": {"intent": "message"}, "adiós": {"intent": "bye"},
                          "con una persona": {"intent": "transfer"}})
    post(client, hook, "/voice", call())
    post(client, hook, "/voice/agent", {**call(), "SpeechResult": "quiero dejar un recado"})
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "Que me llame Marta al rato"}).text
    assert VA._t("vc_message_taken", "es") in says(xml)
    xml = post(client, hook, "/voice/agent", {**call(), "SpeechResult": "adiós"}).text
    assert "<Hangup/>" in xml
    c = the_call()
    assert c["status"] == "done" and c["outcome"] == "message" and "Marta" in c["summary"]
    assert [t["who"] for t in c["turns"]][:3] == ["agent", "caller", "agent"]
    post(client, hook, "/voice", call("CA2"))
    xml = post(client, hook, "/voice/agent", {**call("CA2"), "SpeechResult": "con una persona"}).text
    assert "<Dial" in xml and "<Number>+525512345678</Number>" in xml and "/voice/done" in xml


def test_silence_twice_ends_the_call_and_the_sweep_closes_the_rest(client, monkeypatch):
    biz, hook = setup()
    fake_ai(monkeypatch, {})
    post(client, hook, "/voice", call())
    assert VA._t("vc_didnt_hear", "es") in says(post(client, hook, "/voice/agent", {**call(), "SpeechResult": ""}).text)
    assert "<Hangup/>" in post(client, hook, "/voice/agent", {**call(), "SpeechResult": ""}).text
    post(client, hook, "/voice", call("CA3"))
    assert VA.sweep(datetime.now(timezone.utc) + timedelta(hours=1)) == 1
    with db.tx() as conn:
        assert {r for (r,) in conn.execute(select(db.calls.c.status))} == {"done"}


def test_the_wizard_screens(client):
    from test_web import signup
    signup(client, email="vc@example.com")
    page = client.get("/app/voice-agent").text
    assert 'href="/app/channels#sms"' in page                                                 # step 1: your own number
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        X.connect(conn, biz, "t", channel="sms", name="Número", cfg={"account_sid": "AC9", "auth_token": TOKEN, "from_number": "+525599990001"})
    assert 'href="/app/settings#ai"' in client.get("/app/voice-agent").text                   # step 2: your own AI key
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="suggest")
    t = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/voice-agent").text).group(1)
    assert 'name="greeting"' in client.get("/app/voice-agent").text
    client.post("/app/voice-agent", data={"csrf": t, "step": "greeting", "greeting": "Hola"})
    client.post("/app/voice-agent", data={"csrf": t, "step": "can", "can_book": "1", "transfer_to": ""})
    client.post("/app/voice-agent", data={"csrf": t, "step": "record", "record": "0"})
    client.post("/app/voice-agent", data={"csrf": t, "step": "on"})
    assert "vc-check" in client.get("/app/voice-agent").text                                  # the green check


def test_new_words_are_in_all_11_languages():
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith("vc_")]
    assert len(new) > 80
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
