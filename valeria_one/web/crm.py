"""v0.16 on screen: the owner's own fields (Settings), the pipeline board and the deal page, "when this, do that"
recipes, and forms (the owner's editor and the public page /form/<slug>).

The board works without JavaScript (each card has "Move to…"); with it (static/kanban.js) cards drag between
columns with a mouse or a finger, and move with the arrow keys. Moving a card is the same deals.patch as everywhere,
so the stage-change recipes and follow-ups fire the same way.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import blocks, config, db, money
from ..services import Invalid, NotFound
from ..services import contacts as C
from ..services import deals as D
from ..services import fields as CF
from ..services import forms as FM
from ..services import recipes as RC
from . import _ctx, _flash, _form, _go, _guard, _lang, _limit, _multi_form, _need, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
_form_hits: dict = {}
KIND_ICON = {"text": "Aa", "number": "123", "money": "$", "date": "31", "yesno": "✓✗", "choice": "☰", "phone": "☎", "link": "www"}


def _fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def _uuid(v) -> UUID | None:
    try:
        return UUID(str(v)) if v not in (None, "") else None
    except ValueError:
        raise Invalid("something on the form is out of date; reload the page", "id") from None


def _back(f: dict, default: str) -> str:
    b = str(f.get("back") or "")
    return b if b.startswith("/app/") else default


def _people(conn, business_id) -> list[dict]:
    from ..services import team
    return team.members(conn, business_id)


# ---------------------------------------------------------------- Settings › your own fields
@router.get("/app/settings/fields")
@_guard
async def fields_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        have = CF.list_(conn, biz["id"])
        gone = CF.list_(conn, biz["id"], deleted=True)
    now = datetime.now(timezone.utc)
    for g in gone:
        g["days_left"] = max(0, CF.KEEP_DAYS - (now - g["deleted_at"]).days)
    return render(request, "fields.html", ctx, page="settings", have=have, gone=gone, kinds=CF.KINDS, icons=KIND_ICON,
                  max_fields=CF.MAX_FIELDS)


@router.get("/app/settings/fields/new")
@_guard
async def field_wizard(request: Request):
    """One question per screen: its name → what kind of answer → (the choices) → in the list? → (on the board's cards?)."""
    ctx = _own(request)
    q = request.query_params
    obj = q.get("object") if q.get("object") in CF.OBJECTS else "contact"
    label, kind, options = (q.get("label") or "").strip()[:60], q.get("kind") or "", (q.get("options") or "").strip()[:1200]
    step = q.get("step") or "name"
    if step == "choices_or_list":
        step = "choices" if kind == "choice" else "list"
    if step != "name" and not label:
        step = "name"
    if step in ("choices", "list", "card") and kind not in CF.KINDS:
        step = "kind"
    if step == "list" and kind == "choice" and len([o for o in options.replace(",", "\n").splitlines() if o.strip()]) < 2:
        step = "choices"
        if q.get("options") is not None:
            _flash(request, "cf_need_two", "warn")
    with db.tx() as conn:
        n = len(CF.list_(conn, ctx["biz"]["id"], obj))
    return render(request, "field_new.html", ctx, page="settings", obj=obj, flabel=label, kind=kind, options=options, step=step,
                  kinds=CF.KINDS, icons=KIND_ICON, full=n >= CF.MAX_FIELDS, show_list=q.get("show_in_list") or "")


@router.post("/app/settings/fields")
@_guard
async def field_create(request: Request):
    ctx = _own(request)
    f = await _form(request)
    obj = f.get("object") if f.get("object") in CF.OBJECTS else "contact"
    with db.tx() as conn:
        CF.create(conn, ctx["biz"], ctx["actor"], obj=obj, label=f.get("label", ""), kind=f.get("kind", ""),
                  options=f.get("options") or "", show_in_list=f.get("show_in_list") == "1", show_on_card=f.get("show_on_card") == "1")
    _flash(request, "cf_added")
    return _go(f"/app/settings/fields#{obj}")


@router.post("/app/settings/fields/{field_id}")
@_guard
async def field_change(request: Request, field_id: UUID):
    ctx = _own(request)
    f = await _form(request)
    act = f.get("act")
    with db.tx() as conn:
        cur = CF.get(conn, ctx["biz"]["id"], field_id)
        if act == "delete":
            CF.delete(conn, ctx["biz"], ctx["actor"], field_id)
            _flash(request, "cf_deleted")
        elif act == "restore":
            CF.restore(conn, ctx["biz"], ctx["actor"], field_id)
            _flash(request, "cf_restored")
        elif act == "up":
            CF.patch(conn, ctx["biz"], ctx["actor"], field_id, up=True)
        elif act in ("list", "card"):
            k = "show_in_list" if act == "list" else "show_on_card"
            CF.patch(conn, ctx["biz"], ctx["actor"], field_id, **{k: not cur[k]})
        else:
            CF.patch(conn, ctx["biz"], ctx["actor"], field_id, label=f.get("label"),
                     options=f.get("options") if cur["kind"] == "choice" else None)
            _flash(request, "saved")
    return _go(f"/app/settings/fields#{cur['object']}")


async def _save_values(request: Request, obj: str, row_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    given = {k[4:]: v for k, v in f.items() if k.startswith("cf__")}
    try:
        with db.tx() as conn:
            if obj == "contact":
                row = C.patch(conn, ctx["biz"], ctx["actor"], row_id, {"custom": given})
            else:
                row = D.patch(conn, ctx["biz"], ctx["actor"], row_id, {"custom": given})
            fl = {x["key"]: x for x in CF.list_(conn, ctx["biz"]["id"], obj)}
    except Invalid as e:
        if _fetch(request):
            return JSONResponse({"ok": False, "error": str(e), "field": e.field}, status_code=400)
        raise
    if _fetch(request):
        lang = _lang(request, ctx["biz"])
        return JSONResponse({"ok": True, "saved": t("saved", lang),
                             "values": {k: CF.form_value(ctx["biz"], fl[k], v) for k, v in (row["custom"] or {}).items() if k in fl}})
    _flash(request, "saved")
    return _go(_back(f, f"/app/{'contacts' if obj == 'contact' else 'deals'}/{row_id}"))


@router.post("/app/contacts/{contact_id}/fields")
@_guard
async def contact_fields(request: Request, contact_id: UUID):
    return await _save_values(request, "contact", contact_id)


@router.post("/app/deals/{deal_id}/fields")
@_guard
async def deal_fields(request: Request, deal_id: UUID):
    return await _save_values(request, "deal", deal_id)


def field_rows(biz: dict, fl: list[dict], values: dict | None, lang: str) -> list[dict]:
    """The fields of one customer or deal, ready for the page: the input's value and how it reads."""
    v = values or {}
    return [{**f, "value": CF.form_value(biz, f, v.get(f["key"])), "shown": CF.display(biz, f, v.get(f["key"]), lang)} for f in fl]


# ---------------------------------------------------------------- CSV: customers and deals out, customers in
@router.get("/app/contacts.csv")
@_guard
async def contacts_csv(request: Request):
    from ..services import csvio
    ctx = _own(request)
    with db.tx() as conn:
        body = csvio.contacts_csv(conn, ctx["biz"], ctx["actor"])
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="customers.csv"', "Cache-Control": "no-store"})


@router.get("/app/deals.csv")
@_guard
async def deals_csv(request: Request):
    from ..services import csvio
    ctx = _own(request)
    with db.tx() as conn:
        body = csvio.deals_csv(conn, ctx["biz"], ctx["actor"])
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="deals.csv"', "Cache-Control": "no-store"})


@router.post("/app/contacts/import")
@_guard
async def contacts_import(request: Request):
    from ..services import csvio
    ctx = _own(request)
    f = await _multi_form(request)
    up = f.get("file")
    raw = await up.read() if hasattr(up, "read") else b""
    if not raw:
        raise Invalid(t("cf_pick_file", _lang(request, ctx["biz"])), "file")
    with db.tx() as conn:
        st = csvio.import_contacts(conn, ctx["biz"], f"import:{ctx['actor']}"[:100], raw)
    lang = _lang(request, ctx["biz"])
    msg = t("cf_imported", lang).format(added=st["added"], updated=st["updated"], skipped=len(st["skipped"]))
    if st["skipped"]:
        msg += " · " + ", ".join(f"#{n}: {why}" for n, why in st["skipped"][:5])
    _flash(request, "cf_imported", "ok" if not st["skipped"] else "warn", raw=msg)
    return _go("/app/contacts")


# ---------------------------------------------------------------- the pipeline: board and list
def _next_steps(conn, biz: dict, deals: list[dict], lang: str) -> dict:
    """For each deal: the first open task about it (or its customer), else the customer's next appointment."""
    from ..services import projects as PR
    tasks = PR.tasks(conn, biz["id"])
    by_deal, by_contact = {}, {}
    for x in sorted(tasks, key=lambda x: (x["due_at"] or datetime.max.replace(tzinfo=timezone.utc),
                                          x["due_on"] or datetime.max.date())):
        if x["deal_id"]:
            by_deal.setdefault(x["deal_id"], x)
        if x["contact_id"]:
            by_contact.setdefault(x["contact_id"], x)
    now = datetime.now(timezone.utc)
    appts = {}
    for a in D.appointments(conn, biz["id"], start=now, end=now + timedelta(days=60), limit=500):
        if a["contact_id"] and a["status"] in ("booked", "confirmed"):
            appts.setdefault(a["contact_id"], a)
    out = {}
    for d in deals:
        x = by_deal.get(d["id"]) or (by_contact.get(d["contact_id"]) if d["contact_id"] else None)
        if x:
            out[d["id"]] = {"kind": "task", "title": x["title"], "at": x["due_at"], "on": x["due_on"]}
        elif d["contact_id"] and d["contact_id"] in appts:
            a = appts[d["contact_id"]]
            out[d["id"]] = {"kind": "appt", "title": a["title"], "at": a["starts_at"], "on": None}
    return out


@router.get("/app/pipeline")
@_guard
async def pipeline_page(request: Request, q: str | None = None, field: str | None = None, value: str | None = None):
    ctx = _ctx(request)
    _need(request, ctx, "pipeline")
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        pipe = D.pipeline(conn, biz, lang)
        deals = D.search(conn, biz["id"], limit=200, q=q, field=field or None, value=value)
        names = {r["id"]: r["name"] for r in C.search(conn, biz["id"], limit=200)}
        fl = CF.list_(conn, biz["id"], "deal")
        nxt = _next_steps(conn, biz, deals, lang)
    by_stage: dict[str, list] = {s["key"]: [] for s in pipe}
    for d in deals:
        d["fields"] = [r for r in field_rows(biz, fl, d["custom"], lang) if r["shown"]]
        by_stage.setdefault(d["stage"], []).append(d)
    filtered = bool(q or field)
    for s in pipe:                                            # with a filter on, a column counts what it shows
        if filtered:
            s["count"] = len(by_stage.get(s["key"], []))
            s["amount_minor"] = sum(d["amount_minor"] or 0 for d in by_stage.get(s["key"], []) if d["currency"] == biz["currency"])
    return render(request, "pipeline.html", ctx, pipe=pipe, by_stage=by_stage, names=names, deals=deals, nxt=nxt,
                  contacts=sorted(names.items(), key=lambda x: (x[1] or "").lower()), page="pipeline", fl=fl,
                  card_fields=[f for f in fl if f["show_on_card"]], list_fields=[f for f in fl if f["show_in_list"]],
                  q=q or "", field=field or "", value=value or "", filtered=filtered,
                  stage_label={s["key"]: s["label"] for s in pipe})


@router.post("/app/deals/{deal_id}/stage")
@_guard
async def deal_stage(request: Request, deal_id: UUID):
    """Move a deal (the board's drag, its arrow keys, or "Move to…"): the same change as anywhere, so recipes fire."""
    ctx = _ctx(request)
    f = await _form(request)
    try:
        with db.tx() as conn:
            d = D.patch(conn, ctx["biz"], ctx["actor"], deal_id, {"stage": f.get("stage")})
    except (Invalid, NotFound) as e:
        if _fetch(request):
            return JSONResponse({"ok": False, "error": str(e) or "not found"}, status_code=400)
        raise
    if _fetch(request):
        return JSONResponse({"ok": True, "stage": d["stage"]})
    return _go(_back(f, "/app/pipeline"))


@router.get("/app/deals/{deal_id}")
@_guard
async def deal_page(request: Request, deal_id: UUID):
    ctx = _ctx(request)
    _need(request, ctx, "pipeline")
    biz = ctx["biz"]
    lang = _lang(request, biz)
    from ..services import projects as PR
    with db.tx() as conn:
        d = D.get(conn, biz["id"], deal_id)
        contact = None
        if d["contact_id"]:
            try:
                contact = C.get(conn, biz["id"], d["contact_id"])
            except NotFound:
                contact = None
        rows = field_rows(biz, CF.list_(conn, biz["id"], "deal"), d["custom"], lang)
        pipe = D.pipeline(conn, biz, lang)
        tasks = PR.tasks(conn, biz["id"], deal_id=deal_id)
        names = sorted(((r["id"], r["name"]) for r in C.search(conn, biz["id"], limit=200)), key=lambda x: (x[1] or "").lower())
    return render(request, "deal.html", ctx, d=d, contact=contact, rows=rows, pipe=pipe, tasks=tasks, contacts=names, page="pipeline")


@router.post("/app/deals/{deal_id}")
@_guard
async def deal_save(request: Request, deal_id: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    data: dict = {"title": f.get("title", ""), "notes": f.get("notes", ""), "contact_id": _uuid(f.get("contact_id"))}
    amt = (f.get("amount") or "").replace(",", "").strip()
    if amt:
        try:
            data["amount_minor"] = money.to_minor(amt, biz["currency"])
        except Exception:
            raise Invalid("amount: use digits, e.g. 1500.00", "amount") from None
    else:
        data["amount_minor"] = None
    with db.tx() as conn:
        D.patch(conn, biz, ctx["actor"], deal_id, data)
    _flash(request, "saved")
    return _go(f"/app/deals/{deal_id}")


# ---------------------------------------------------------------- recipes: "when this, do that"
def _recipe_page_ctx(request: Request) -> dict:
    ctx = _own(request)
    _need(request, ctx, "followups")
    return ctx


@router.get("/app/recipes")
@_guard
async def recipes_page(request: Request):
    ctx = _recipe_page_ctx(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        gallery = RC.presets(conn, biz, lang, ctx["user"]["id"] if ctx["user"] else None)
        mine = RC.list_(conn, biz["id"])
        for r in mine:
            r["sentence"] = RC.describe(conn, biz, r, lang)
        runs = RC.runs(conn, biz["id"], limit=30)
    for x in runs:
        x["words"] = [RC.line_text(line, lang) for line in x["lines"] or []]
    return render(request, "recipes.html", ctx, page="recipes", gallery=gallery, mine=mine, runs=runs)


@router.post("/app/recipes/on/{key}")
@_guard
async def recipe_on(request: Request, key: str):
    ctx = _recipe_page_ctx(request)
    await _form(request)
    with db.tx() as conn:
        RC.switch_on(conn, ctx["biz"], ctx["actor"], key, ctx["user"]["id"] if ctx["user"] else None)
    _flash(request, "rc_switched_on")
    return _go("/app/recipes#mine")


def _builder(request: Request, ctx: dict, r: dict, recipe_id: UUID | None):
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        fl = CF.list_(conn, biz["id"])
        forms = FM.list_(conn, biz["id"])
        people = _people(conn, biz["id"])
        pipe = D.pipeline(conn, biz, lang)
        sentence = RC.describe(conn, biz, r, lang) if r.get("actions") else ""
        from ..services import sequences as SQ
        seqs = SQ.list_(conn, biz["id"])                     # v0.18: "add to sequence"
    acts = list(r.get("actions") or [])
    acts += [{}] * (RC.MAX_ACTIONS - len(acts))
    return render(request, "recipe_edit.html", ctx, page="recipes", r=r, acts=acts, recipe_id=recipe_id, fl=fl, forms=forms,
                  people=people, pipe=pipe, triggers=RC.TRIGGERS, actions=RC.ACTIONS, conditions=RC.CONDITIONS, sentence=sentence,
                  seqs=seqs)


@router.get("/app/recipes/new")
@_guard
async def recipe_new(request: Request):
    ctx = _recipe_page_ctx(request)
    key = request.query_params.get("preset")
    r = {"trigger": "lead.new", "trigger_arg": {}, "condition": {}, "actions": [], "ask_first": True, "name": ""}
    if key:
        with db.tx() as conn:
            p = next((x for x in RC.presets(conn, ctx["biz"], ctx["biz"]["language"], ctx["user"]["id"] if ctx["user"] else None)
                      if x["key"] == key), None)
        if p:
            r = {**r, **{k: p[k] for k in ("trigger", "trigger_arg", "condition", "actions", "ask_first")}, "preset": key}
    return _builder(request, ctx, r, None)


@router.get("/app/recipes/{recipe_id}")
@_guard
async def recipe_edit(request: Request, recipe_id: UUID):
    ctx = _recipe_page_ctx(request)
    with db.tx() as conn:
        r = RC.get(conn, ctx["biz"]["id"], recipe_id)
    return _builder(request, ctx, r, recipe_id)


def _recipe_data(f: dict) -> dict:
    trig = f.get("trigger") or ""
    arg = {"stage": f.get("t_stage"), "hours": f.get("t_hours"), "form_id": f.get("t_form"), "key": f.get("t_field"),
           "value": f.get("t_value")}
    cond = {"kind": f.get("c_kind") or "", "value": f.get("c_value") or "", "key": f.get("c_field")}
    acts = []
    for i in range(RC.MAX_ACTIONS):
        do = f.get(f"a{i}_do") or ""
        if not do:
            continue
        acts.append({"do": do, "tag": f.get(f"a{i}_tag"), "user_id": f.get(f"a{i}_user"), "title": f.get(f"a{i}_title"),
                     "hours": f.get(f"a{i}_hours"), "stage": f.get(f"a{i}_stage"), "text": f.get(f"a{i}_text"),
                     "sequence_id": f.get(f"a{i}_sequence")})
    return {"trigger": trig, "trigger_arg": arg, "condition": cond, "actions": acts, "ask_first": f.get("ask_first") == "1",
            "name": f.get("name") or ""}


@router.post("/app/recipes")
@_guard
async def recipe_create(request: Request):
    ctx = _recipe_page_ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        RC.save(conn, ctx["biz"], ctx["actor"], _recipe_data(f), preset=(f.get("preset") or None))
    _flash(request, "rc_saved")
    return _go("/app/recipes#mine")


@router.post("/app/recipes/{recipe_id}")
@_guard
async def recipe_save(request: Request, recipe_id: UUID):
    ctx = _recipe_page_ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        if f.get("act") == "delete":
            RC.delete(conn, ctx["biz"], ctx["actor"], recipe_id)
            _flash(request, "rc_deleted")
        elif f.get("act") in ("on", "off"):
            RC.set_active(conn, ctx["biz"], ctx["actor"], recipe_id, f["act"] == "on")
            _flash(request, "rc_switched_on" if f["act"] == "on" else "rc_switched_off")
        else:
            RC.save(conn, ctx["biz"], ctx["actor"], _recipe_data(f), recipe_id=recipe_id)
            _flash(request, "rc_saved")
    return _go("/app/recipes#mine")


# ---------------------------------------------------------------- forms: the owner's editor
@router.get("/app/forms")
@_guard
async def forms_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        rows = FM.list_(conn, ctx["biz"]["id"])
    for r in rows:
        r["url"] = FM.url(r)
    return render(request, "forms.html", ctx, page="forms", rows=rows)


def _form_editor(request: Request, ctx: dict, fm: dict, form_id: UUID | None):
    biz = ctx["biz"]
    lang = _lang(request, biz)
    with db.tx() as conn:
        custom = CF.list_(conn, biz["id"], "contact")
        pipe = D.pipeline(conn, biz, lang)
        subs = FM.submissions(conn, biz["id"], form_id) if form_id else []
    picked = {x["key"]: x for x in fm.get("fields") or []}
    choices = [{"key": k, "label": t("fm_" + k, lang), "on": k in picked, "required": picked.get(k, {}).get("required", k == "name")}
               for k in FM.BASICS]
    choices += [{"key": "cf:" + f["key"], "label": f["label"], "on": "cf:" + f["key"] in picked,
                 "required": picked.get("cf:" + f["key"], {}).get("required", False)} for f in custom]
    return render(request, "form_edit.html", ctx, page="forms", fm=fm, form_id=form_id, choices=choices, pipe=pipe, subs=subs,
                  url=FM.url(fm) if form_id else "", embed=FM.embed(fm) if form_id else "", pipeline_on=blocks.on(biz, "pipeline"))


@router.get("/app/forms/new")
@_guard
async def form_new(request: Request):
    ctx = _own(request)
    lang = _lang(request, ctx["biz"])
    fm = {"name": "", "intro": "", "thanks": t("fm_thanks_default", lang), "stage": None, "tag": "", "on_site": True, "active": True,
          "fields": [{"key": "name", "required": True}, {"key": "phone", "required": True}, {"key": "message", "required": False}]}
    return _form_editor(request, ctx, fm, None)


@router.get("/app/forms/{form_id}")
@_guard
async def form_edit(request: Request, form_id: UUID):
    ctx = _own(request)
    with db.tx() as conn:
        fm = FM.get(conn, ctx["biz"]["id"], form_id)
    return _form_editor(request, ctx, fm, form_id)


async def _form_data(request: Request) -> dict:
    f = await _multi_form(request)
    keys = f.getlist("field")
    req = set(f.getlist("required"))
    return {"name": f.get("name"), "intro": f.get("intro"), "thanks": f.get("thanks"), "stage": f.get("stage"), "tag": f.get("tag"),
            "on_site": f.get("on_site") == "1", "active": f.get("active", "1") == "1", "act": f.get("act"),
            "fields": [{"key": k, "required": k in req} for k in keys]}


@router.post("/app/forms")
@_guard
async def form_create(request: Request):
    ctx = _own(request)
    data = await _form_data(request)
    with db.tx() as conn:
        fm = FM.save(conn, ctx["biz"], ctx["actor"], data)
    _flash(request, "fm_saved")
    return _go(f"/app/forms/{fm['id']}#share")


@router.post("/app/forms/{form_id}")
@_guard
async def form_save(request: Request, form_id: UUID):
    ctx = _own(request)
    data = await _form_data(request)
    with db.tx() as conn:
        if data.get("act") == "delete":
            FM.delete(conn, ctx["biz"], ctx["actor"], form_id)
            _flash(request, "fm_deleted")
            return _go("/app/forms")
        FM.save(conn, ctx["biz"], ctx["actor"], data, form_id=form_id)
    _flash(request, "fm_saved")
    return _go(f"/app/forms/{form_id}#share")


# ---------------------------------------------------------------- forms: the public page (the business's own look)
def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.get("VO_SECRET_KEY", required=True), salt="vo-form")


def _public_form(request: Request, slug: str, *, sent: bool = False, error: str | None = None, values: dict | None = None,
                 status: int = 200):
    with db.tx() as conn:
        fm, biz = FM.public(conn, slug)
        shown = FM.shown(conn, biz, fm)
    embed = request.query_params.get("embed") == "1"
    ctx = {"biz": biz, "user": None, "actor": "form", "role": None, "businesses": []}
    resp = render(request, "form_public.html", ctx, fm=fm, shown=shown, s=biz["site"] or {}, sent=sent, error=error,
                  values=values or {}, token=_signer().dumps(fm["slug"]), embed=embed, brand=biz if (biz["site"] or {}).get("brand") else None)
    resp.status_code = status
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/form/{slug}")
@_guard
async def form_public(request: Request, slug: str):
    return _public_form(request, slug, sent=request.query_params.get("sent") == "1")


@router.post("/form/{slug}")
@_guard
async def form_send(request: Request, slug: str):
    """Sent from the public page or from an embed on the business's own site (no session cookie there: the page's
    signed token stands in for the CSRF check). Honeypot + a per-connection limit, like the other public forms."""
    raw = await request.form()
    f = {k: v for k, v in raw.multi_items() if isinstance(v, str)}
    q = "?embed=1&sent=1" if request.query_params.get("embed") == "1" else "?sent=1"
    if f.get("website"):                                      # honeypot: people never fill it, bots do
        return _go(f"/form/{slug}{q}")
    try:
        if _signer().loads(f.get("token", ""), max_age=2 * 86400) != slug:
            raise BadSignature("other form")
    except BadSignature:
        with db.tx() as conn:
            fm, biz = FM.public(conn, slug)
        return _public_form(request, slug, error=t("fm_expired", _lang(request, biz)), values=f, status=400)
    if not _limit(_form_hits, request, 10):
        with db.tx() as conn:
            fm, biz = FM.public(conn, slug)
        return _public_form(request, slug, error=t("fm_too_many", _lang(request, biz)), values=f, status=429)
    with db.tx() as conn:
        fm, biz = FM.public(conn, slug)
    lang = _lang(request, biz)
    try:
        with db.tx() as conn:
            FM.submit(conn, biz, fm, f, request_consent=f.get("consent_request") == "1",
                      marketing_consent=f.get("consent_marketing") == "1", lang=lang)
    except Invalid as e:
        return _public_form(request, slug, error=str(e), values=f, status=400)
    return _go(f"/form/{slug}{q}")


def site_forms(biz: dict) -> list[dict]:
    """The forms the owner put on the website and the directory listing (buttons to /form/<slug>)."""
    with db.tx() as conn:
        return [{"name": x["name"], "url": f"/form/{x['slug']}"} for x in FM.list_(conn, biz["id"], on_site=True)]


