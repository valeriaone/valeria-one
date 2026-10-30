"""Workflows (v0.18): "when this happens, then these steps" — a step up from recipes (services/recipes.py).

A workflow is one trigger and a list of steps, saved as JSON (workflows.steps):

  {"t": "message", "text", "kind": service|marketing, "ask": true}     to the customer (asks the owner first unless "ask" is off)
  {"t": "tag" | "untag", "tag"}        {"t": "field", "key", "value"}    {"t": "assign", "user_id"}
  {"t": "move" | "deal", "stage"}      {"t": "task", "title", "hours"}   {"t": "sequence", "sequence_id"}   {"t": "notify", "text"}
  {"t": "wait", "amount", "unit": minutes|hours|days}
  {"t": "wait_until", "key" (a date field), "offset" (days), "yearly", "at": "09:00"}
  {"t": "if", "cond": {"kind": tag|field|channel|replied|source, "key"?, "value"?}, "yes": [steps], "no": [steps]}

and an optional goal (booked | paid | replied): when the customer reaches it, their run stops there.

On a phone it is a vertical list with "+ add step" between steps; an "if" shows its two branches as columns
(stacked on a narrow screen). No free-form canvas: nothing to drag, nothing to get lost.

How it runs. Events come from services.log() (as recipes): a matching workflow starts a run for that customer
— once per event (workflow_runs UNIQUE(workflow, dedupe)) — and the steps run at once, in the event's
transaction and its own savepoint, up to the first wait. The jobs round (round_) wakes the runs whose wait is over
(claimed once, SKIP LOCKED), starts the time-based ones (a date field — birthdays —, no visit in N days, no reply
for N hours) and keeps going. Every step is written in the run's history, in words.

Loop protection: a run takes at most MAX_RUN_STEPS steps; a customer is never in the same workflow twice at once;
something a workflow did can't start a workflow that already ran for the same customer in the last hour (status
"loop"), and chains are cut after MAX_DEPTH (shared with recipes). Steps never point backwards (a list, not a
graph), so a run always ends.

Customers. Messages go through Valeria exactly like a recipe's (ask the owner first unless the owner switched it
off and Valeria isn't paused; then a minute to undo) and through inbox.send: consent for offers, STOP, quiet hours,
the 24-hour window. An offer ("marketing") never goes to someone without marketing consent.
Recipes keep working as they are; "open as a workflow" makes a workflow from one (and switches the recipe off).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, db, packs
from . import Invalid, NotFound, log
from . import recipes as RC

W, WR = db.workflows, db.workflow_runs
TRIGGERS = ("lead.new", "deal.stage", "booking.made", "booking.done", "booking.no_show", "field.changed", "form.submitted",
            "tag.added", "payment.paid", "contact.quiet", "contact.inactive", "date.field", "manual")
EVENT_TRIGGER = {"contact.created": "lead.new", "deal.stage_changed": "deal.stage", "appointment.booked": "booking.made",
                 "appointment.done": "booking.done", "appointment.no_show": "booking.no_show", "field.changed": "field.changed",
                 "form.submitted": "form.submitted", "contact.updated": "tag.added", "payment.paid": "payment.paid"}
GOALS = ("booked", "paid", "replied")
GOAL_EVENT = {"appointment.booked": "booked", "payment.paid": "paid", "message.received": "replied"}
ACTIONS = ("message", "tag", "untag", "field", "assign", "move", "deal", "task", "sequence", "notify")
STEPS = ACTIONS + ("wait", "wait_until", "if")
CONDS = ("tag", "field", "channel", "replied", "source")
UNITS = {"minutes": 1, "hours": 60, "days": 1440}
MAX_STEPS = 40                                               # in one workflow, all branches together
MAX_NEST = 3                                                 # an if inside an if inside an if
MAX_RUN_STEPS = 60                                           # one run, however it branches (loop guard)
MAX_WORKFLOWS = 50
LOOP_HOURS = 1
NOT_LEADS = RC.NOT_LEADS + ("workflow:", "sequence:")
_DEPTH = RC._DEPTH                                            # shared with recipes: a chain of either is cut at MAX_DEPTH


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- reading and checking a workflow
def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(W).where(W.c.business_id == business_id).order_by(W.c.created_at)).mappings()]
    live = dict(conn.execute(select(WR.c.workflow_id, func.count()).where(WR.c.business_id == business_id,
                                                                         WR.c.status.in_(("running", "waiting")))
                             .group_by(WR.c.workflow_id)).all())
    for r in rows:
        r["in_now"] = live.get(r["id"], 0)
    return rows


def get(conn: Connection, business_id: UUID, wf_id: UUID) -> dict:
    r = conn.execute(select(W).where(W.c.business_id == business_id, W.c.id == wf_id)).mappings().first()
    if not r:
        raise NotFound("workflow")
    return dict(r)


def _int(v, lo: int, hi: int, field: str, default: int = 0) -> int:
    try:
        n = int(str(v).strip()) if str(v if v is not None else "").strip() else default
    except ValueError:
        raise Invalid(f"{field}: a whole number", field) from None
    if not lo <= n <= hi:
        raise Invalid(f"{field}: {lo} to {hi}", field)
    return n


def _count(steps: list) -> int:
    return sum(1 + (_count(s.get("yes") or []) + _count(s.get("no") or []) if s.get("t") == "if" else 0) for s in steps)


def clean_steps(conn: Connection, biz: dict, raw, depth: int = 0) -> list[dict]:
    from . import fields as CF
    if depth > MAX_NEST:
        raise Invalid(f"at most {MAX_NEST} questions inside each other", "steps")
    out = []
    date_keys = {f["key"] for f in CF.list_(conn, biz["id"], "contact") if f["kind"] == "date"}
    for s in raw or []:
        t = (s or {}).get("t")
        if t not in STEPS:
            raise Invalid("pick what this step does", "steps")
        if t in ACTIONS:
            if t == "untag":
                tag = str(s.get("tag") or "").strip().lower()[:60]
                if not tag:
                    raise Invalid("write the tag", "tag")
                out.append({"t": t, "tag": tag})
                continue
            if t == "field":
                f = next((x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == s.get("key")), None)
                if not f:
                    raise Invalid("pick one of your fields", "key")
                v = str(s.get("value") or "").strip()[:200]
                CF.clean_value(biz, f, v) if v else None
                out.append({"t": t, "key": f["key"], "value": v})
                continue
            act = RC._clean(conn, biz, {"trigger": "lead.new", "actions": [{**s, "do": t}]})["actions"][0]
            step = {"t": t, **{k: v for k, v in act.items() if k != "do"}}
            if t == "message":
                step["kind"] = "marketing" if s.get("kind") == "marketing" else "service"
                step["ask"] = s.get("ask") not in (False, "0", "false", "off")
            out.append(step)
        elif t == "wait":
            unit = s.get("unit") if s.get("unit") in UNITS else "hours"
            amount = _int(s.get("amount"), 1, 525600 // UNITS[unit], "amount", 1)
            out.append({"t": t, "amount": amount, "unit": unit})
        elif t == "wait_until":
            if s.get("key") not in date_keys:
                raise Invalid("pick one of your date fields", "key")
            at = str(s.get("at") or "09:00")
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                raise Invalid("time: HH:MM", "at")
            out.append({"t": t, "key": s["key"], "offset": _int(s.get("offset"), -365, 365, "offset", 0),
                        "yearly": bool(s.get("yearly")) and s.get("yearly") not in ("0", "false"), "at": at})
        else:
            c = s.get("cond") or {}
            kind = c.get("kind")
            if kind not in CONDS:
                raise Invalid("pick what to check", "cond")
            cond: dict = {"kind": kind}
            if kind in ("tag", "source"):
                v = str(c.get("value") or "").strip().lower()[:60]
                if not v:
                    raise Invalid("write what it must be", "cond")
                cond["value"] = v
            elif kind == "field":
                f = next((x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == c.get("key")), None)
                if not f:
                    raise Invalid("pick one of your fields", "cond")
                v = str(c.get("value") or "").strip()[:60]
                cond.update(key=f["key"], value=CF.as_text(CF.clean_value(biz, f, v)) if v else "")
            elif kind == "channel":
                if c.get("value") not in ("whatsapp", "sms", "email", "messenger", "instagram", "telegram", "viber"):
                    raise Invalid("pick a channel", "cond")
                cond["value"] = c["value"]
            elif kind == "replied":
                cond["hours"] = _int(c.get("hours"), 0, 24 * 90, "hours", 0)       # 0 = since the run started
            out.append({"t": "if", "cond": cond, "yes": clean_steps(conn, biz, s.get("yes"), depth + 1),
                        "no": clean_steps(conn, biz, s.get("no"), depth + 1)})
    if depth == 0:
        if not out:
            raise Invalid("add at least one step", "steps")
        if _count(out) > MAX_STEPS:
            raise Invalid(f"at most {MAX_STEPS} steps", "steps")
    return out


def _clean_trigger(conn: Connection, biz: dict, trig: str, a: dict) -> dict:
    from . import fields as CF
    if trig not in TRIGGERS:
        raise Invalid("pick what sets it off", "trigger")
    a = a or {}
    if trig in ("deal.stage", "lead.new", "booking.made", "form.submitted", "field.changed", "contact.quiet"):
        return RC._clean(conn, biz, {"trigger": trig, "trigger_arg": a,
                                     "actions": [{"do": "notify"}]})["trigger_arg"]
    if trig == "tag.added":
        tag = str(a.get("tag") or "").strip().lower()[:60]
        if not tag:
            raise Invalid("write the tag", "tag")
        return {"tag": tag}
    if trig == "contact.inactive":
        return {"days": _int(a.get("days"), 7, 730, "days", 60)}
    if trig == "date.field":
        f = next((x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == a.get("key") and x["kind"] == "date"), None)
        if not f:
            raise Invalid("pick one of your date fields", "key")
        return {"key": f["key"], "offset": _int(a.get("offset"), -30, 30, "offset", 0)}
    return {}


def save(conn: Connection, biz: dict, actor: str, data: dict, wf_id: UUID | None = None) -> dict:
    trig = data.get("trigger")
    vals = {"name": " ".join(str(data.get("name") or "").split())[:160], "trigger": trig,
            "trigger_arg": _clean_trigger(conn, biz, trig, data.get("trigger_arg") or {}),
            "steps": clean_steps(conn, biz, data.get("steps")),
            "goal": data.get("goal") if data.get("goal") in GOALS else None}
    if wf_id:
        get(conn, biz["id"], wf_id)
        conn.execute(update(W).where(W.c.business_id == biz["id"], W.c.id == wf_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "workflow.updated", actor, wf_id, {"trigger": trig})
        return get(conn, biz["id"], wf_id)
    if conn.execute(select(func.count()).where(W.c.business_id == biz["id"])).scalar() >= MAX_WORKFLOWS:
        raise Invalid(f"at most {MAX_WORKFLOWS} workflows", "name")
    row = conn.execute(insert(W).values(business_id=biz["id"], created_by=actor, active=bool(data.get("active")),
                                        template=(data.get("template") or None), recipe_id=data.get("recipe_id"), **vals)
                       .returning(W)).mappings().one()
    log(conn, biz["id"], "workflow.created", actor, row["id"], {"trigger": trig})
    return dict(row)


def set_active(conn: Connection, biz: dict, actor: str, wf_id: UUID, on: bool) -> dict:
    get(conn, biz["id"], wf_id)
    conn.execute(update(W).where(W.c.business_id == biz["id"], W.c.id == wf_id).values(active=bool(on), updated_at=func.now()))
    if not on:                                               # off: the customers still in it stop where they are
        conn.execute(update(WR).where(WR.c.workflow_id == wf_id, WR.c.status.in_(("running", "waiting")))
                     .values(status="stopped", wake_at=None, updated_at=func.now()))
    log(conn, biz["id"], "workflow.on" if on else "workflow.off", actor, wf_id)
    return get(conn, biz["id"], wf_id)


def delete(conn: Connection, biz: dict, actor: str, wf_id: UUID) -> None:
    get(conn, biz["id"], wf_id)
    conn.execute(W.delete().where(W.c.business_id == biz["id"], W.c.id == wf_id))
    log(conn, biz["id"], "workflow.deleted", actor, wf_id)


def runs(conn: Connection, business_id: UUID, *, workflow_id: UUID | None = None, contact_id: UUID | None = None,
         limit: int = 40) -> list[dict]:
    C = db.contacts
    q = (select(WR, W.c.name.label("workflow_name"), C.c.name.label("contact_name"))
         .join(W, W.c.id == WR.c.workflow_id).outerjoin(C, C.c.id == WR.c.contact_id).where(WR.c.business_id == business_id))
    if workflow_id:
        q = q.where(WR.c.workflow_id == workflow_id)
    if contact_id:
        q = q.where(WR.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(WR.c.created_at.desc()).limit(max(1, min(limit, 200)))).mappings()]


def line_text(line: dict, lang: str) -> str:
    return RC.line_text(line, lang)


# ---------------------------------------------------------------- the gallery
def _date_fields(conn: Connection, biz: dict) -> list[dict]:
    from . import fields as CF
    return [f for f in CF.list_(conn, biz["id"], "contact") if f["kind"] == "date"]


def templates(conn: Connection, biz: dict) -> list[dict]:
    """Ready-made workflows, in the business's language, ready to open and switch on."""
    L = biz["language"]
    dates = _date_fields(conn, biz)
    bday = next((f for f in dates if re.search(r"birth|cumple|anivers|nasc|geburt|naissance|kaarawan|ulang|जन्म|ميلاد|誕生|生日",
                                               f["label"], re.I)), dates[0] if dates else None)
    out = [
        {"key": "lead_nurture", "trigger": "lead.new", "trigger_arg": {}, "goal": "booked", "steps": [
            {"t": "message", "text": _t("wf_tx_welcome", L), "kind": "service", "ask": True},
            {"t": "wait", "amount": 1, "unit": "days"},
            {"t": "if", "cond": {"kind": "replied", "hours": 0}, "yes": [], "no": [
                {"t": "message", "text": _t("wf_tx_checkin", L), "kind": "service", "ask": True},
                {"t": "wait", "amount": 2, "unit": "days"},
                {"t": "if", "cond": {"kind": "replied", "hours": 0}, "yes": [],
                 "no": [{"t": "task", "title": _t("wf_tx_call", L), "hours": 0}]}]}]},
        {"key": "no_show", "trigger": "booking.no_show", "trigger_arg": {}, "goal": "booked", "steps": [
            {"t": "wait", "amount": 30, "unit": "minutes"},
            {"t": "message", "text": _t("wf_tx_noshow", L), "kind": "service", "ask": True},
            {"t": "wait", "amount": 1, "unit": "days"},
            {"t": "if", "cond": {"kind": "replied", "hours": 0}, "yes": [],
             "no": [{"t": "task", "title": _t("wf_tx_noshow_call", L), "hours": 0}]}]},
        {"key": "review", "trigger": "booking.done", "trigger_arg": {}, "goal": None, "needs": None if biz.get("review_url") else "review",
         "steps": [{"t": "wait", "amount": 2, "unit": "hours"},
                   {"t": "message", "text": _t("wf_tx_review", L), "kind": "service", "ask": True}]},
        {"key": "birthday", "trigger": "date.field", "trigger_arg": {"key": bday["key"], "offset": 0} if bday else {},
         "goal": None, "needs": None if bday else "date_field",
         "steps": [{"t": "message", "text": _t("wf_tx_birthday", L), "kind": "marketing", "ask": True}]},
        {"key": "winback", "trigger": "contact.inactive", "trigger_arg": {"days": 60}, "goal": "booked", "steps": [
            {"t": "message", "text": _t("wf_tx_winback", L), "kind": "marketing", "ask": True},
            {"t": "wait", "amount": 7, "unit": "days"},
            {"t": "if", "cond": {"kind": "replied", "hours": 0}, "yes": [],
             "no": [{"t": "notify", "text": _t("wf_tx_winback_note", L)}]}]},
    ]
    for p in out:
        p.setdefault("needs", None)
        p["name"] = _t(f"wf_tpl_{p['key']}", L)
    return out


def from_template(conn: Connection, biz: dict, actor: str, key: str) -> dict:
    p = next((x for x in templates(conn, biz) if x["key"] == key), None)
    if not p:
        raise NotFound("template")
    if p["needs"]:
        raise Invalid(_t(f"wf_needs_{p['needs']}", biz["language"]), "needs")
    return save(conn, biz, actor, {"name": p["name"], "trigger": p["trigger"], "trigger_arg": p["trigger_arg"], "steps": p["steps"],
                                   "goal": p["goal"], "template": key, "active": False})


def from_recipe(conn: Connection, biz: dict, actor: str, recipe_id: UUID) -> dict:
    """"Open this recipe as a workflow": the same trigger, its "only if" as an if-step, its actions as steps.
    The recipe is switched off (so the two never both run); the workflow starts switched on if the recipe was."""
    r = RC.get(conn, biz["id"], recipe_id)
    acts = []
    for a in r["actions"] or []:
        st = {"t": a["do"], **{k: v for k, v in a.items() if k != "do"}}
        if a["do"] == "message":
            st.update(kind="service", ask=r["ask_first"])
        acts.append(st)
    c = r["condition"] or {}
    steps = [{"t": "if", "cond": {"kind": c["kind"], "value": c.get("value"), **({"key": c["key"]} if c.get("key") else {})},
              "yes": acts, "no": []}] if c.get("kind") else acts
    name = r["name"] or RC.describe(conn, biz, r, biz["language"])
    wf = save(conn, biz, actor, {"name": name[:160], "trigger": r["trigger"], "trigger_arg": r["trigger_arg"], "steps": steps,
                                 "active": r["active"], "recipe_id": r["id"]})
    if r["active"]:
        RC.set_active(conn, biz, actor, recipe_id, False)
    return wf


# ---------------------------------------------------------------- moving through the steps
def at(steps: list, ptr: list):
    cur, i = steps, 0
    try:
        while True:
            step = cur[ptr[i]]
            if i + 1 >= len(ptr):
                return step
            cur, i = step[ptr[i + 1]], i + 2
    except (IndexError, KeyError, TypeError):
        return None


def after(steps: list, ptr: list) -> list | None:
    """The pointer of the step after this one (out of a branch when it ends); None = the end."""
    p = list(ptr)
    while p:
        p[-1] += 1
        if at(steps, p) is not None:
            return p
        if len(p) == 1:
            return None
        p = p[:-2]
    return None


def enter(steps: list, ptr: list, branch: str) -> list | None:
    inner = ptr + [branch, 0]
    return inner if at(steps, inner) is not None else after(steps, ptr)


def _reached(conn: Connection, wf: dict, run: dict) -> bool:
    goal, cid = wf.get("goal"), run.get("contact_id")
    if not goal or not cid:
        return False
    since = run["created_at"]
    if goal == "booked":
        AP = db.appointments
        return conn.execute(select(AP.c.id).where(AP.c.business_id == wf["business_id"], AP.c.contact_id == cid,
                                                  AP.c.created_at > since, AP.c.status != "cancelled")).first() is not None
    if goal == "paid":
        PY = db.payments
        return conn.execute(select(PY.c.id).where(PY.c.business_id == wf["business_id"], PY.c.contact_id == cid,
                                                  PY.c.status == "paid", PY.c.paid_at > since)).first() is not None
    return _replied(conn, wf["business_id"], cid, since)


def _replied(conn: Connection, business_id: UUID, contact_id: UUID, since: datetime) -> bool:
    M, CV = db.messages, db.conversations
    return conn.execute(select(M.c.id).join(CV, CV.c.id == M.c.conversation_id).where(
        CV.c.business_id == business_id, CV.c.contact_id == contact_id, M.c.direction == "in", M.c.created_at > since)
        .limit(1)).first() is not None


def _cond(conn: Connection, biz: dict, cond: dict, contact: dict | None, run: dict, now: datetime) -> bool:
    from . import fields as CF
    if not contact:
        return False
    k = cond.get("kind")
    if k == "tag":
        return cond.get("value") in (contact.get("tags") or [])
    if k == "source":
        return (cond.get("value") or "") in (contact.get("source") or "").lower()
    if k == "field":
        v = (contact.get("custom") or {}).get(cond.get("key"))
        if not cond.get("value"):
            return v not in (None, "")
        return v is not None and CF.as_text(v).lower() == str(cond["value"]).lower()
    if k == "channel":
        I = db.identities
        chans = {r[0] for r in conn.execute(select(I.c.channel).where(I.c.business_id == biz["id"], I.c.contact_id == contact["id"]))}
        want = {"whatsapp": {"whatsapp", "whatsapp_twilio"}}.get(cond.get("value"), {cond.get("value")})
        if not chans & want:
            return False
        from .followups import _pick_account
        acc = _pick_account(conn, biz, contact["id"])
        ch = conn.execute(select(db.channel_accounts.c.channel).where(db.channel_accounts.c.id == acc)).scalar() if acc else None
        return ch in want if ch else True
    if k == "replied":
        since = now - timedelta(hours=cond["hours"]) if cond.get("hours") else run["created_at"]
        return _replied(conn, biz["id"], contact["id"], since)
    return False


def _until(biz: dict, step: dict, contact: dict | None, now: datetime) -> datetime | None:
    """When "wait until <date field>" ends: None = it has passed (go on now)."""
    from . import fields as CF
    raw = ((contact or {}).get("custom") or {}).get(step["key"])
    if not raw:
        return None
    try:
        d = date.fromisoformat(CF.as_text(raw)[:10])
    except ValueError:
        return None
    tz = ZoneInfo(biz["timezone"])
    hh, mm = (int(x) for x in step.get("at", "09:00").split(":"))
    today = now.astimezone(tz).date()

    def moment(day: date) -> datetime:
        return datetime.combine(day + timedelta(days=step.get("offset") or 0), time(hh, mm), tz).astimezone(timezone.utc)
    target = moment(d)
    if step.get("yearly"):
        for y in (today.year, today.year + 1):
            try:
                cand = moment(d.replace(year=y))
            except ValueError:                                # 29 February in a short year: the 28th
                cand = moment(date(y, 2, 28))
            if cand > now:
                return cand
        return None
    return target if target > now else None


def _ptr_key(ptr: list) -> str:
    return ".".join(str(p) for p in ptr)


def advance(conn: Connection, biz: dict, wf: dict, run: dict, now: datetime) -> dict:
    """Run steps from the run's pointer until a wait, the end, the goal or the guard. Returns the run's new values."""
    steps = wf["steps"] or []
    ptr = list(run["pointer"] or [0])
    done = run["steps_done"] or 0
    lines = list(run["history"] or [])
    contact = RC._row(conn, db.contacts, biz["id"], run["contact_id"])
    deal = RC._row(conn, db.deals, biz["id"], run["deal_id"])
    me = f"workflow:{wf['id']}"
    status, wake = "running", None

    def note(k: str, **p):
        lines.append({"at": now.isoformat(), "k": k, "p": p})
    while True:
        if _reached(conn, wf, run):
            status = "goal"
            note("wf_l_goal", goal=_t(f"wf_goal_{wf['goal']}", biz["language"]))
            break
        step = at(steps, ptr) if ptr else None
        if step is None:
            status = "done"
            note("wf_l_done")
            break
        if done >= MAX_RUN_STEPS:
            status = "loop"
            note("rc_l_loop")
            break
        if not contact:
            status = "stopped"
            note("rc_l_no_contact")
            break
        done += 1
        t = step["t"]
        if t == "wait":
            wake = now + timedelta(minutes=step["amount"] * UNITS[step["unit"]])
            note("wf_l_wait", n=step["amount"], unit=_t(f"wf_unit_{step['unit']}", biz["language"]))
            ptr = after(steps, ptr)
            status = "waiting" if ptr is not None else "done"
            if ptr is None:
                wake = None
            break
        if t == "wait_until":
            when = _until(biz, step, contact, now)
            nxt = after(steps, ptr)
            if when and nxt is not None:
                wake, ptr, status = when, nxt, "waiting"
                note("wf_l_until", day=when.astimezone(ZoneInfo(biz["timezone"])).date().isoformat())
                break
            ptr = nxt
            note("wf_l_until_now")
            if ptr is None:
                status = "done"
                break
            continue
        if t == "if":
            ok = _cond(conn, biz, step["cond"], contact, run, now)
            note("wf_l_if_yes" if ok else "wf_l_if_no")
            ptr = enter(steps, ptr, "yes" if ok else "no")
            if ptr is None:
                status = "done"
                note("wf_l_done")
                break
            continue
        try:
            with conn.begin_nested():
                line, contact, deal = _act(conn, biz, wf, step, contact, deal, f"wf:{run['id']}:{_ptr_key(ptr)}", now, me)
        except Exception as e:                               # noqa: BLE001 — written down; the run stops, the event stands
            why = str(e) if isinstance(e, (Invalid, NotFound)) else type(e).__name__
            note("rc_l_failed", why=why[:200])
            status = "failed"
            break
        lines.append({"at": now.isoformat(), **line})
        ptr = after(steps, ptr)
        if ptr is None:
            status = "done"
            note("wf_l_done")
            break
    return {"status": status, "pointer": ptr or [], "wake_at": wake, "steps_done": done, "history": lines[-100:],
            "deal_id": deal["id"] if deal else run["deal_id"], "updated_at": now}


def _act(conn: Connection, biz: dict, wf: dict, step: dict, contact: dict | None, deal: dict | None, dedupe: str,
         now: datetime, me: str) -> tuple[dict, dict | None, dict | None]:
    from . import contacts as C
    from . import fields as CF
    t = step["t"]
    if t == "untag":
        if step["tag"] in (contact["tags"] or []):
            contact = C.patch(conn, biz, me, contact["id"], {"tags": [x for x in contact["tags"] if x != step["tag"]]})
        return {"k": "wf_l_untagged", "p": {"tag": step["tag"]}}, contact, deal
    if t == "field":
        contact = C.patch(conn, biz, me, contact["id"], {"custom": {step["key"]: step.get("value") or None}})
        f = next((x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == step["key"]), None)
        return {"k": "wf_l_field", "p": {"field": f["label"] if f else "—", "value": step.get("value") or "—"}}, contact, deal
    r = {"id": wf["id"], "business_id": wf["business_id"], "ask_first": step.get("ask", True)}
    a = {"do": t, **{k: v for k, v in step.items() if k != "t"}}
    return RC._do(conn, biz, r, a, contact, deal, {}, dedupe, now, me=me)


# ---------------------------------------------------------------- starting runs
def _looped(conn: Connection, wf: dict, subject_id, actor: str, now: datetime) -> bool:
    depth = conn.info.get(_DEPTH, 0)
    if depth >= RC.MAX_DEPTH:
        return True
    return actor.startswith(("workflow:", "recipe:")) and conn.execute(select(WR.c.id).where(
        WR.c.workflow_id == wf["id"], WR.c.subject_id == subject_id, WR.c.status != "loop",
        WR.c.created_at > now - timedelta(hours=LOOP_HOURS))).first() is not None


def start(conn: Connection, wf: dict, *, contact_id, deal_id=None, subject_id=None, dedupe: str, actor: str,
          now: datetime | None = None) -> str | None:
    """Start one run (once per dedupe) and take its first steps. -> its status, or None (not started)."""
    now = now or datetime.now(timezone.utc)
    bid = wf["business_id"]
    subject_id = subject_id or contact_id
    inside = bool(contact_id) and conn.execute(select(WR.c.id).where(WR.c.workflow_id == wf["id"], WR.c.contact_id == contact_id,
                                                                      WR.c.status.in_(("running", "waiting")))).first() is not None
    if inside and not actor.startswith(("workflow:", "recipe:")):
        return None                                          # already in it: never twice at once
    if inside or _looped(conn, wf, subject_id, actor, now):  # set off by itself (or a chain too long): stopped, and written down
        conn.execute(pg_insert(WR).values(business_id=bid, workflow_id=wf["id"], contact_id=contact_id, deal_id=deal_id,
                                          subject_id=subject_id, dedupe=dedupe[:120], status="loop", pointer=[],
                                          history=[{"at": now.isoformat(), "k": "rc_l_loop", "p": {}}])
                     .on_conflict_do_nothing(constraint="uq_workflow_run"))
        return "loop"
    run = conn.execute(pg_insert(WR).values(business_id=bid, workflow_id=wf["id"], contact_id=contact_id, deal_id=deal_id,
                                            subject_id=subject_id, dedupe=dedupe[:120], status="running", pointer=[0],
                                            created_at=now, updated_at=now)
                       .on_conflict_do_nothing(constraint="uq_workflow_run").returning(WR)).mappings().first()
    if not run:
        return None                                          # this already started it
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
    depth = conn.info.get(_DEPTH, 0)
    conn.info[_DEPTH] = depth + 1
    try:
        vals = advance(conn, biz, wf, dict(run), now)
    finally:
        conn.info[_DEPTH] = depth
    conn.execute(update(WR).where(WR.c.id == run["id"]).values(**vals))
    log(conn, bid, "workflow.started", actor if actor.startswith("user:") else "system", contact_id, {"workflow_id": str(wf["id"])})
    return vals["status"]


def start_by_hand(conn: Connection, biz: dict, actor: str, wf_id: UUID, contact_id: UUID) -> str | None:
    from . import contacts as C
    wf = get(conn, biz["id"], wf_id)
    C.get(conn, biz["id"], contact_id)
    return start(conn, wf, contact_id=contact_id, dedupe=f"hand:{contact_id}:{datetime.now(timezone.utc).isoformat()}", actor=actor)


def _matches(wf: dict, data: dict) -> bool:
    a, trig = wf["trigger_arg"] or {}, wf["trigger"]
    if trig == "tag.added":
        return a.get("tag") in (data.get("tags_added") or [])
    if trig in ("deal.stage", "form.submitted", "field.changed"):
        return RC._matches({"trigger": trig, "trigger_arg": a}, data)
    return True


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict, actor: str,
             event_id: int) -> None:
    """Called by services.log() for every event, inside its transaction: goals reached, workflows started."""
    if not subject_id:
        return
    goal = GOAL_EVENT.get(type_)
    if goal:
        _goal(conn, business_id, type_, subject_id, data, goal)
    trig = EVENT_TRIGGER.get(type_)
    if not trig or (trig == "tag.added" and not data.get("tags_added")):
        return
    if trig == "lead.new" and (actor or "").startswith(NOT_LEADS):
        return
    rows = conn.execute(select(W).where(W.c.business_id == business_id, W.c.active, W.c.trigger == trig)
                        .order_by(W.c.created_at)).mappings().all()
    if not rows or not blocks.on(blocks.state(conn, business_id), "followups"):
        return
    if trig in ("booking.done", "booking.no_show"):
        ap = conn.execute(select(db.appointments.c.contact_id, db.appointments.c.deal_id).where(
            db.appointments.c.id == subject_id)).first()
        contact_id, deal_id = (ap[0], ap[1]) if ap else (None, None)
    elif trig == "payment.paid":
        pr = conn.execute(select(db.payments.c.contact_id, db.payments.c.deal_id).where(db.payments.c.id == subject_id)).first()
        contact_id, deal_id = (pr[0], pr[1]) if pr else (None, None)
    elif trig == "tag.added":
        contact_id, deal_id = subject_id, None
    else:
        contact_id, deal_id = RC._who_for(conn, business_id, trig, subject_id, data)
    if not contact_id:
        return
    for wf in rows:
        if _matches(dict(wf), data):
            with conn.begin_nested():
                start(conn, dict(wf), contact_id=contact_id, deal_id=deal_id, subject_id=subject_id, dedupe=f"ev:{event_id}",
                      actor=actor or "system")


def _goal(conn: Connection, business_id: UUID, type_: str, subject_id: UUID, data: dict, goal: str) -> None:
    if type_ == "message.received":
        cid = data.get("contact_id")
    elif type_ == "appointment.booked":
        cid = conn.execute(select(db.appointments.c.contact_id).where(db.appointments.c.id == subject_id)).scalar()
    else:
        cid = conn.execute(select(db.payments.c.contact_id).where(db.payments.c.id == subject_id)).scalar()
    if not cid:
        return
    live = select(W.c.id).where(W.c.business_id == business_id, W.c.goal == goal)
    now = datetime.now(timezone.utc)
    for rid, hist in conn.execute(select(WR.c.id, WR.c.history).where(
            WR.c.business_id == business_id, WR.c.contact_id == UUID(str(cid)), WR.c.status.in_(("running", "waiting")),
            WR.c.workflow_id.in_(live))).all():
        conn.execute(update(WR).where(WR.c.id == rid).values(
            status="goal", wake_at=None, updated_at=now,
            history=(hist or []) + [{"at": now.isoformat(), "k": "wf_l_goal_reached", "p": {}}]))


# ---------------------------------------------------------------- the job
def round_(now: datetime | None = None, limit: int = 200) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"woke": 0, "started": 0}
    with db.tx() as conn:
        pick = (select(WR.c.id).where(WR.c.status == "waiting", WR.c.wake_at <= now).order_by(WR.c.wake_at).limit(limit)
                .with_for_update(skip_locked=True).scalar_subquery())
        ids = [r[0] for r in conn.execute(update(WR).where(WR.c.id.in_(pick), WR.c.status == "waiting")
                                          .values(status="running", updated_at=now).returning(WR.c.id))]
    for rid in ids:
        with db.tx() as conn:
            run = dict(conn.execute(select(WR).where(WR.c.id == rid)).mappings().one())
            wf = conn.execute(select(W).where(W.c.id == run["workflow_id"])).mappings().first()
            if not wf or not wf["active"] or not blocks.on(blocks.state(conn, run["business_id"]), "followups"):
                conn.execute(update(WR).where(WR.c.id == rid).values(status="stopped", wake_at=None))
                continue
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == run["business_id"])).mappings().one())
            vals = advance(conn, biz, dict(wf), run, now)
            conn.execute(update(WR).where(WR.c.id == rid).values(**vals))
            stats["woke"] += 1
    stats["started"] += _time_triggers(now)
    return stats


def _time_triggers(now: datetime) -> int:
    """Workflows started by time: a date field (each year), no visit in N days, no reply for N hours."""
    from . import fields as CF
    n = 0
    with db.tx() as conn:
        wfs = [dict(r) for r in conn.execute(select(W).where(W.c.active, W.c.trigger.in_(
            ("date.field", "contact.inactive", "contact.quiet")))).mappings()]
    for wf in wfs:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == wf["business_id"])).mappings().one())
            if not blocks.on(biz, "followups"):
                continue
            tz = ZoneInfo(biz["timezone"])
            local = now.astimezone(tz)
            a = wf["trigger_arg"] or {}
            C = db.contacts
            if wf["trigger"] == "date.field":
                if local.hour < 9:
                    continue                                 # birthdays go in the morning, the business's time
                day = local.date() - timedelta(days=a.get("offset") or 0)
                mmdd = day.strftime("-%m-%d")
                rows = conn.execute(select(C.c.id).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None),
                                                         C.c.custom[a.get("key")].astext.like(f"%{mmdd}"))).all()
                for (cid,) in rows:
                    with conn.begin_nested():
                        st = start(conn, wf, contact_id=cid, dedupe=f"date:{a.get('key')}:{day.year}:{cid}", actor="system", now=now)
                    n += st is not None
            elif wf["trigger"] == "contact.inactive":
                if local.hour < 10:
                    continue
                from .growth import _seen
                seen = _seen(biz["id"])
                days = int(a.get("days") or 60)
                rows = conn.execute(select(C.c.id).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None),
                                                         seen < now - timedelta(days=days),
                                                         seen > now - timedelta(days=days + 30)).limit(100)).all()
                for (cid,) in rows:
                    with conn.begin_nested():
                        st = start(conn, wf, contact_id=cid, dedupe=f"inactive:{cid}:{local.date().isoformat()[:7]}", actor="system",
                                   now=now)
                    n += st is not None
            else:
                hours = int(a.get("hours") or 2)
                CV, M = db.conversations, db.messages
                answered = exists().where(M.c.conversation_id == CV.c.id, M.c.direction == "out", M.c.created_at > CV.c.last_inbound_at)
                rows = conn.execute(select(CV.c.id, CV.c.contact_id, CV.c.last_inbound_at).where(
                    CV.c.business_id == biz["id"], CV.c.status == "open", CV.c.last_inbound_at.isnot(None),
                    CV.c.last_inbound_at <= now - timedelta(hours=hours),
                    CV.c.last_inbound_at > now - timedelta(hours=hours + RC.QUIET_LOOKBACK_HOURS), ~answered).limit(200)).all()
                for cv_id, cid, last in rows:
                    with conn.begin_nested():
                        st = start(conn, wf, contact_id=cid, dedupe=f"quiet:{cv_id}:{int(last.timestamp())}", actor="system", now=now)
                    n += st is not None
    return n


# ---------------------------------------------------------------- in words (the list, the gallery)
def describe_trigger(conn: Connection, biz: dict, wf: dict, lang: str) -> str:
    a, trig = wf.get("trigger_arg") or {}, wf["trigger"]
    if trig in RC.TRIGGERS:
        return RC._sentence(conn, biz, {"trigger": trig, "trigger_arg": a, "condition": {}, "actions": []}, lang).split(" → ")[0]
    if trig == "tag.added":
        return _t("wf_t_tag.added", lang).format(tag=a.get("tag") or "—")
    if trig == "contact.inactive":
        return _t("wf_t_contact.inactive", lang).format(days=a.get("days") or 60)
    if trig == "date.field":
        return _t("wf_t_date.field", lang).format(field=RC._field_label(conn, biz, a.get("key")))
    return _t(f"wf_t_{trig}", lang)


def flat(steps: list) -> list[dict]:
    """Every step, branches included, in order (for counting and the read-only view)."""
    out = []
    for s in steps or []:
        out.append(s)
        if s.get("t") == "if":
            out += flat(s.get("yes") or []) + flat(s.get("no") or [])
    return out


def editor_data(conn: Connection, biz: dict, lang: str) -> dict:
    """What the builder's dropdowns offer: the business's own stages, fields, team, sequences."""
    from . import fields as CF
    from . import sequences as SQ
    from . import team as TM
    from ..web.i18n import _load, t
    ip = packs.industry(biz["industry"])
    keys = [k for k in _load()["en"] if k.startswith("wf_e_")]
    return {"stages": [{"key": s.key, "label": blocks.word(biz, f"stage:{s.key}", lang)} for s in ip.stages],
            "fields": [{"key": f["key"], "label": f["label"], "kind": f["kind"], "options": f["options"]}
                       for f in CF.list_(conn, biz["id"], "contact")],
            "team": [{"id": str(m["id"]), "name": m.get("name") or m.get("email")} for m in TM.members(conn, biz["id"])],
            "sequences": [{"id": str(s["id"]), "name": s["name"]} for s in SQ.list_(conn, biz["id"])],
            "words": {k: t(k, lang) for k in keys}}
