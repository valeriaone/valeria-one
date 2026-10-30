"""mon01 (v0.16): the owner's own fields on customers and deals — typed, searchable, in CSV and the API, undo for 30 days."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import make_business
from test_web import signup
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import fields as CF


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def the_biz():
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses)).mappings().one())


@pytest.fixture
def biz():
    b, _ = make_business("MX", "salon")
    return b


def test_every_type_checks_its_values(biz):
    with db.tx() as conn:
        f = {k: CF.create(conn, biz, "test", obj="contact", label=f"F {k}", kind=k, options="Chica, Mediana, Grande" if k == "choice" else None)
             for k in CF.KINDS}
        good = {"F text": "  hola  ", "F number": "3,5", "F money": "1,500.50", "F date": "2026-12-24", "F yesno": "sí",
                "F choice": "mediana", "F phone": "999 123 4567", "F link": "www.example.com/menu"}
        c = C.create(conn, biz, "test", {"name": "Ana", "custom": good})
        v = c["custom"]
        assert v[f["text"]["key"]] == "hola" and v[f["number"]["key"]] == 3.5 and v[f["money"]["key"]] == 150050
        assert v[f["date"]["key"]] == "2026-12-24" and v[f["yesno"]["key"]] is True and v[f["choice"]["key"]] == "Mediana"
        assert v[f["phone"]["key"]] == "+529991234567" and v[f["link"]["key"]] == "https://www.example.com/menu"
        for label, bad in [("F number", "doce"), ("F money", "mucho"), ("F date", "2026-13-01"), ("F yesno", "quizás"),
                           ("F choice", "Enorme"), ("F phone", "12"), ("F link", "javascript:alert(1)")]:
            with pytest.raises(Invalid):
                C.patch(conn, biz, "test", c["id"], {"custom": {label: bad}})
        with pytest.raises(Invalid):
            C.patch(conn, biz, "test", c["id"], {"custom": {"no such field": "x"}})
        c = C.patch(conn, biz, "test", c["id"], {"custom": {"F text": ""}})          # empty = removed
        assert f["text"]["key"] not in c["custom"]


def test_wizard_rules_twenty_names_choices(biz):
    with db.tx() as conn:
        with pytest.raises(Invalid):
            CF.create(conn, biz, "test", obj="contact", label="Talla", kind="choice", options="Solo una")
        with pytest.raises(Invalid):
            CF.create(conn, biz, "test", obj="contact", label="X", kind="colour")
        CF.create(conn, biz, "test", obj="deal", label="Campo 1", kind="text")
        with pytest.raises(Invalid):
            CF.create(conn, biz, "test", obj="deal", label="CAMPO 1", kind="number")   # one name once
        CF.create(conn, biz, "test", obj="contact", label="Campo 1", kind="text")    # the same name on customers is fine
        for n in range(2, CF.MAX_FIELDS + 1):
            CF.create(conn, biz, "test", obj="deal", label=f"Campo {n}", kind="text")
        with pytest.raises(Invalid):
            CF.create(conn, biz, "test", obj="deal", label="Uno más", kind="text")


def test_delete_keeps_values_30_days_then_purges(biz):
    with db.tx() as conn:
        f = CF.create(conn, biz, "test", obj="contact", label="Cumpleaños", kind="date")
        c = C.create(conn, biz, "test", {"name": "Ana", "custom": {"Cumpleaños": "1990-05-04"}})
        CF.delete(conn, biz, "test", f["id"])
        assert CF.list_(conn, biz["id"], "contact") == [] and C.get(conn, biz["id"], c["id"])["custom"] == {f["key"]: "1990-05-04"}
        C.patch(conn, biz, "test", c["id"], {"name": "Ana López"})                   # values of a deleted field stay as they are
        CF.restore(conn, biz, "test", f["id"])
        assert [x["key"] for x in CF.list_(conn, biz["id"], "contact")] == [f["key"]]
        CF.delete(conn, biz, "test", f["id"])
    assert CF.purge_due(datetime.now(timezone.utc) + timedelta(days=29)) == 0
    assert CF.purge_due(datetime.now(timezone.utc) + timedelta(days=31)) == 1
    with db.tx() as conn:
        assert C.get(conn, biz["id"], c["id"])["custom"] == {}
        assert conn.execute(select(db.custom_fields)).first() is None


def test_lists_search_and_filter_by_fields(biz):
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="contact", label="Nivel", kind="choice", options=["Oro", "Plata"])
        CF.create(conn, biz, "test", obj="contact", label="VIP", kind="yesno")
        CF.create(conn, biz, "test", obj="deal", label="Color", kind="text")
        a = C.create(conn, biz, "test", {"name": "Ana", "custom": {"Nivel": "Oro", "VIP": "yes"}})
        C.create(conn, biz, "test", {"name": "Bea", "custom": {"Nivel": "Plata", "VIP": "no"}})
        key = CF.list_(conn, biz["id"], "contact")[0]["key"]
        assert [c["name"] for c in C.search(conn, biz["id"], field=key, value="oro")] == ["Ana"]
        assert [c["name"] for c in C.search(conn, biz["id"], field="VIP", value="yes")] == ["Ana"]
        assert [c["name"] for c in C.search(conn, biz["id"], field="VIP", value="no")] == ["Bea"]
        assert [c["name"] for c in C.search(conn, biz["id"], q="plata")] == ["Bea"]       # the search box looks in the fields too
        D.create(conn, biz, "test", {"title": "Tinte", "contact_id": a["id"], "custom": {"Color": "Caoba rojizo"}})
        assert [d["title"] for d in D.search(conn, biz["id"], field="Color", value="caoba")] == ["Tinte"]
        assert [d["title"] for d in D.search(conn, biz["id"], q="Ana")] == ["Tinte"]


def test_field_change_is_an_event(biz):
    with db.tx() as conn:
        f = CF.create(conn, biz, "test", obj="contact", label="VIP", kind="yesno")
        c = C.create(conn, biz, "test", {"name": "Ana"})
        C.patch(conn, biz, "test", c["id"], {"custom": {"VIP": "yes"}})
        C.patch(conn, biz, "test", c["id"], {"custom": {"VIP": "yes"}})                # no change: no event
        ev = conn.execute(select(db.events.c.data).where(db.events.c.type == "field.changed")).scalars().all()
    assert ev == [{"object": "contact", "key": f["key"], "value": "yes"}]


def test_settings_wizard_one_question_per_screen_and_inline_edit(client):
    signup(client)
    t = tok(client)
    assert "cf_" not in client.get("/app/settings/fields").text
    r = client.get("/app/settings/fields/new?object=contact")
    assert 'name="label"' in r.text and 'name="step" value="kind"' in r.text
    r = client.get("/app/settings/fields/new?object=contact&step=kind&label=Talla")
    assert r.text.count('name="kind" value=') == 8                                      # eight big buttons
    r = client.get("/app/settings/fields/new?object=contact&step=choices_or_list&label=Talla&kind=choice")
    assert 'name="options"' in r.text
    r = client.get("/app/settings/fields/new?object=contact&step=list&label=Talla&kind=choice&options=Chica%0AGrande")
    assert 'name="show_in_list" value="1"' in r.text and 'action="/app/settings/fields"' in r.text
    r = client.post("/app/settings/fields", data={"csrf": t, "object": "contact", "label": "Talla", "kind": "choice",
                                                  "options": "Chica\nGrande", "show_in_list": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/app/settings/fields/new?object=deal&step=list&label=Presupuesto&kind=money")
    assert 'name="step" value="card"' in r.text                                         # deals ask one more: on the board?
    client.post("/app/settings/fields", data={"csrf": t, "object": "deal", "label": "Presupuesto", "kind": "money",
                                              "show_in_list": "0", "show_on_card": "1"})
    biz = the_biz()
    with db.tx() as conn:
        c = C.create(conn, biz, "test", {"name": "Ana", "phone": "9991234567"})
        f = CF.list_(conn, biz["id"], "contact")[0]
        assert f["show_in_list"] and f["options"] == ["Chica", "Grande"]
        assert CF.list_(conn, biz["id"], "deal")[0]["show_on_card"]
    page = client.get(f"/app/contacts/{c['id']}").text
    assert f'name="cf__{f["key"]}"' in page and "data-inline" in page
    r = client.post(f"/app/contacts/{c['id']}/fields", data={"csrf": t, f"cf__{f['key']}": "grande"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["values"][f["key"]] == "Grande"
    r = client.post(f"/app/contacts/{c['id']}/fields", data={"csrf": t, f"cf__{f['key']}": "Enorme"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400 and not r.json()["ok"] and "Talla" in r.json()["error"]
    lst = client.get("/app/contacts").text
    assert "Talla: <bdi>Grande</bdi>" in lst
    assert "Ana" in client.get(f"/app/contacts?field={f['key']}&value=grande").text
    assert "Ana" not in client.get(f"/app/contacts?field={f['key']}&value=chica").text
    r = client.post(f"/app/settings/fields/{f['id']}", data={"csrf": t, "act": "delete"}, follow_redirects=False)
    assert r.status_code == 303 and "data-confirm" in client.get("/app/settings/fields").text
    assert "Deshacer" in client.get("/app/settings/fields").text or "Undo" in client.get("/app/settings/fields").text


def test_settings_are_owners_only(client):
    from test_team import invite_link, owner_signup
    owner_signup(client)
    link = invite_link(client, "staff@example.com")
    client.cookies.clear()
    t = tok(client, link)
    client.post(link, data={"csrf": t, "mode": "new", "password": "otra-clave-larga"})
    t = tok(client)
    for path in ("/app/settings/fields", "/app/settings/fields/new", "/app/recipes", "/app/forms", "/app/forms/new", "/app/contacts.csv"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app", path
    r = client.post("/app/settings/fields", data={"csrf": t, "object": "contact", "label": "X", "kind": "text"}, follow_redirects=False)
    assert r.headers["location"] == "/app"
    with db.tx() as conn:
        assert conn.execute(select(db.custom_fields)).first() is None


def test_csv_out_and_in_dedupes_and_never_imports_consent(client):
    signup(client)
    t = tok(client)
    biz = the_biz()
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="contact", label="Talla", kind="choice", options=["Chica", "Grande"])
        CF.create(conn, biz, "test", obj="deal", label="Presupuesto", kind="money")
        a = C.create(conn, biz, "test", {"name": "Ana", "phone": "9991234567", "custom": {"Talla": "Chica"}, "tags": ["vip"]})
        D.create(conn, biz, "test", {"title": "Tinte", "contact_id": a["id"], "custom": {"Presupuesto": "1200"}})
    out = client.get("/app/contacts.csv")
    assert out.status_code == 200 and out.headers["content-type"].startswith("text/csv")
    assert "Talla" in out.text.splitlines()[0] and "Ana,+529991234567" in out.text and "Chica" in out.text
    deals = client.get("/app/deals.csv").text
    assert "Presupuesto" in deals.splitlines()[0] and ",1200\r" in deals
    csv = "name,phone,email,tags,marketing_consent,Talla\nAna López,999 123 4567,,nuevo,yes,Grande\nBea,,bea@example.com,,yes,Grande\nMal,12,,,,\n"
    r = client.post("/app/contacts/import", data={"csrf": t}, files={"file": ("c.csv", csv.encode(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        rows = {c["name"]: c for c in C.search(conn, biz["id"], limit=50)}
        key = CF.list_(conn, biz["id"], "contact")[0]["key"]
    assert set(rows) == {"Ana", "Bea"}                                               # Ana updated (same phone), not doubled; "Mal" skipped
    assert rows["Ana"]["tags"] == ["nuevo", "vip"] and rows["Ana"]["custom"][key] == "Chica"   # her own answer stays
    assert rows["Bea"]["custom"][key] == "Grande" and rows["Bea"]["source"] == "import"
    assert not rows["Bea"]["marketing_consent"]                                    # a file can't prove consent
    assert "1" in client.get("/app/contacts").text


def test_api_reads_and_writes_custom(client, mx):
    biz, h = mx
    with db.tx() as conn:
        f = CF.create(conn, biz, "test", obj="contact", label="Nivel", kind="choice", options=["Oro", "Plata"])
        CF.create(conn, biz, "test", obj="deal", label="Metros", kind="number")
    fl = client.get("/v1/fields", headers=h).json()
    assert {x["label"] for x in fl} == {"Nivel", "Metros"} and client.get("/v1/fields?object=deal", headers=h).json()[0]["kind"] == "number"
    r = client.post("/v1/contacts", headers=h, json={"name": "Ana", "custom": {"Nivel": "oro"}})
    assert r.status_code == 201 and r.json()["custom"] == {f["key"]: "Oro"}
    assert client.post("/v1/contacts", headers=h, json={"name": "Bea", "custom": {"Nivel": "Bronce"}}).status_code == 422
    cid = r.json()["id"]
    assert client.patch(f"/v1/contacts/{cid}", headers=h, json={"custom": {f["key"]: "Plata"}}).json()["custom"][f["key"]] == "Plata"
    assert [c["name"] for c in client.get(f"/v1/contacts?field=Nivel&value=plata", headers=h).json()] == ["Ana"]
    d = client.post("/v1/deals", headers=h, json={"title": "Piso", "custom": {"Metros": "12.5"}}).json()
    assert list(d["custom"].values()) == [12.5]
    assert client.get("/v1/deals?field=Metros&value=12.5", headers=h).json()[0]["id"] == d["id"]


def test_valeria_in_the_office_reads_the_fields(biz):
    from valeria_one.services import copilot as CP
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="contact", label="Alergias", kind="text")
        CF.create(conn, biz, "test", obj="deal", label="Presupuesto", kind="money")
        c = C.create(conn, biz, "test", {"name": "Ana", "custom": {"Alergias": "Amoníaco"}})
        D.create(conn, biz, "test", {"title": "Tinte", "contact_id": c["id"], "custom": {"Presupuesto": "800"}})
    got = CP.run_read(biz, "client", {"id": str(c["id"])})
    assert got["fields"] == {"Alergias": "Amoníaco"} and got["deals"][0]["fields"]["Presupuesto"].startswith("$")


def test_export_includes_the_new_tables(client):
    signup(client)
    biz = the_biz()
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="contact", label="Talla", kind="text")
    data = client.get("/app/export").json()
    for k in ("custom_fields", "recipes", "recipe_runs", "forms", "form_submissions", "tasks"):
        assert k in data, k
    assert data["custom_fields"][0]["label"] == "Talla"
