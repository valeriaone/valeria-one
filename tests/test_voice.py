"""Voice notes: written down with the business's own AI key; customers' in the inbox (with a suggested reply), the
owner's as commands; no key = still delivered, and the inbox says how to read it."""
import json

import httpx
import pytest

from _va import Net, b64, customer, gw_in, link_owner, owner_session, refuse_network, shop, tg_in, with_ai
from valeria_one import channels, db
from valeria_one.services import ai as AI
from valeria_one.services import owner as OW
from valeria_one.services import voice as VC
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def last_in(biz):
    with db.tx() as conn:
        return dict(conn.execute(db.messages.select().where(db.messages.c.business_id == biz["id"], db.messages.c.direction == "in")
                                 .order_by(db.messages.c.created_at.desc()).limit(1)).mappings().one())


NOTE = [{"type": "audio", "mime": "audio/ogg; codecs=opus", "url": "https://gw.example/media/1"}]


class GwMedia(Net):
    """The gateway channel can't fetch files; the sandbox-style 'data' is used through a fake download."""


def test_customer_voice_note_is_written_down_and_a_reply_suggested(monkeypatch, client):
    biz, tg, gw = shop()
    biz = with_ai(biz)
    customer(biz, gw, "Ana", who="ana-1")
    monkeypatch.setattr(channels.adapter("webhook").__class__, "download", lambda self, cfg, m: (b"OggS-voice", "audio/ogg"))
    net = Net(audio_words="Hola, ¿tienen cita mañana a las diez?", ai_text="¡Claro! Te esperamos mañana a las 10.")
    _, res = gw_in(gw, "ana-1", "", media=NOTE, net=net)
    m = last_in(biz)
    assert m["transcript"] == "Hola, ¿tienen cita mañana a las diez?" and m["body"] == ""
    assert m["suggestion"] == "¡Claro! Te esperamos mañana a las 10."
    sent_audio = [c for c in net.ai_calls if any("inline_data" in p for p in c["contents"][-1]["parts"])][0]
    assert sent_audio["contents"][-1]["parts"][0]["inline_data"]["mime_type"] == "audio/ogg"     # the codec part dropped
    owner_session(client, biz)
    conv = m["conversation_id"]
    page = client.get(f"/app/inbox?c={conv}").text
    assert "Hola, ¿tienen cita mañana a las diez?" in page and "Te esperamos mañana a las 10." in page
    assert 'name="ai_draft"' in page                                                    # an edit of it teaches her the owner's voice


def test_no_ai_key_the_note_is_kept_and_the_inbox_says_how_to_read_it(client):
    biz, tg, gw = shop()
    customer(biz, gw, "Ana", who="ana-1")
    net = Net()
    gw_in(gw, "ana-1", "", media=NOTE, net=net)
    m = last_in(biz)
    assert m["transcript"] is None and m["media"][0]["type"] == "audio" and not net.ai_calls
    with db.tx() as conn:
        ev = conn.execute(db.events.select().where(db.events.c.type == "voice.unread")).mappings().first()
    assert ev and ev["data"]["why"] == "no AI key"
    owner_session(client, biz)
    assert t("vn_unread", "es") in client.get(f"/app/inbox?c={m['conversation_id']}").text


def test_an_ai_that_cannot_listen_degrades_gracefully():
    biz, tg, gw = shop()
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="anthropic", api_key="sk-own", mode="suggest")
    with pytest.raises(AI.AIError):
        AI.transcribe(biz, b"OggS", "audio/ogg", Net().http)
    with pytest.raises(AI.AIError):
        AI.transcribe(biz, b"x" * (AI.MAX_AUDIO + 1), "audio/ogg", Net().http)


def test_openai_key_uses_speech_to_text():
    biz, tg, gw = shop()
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="openai", api_key="sk-own", mode="suggest")
    seen = []

    def oa(req):
        seen.append(req)
        return httpx.Response(200, json={"text": "hola qué tal"})
    assert AI.transcribe(biz, b"OggS", "audio/ogg", httpx.Client(transport=httpx.MockTransport(oa))) == "hola qué tal"
    assert seen[0].url.path == "/v1/audio/transcriptions" and b'filename="voice.ogg"' in seen[0].content


def test_the_operators_demo_ai_is_never_used_for_the_assistant():
    from conftest import make_business
    biz, _ = make_business("MX", "salon", sandbox=True)
    with db.tx() as conn:
        AI.save(conn, biz, "t", provider="demo", mode="auto")
        assert AI.own_key(conn, biz) is None
    with pytest.raises(AI.AIError):
        AI.transcribe(biz, b"OggS", "audio/ogg", Net().http)


def test_owner_voice_note_is_a_command():
    biz, tg, gw = shop()
    biz = with_ai(biz)
    link_owner(biz, tg, "900")
    net = Net(audio_words="¿quién me debe?", ai_text=lambda p, j: json.dumps({"intent": "owes"}))
    st, out = tg_in(tg, "900", voice="vf-1", net=net)
    assert out == ["answered"] and net.last() == t("va_nobody_owes", "es")
    with db.tx() as conn:
        m = conn.execute(db.assistant_messages.select().where(db.assistant_messages.c.direction == "in",
                                                              db.assistant_messages.c.media != [])).mappings().one()
    assert m["transcript"] == "¿quién me debe?"


def test_owner_voice_note_without_a_key_says_how():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    net = Net()
    st, out = tg_in(tg, "900", voice="vf-2", net=net)
    assert out == ["voice_no_ai"] and "/app/settings#ai" in net.last()


def test_telegram_and_whatsapp_media_are_read_and_fetched_with_the_business_account():
    tg = channels.adapter("telegram")
    from valeria_one.channels import HookRequest
    from _va import tg_update
    items = tg.parse({"secret": "s"}, HookRequest("POST", "", {"x-telegram-bot-api-secret-token": "s"}, {}, tg_update("7", voice="abc")))
    assert items[0].media == [{"type": "audio", "provider_media_id": "abc", "mime": "audio/ogg"}]
    items = tg.parse({"secret": "s"}, HookRequest("POST", "", {"x-telegram-bot-api-secret-token": "s"}, {}, tg_update("7", "mira", photo="p1")))
    assert items[0].media[0] == {"type": "image", "provider_media_id": "p1", "mime": "image/jpeg"} and items[0].body == "mira"
    net = Net()
    net.files["abc"] = b"OggS-123"
    raw, mime = channels.adapter("telegram", net.http).download({"bot_token": "1:x"}, items and {"provider_media_id": "abc", "mime": "audio/ogg"})
    assert raw == b"OggS-123" and mime == "audio/ogg"
    calls = []

    def meta(req):
        calls.append(str(req.url))
        if req.url.path.endswith("/m-9"):
            return httpx.Response(200, json={"url": "https://lookaside.example/m9", "mime_type": "audio/ogg; codecs=opus"})
        return httpx.Response(200, content=b"OggS-wa")
    wa = channels.adapter("whatsapp", httpx.Client(transport=httpx.MockTransport(meta)))
    raw, mime = wa.download({"token": "own"}, {"provider_media_id": "m-9"})
    assert raw == b"OggS-wa" and mime.startswith("audio/ogg") and len(calls) == 2
    msg = wa.items({"phone_number_id": "1"}, {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "1"}, "messages": [
        {"from": "5215551234567", "id": "w1", "type": "audio", "audio": {"id": "m-9", "mime_type": "audio/ogg; codecs=opus", "voice": True}}]}}]}]})
    assert VC.audio_of(msg[0].media)["provider_media_id"] == "m-9"


def test_sandbox_voice_note_end_to_end_with_own_gemini_key():
    from conftest import make_business
    from valeria_one.channels import HookRequest
    from valeria_one.services import autoreply
    from valeria_one.services import inbox as X
    biz, _ = make_business("MX", "salon", sandbox=True)
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="sandbox", name="Sandbox")
        AI.save(conn, biz, "t", provider="gemini", api_key="own", mode="suggest")
        row = OW.start_pairing(conn, biz, "t")
    from _va import raw_acc
    st = X.receive(raw_acc(acc["id"]), HookRequest("POST", "", {}, {}, json.dumps({"from": "me", "body": row["pair_code"]}).encode()))
    net = Net(audio_words="hoy", ai_text=lambda p, j: json.dumps({"intent": "today"}))
    assert autoreply.respond_owner(biz["id"], st["owner_ids"][0], http=net.http) == "paired"
    st = X.receive(raw_acc(acc["id"]), HookRequest("POST", "", {}, {}, json.dumps(
        {"from": "me", "body": "", "media": [{"type": "audio", "mime": "audio/ogg", "data": b64(b"OggS")}]}).encode()))
    assert autoreply.respond_owner(biz["id"], st["owner_ids"][0], http=net.http) == "answered"
    with db.tx() as conn:
        m = conn.execute(db.assistant_messages.select().where(db.assistant_messages.c.direction == "in", db.assistant_messages.c.transcript.isnot(None))).mappings().one()
    assert "data" not in m["media"][0]                                                  # the file isn't kept twice
