"""mon01 (v0.16): the pipeline board (it existed as columns with a dropdown; now cards drag, move by keyboard or
"Move to…", show the next step and the owner's fields) and the same board for tasks: To do / Doing / Done."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from test_web import signup
from valeria_one import db, packs
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import fields as CF
from valeria_one.services import projects as PR
from valeria_one.services import recipes as RC

STATIC = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "static"
FETCH = {"X-Requested-With": "fetch"}


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def the_biz():
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses)).mappings().one())


def test_pipeline_board_cards_next_step_and_fields(client):
    signup(client)
    biz = the_biz()
    stages = packs.industry(biz["industry"]).stages
    with db.tx() as conn:
        CF.create(conn, biz, "test", obj="deal", label="Presupuesto", kind="money", show_on_card=True)
        CF.create(conn, biz, "test", obj="deal", label="Color", kind="text", show_in_list=True)
        c = C.create(conn, biz, "test", {"name": "Ana López"})
        d = D.create(conn, biz, "test", {"title": "Paquete novia", "contact_id": c["id"], "amount_minor": 250000,
                                         "custom": {"Presupuesto": "3000", "Color": "Caoba"}})
        PR.add_task(conn, biz, "test", {"title": "Llamar para la prueba", "deal_id": d["id"], "contact_id": c["id"]})
    t = client.get("/app/pipeline").text
    assert t.count('class="kb-col') == len(stages) and f'data-card="{d["id"]}"' in t and 'tabindex="0"' in t
    assert "Paquete novia" in t and "Ana López" in t and "Llamar para la prueba" in t           # the next step on the card
    assert "Presupuesto:</span> <bdi>$3,000.00" in t and "Color:</span>" not in t.split('data-view-pane="list"')[0]
    assert "/static/kanban.js" in t and "data-kb-live" in t and 'aria-live="polite"' in t
    assert t.count("data-move-to") == len(stages) and 'name="stage" value="' in t                # "Move to…" works without JS
    assert 'class="board"' not in t                                                    # no clash with the mood board's .board
    lst = client.get("/app/pipeline?view=list").text
    assert '<div class="card" data-view-pane="list" >' in lst or 'data-view-pane="list" >' in lst
    assert "<th>Color</th>" in lst and "<bdi>Caoba</bdi>" in lst
    assert "Paquete novia" in client.get("/app/pipeline?field=Color&value=caoba").text
    assert "Paquete novia" not in client.get("/app/pipeline?field=Color&value=azul").text
    page = client.get(f"/app/deals/{d['id']}").text
    assert "Paquete novia" in page and "Llamar para la prueba" in page and 'name="cf__' in page


def test_moving_a_card_fires_the_stage_recipes(client):
    signup(client)
    biz = the_biz()
    opens = [s.key for s in packs.industry(biz["industry"]).stages if s.outcome == "open"]
    t = tok(client)
    with db.tx() as conn:
        RC.save(conn, biz, "user:x", {"trigger": "deal.stage", "trigger_arg": {"stage": opens[1]},
                                      "actions": [{"do": "task", "title": "Confirmar con {name}", "hours": 4}]})
        c = C.create(conn, biz, "test", {"name": "Ana López"})
        d = D.create(conn, biz, "test", {"title": "Paquete", "contact_id": c["id"]})
    r = client.post(f"/app/deals/{d['id']}/stage", data={"csrf": t, "stage": opens[1]}, headers=FETCH)
    assert r.status_code == 200 and r.json() == {"ok": True, "stage": opens[1]}
    with db.tx() as conn:
        assert D.get(conn, biz["id"], d["id"])["stage"] == opens[1]
        assert [x["title"] for x in PR.tasks(conn, biz["id"])] == ["Confirmar con Ana"]
    r = client.post(f"/app/deals/{d['id']}/stage", data={"csrf": t, "stage": "nope"}, headers=FETCH)
    assert r.status_code == 400 and r.json()["ok"] is False
    r = client.post(f"/app/deals/{d['id']}/stage", data={"csrf": t, "stage": opens[0], "back": "/app/pipeline"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/pipeline"           # "Move to…" without JavaScript
    r = client.post(f"/app/deals/{d['id']}/stage", data={"csrf": t, "stage": opens[0], "back": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/app/pipeline"


def test_task_board_to_do_doing_done(client):
    signup(client)
    biz = the_biz()
    t = tok(client)
    with db.tx() as conn:
        x = PR.add_task(conn, biz, "test", {"title": "Comprar tinte"})
        PR.add_task(conn, biz, "test", {"title": "Limpiar"})
    page = client.get("/app/tasks?view=board").text
    assert page.count('class="kb-col') == 3 and 'data-move-url="/app/tasks/{id}"' in page and 'data-field="status"' in page
    r = client.post(f"/app/tasks/{x['id']}", data={"csrf": t, "status": "doing"}, headers=FETCH)
    assert r.json() == {"ok": True, "status": "doing"}
    with db.tx() as conn:
        row = conn.execute(select(db.tasks).where(db.tasks.c.id == x["id"])).mappings().one()
        assert row["started_at"] and not row["done_at"] and PR.task_status(dict(row)) == "doing"
    client.post(f"/app/tasks/{x['id']}", data={"csrf": t, "status": "done"}, headers=FETCH)
    board = client.get("/app/tasks?view=board").text
    done_col = board.split('data-col="done"')[1]
    assert "Comprar tinte" in done_col                                                  # Done shows the last two weeks
    assert "Comprar tinte" not in client.get("/app/tasks").text.split('data-view-pane="board"')[0]   # the list: open ones only
    client.post(f"/app/tasks/{x['id']}", data={"csrf": t, "status": "todo"}, headers=FETCH)
    with db.tx() as conn:
        row = conn.execute(select(db.tasks).where(db.tasks.c.id == x["id"])).mappings().one()
        assert row["started_at"] is None and row["done_at"] is None
        kinds = conn.execute(select(db.events.c.type).where(db.events.c.subject_id == x["id"]).order_by(db.events.c.id)).scalars().all()
    assert kinds == ["task.added", "task.started", "task.done", "task.reopened"]
    assert client.post(f"/app/tasks/{x['id']}", data={"csrf": t, "status": "later"}, headers=FETCH).status_code == 400


def test_tasks_are_for_every_business_now(client):
    """Recipes add "reply to Ana" tasks, so Tasks no longer needs the projects block."""
    from valeria_one import blocks
    signup(client, industry="generic", email="g@example.com")
    t = tok(client)
    client.post("/app/start/blocks", data={"csrf": t, "block": [b for b in blocks.BLOCKS if b not in ("projects", "studio")]})
    nav = client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
    assert 'href="/app/tasks"' in nav and 'href="/app/projects"' not in nav
    assert client.get("/app/tasks").status_code == 200
    with db.tx() as conn:
        c = C.create(conn, the_biz(), "test", {"name": "Ana"})
    r = client.post("/app/tasks", data={"csrf": t, "title": "Mandar fotos", "contact_id": str(c["id"]), "back": f"/app/contacts/{c['id']}#tasks"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/app/contacts/{c['id']}#tasks"
    assert "Mandar fotos" in client.get(f"/app/contacts/{c['id']}").text


def test_the_board_script_is_light_plain_and_keyboard_ready():
    js = (STATIC / "kanban.js").read_text()
    assert len(js.encode()) < 12_000 and "import " not in js and "{{" not in js
    for must in ("pointerdown", "ArrowLeft", "ArrowRight", "localStorage", "X-Requested-With", "aria-pressed", "data-move-to", "dragstart"):
        assert must in js, must
    assert "[data-view-pane][hidden]" in (STATIC / "app.css").read_text()       # a board's display:grid must not beat hidden
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(STATIC / "kanban.js")]).returncode == 0
        assert subprocess.run(["node", "--check", str(STATIC / "app.js")]).returncode == 0


@pytest.mark.parametrize("path", ["/app/pipeline", "/app/tasks"])
def test_board_pages_wear_the_valeria_one_look(client, path):
    signup(client)
    t = client.get(path).text
    assert "/static/vo.css" in t and '<body class="vo">' in t and 'data-view-toggle' in t
