"""close01 (v0.51.0): voicemail — when nobody answers, the caller can leave a message; it lands in the inbox as their message
(how long, their words written down with the business's own AI key, a private link that plays it from the business's own
Twilio account). The text-back still goes. «voicemail»: "off" turns it off."""
import httpx

from test_calls_reviews import CFG, call, connect, twilio  # noqa: F401  (fixture)
from valeria_one import db
from valeria_one.services import ai as AI
from valeria_one.services import calls
from valeria_one.services import inbox as X

SID = "RE" + "a" * 32


def test_nobody_answers_they_leave_a_message_and_it_is_in_the_inbox(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    form = {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"}
    r = call(client, path + "/voice", form)
    assert "just sent you a text" in r.text and "leave your message after the tone" in r.text
    assert '<Record maxLength="120"' in r.text and f'action="https://one.test{path}/voice/voicemail"' in r.text
    assert twilio.sent[-1]["To"] == ["+19514343352"]                                # the text-back still went
    vm = {**form, "RecordingUrl": f"https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/{SID}", "RecordingSid": SID,
          "RecordingDuration": "14"}
    assert client.post(path + "/voice/voicemail", data=vm, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    r = call(client, path + "/voice/voicemail", vm)
    assert r.status_code == 200 and "<Hangup/>" in r.text
    call(client, path + "/voice/voicemail", vm)                                    # Twilio retries: still one message
    conv = client.get("/v1/conversations", headers=h).json()[0]
    msgs = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]
    ins = [m for m in msgs if m["direction"] == "in"]
    assert len(ins) == 1 and "Voicemail (14 s)" in ins[0]["body"] and f"/voicemail/{SID}.mp3" in ins[0]["body"]


def test_after_the_phone_rang_unanswered_they_can_leave_a_message_too(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, extra={"forward_to": "(951) 555-0100"}, monkeypatch=monkeypatch)
    form = {"From": "+19514343352", "To": "+15550001111", "CallSid": "CA1"}
    r = call(client, path + "/voice/done", {**form, "DialCallStatus": "no-answer"})
    assert '<Record maxLength="120"' in r.text and f'action="https://one.test{path}/voice/voicemail"' in r.text
    r = call(client, path + "/voice/done", {**form, "DialCallStatus": "completed"})
    assert "<Record" not in r.text                                                  # they picked up: no voicemail


def test_voicemail_can_be_off_and_silence_files_nothing(client, us, twilio, monkeypatch):
    _, h = us
    path = connect(client, h, extra={"voicemail": "off"}, monkeypatch=monkeypatch)
    r = call(client, path + "/voice", {"From": "+19514343352", "To": "+15550001111"})
    assert "<Record" not in r.text                                                  # off: the old way (text-back + goodbye)
    r = call(client, path + "/voice/voicemail", {"From": "+19514343352", "RecordingUrl": "https://x", "RecordingSid": SID,
                                                  "RecordingDuration": "0"})
    assert "<Hangup/>" in r.text
    ins = [m for c in client.get("/v1/conversations", headers=h).json()
           for m in client.get(f"/v1/conversations/{c['id']}", headers=h).json()["messages"] if m["direction"] == "in"]
    assert ins == []                                                                # hung up at the tone: nothing filed


def test_their_words_are_written_down_with_the_business_own_key(client, us, twilio, monkeypatch):
    bz, h = us
    path = connect(client, h, monkeypatch=monkeypatch)
    monkeypatch.setattr(AI, "own_key", lambda conn, biz: True)
    monkeypatch.setattr(AI, "transcribe", lambda biz, raw, mime, http=None: "Hola, quiero agendar el sábado" if raw == b"MP3" else "")
    fake = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"MP3")))
    real = calls.voicemail
    monkeypatch.setattr(calls, "voicemail", lambda acc, req, http=None: real(acc, req, http=fake))
    vm = {"From": "+19514343352", "To": "+15550001111", "RecordingUrl": "https://api.twilio.com/x/" + SID, "RecordingSid": SID,
          "RecordingDuration": "9"}
    call(client, path + "/voice/voicemail", vm)
    conv = client.get("/v1/conversations", headers=h).json()[0]
    body = [m for m in client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"] if m["direction"] == "in"][0]["body"]
    assert "«Hola, quiero agendar el sábado»" in body


def test_the_private_link_plays_from_their_own_twilio(client, us, monkeypatch):
    _, h = us
    r = client.post("/v1/channels", headers=h, json={"channel": "sms", "name": "Line", "config": {**CFG, "account_sid": "AC" + "b" * 32}})
    hook = r.json()["webhook_url"].rsplit("/", 1)[1]
    seen = []
    fake = httpx.Client(transport=httpx.MockTransport(lambda req: seen.append(str(req.url)) or httpx.Response(200, content=b"ID3audio")))
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    r = client.get(f"/hooks/{hook}/voicemail/{SID}.mp3")
    assert r.status_code == 200 and r.content == b"ID3audio" and r.headers["content-type"] == "audio/mpeg"
    assert seen and seen[-1].endswith(f"/Accounts/AC{'b' * 32}/Recordings/{SID}.mp3")
    assert client.get(f"/hooks/{hook}/voicemail/nope.mp3").status_code == 404
    assert client.get(f"/hooks/zzz/voicemail/{SID}.mp3").status_code == 404


def test_money_per_ad_each_ad_on_its_own_line(client):
    import re
    from test_ars02 import fresh, signed_in, visitor
    from valeria_one.services import ads as ADS
    from valeria_one.services import leads as LD
    from valeria_one.services import payments as PAY
    from valeria_one.services import site as ST
    from sqlalchemy import select
    biz = signed_in(client, name="Barbería Max")
    with db.tx() as conn:
        biz = ST.save(conn, fresh(biz), "t", enabled=True)
        a1 = ADS.save(conn, biz, "t", {"name": "Martes 20%", "platform": "meta", "dest": f"https://one.test/s/{biz['slug']}",
                                       "headlines": ["Martes"], "body": "Ven", "audience": "x"})
        a2 = ADS.save(conn, biz, "t", {"name": "Barba gratis", "platform": "meta", "dest": f"https://one.test/s/{biz['slug']}",
                                       "headlines": ["Barba"], "body": "Ven", "audience": "x"})
        a3 = ADS.save(conn, biz, "t", {"name": "Sin clientes", "platform": "google", "dest": f"https://one.test/s/{biz['slug']}",
                                       "headlines": ["Nada"], "body": "Ven", "audience": "x"})
    for code, name, email in ((a1["code"], "Pepe", "pepe@ex.com"), (a1["code"], "Lalo", "lalo@ex.com"), (a2["code"], "Toño", "tono@ex.com")):
        v = visitor()
        v.get(f"/go/{code}", follow_redirects=False)
        st = re.search(r'name="csrf" value="([^"]+)"', v.get(f"/s/{biz['slug']}").text).group(1)
        v.post(f"/s/{biz['slug']}", data={"csrf": st, "name": name, "reach": email, "message": "¿Hay lugar?"})
    visitor().get(f"/go/{a3['code']}", follow_redirects=False)                        # a click, no customer
    with db.tx() as conn:
        pepe = conn.execute(select(db.contacts.c.id).where(db.contacts.c.email == "pepe@ex.com")).scalar()
    PAY.record_received(biz, "t", amount_minor=25000, description="Corte", contact_id=pepe)
    with db.tx() as conn:
        rows = {r["label"]: r for r in LD.report(conn, biz, 90, "es")["rows"]}
    m, b, n = rows.get("Anuncio · Martes 20%"), rows.get("Anuncio · Barba gratis"), rows.get("Anuncio · Sin clientes")
    assert m and b and n, list(rows)
    assert (m["leads"], m["paid_minor"], m["clicks"]) == (2, 25000, 2) and (b["leads"], b["paid_minor"]) == (1, 0)
    assert (n["leads"], n["clicks"]) == (0, 1)                                          # clicks but nobody came: the owner sees it
