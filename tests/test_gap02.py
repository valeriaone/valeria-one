"""gap02 (v0.42.0): tap-to-pick on every channel — WhatsApp buttons/list/Flow/catalog, Messenger quick replies, Telegram inline
buttons, a numbered list where there are no taps; the tapped option's id comes back."""
import json

import httpx

from valeria_one.channels import HookRequest, Outbound, numbered
from valeria_one.channels.meta import Messenger, WhatsApp
from valeria_one.channels.telegram import Telegram
from valeria_one.channels.twilio_sms import TwilioSMS
from valeria_one.channels.twilio_whatsapp import TwilioWhatsApp


def cap(resp=None):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=resp or {"messages": [{"id": "wamid.1"}], "message_id": "m1", "ok": True,
                                                 "result": {"message_id": 7}, "sid": "SM1", "status": "queued"})
    return seen, httpx.Client(transport=httpx.MockTransport(handler))


OPTS3 = [{"id": "si", "title": "Sí, apártala"}, {"id": "no", "title": "Ahora no"}, {"id": "mas", "title": "Ver más"}]


def test_whatsapp_three_short_options_are_buttons_more_are_a_list():
    seen, http = cap()
    WhatsApp(http=http).send({"phone_number_id": "1", "token": "t"}, "+529991", Outbound(to="x", body="¿La apartamos?", options=OPTS3))
    it = json.loads(seen[0].content)["interactive"]
    assert it["type"] == "button" and [b["reply"]["id"] for b in it["action"]["buttons"]] == ["si", "no", "mas"]
    WhatsApp(http=http).send({"phone_number_id": "1", "token": "t"}, "+529991",
                             Outbound(to="x", body="Elige", options=[{"id": str(i), "title": f"Opción {i}"} for i in range(5)]))
    assert json.loads(seen[1].content)["interactive"]["type"] == "list"


def test_whatsapp_flow_and_catalog():
    seen, http = cap()
    WhatsApp(http=http).send({"phone_number_id": "1", "token": "t"}, "+52", Outbound(to="x", body="Reserva aquí",
                                                                                     options=[{"flow_id": "123", "flow_cta": "Reservar"}]))
    f = json.loads(seen[0].content)["interactive"]
    assert f["type"] == "flow" and f["action"]["parameters"]["flow_id"] == "123" and f["action"]["parameters"]["flow_cta"] == "Reservar"
    WhatsApp(http=http).send({"phone_number_id": "1", "token": "t"}, "+52", Outbound(to="x", body="Nuestro catálogo", options=[{"catalog": True}]))
    assert json.loads(seen[1].content)["interactive"]["type"] == "catalog_message"


def test_whatsapp_tapped_button_keeps_its_id():
    data = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "1"}, "messages": [
        {"from": "529991", "id": "w1", "type": "interactive", "interactive": {"button_reply": {"id": "si", "title": "Sí, apártala"}}}]}}]}]}
    it = WhatsApp().items({"phone_number_id": "1"}, data)[0]
    assert it.body == "Sí, apártala" and it.pick == {"id": "si", "title": "Sí, apártala"}


def test_messenger_quick_replies_both_ways():
    seen, http = cap()
    Messenger(http=http).send({"page_token": "t"}, "psid", Outbound(to="x", body="¿Te aparto?", options=OPTS3[:2]))
    assert [q["payload"] for q in json.loads(seen[0].content)["message"]["quick_replies"]] == ["si", "no"]
    data = {"object": "page", "entry": [{"id": "pg", "messaging": [{"sender": {"id": "u1"}, "timestamp": 1,
            "message": {"mid": "m1", "text": "Ahora no", "quick_reply": {"payload": "no"}}}]}]}
    assert Messenger().items({"page_id": "pg"}, data)[0].pick == {"id": "no", "title": "Ahora no"}


def test_telegram_inline_buttons_both_ways():
    seen, http = cap()
    Telegram(http=http).send({"bot_token": "b"}, "55", Outbound(to="x", body="¿Te aparto?", options=OPTS3[:2]))
    kb = json.loads(seen[0].content)["reply_markup"]["inline_keyboard"]
    assert kb[0][0] == {"text": "Sí, apártala", "callback_data": "si"}
    upd = {"callback_query": {"id": "c1", "data": "si", "from": {"first_name": "Ana"},
                              "message": {"chat": {"id": 55}, "reply_markup": {"inline_keyboard": kb}}}}
    it = Telegram().parse({"secret": "s"}, HookRequest("POST", "", {"x-telegram-bot-api-secret-token": "s"}, {}, json.dumps(upd).encode()))[0]
    assert it.body == "Sí, apártala" and it.pick == {"id": "si", "title": "Sí, apártala"} and it.address == "55"


def test_no_taps_means_a_numbered_list():
    assert numbered("¿Te aparto?", OPTS3[:2]) == "¿Te aparto?\n\n1. Sí, apártala\n2. Ahora no"
    for ad, cfg in ((TwilioSMS, {"from_number": "+1"}), (TwilioWhatsApp, {"from_number": "+1"})):
        form = ad()._form({"account_sid": "A", "auth_token": "t", **cfg}, "+52", Outbound(to="x", body="¿Te aparto?", options=OPTS3[:2]))
        assert form["Body"].endswith("1. Sí, apártala\n2. Ahora no"), ad
