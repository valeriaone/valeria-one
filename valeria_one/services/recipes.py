"""Recipes: "when this happens, do that" — the owner switches a ready-made one on with one tap, or builds their own
from dropdowns: one trigger, at most one condition ("only if…"), one to three actions.

  When                                        Then (1–3)
  a new lead comes in (any channel)           tag them · give them to someone on the team · add a task ("reply", in 1 h)
  a deal moves to a stage                     move the deal · start a deal · tell the owner on Valeria
  a booking is made                           send the customer a message (asks the owner first unless allowed)
  one of the owner's fields changes to a value
  a form is sent
  a customer has had no reply for N hours

How it runs. Events come from services.log() — the same place the follow-ups, loyalty and the webhooks listen —
inside the event's own transaction, each recipe in its own savepoint, so a recipe that fails never undoes what set
it off (a message arriving, a deal moving). "No reply for N hours" is not an event: the jobs round looks for it.
Each recipe runs at most once per thing that set it off (recipe_runs: one row per recipe and event, UNIQUE), so a
retried webhook or a second jobs round never runs it twice. What it did is written down in words ("what happened").

Loop protection. Whatever a recipe does is done as "recipe:<id>"; when something a recipe did would set off a recipe
that already ran for the same customer/deal in the last hour, that recipe stops and says so (status "loop"). Chains
are also cut after MAX_DEPTH recipes in a row.

Customers. A message to a customer is never sent from here: it becomes Valeria's question to the owner ("Send this
to Ana?"), or — when the owner switched "ask me first" off for this recipe and Valeria isn't paused — it is queued
with her usual minute to undo. Either way it goes out through inbox.send, so consent, STOP, quiet hours and the
24-hour window apply as everywhere else; a customer who said STOP is never written to by a recipe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, db, packs
from . import Invalid, NotFound, log

R, RR = db.recipes, db.recipe_runs
TRIGGERS = ("lead.new", "deal.stage", "booking.made", "field.changed", "form.submitted", "contact.quiet")
EVENT_TRIGGER = {"contact.created": "lead.new", "deal.stage_changed": "deal.stage", "appointment.booked": "booking.made",
                 "field.changed": "field.changed", "form.submitted": "form.submitted"}
NOT_LEADS = ("user:", "demo_visitor", "import", "recipe:", "valeria", "seed")   # added by the team, not a lead coming in
ACTIONS = ("tag", "assign", "task", "move", "deal", "notify", "message", "sequence")   # v0.18: "add to sequence" (a drip)
CONDITIONS = ("tag", "field", "source")
MAX_ACTIONS = 3
MAX_RECIPES = 50
LOOP_HOURS = 1
MAX_DEPTH = 3
QUIET_LOOKBACK_HOURS = 48                                  # "no reply for N hours" looks at the last two days only
_DEPTH = "vo_recipe_depth"


def _t(key: str, lang: str) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- the gallery
def _won(biz: dict) -> str | None:
    return next((s.key for s in packs.industry(biz["industry"]).stages if s.outcome == "won"), None)


def _open_stages(biz: dict) -> list[str]:
    return [s.key for s in packs.industry(biz["industry"]).stages if s.outcome == "open"]


def presets(conn: Connection, biz: dict, lang: str, user_id: UUID | None = None) -> list[dict]:
    """The ready-made recipes for this business (its own stages, fields and forms), each ready to switch on."""
    L = biz["language"]
    won, opens = _won(biz), _open_stages(biz)
    second = opens[1] if len(opens) > 1 else (opens[0] if opens else None)
    from . import fields as CF
    from . import forms as FM
    fl = [f for f in CF.list_(conn, biz["id"]) if f["kind"] in ("choice", "yesno")] or CF.list_(conn, biz["id"])
    f0 = fl[0] if fl else None
    owner = user_id or conn.execute(select(db.memberships.c.user_id).where(db.memberships.c.business_id == biz["id"],
                                                                           db.memberships.c.role == "owner")
                                    .order_by(db.memberships.c.created_at).limit(1)).scalar()
    out = [
        {"key": "lead_task", "trigger": "lead.new", "trigger_arg": {},
         "actions": [{"do": "task", "title": _t("rc_txt_reply", L), "hours": 1}]},
        {"key": "lead_tag", "trigger": "lead.new", "trigger_arg": {}, "actions": [{"do": "tag", "tag": _t("rc_txt_newlead_tag", L)}]},
        {"key": "lead_assign", "trigger": "lead.new", "trigger_arg": {},
         "actions": [{"do": "assign", "user_id": str(owner) if owner else ""}], "needs": None if owner else "team"},
        {"key": "won_thanks", "trigger": "deal.stage", "trigger_arg": {"stage": won},
         "actions": [{"do": "message", "text": _t("rc_txt_thanks", L)}], "needs": None if won else "stage"},
        {"key": "stage_task", "trigger": "deal.stage", "trigger_arg": {"stage": second},
         "actions": [{"do": "task", "title": _t("rc_txt_follow", L), "hours": 24}], "needs": None if second else "stage"},
        {"key": "won_notify", "trigger": "deal.stage", "trigger_arg": {"stage": won},
         "actions": [{"do": "notify", "text": _t("rc_txt_won", L)}], "needs": None if won else "stage"},
        {"key": "booking_tag", "trigger": "booking.made", "trigger_arg": {}, "actions": [{"do": "tag", "tag": _t("rc_txt_booked_tag", L)}]},
        {"key": "quiet_notify", "trigger": "contact.quiet", "trigger_arg": {"hours": 2},
         "actions": [{"do": "notify", "text": _t("rc_txt_waiting", L)}]},
        {"key": "form_deal", "trigger": "form.submitted", "trigger_arg": {},
         "actions": [{"do": "deal", "stage": opens[0] if opens else None}]},
        {"key": "field_notify", "trigger": "field.changed",
         "trigger_arg": {"key": f0["key"], "value": (f0["options"][0] if f0["kind"] == "choice" else ("yes" if f0["kind"] == "yesno" else ""))}
         if f0 else {}, "actions": [{"do": "notify", "text": _t("rc_txt_field", L)}], "needs": None if f0 else "fields"},
    ]
    mine = {r["preset"]: r for r in list_(conn, biz["id"]) if r["preset"]}
    forms = FM.list_(conn, biz["id"])
    for p in out:
        p.setdefault("needs", None)
        if p["key"] == "form_deal" and not forms:
            p["needs"] = "forms"
        p["condition"] = {}
        p["ask_first"] = True
        p["mine"] = mine.get(p["key"])
        p["sentence"] = describe(conn, biz, p, lang)
    return out


def switch_on(conn: Connection, biz: dict, actor: str, key: str, user_id: UUID | None = None) -> dict:
    """One tap: the ready-made recipe, on (again, if it was switched off)."""
    p = next((x for x in presets(conn, biz, biz["language"], user_id) if x["key"] == key), None)
    if not p:
        raise NotFound("recipe")
    if p["needs"]:
        raise Invalid({"fields": "add one of your own fields first", "forms": "make a form first", "team": "add someone to the team first",
                       "stage": "your pipeline has no stage for this"}.get(p["needs"], "not ready"), "needs")
    if p["mine"]:
        return set_active(conn, biz, actor, p["mine"]["id"], True)
    return save(conn, biz, actor, {"trigger": p["trigger"], "trigger_arg": p["trigger_arg"], "condition": {},
                                   "actions": p["actions"], "ask_first": True, "name": ""}, preset=key)


# ---------------------------------------------------------------- the owner's recipes
def list_(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(R).where(R.c.business_id == business_id).order_by(R.c.created_at)).mappings()]


def get(conn: Connection, business_id: UUID, recipe_id: UUID) -> dict:
    r = conn.execute(select(R).where(R.c.business_id == business_id, R.c.id == recipe_id)).mappings().first()
    if not r:
        raise NotFound("recipe")
    return dict(r)


def _stage_ok(biz: dict, key) -> str:
    if not key or not packs.industry(biz["industry"]).stage(str(key)):
        raise Invalid("pick a stage", "stage")
    return str(key)


def _member(conn: Connection, business_id: UUID, uid) -> str:
    try:
        u = UUID(str(uid))
    except ValueError:
        raise Invalid("pick someone on the team", "user_id") from None
    if not conn.execute(select(db.memberships.c.user_id).where(db.memberships.c.business_id == business_id,
                                                               db.memberships.c.user_id == u)).scalar():
        raise Invalid("pick someone on the team", "user_id")
    return str(u)


def _hours(v, lo: int, hi: int, default: int) -> int:
    try:
        h = int(str(v).strip()) if str(v or "").strip() else default
    except ValueError:
        raise Invalid(f"hours: {lo} to {hi}", "hours") from None
    if not lo <= h <= hi:
        raise Invalid(f"hours: {lo} to {hi}", "hours")
    return h


def _clean(conn: Connection, biz: dict, data: dict) -> dict:
    from . import fields as CF
    trig = data.get("trigger")
    if trig not in TRIGGERS:
        raise Invalid("pick what sets it off", "trigger")
    a = data.get("trigger_arg") or {}
    arg: dict = {}
    if trig == "deal.stage" and a.get("stage"):
        arg["stage"] = _stage_ok(biz, a["stage"])
    if trig == "contact.quiet":
        arg["hours"] = _hours(a.get("hours"), 1, 168, 2)
    if trig == "form.submitted" and a.get("form_id"):
        fid = UUID(str(a["form_id"]))
        if not conn.execute(select(db.forms.c.id).where(db.forms.c.business_id == biz["id"], db.forms.c.id == fid)).scalar():
            raise Invalid("pick one of your forms", "form_id")
        arg["form_id"] = str(fid)
    if trig == "field.changed":
        f = next((x for x in CF.list_(conn, biz["id"]) if x["key"] == a.get("key")), None)
        if not f:
            raise Invalid("pick one of your fields", "key")
        arg["key"] = f["key"]
        v = str(a.get("value") or "").strip()[:60]
        if v:
            arg["value"] = CF.as_text(CF.clean_value(biz, f, v))
    c = data.get("condition") or {}
    cond: dict = {}
    if c.get("kind"):
        if c["kind"] not in CONDITIONS:
            raise Invalid("pick a condition", "condition")
        v = str(c.get("value") or "").strip()[:60]
        if not v:
            raise Invalid("write what it must be", "condition")
        cond = {"kind": c["kind"], "value": v.lower() if c["kind"] in ("tag", "source") else v}
        if c["kind"] == "field":
            f = next((x for x in CF.list_(conn, biz["id"]) if x["key"] == c.get("key")), None)
            if not f:
                raise Invalid("pick one of your fields", "condition")
            cond["key"] = f["key"]
            cond["value"] = CF.as_text(CF.clean_value(biz, f, v))
    acts = []
    for x in data.get("actions") or []:
        do = (x or {}).get("do")
        if not do:
            continue
        if do not in ACTIONS:
            raise Invalid("pick what to do", "actions")
        if do == "tag":
            tag = str(x.get("tag") or "").strip().lower()[:60]
            if not tag:
                raise Invalid("write the tag", "tag")
            acts.append({"do": do, "tag": tag})
        elif do == "assign":
            acts.append({"do": do, "user_id": _member(conn, biz["id"], x.get("user_id"))})
        elif do == "task":
            title = " ".join(str(x.get("title") or "").split())[:200]
            if not title:
                raise Invalid("write the task", "title")
            act = {"do": do, "title": title, "hours": _hours(x.get("hours"), 0, 720, 1)}
            if x.get("user_id"):
                act["user_id"] = _member(conn, biz["id"], x["user_id"])
            acts.append(act)
        elif do in ("move", "deal"):
            acts.append({"do": do, "stage": _stage_ok(biz, x.get("stage"))})
        elif do == "notify":
            acts.append({"do": do, "text": str(x.get("text") or "").strip()[:500] or _t("rc_txt_notify", biz["language"])})
        elif do == "message":
            text = str(x.get("text") or "").strip()[:1000]
            if not text:
                raise Invalid("write the message", "text")
            acts.append({"do": do, "text": text})
        elif do == "sequence":
            acts.append({"do": do, "sequence_id": sequence_ok(conn, biz["id"], x.get("sequence_id"))})
    if not acts:
        raise Invalid("pick at least one thing to do", "actions")
    if len(acts) > MAX_ACTIONS:
        raise Invalid(f"at most {MAX_ACTIONS} things to do", "actions")
    return {"trigger": trig, "trigger_arg": arg, "condition": cond, "actions": acts,
            "ask_first": bool(data.get("ask_first", True)), "name": " ".join(str(data.get("name") or "").split())[:160]}


def sequence_ok(conn: Connection, business_id: UUID, sid) -> str:
    try:
        u = UUID(str(sid))
    except ValueError:
        raise Invalid("pick one of your sequences", "sequence_id") from None
    if not conn.execute(select(db.sequences.c.id).where(db.sequences.c.business_id == business_id, db.sequences.c.id == u)).scalar():
        raise Invalid("pick one of your sequences", "sequence_id")
    return str(u)


def save(conn: Connection, biz: dict, actor: str, data: dict, recipe_id: UUID | None = None, preset: str | None = None) -> dict:
    vals = _clean(conn, biz, data)
    if recipe_id:
        get(conn, biz["id"], recipe_id)
        conn.execute(update(R).where(R.c.business_id == biz["id"], R.c.id == recipe_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "recipe.updated", actor, recipe_id, {"trigger": vals["trigger"]})
        return get(conn, biz["id"], recipe_id)
    if len(list_(conn, biz["id"])) >= MAX_RECIPES:
        raise Invalid(f"at most {MAX_RECIPES} recipes", "name")
    row = conn.execute(insert(R).values(business_id=biz["id"], preset=preset, created_by=actor, **vals).returning(R)).mappings().one()
    log(conn, biz["id"], "recipe.created", actor, row["id"], {"trigger": vals["trigger"], "preset": preset})
    return dict(row)


def set_active(conn: Connection, biz: dict, actor: str, recipe_id: UUID, on: bool) -> dict:
    get(conn, biz["id"], recipe_id)
    conn.execute(update(R).where(R.c.business_id == biz["id"], R.c.id == recipe_id).values(active=bool(on), updated_at=func.now()))
    log(conn, biz["id"], "recipe.on" if on else "recipe.off", actor, recipe_id)
    return get(conn, biz["id"], recipe_id)


def delete(conn: Connection, biz: dict, actor: str, recipe_id: UUID) -> None:
    get(conn, biz["id"], recipe_id)
    conn.execute(R.delete().where(R.c.business_id == biz["id"], R.c.id == recipe_id))
    log(conn, biz["id"], "recipe.deleted", actor, recipe_id)


def runs(conn: Connection, business_id: UUID, limit: int = 40, recipe_id: UUID | None = None) -> list[dict]:
    """What happened, newest first, with the recipe and the customer it was about."""
    C = db.contacts
    q = (select(RR, R.c.name.label("recipe_name"), R.c.preset, R.c.trigger, C.c.name.label("contact_name"))
         .join(R, R.c.id == RR.c.recipe_id).outerjoin(C, C.c.id == RR.c.contact_id).where(RR.c.business_id == business_id))
    if recipe_id:
        q = q.where(RR.c.recipe_id == recipe_id)
    return [dict(r) for r in conn.execute(q.order_by(RR.c.created_at.desc()).limit(max(1, min(limit, 200)))).mappings()]


# ---------------------------------------------------------------- in words
def describe(conn: Connection, biz: dict, r: dict, lang: str) -> str:
    """The recipe as one plain sentence, in the reader's language."""
    return _sentence(conn, biz, r, lang)


def _stage_label(biz: dict, key: str | None, lang: str) -> str:
    return blocks.word(biz, f"stage:{key}", lang) if key else "—"


def _field_label(conn: Connection, biz: dict, key: str | None) -> str:
    from . import fields as CF
    f = next((x for x in CF.list_(conn, biz["id"], deleted=False) + CF.list_(conn, biz["id"], deleted=True) if x["key"] == key), None)
    return f["label"] if f else "—"


def _who(conn: Connection, uid) -> str:
    if not uid:
        return "—"
    try:
        u = conn.execute(select(db.users.c.name, db.users.c.email).where(db.users.c.id == UUID(str(uid)))).first()
    except ValueError:
        return "—"
    return (u[0] or u[1]) if u else "—"


def _sentence(conn: Connection, biz: dict, r: dict, lang: str) -> str:
    a, trig = r.get("trigger_arg") or {}, r["trigger"]
    if trig == "deal.stage":
        when = _t("rc_t_deal.stage", lang).format(stage=_stage_label(biz, a["stage"], lang)) if a.get("stage") else _t("rc_t_deal.stage_any", lang)
    elif trig == "contact.quiet":
        when = _t("rc_t_contact.quiet", lang).format(hours=a.get("hours") or 2)
    elif trig == "form.submitted":
        name = conn.execute(select(db.forms.c.name).where(db.forms.c.id == UUID(a["form_id"]))).scalar() if a.get("form_id") else None
        when = _t("rc_t_form.submitted", lang).format(form=name) if name else _t("rc_t_form.submitted_any", lang)
    elif trig == "field.changed":
        when = (_t("rc_t_field.changed", lang) if a.get("value") else _t("rc_t_field.changed_any", lang)).format(
            field=_field_label(conn, biz, a.get("key")), value=_yn(a.get("value"), lang))
    else:
        when = _t(f"rc_t_{trig}", lang)
    c = r.get("condition") or {}
    only = ""
    if c.get("kind"):
        only = " " + _t(f"rc_c_{c['kind']}", lang).format(value=_yn(c.get("value"), lang), field=_field_label(conn, biz, c.get("key")))
    dos = []
    for x in r.get("actions") or []:
        d = x["do"]
        if d == "tag":
            dos.append(_t("rc_a_tag", lang).format(tag=x["tag"]))
        elif d == "assign":
            dos.append(_t("rc_a_assign", lang).format(who=_who(conn, x.get("user_id"))))
        elif d == "task":
            dos.append(_t("rc_a_task" if x.get("hours") else "rc_a_task_now", lang).format(title=_plain(x["title"], biz), hours=x.get("hours")))
        elif d in ("move", "deal"):
            dos.append(_t(f"rc_a_{d}", lang).format(stage=_stage_label(biz, x.get("stage"), lang)))
        elif d == "notify":
            dos.append(_t("rc_a_notify", lang))
        elif d == "message":
            dos.append(_t("rc_a_message", lang).format(text=_short(_plain(x["text"], biz))) + " " +
                       _t("rc_ask_suffix" if r.get("ask_first", True) else "rc_noask_suffix", lang))
        elif d == "sequence":
            dos.append(_t("rc_a_sequence", lang).format(name=_seq_name(conn, x.get("sequence_id"))))
    return f"{when}{only} → " + "; ".join(dos)


def _seq_name(conn: Connection, sid) -> str:
    try:
        return conn.execute(select(db.sequences.c.name).where(db.sequences.c.id == UUID(str(sid)))).scalar() or "—"
    except ValueError:
        return "—"


def _short(s: str, n: int = 60) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _plain(text: str, biz: dict) -> str:
    """The owner's words as a sentence shows them: their business by name, the customer's name and stage as "…"."""
    return (text or "").replace("{business}", biz["name"]).replace("{name}", "…").replace("{stage}", "…")


def _yn(v, lang: str) -> str:
    if v in ("yes", "no"):
        return _t(v, lang)
    return v or ""


def line_text(line: dict, lang: str) -> str:
    """One thing a run did, as the owner reads it."""
    try:
        return _t(line["k"], lang).format(**(line.get("p") or {}))
    except (KeyError, IndexError):
        return _t(line.get("k", ""), lang)


# ---------------------------------------------------------------- running
def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict, actor: str,
             event_id: int) -> None:
    """Called by services.log() for every event, inside its transaction."""
    trig = EVENT_TRIGGER.get(type_)
    if not trig or not subject_id:
        return
    if trig == "lead.new" and (actor or "").startswith(NOT_LEADS):
        return
    rows = conn.execute(select(R).where(R.c.business_id == business_id, R.c.active, R.c.trigger == trig)
                        .order_by(R.c.created_at)).mappings().all()
    if not rows or not blocks.on(blocks.state(conn, business_id), "followups"):
        return
    contact_id, deal_id = _who_for(conn, business_id, trig, subject_id, data)
    for r in rows:
        if _matches(dict(r), data):
            fire(conn, dict(r), subject_id=subject_id, contact_id=contact_id, deal_id=deal_id, dedupe=f"ev:{event_id}",
                 actor=actor or "system", data=data)


def _who_for(conn: Connection, business_id: UUID, trig: str, subject_id: UUID, data: dict) -> tuple[UUID | None, UUID | None]:
    """The customer and the deal an event is about."""
    if trig in ("lead.new", "form.submitted", "contact.quiet"):
        d = data.get("deal_id")
        return subject_id, UUID(str(d)) if d else None
    if trig == "deal.stage" or (trig == "field.changed" and data.get("object") == "deal"):
        cid = conn.execute(select(db.deals.c.contact_id).where(db.deals.c.business_id == business_id, db.deals.c.id == subject_id)).scalar()
        return cid, subject_id
    if trig == "booking.made":
        ap = conn.execute(select(db.appointments.c.contact_id, db.appointments.c.deal_id).where(
            db.appointments.c.business_id == business_id, db.appointments.c.id == subject_id)).first()
        return (ap[0], ap[1]) if ap else (None, None)
    return subject_id, None                                  # field.changed on a customer


def _matches(r: dict, data: dict) -> bool:
    a = r["trigger_arg"] or {}
    if r["trigger"] == "deal.stage":
        return not a.get("stage") or a["stage"] == data.get("to")
    if r["trigger"] == "form.submitted":
        return not a.get("form_id") or a["form_id"] == str(data.get("form_id"))
    if r["trigger"] == "field.changed":
        if a.get("key") != data.get("key"):
            return False
        return not a.get("value") or str(a["value"]).lower() == str(data.get("value") or "").lower()
    return True


def _condition(conn: Connection, r: dict, contact: dict | None, deal: dict | None) -> bool:
    c = r["condition"] or {}
    if not c.get("kind"):
        return True
    from . import fields as CF
    if c["kind"] == "tag":
        return bool(contact) and c["value"] in (contact["tags"] or [])
    if c["kind"] == "source":
        return bool(contact) and c["value"] in (contact.get("source") or "").lower()
    if c["kind"] == "field":
        for row in (contact, deal):
            if row and c.get("key") in (row.get("custom") or {}):
                return CF.as_text(row["custom"][c["key"]]).lower() == str(c["value"]).lower()
        return False
    return False


def _row(conn: Connection, T, business_id: UUID, rid) -> dict | None:
    if not rid:
        return None
    r = conn.execute(select(T).where(T.c.business_id == business_id, T.c.id == rid)).mappings().first()
    return dict(r) if r and not r.get("deleted_at") else None


def fire(conn: Connection, r: dict, *, subject_id, contact_id, deal_id, dedupe: str, actor: str, data: dict | None = None,
         now: datetime | None = None) -> str | None:
    """Run one recipe for one thing, once. -> the run's status (None = not run: already ran, or "only if" said no)."""
    now = now or datetime.now(timezone.utc)
    bid = r["business_id"]
    depth = conn.info.get(_DEPTH, 0)
    looped = depth >= MAX_DEPTH or (actor.startswith("recipe:") and conn.execute(select(RR.c.id).where(
        RR.c.recipe_id == r["id"], RR.c.subject_id == subject_id, RR.c.status != "loop",
        RR.c.created_at > now - timedelta(hours=LOOP_HOURS))).first() is not None)
    if looped:
        conn.execute(pg_insert(RR).values(business_id=bid, recipe_id=r["id"], dedupe=dedupe[:120], subject_id=subject_id,
                                          contact_id=contact_id, status="loop", lines=[{"k": "rc_l_loop", "p": {}}])
                     .on_conflict_do_nothing(constraint="uq_recipe_run"))
        return "loop"
    contact = _row(conn, db.contacts, bid, contact_id)
    deal = _row(conn, db.deals, bid, deal_id)
    if not _condition(conn, r, contact, deal):
        return None
    run_id = conn.execute(pg_insert(RR).values(business_id=bid, recipe_id=r["id"], dedupe=dedupe[:120], subject_id=subject_id,
                                               contact_id=contact["id"] if contact else None, status="done")
                          .on_conflict_do_nothing(constraint="uq_recipe_run").returning(RR.c.id)).scalar()
    if not run_id:
        return None                                          # this recipe already ran for this
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
    lines, status = [], "done"
    conn.info[_DEPTH] = depth + 1
    try:
        with conn.begin_nested():
            for i, a in enumerate(r["actions"] or []):
                line, contact, deal = _do(conn, biz, r, a, contact, deal, data or {}, f"rc:{run_id}:{i}", now)
                lines.append(line)
    except Exception as e:                                   # noqa: BLE001 — the event that set it off still stands
        status = "failed"
        why = str(e) if isinstance(e, (Invalid, NotFound)) else type(e).__name__
        lines = [{"k": "rc_l_failed", "p": {"why": why[:200]}}]
    finally:
        conn.info[_DEPTH] = depth
    conn.execute(update(RR).where(RR.c.id == run_id).values(status=status, lines=lines))
    return status


def _fill(text: str, biz: dict, contact: dict | None, deal: dict | None, lang: str) -> str:
    from .. import config
    first = ((contact or {}).get("name") or "").split(" ")[0] or "—"
    stage = blocks.word(biz, f"stage:{deal['stage']}", lang) if deal else ""
    book = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    return (text.replace("{name}", first).replace("{business}", biz["name"]).replace("{stage}", stage)
            .replace("{book_link}", book).strip())


def _do(conn: Connection, biz: dict, r: dict, a: dict, contact: dict | None, deal: dict | None, data: dict, dedupe: str,
        now: datetime, me: str | None = None) -> tuple[dict, dict | None, dict | None]:
    """One action. Workflows (services/workflows.py) use the same actions, as "workflow:<id>"."""
    from . import contacts as C
    from . import deals as D
    from . import owner as OW
    me = me or f"recipe:{r['id']}"
    L = biz["language"]
    d = a["do"]
    if d in ("tag", "assign", "task", "deal", "message", "sequence") and not contact:
        return {"k": "rc_l_no_contact", "p": {}}, contact, deal
    if d == "tag":
        if a["tag"] not in (contact["tags"] or []):
            contact = C.patch(conn, biz, me, contact["id"], {"tags": [*contact["tags"], a["tag"]]})
        return {"k": "rc_l_tagged", "p": {"tag": a["tag"]}}, contact, deal
    if d == "assign":
        contact = C.patch(conn, biz, me, contact["id"], {"assignee_id": a["user_id"]})
        return {"k": "rc_l_assigned", "p": {"who": _who(conn, a["user_id"])}}, contact, deal
    if d == "task":
        from . import projects as PR
        title = _fill(a["title"], biz, contact, deal, L)
        due = now + timedelta(hours=a["hours"]) if a.get("hours") else None
        PR.add_task(conn, biz, me, {"title": title, "contact_id": contact["id"], "deal_id": deal["id"] if deal else None,
                                    "due_at": due, "assignee_id": a.get("user_id") or contact.get("assignee_id")})
        return {"k": "rc_l_task", "p": {"title": title}}, contact, deal
    if d == "move":
        target = deal or _open_deal(conn, biz, contact)
        if not target:
            return {"k": "rc_l_no_deal", "p": {}}, contact, deal
        if target["stage"] != a["stage"]:
            target = D.patch(conn, biz, me, target["id"], {"stage": a["stage"]})
        return {"k": "rc_l_moved", "p": {"stage": _stage_label(biz, a["stage"], L)}}, contact, target
    if d == "deal":
        have = deal or _open_deal(conn, biz, contact)
        if have:
            return {"k": "rc_l_deal_had", "p": {}}, contact, have
        title = contact["name"] or contact.get("phone") or contact.get("email") or "—"
        if data.get("form"):
            title = f"{data['form']} · {title}"
        deal = D.create(conn, biz, me, {"title": title, "contact_id": contact["id"], "stage": a["stage"]})
        return {"k": "rc_l_deal", "p": {"stage": _stage_label(biz, a["stage"], L)}}, contact, deal
    if d == "notify":
        text = _fill(a.get("text") or "", biz, contact, deal, L)
        OW.ask(conn, biz, "notice", text, {"text": text, "recipe_id": str(r["id"])}, contact_id=contact["id"] if contact else None,
               by=me, dedupe=dedupe, owner_says_go=True, now=now)
        return {"k": "rc_l_notified", "p": {}}, contact, deal
    if d == "message":
        if contact.get("unsubscribed_at"):
            return {"k": "rc_l_optout", "p": {}}, contact, deal          # they said STOP: never from a recipe
        kind = "marketing" if a.get("kind") == "marketing" else "service"
        if kind == "marketing" and not contact.get("marketing_consent"):
            return {"k": "rc_l_no_consent", "p": {}}, contact, deal      # an offer only to someone who said yes
        text = _fill(a["text"], biz, contact, deal, L)
        if "{review_link}" in text:                          # v0.18: the same review ask for everyone (never gated), once per 90 days
            from . import reviews as RV
            if not biz.get("review_url"):
                return {"k": "rc_l_no_review_link", "p": {}}, contact, deal
            if RV.recently_asked(conn, biz["id"], contact["id"], now):
                return {"k": "rc_l_review_recent", "p": {}}, contact, deal
            _, url = RV.new_link(conn, biz, contact["id"], "automatic")
            text = text.replace("{review_link}", url)
        alone = not r.get("ask_first", True) and not OW.paused(conn, biz["id"])
        summary = _t("rc_q_send", L).format(name=(contact["name"] or "—").split(" ")[0], text=text)
        OW.ask(conn, biz, "reply", summary, {"text": text, "recipe_id": str(r["id"]), "draft": text, "kind": kind},
               contact_id=contact["id"], by=me, dedupe=dedupe, owner_says_go=alone, now=now)
        return {"k": "rc_l_queued" if alone else "rc_l_asked", "p": {}}, contact, deal
    if d == "sequence":
        from . import sequences as SQ
        try:
            seq = SQ.get(conn, biz["id"], UUID(str(a.get("sequence_id"))))
        except (NotFound, ValueError):
            return {"k": "rc_l_no_sequence", "p": {}}, contact, deal
        got = SQ.enroll(conn, biz, seq, contact["id"], me, now)
        return {"k": "rc_l_sequence" if got else "rc_l_sequence_had", "p": {"name": seq["name"]}}, contact, deal
    raise Invalid("unknown action", "actions")


def _open_deal(conn: Connection, biz: dict, contact: dict | None) -> dict | None:
    if not contact:
        return None
    opens = _open_stages(biz)
    r = conn.execute(select(db.deals).where(db.deals.c.business_id == biz["id"], db.deals.c.contact_id == contact["id"],
                                            db.deals.c.stage.in_(opens)).order_by(db.deals.c.updated_at.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


# ---------------------------------------------------------------- the job: "no reply for N hours", old fields
def round_(now: datetime | None = None) -> dict:
    from . import fields as CF
    now = now or datetime.now(timezone.utc)
    stats = {"ran": 0, "purged": CF.purge_due(now)}
    with db.tx() as conn:
        quiet = [dict(r) for r in conn.execute(select(R).where(R.c.active, R.c.trigger == "contact.quiet")).mappings()]
    CV, M = db.conversations, db.messages
    for r in quiet:
        hours = int((r["trigger_arg"] or {}).get("hours") or 2)
        with db.tx() as conn:
            if not blocks.on(blocks.state(conn, r["business_id"]), "followups"):
                continue
            answered = exists().where(M.c.conversation_id == CV.c.id, M.c.direction == "out", M.c.created_at > CV.c.last_inbound_at)
            rows = conn.execute(select(CV.c.id, CV.c.contact_id, CV.c.last_inbound_at).where(
                CV.c.business_id == r["business_id"], CV.c.status == "open", CV.c.last_inbound_at.isnot(None),
                CV.c.last_inbound_at <= now - timedelta(hours=hours),
                CV.c.last_inbound_at > now - timedelta(hours=hours + QUIET_LOOKBACK_HOURS), ~answered).limit(200)).all()
            for cv_id, cid, last in rows:
                st = fire(conn, r, subject_id=cid, contact_id=cid, deal_id=None, dedupe=f"quiet:{cv_id}:{int(last.timestamp())}",
                          actor="system", now=now)
                stats["ran"] += st == "done"
    return stats
