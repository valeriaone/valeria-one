"""v0.18 on screen: workflows — the gallery, the builder (static/flow.js: a vertical list of steps with "+ add step",
an "if" shows its two branches side by side, stacked on a phone; no dragging), and what happened to whom."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request

from .. import db
from ..services import Invalid, NotFound
from ..services import workflows as WF
from . import _ctx, _flash, _form, _go, _guard, _lang, _need, _own, render

router = APIRouter(include_in_schema=False)


def _owner(request: Request) -> dict:
    ctx = _own(request)
    _need(request, ctx, "followups")
    return ctx


def _uuid(v) -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound("workflow") from None


def _words(rows: list[dict], lang: str) -> None:
    for r in rows:
        r["words"] = [WF.line_text(x, lang) for x in (r.get("history") or [])]


@router.get("/app/workflows")
@_guard
async def workflows_page(request: Request):
    ctx = _owner(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        mine = WF.list_(conn, biz["id"])
        for w in mine:
            w["when"] = WF.describe_trigger(conn, biz, w, lang)
            w["n_steps"] = len(WF.flat(w["steps"]))
        gallery = WF.templates(conn, biz)
        have = {w["template"] for w in mine if w["template"]}
        for g in gallery:
            g["have"] = g["key"] in have
            g["when"] = WF.describe_trigger(conn, biz, g, lang) if not g["needs"] else ""
        runs = WF.runs(conn, biz["id"], limit=30)
    _words(runs, lang)
    return render(request, "workflows.html", ctx, mine=mine, gallery=gallery, runs=runs, page="workflows")


@router.post("/app/workflows/template/{key}")
@_guard
async def workflow_from_template(request: Request, key: str):
    ctx = _owner(request)
    await _form(request)
    with db.tx() as conn:
        wf = WF.from_template(conn, ctx["biz"], ctx["actor"], key)
    return _go(f"/app/workflows/{wf['id']}")


@router.post("/app/recipes/{recipe_id}/workflow")
@_guard
async def recipe_to_workflow(request: Request, recipe_id: UUID):
    """"Open this recipe as a workflow": made from it; the recipe is switched off so the two never both run."""
    ctx = _owner(request)
    await _form(request)
    with db.tx() as conn:
        wf = WF.from_recipe(conn, ctx["biz"], ctx["actor"], recipe_id)
    _flash(request, "wf_from_recipe_done")
    return _go(f"/app/workflows/{wf['id']}")


def _editor(conn, biz: dict, lang: str) -> dict:
    data = WF.editor_data(conn, biz, lang)
    data["types"] = list(WF.STEPS)
    data["conds"] = list(WF.CONDS)
    data["units"] = list(WF.UNITS)
    return data


@router.get("/app/workflows/{wid}")
@_guard
async def workflow_page(request: Request, wid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    from ..services import fields as CF
    from ..services import forms as FM
    with db.tx() as conn:
        wf = None if wid == "new" else WF.get(conn, biz["id"], _uuid(wid))
        runs = WF.runs(conn, biz["id"], workflow_id=wf["id"], limit=40) if wf else []
        editor = _editor(conn, biz, lang)
        forms = FM.list_(conn, biz["id"])
        fields = CF.list_(conn, biz["id"], "contact")
        when = WF.describe_trigger(conn, biz, wf, lang) if wf else ""
    _words(runs, lang)
    from .. import packs
    return render(request, "workflow.html", ctx, wf=wf, runs=runs, when=when, triggers=WF.TRIGGERS, goals=WF.GOALS,
                  steps_json=json.dumps((wf or {}).get("steps") or [], ensure_ascii=False),
                  editor_json=json.dumps(editor, ensure_ascii=False).replace("<", "\\u003c"), forms=forms, fields=fields,
                  date_fields=[f for f in fields if f["kind"] == "date"], stages=packs.industry(biz["industry"]).stages,
                  page="workflows")


@router.post("/app/workflows/{wid}")
@_guard
async def workflow_save(request: Request, wid: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    f = await _form(request)
    act = f.get("act") or "save"
    with db.tx() as conn:
        if act in ("on", "off"):
            WF.set_active(conn, biz, ctx["actor"], _uuid(wid), act == "on")
            return _go(f"/app/workflows/{wid}")
        if act == "delete":
            WF.delete(conn, biz, ctx["actor"], _uuid(wid))
            return _go("/app/workflows")
        try:
            steps = json.loads(f.get("steps") or "[]")
        except ValueError:
            raise Invalid("the steps could not be read; reload the page", "steps") from None
        trig = f.get("trigger") or ""
        arg = {k: f.get(f"ta_{k}") for k in ("stage", "hours", "form_id", "key", "value", "tag", "days", "offset") if f.get(f"ta_{k}")}
        data = {"name": f.get("name"), "trigger": trig, "trigger_arg": arg, "steps": steps, "goal": f.get("goal") or None,
                "active": f.get("active") == "1"}
        wf = WF.save(conn, biz, ctx["actor"], data, None if wid == "new" else _uuid(wid))
        if wid != "new" and f.get("active") == "1" and not wf["active"]:
            wf = WF.set_active(conn, biz, ctx["actor"], wf["id"], True)
    _flash(request, "saved")
    return _go(f"/app/workflows/{wf['id']}")


@router.post("/app/contacts/{contact_id}/workflow")
@_guard
async def contact_workflow(request: Request, contact_id: UUID):
    """Start a workflow for one customer by hand (from their page)."""
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        st = WF.start_by_hand(conn, ctx["biz"], ctx["actor"], _uuid(f.get("workflow_id")), contact_id)
    _flash(request, "wf_started" if st else "wf_not_started", "ok" if st else "warn")
    return _go(f"/app/contacts/{contact_id}#workflows")

