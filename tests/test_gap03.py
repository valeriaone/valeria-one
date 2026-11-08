"""gap03 (v0.42.0): a workflow starts on a word in what a customer wrote, or on the option they tapped; message steps carry up
to 3 buttons (through Valeria's ask-first, to the channel); the editor shows the whole flow as a map."""
import pytest
from sqlalchemy import select

from valeria_one import db
from valeria_one.channels import Inbound
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import inbox as X
from valeria_one.services import workflows as WF

from _va import owner_session
from test_workflows import biz_with_sandbox, runs


def kw_flow(biz, arg, steps=None):
    with db.tx() as conn:
        return WF.save(conn, biz, "user:x", {"name": "Precio", "trigger": "message.keyword", "trigger_arg": arg, "active": True,
                                             "steps": steps or [{"t": "tag", "tag": "pidio-precio"}]})


def tags(biz, phone):
    with db.tx() as conn:
        cid = conn.execute(select(db.contacts.c.id).where(db.contacts.c.business_id == biz["id"], db.contacts.c.phone == phone)).scalar() \
            or conn.execute(select(db.identities.c.contact_id).where(db.identities.c.business_id == biz["id"],
                                                                     db.identities.c.address == phone)).scalar()
        return C.get(conn, biz["id"], cid)["tags"] if cid else []


def test_a_word_in_the_message_starts_the_flow_and_other_words_do_not():
    biz, acc = biz_with_sandbox()
    wf = kw_flow(biz, {"words": ["Precio", "cuánto"]})
    assert wf["trigger_arg"] == {"words": ["precio", "cuánto"]}
    X.receive_items(acc | {"business_id": biz["id"]}, [Inbound(address="+525511110001", body="Hola, ¿cuánto cuesta?", provider_id="k1")])
    X.receive_items(acc | {"business_id": biz["id"]}, [Inbound(address="+525511110002", body="Hola, buen día", provider_id="k2")])
    assert len(runs(wf["id"])) == 1 and "pidio-precio" in tags(biz, "+525511110001") and "pidio-precio" not in tags(biz, "+525511110002")


def test_a_tapped_option_starts_its_flow():
    biz, acc = biz_with_sandbox()
    wf = kw_flow(biz, {"pick": "Sí, apártala"})
    X.receive_items(acc | {"business_id": biz["id"]},
                    [Inbound(address="+525511110003", body="Sí, apártala", provider_id="k3", pick={"id": "Sí, apártala", "title": "Sí, apártala"})])
    assert len(runs(wf["id"])) == 1


def test_a_keyword_flow_needs_a_word_or_an_option():
    biz, _ = biz_with_sandbox()
    with pytest.raises(Invalid):
        kw_flow(biz, {"words": [" "]})


def test_message_buttons_are_kept_and_travel_to_valeria(client):
    biz, acc = biz_with_sandbox()
    steps = [{"t": "message", "text": "¿Te aparto la cabecera?", "ask": True,
              "options": [{"title": "Sí, apártala"}, {"title": "Ahora no"}, {"title": "Ver precios"}, {"title": "Cuarto de más"}]}]
    wf = kw_flow(biz, {"words": ["cabecera"]}, steps)
    assert [o["title"] for o in wf["steps"][0]["options"]] == ["Sí, apártala", "Ahora no", "Ver precios"]   # ≤3
    X.receive_items(acc | {"business_id": biz["id"]}, [Inbound(address="+525511110004", body="me interesa la cabecera", provider_id="k4")])
    with db.tx() as conn:
        p = conn.execute(select(db.assistant_actions.c.payload).where(db.assistant_actions.c.business_id == biz["id"])
                         .order_by(db.assistant_actions.c.created_at.desc())).scalars().first()
    assert p and [o["title"] for o in p["options"]] == ["Sí, apártala", "Ahora no", "Ver precios"]


def test_the_editor_shows_the_map_and_the_new_trigger(client):
    biz, _ = biz_with_sandbox()
    owner_session(client, biz)
    page = client.get("/app/workflows/new", headers={"Accept-Language": "es-MX"}).text
    assert 'id="wf-map"' in page and 'value="message.keyword"' in page and "Un mensaje con una palabra" in page
