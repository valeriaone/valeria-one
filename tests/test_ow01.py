"""ow01 (v0.44.0): Valeria by WhatsApp does more — a shared contact card or a contacts file (.vcf / .csv) becomes customers
(no duplicates, nothing sent), «agrega a …» saves one, «mándale un correo a …» emails from the business's own account and
«mándales a los de #tag: …» reaches that group (only who agreed to offers) — the last two always ask first."""
import json

import pytest
from sqlalchemy import func, select

from _va import TG_SECRET, Net, actions, due_now, link_owner, raw_acc, refuse_network, shop, tg_in
from valeria_one import channels, db
from valeria_one.channels import HookRequest
from valeria_one.services import autoreply
from valeria_one.services import contacts as C
from valeria_one.services import inbox as X
from valeria_one.services import owner as OW
from valeria_one.web.i18n import t

VCF = (b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Juan P\xc3\xa9rez\r\nTEL;TYPE=CELL:+52 999 123 4567\r\nEND:VCARD\r\n"
       b"BEGIN:VCARD\r\nVERSION:2.1\r\nN:L\xc3\xb3pez;Mar\xc3\xada;;;\r\nTEL;CELL:9991112233\r\nEMAIL:maria@x.mx\r\nEND:VCARD\r\n"
       b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Sin datos\r\nEND:VCARD\r\n")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def people(biz):
    with db.tx() as conn:
        return {r.name: r for r in conn.execute(select(db.contacts.c.name, db.contacts.c.phone, db.contacts.c.email)
                                                .where(db.contacts.c.business_id == biz["id"]))}


def tg_raw(tg, message: dict, net: Net):
    """The owner sends something other than text (a contact card, a file) to the business's Telegram bot."""
    message = {"message_id": abs(hash(json.dumps(message, sort_keys=True))) % 10**9, "chat": {"id": "900"},
               "from": {"first_name": "Dueña"}, "date": 1761300000, **message}
    raw = json.dumps({"update_id": message["message_id"], "message": message}).encode()
    st = X.receive(raw_acc(tg["id"]), HookRequest("POST", "", {"x-telegram-bot-api-secret-token": TG_SECRET}, {}, raw))
    return [autoreply.respond_owner(tg["business_id"], mid, http=net.http) for mid in st.get("owner_ids", [])]


def test_a_shared_contact_card_is_saved_once():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    card = {"contact": {"first_name": "Juan", "last_name": "Pérez", "phone_number": "+529991234567"}}
    assert tg_raw(tg, card, net) == ["contacts"]
    assert net.last() == t("va_added", "es").format(name="Juan Pérez")
    assert people(biz)["Juan Pérez"].phone == "+529991234567"
    assert tg_raw(tg, {**card, "caption": "otra vez"}, net) == ["contacts"]
    assert net.last() == t("va_added_dup", "es").format(name="Juan Pérez") and len(people(biz)) == 1
    assert not net.gw                                                                   # nobody was messaged


def test_a_phone_contacts_file_imports_everyone_with_no_duplicates():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["vcf1"] = VCF
    doc = {"document": {"file_id": "vcf1", "file_name": "contactos.vcf", "mime_type": "text/x-vcard"}}
    assert tg_raw(tg, doc, net) == ["contacts_file"]
    assert net.last() == t("va_imported", "es").format(n=2, skipped=1)
    p = people(biz)
    assert p["Juan Pérez"].phone == "+529991234567" and p["María López"].email == "maria@x.mx"
    assert p["María López"].phone == "+529991112233"                                   # local number → E.164 for MX
    net.files["vcf2"] = VCF
    assert tg_raw(tg, {"document": {"file_id": "vcf2", "file_name": "otra.vcf"}}, net) == ["contacts_file"]
    assert net.last() == t("va_imported", "es").format(n=0, skipped=3) and len(people(biz)) == 2
    assert not net.gw


def test_a_csv_with_spanish_headers_and_an_unknown_file():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    net.files["c1"] = "﻿Nombre,Teléfono,Correo\nAna Ruiz,999 555 0001,ana@x.mx\nBeto,,\n".encode()
    assert tg_raw(tg, {"document": {"file_id": "c1", "file_name": "clientes.csv"}}, net) == ["contacts_file"]
    assert net.last() == t("va_imported", "es").format(n=1, skipped=1)
    assert people(biz)["Ana Ruiz"].phone == "+529995550001"
    net.files["c2"] = b"hola, esto no es una lista"
    tg_raw(tg, {"document": {"file_id": "c2", "file_name": "notas.csv"}}, net)
    assert len(people(biz)) == 1


def test_add_a_contact_by_words():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    net = Net()
    tg_in(tg, "900", "agrega a Juan Pérez 999 123 4567 juan@x.mx", net=net)
    assert net.last() == t("va_added", "es").format(name="Juan Pérez")
    p = people(biz)["Juan Pérez"]
    assert p.phone == "+529991234567" and p.email == "juan@x.mx"
    tg_in(tg, "900", "agrega a Juan", net=net)
    assert net.last() == t("va_add_what", "es") and len(people(biz)) == 1


def test_email_a_customer_asks_first_and_goes_from_the_business_email(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context):
            pass
        def login(self, u, p):
            pass
        def send_message(self, m):
            sent.append(m)
    monkeypatch.setattr(channels.adapter("email").__class__, "smtp_factory", FakeSMTP)
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    with db.tx() as conn:
        C.create(conn, biz, "t", {"name": "Ana Ruiz", "phone": "+529995550001"})
    net = Net()
    tg_in(tg, "900", "mándale un correo a Ana: ya llegó tu pedido", net=net)
    assert net.last() == t("va_no_email_channel", "es").format(link=net.last().split(": ", 1)[1])
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="email", name="Correo", cfg={"smtp_host": "smtp.x", "smtp_port": "587", "username": "u",
                                                                       "password": "p", "from_email": "hola@sol.mx", "secret": "s"})
    tg_in(tg, "900", "mándale un correo a Ana: ya llegó tu pedido", net=net)
    assert net.last() == t("va_no_email", "es").format(name="Ana")
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id).where(db.contacts.c.business_id == biz["id"])).scalar()
        C.patch(conn, biz, "t", cid, {"email": "ana@x.mx"})
    tg_in(tg, "900", "mándale un correo a Ana: ya llegó tu pedido", net=net)
    assert "ya llegó tu pedido" in net.last() and not sent                             # a question, nothing sent
    q = actions(biz, kind="reply")[-1]
    assert q["status"] == "asked" and q["payload"]["channel"] == "email" and q["payload"]["subject"] == biz["name"]
    tg_in(tg, "900", "sí", net=net)
    due_now(biz)
    assert OW.run_due(http=net.http) == {"done": 1, "failed": 0}
    assert sent[-1]["To"] == "ana@x.mx" and sent[-1]["Subject"] == biz["name"] and not net.gw


def test_a_message_to_a_tag_reaches_only_who_agreed_and_asks_first():
    biz, tg, _ = shop()
    link_owner(biz, tg, "900")
    yes = {"marketing_consent": True, "marketing_consent_source": "signed at the counter"}
    with db.tx() as conn:
        C.create(conn, biz, "t", {"name": "Ana", "phone": "+529995550001", "tags": ["altabrisa"], **yes})
        C.create(conn, biz, "t", {"name": "Beto", "phone": "+529995550002", "tags": ["altabrisa"], **yes})
        C.create(conn, biz, "t", {"name": "Caro", "phone": "+529995550003", "tags": ["altabrisa"]})       # never said yes
    net = Net()
    tg_in(tg, "900", "mándales a los de #norte: ya tengo casa", net=net)
    assert net.last() == t("va_no_group", "es").format(tag="norte")
    tg_in(tg, "900", "mándales a los de #altabrisa: ya tengo casa nueva", net=net)
    assert net.last().startswith(t("va_q_group", "es").format(n=2, tag="altabrisa", text="ya tengo casa nueva", skipped=1))
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.campaigns).where(db.campaigns.c.business_id == biz["id"])).scalar() == 0
    tg_in(tg, "900", "sí", net=net)
    due_now(biz)
    assert OW.run_due(http=net.http)["done"] == 1
    with db.tx() as conn:
        cp = conn.execute(select(db.campaigns).where(db.campaigns.c.business_id == biz["id"])).mappings().one()
    assert cp["tag"] == "altabrisa" and cp["message"] == "ya tengo casa nueva" and not net.gw   # the campaign outbox sends it
    assert actions(biz, kind="promo")[-1]["result"]["say"] == t("va_group_queued", "es").format(tag="altabrisa")


def test_whatsapp_contact_cards_and_file_names_are_read():
    wa = channels.adapter("whatsapp")
    data = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P1"}, "messages": [
        {"from": "5219990001111", "id": "m1", "timestamp": "1761300000", "type": "contacts", "contacts": [
            {"name": {"formatted_name": "Juan Pérez"}, "phones": [{"phone": "+52 999 123 4567", "wa_id": "5219991234567"}],
             "emails": [{"email": "juan@x.mx"}]}]},
        {"from": "5219990001111", "id": "m2", "timestamp": "1761300000", "type": "document",
         "document": {"id": "D1", "mime_type": "text/vcard", "filename": "contactos.vcf"}}]}}]}]}
    a, b = [i for i in wa.items({"phone_number_id": "P1"}, data) if getattr(i, "provider_id", None) in ("m1", "m2")]
    assert a.media == [{"type": "contact", "name": "Juan Pérez", "phone": "+52 999 123 4567", "email": "juan@x.mx"}]
    assert b.media[0]["filename"] == "contactos.vcf" and b.media[0]["provider_media_id"] == "D1"


def test_staff_without_reply_permission_cannot_import():
    biz, tg, _ = shop()
    with db.tx() as conn:
        row = OW.start_pairing(conn, biz, "t", role="staff", perms=["calendar"])
    tg_in(tg, "901", f"hola {row['pair_code']}")
    net = Net()
    out = tg_raw(tg, {"chat": {"id": "901"}, "contact": {"first_name": "Juan", "phone_number": "+529991234567"}}, net)
    assert out == ["contacts"] and net.last() == t("va_not_allowed", "es") and not people(biz)
