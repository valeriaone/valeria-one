"""Drip sequences (v0.18): a few messages, one after another with waits, for one customer at a time.

  "Welcome" — when someone sends my form: now a thank-you, 2 days later a tip, 5 days later an offer.

How a customer gets in: a trigger (a new lead, a form sent, a tag added, a booking made), the recipe/workflow action
"add to sequence", or the owner by hand. A customer goes through a sequence once (UNIQUE(sequence, contact)).
How they get out: they reply, they book, or they unsubscribe (each switchable; unsubscribe always stops a
marketing sequence) — and at the end.

Each step is queued into the same outbox as campaigns (scheduled_messages, UNIQUE(enrollment, step): never twice)
by the job, when its wait is over, and sent by the same code (services/campaigns.deliver): consent, STOP, polite
hours, WhatsApp's template rule and the frequency caps all apply. A "service" sequence (about something the
customer asked for, e.g. the quote they requested) needs no marketing consent and has no frequency cap — but it
still stops when they reply or say STOP. Off (or the Follow-ups block off) = nothing new goes out.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, db
from . import Invalid, NotFound, log

S, E, SM = db.sequences, db.sequence_enrollments, db.scheduled_messages
TRIGGERS = ("manual", "lead.new", "form.submitted", "tag.added", "booking.made")
EXITS = ("reply", "booking", "unsubscribe")
KINDS = ("marketing", "service")
STEP_CHANNELS = ("auto", "email", "sms", "whatsapp")
MAX_STEPS = 10
MAX_SEQUENCES = 30
MAX_WAIT_MIN = 60 * 24 * 90
UNITS = {"minutes": 1, "hours": 60, "days": 1440}
NOT_LEADS = ("user:", "demo_visitor", "import", "recipe:", "workflow:", "valeria", "seed")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(S).where(S.c.business_id == business_id).order_by(S.c.created_at)).mappings()]
    counts = dict(conn.execute(select(E.c.sequence_id, func.count()).where(E.c.business_id == business_id, E.c.status == "active")
                               .group_by(E.c.sequence_id)).all())
    for r in rows:
        r["in_now"] = counts.get(r["id"], 0)
    return rows


def get(conn: Connection, business_id: UUID, seq_id: UUID) -> dict:
    r = conn.execute(select(S).where(S.c.business_id == business_id, S.c.id == seq_id)).mappings().first()
    if not r:
        raise NotFound("sequence")
    return dict(r)


def _wait(step: dict) -> int:
    try:
        amount = int(str(step.get("amount", step.get("wait", 0)) or 0))
    except ValueError:
        raise Invalid("wait: a whole number", "steps") from None
    mins = amount * UNITS.get(step.get("unit") or "minutes", 1) if "amount" in step else amount
    if not 0 <= mins <= MAX_WAIT_MIN:
        raise Invalid("wait: up to 90 days", "steps")
    return mins


def clean_steps(raw) -> list[dict]:
    from .campaigns import clean_blocks, links_of
    out = []
    for s in (raw or [])[:MAX_STEPS + 1]:
        ch = s.get("channel") or "auto"
        if ch not in STEP_CHANNELS:
            raise Invalid("pick a channel", "steps")
        st = {"wait": _wait(s), "channel": ch, "text": str(s.get("text") or "").strip()[:1600],
              "subject": " ".join(str(s.get("subject") or "").split())[:200], "blocks": clean_blocks(s.get("blocks")),
              "template": str(s.get("template") or "").strip()[:100] or None}
        if not (st["text"] or st["blocks"]):
            continue
        if ch == "email" and not st["subject"]:
            raise Invalid("an email needs a subject", "steps")
        if s.get("design_id"):                               # v0.21: made from a saved email design (emails.step_from)
            st["design_id"] = str(s["design_id"])[:36]
        st["links"] = links_of(st)
        out.append(st)
    if not out:
        raise Invalid("add at least one message", "steps")
    if len(out) > MAX_STEPS:
        raise Invalid(f"at most {MAX_STEPS} messages", "steps")
    return out


def save(conn: Connection, biz: dict, actor: str, data: dict, seq_id: UUID | None = None) -> dict:
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise Invalid("give it a name", "name")
    kind = data.get("kind") or "marketing"
    if kind not in KINDS:
        raise Invalid("marketing or service", "kind")
    trig = data.get("trigger") or "manual"
    if trig not in TRIGGERS:
        raise Invalid("pick what starts it", "trigger")
    a = data.get("trigger_arg") or {}
    arg: dict = {}
    if trig == "tag.added":
        tag = str(a.get("tag") or "").strip().lower()[:60]
        if not tag:
            raise Invalid("write the tag", "tag")
        arg["tag"] = tag
    if trig == "form.submitted" and a.get("form_id"):
        try:
            fid = UUID(str(a["form_id"]))
        except ValueError:
            raise Invalid("pick one of your forms", "form_id") from None
        if not conn.execute(select(db.forms.c.id).where(db.forms.c.business_id == biz["id"], db.forms.c.id == fid)).scalar():
            raise Invalid("pick one of your forms", "form_id")
        arg["form_id"] = str(fid)
    exits = sorted({x for x in (data.get("exit_on") or []) if x in EXITS})
    if kind == "marketing" and "unsubscribe" not in exits:
        exits.append("unsubscribe")                          # an unsubscribe always ends offers
    vals = {"name": name, "kind": kind, "trigger": trig, "trigger_arg": arg, "steps": clean_steps(data.get("steps")),
            "exit_on": sorted(exits)}
    if seq_id:
        get(conn, biz["id"], seq_id)
        conn.execute(update(S).where(S.c.business_id == biz["id"], S.c.id == seq_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "sequence.updated", actor, seq_id, {"trigger": trig})
        return get(conn, biz["id"], seq_id)
    if conn.execute(select(func.count()).where(S.c.business_id == biz["id"])).scalar() >= MAX_SEQUENCES:
        raise Invalid(f"at most {MAX_SEQUENCES} sequences", "name")
    row = conn.execute(insert(S).values(business_id=biz["id"], created_by=actor, active=bool(data.get("active")), **vals)
                       .returning(S)).mappings().one()
    log(conn, biz["id"], "sequence.created", actor, row["id"], {"trigger": trig})
    return dict(row)


def set_active(conn: Connection, biz: dict, actor: str, seq_id: UUID, on: bool) -> dict:
    get(conn, biz["id"], seq_id)
    conn.execute(update(S).where(S.c.business_id == biz["id"], S.c.id == seq_id).values(active=bool(on), updated_at=func.now()))
    log(conn, biz["id"], "sequence.on" if on else "sequence.off", actor, seq_id)
    return get(conn, biz["id"], seq_id)


def delete(conn: Connection, biz: dict, actor: str, seq_id: UUID) -> None:
    get(conn, biz["id"], seq_id)
    conn.execute(update(SM).where(SM.c.business_id == biz["id"], SM.c.status == "pending", SM.c.enrollment_id.in_(
        select(E.c.id).where(E.c.sequence_id == seq_id))).values(status="skipped", error="sequence deleted"))
    conn.execute(S.delete().where(S.c.business_id == biz["id"], S.c.id == seq_id))
    log(conn, biz["id"], "sequence.deleted", actor, seq_id)


def enrollments(conn: Connection, business_id: UUID, seq_id: UUID, limit: int = 50) -> list[dict]:
    C = db.contacts
    return [dict(r) for r in conn.execute(select(E, C.c.name.label("contact_name")).join(C, C.c.id == E.c.contact_id)
                                          .where(E.c.business_id == business_id, E.c.sequence_id == seq_id)
                                          .order_by(E.c.created_at.desc()).limit(limit)).mappings()]


def of_contact(conn: Connection, business_id: UUID, contact_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(E, S.c.name.label("sequence_name")).join(S, S.c.id == E.c.sequence_id)
                                          .where(E.c.business_id == business_id, E.c.contact_id == contact_id)
                                          .order_by(E.c.created_at.desc())).mappings()]


# ---------------------------------------------------------------- in and out
def enroll(conn: Connection, biz_or_id, seq: dict, contact_id: UUID, by: str, now: datetime | None = None) -> dict | None:
    """Put a customer in a sequence (once ever). None = already in it (or was), or it can't be for them."""
    now = now or datetime.now(timezone.utc)
    bid = seq["business_id"]
    c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == bid, db.contacts.c.id == contact_id,
                                               db.contacts.c.deleted_at.is_(None))).mappings().first()
    if not c:
        return None
    if seq["kind"] == "marketing" and (c["unsubscribed_at"] or not c["marketing_consent"]):
        return None                                          # offers only to those who said yes
    first = (seq["steps"] or [{}])[0]
    row = conn.execute(pg_insert(E).values(business_id=bid, sequence_id=seq["id"], contact_id=contact_id, step=0,
                                           next_at=now + timedelta(minutes=int(first.get("wait") or 0)), entered_by=by[:100])
                       .on_conflict_do_nothing(constraint="uq_enrollment").returning(E)).mappings().first()
    if row:
        log(conn, bid, "sequence.entered", by, contact_id, {"sequence_id": str(seq["id"])})
    return dict(row) if row else None


def stop(conn: Connection, business_id: UUID, enrollment_id: UUID, reason: str, actor: str) -> bool:
    r = conn.execute(update(E).where(E.c.business_id == business_id, E.c.id == enrollment_id, E.c.status == "active")
                     .values(status="stopped" if reason == "owner" else "exited", exit_reason=reason[:20], updated_at=func.now())
                     .returning(E.c.contact_id)).first()
    if not r:
        return False
    conn.execute(update(SM).where(SM.c.enrollment_id == enrollment_id, SM.c.status == "pending").values(status="skipped", error=reason))
    log(conn, business_id, "sequence.left", actor, r[0], {"why": reason})
    return True


def _exit(conn: Connection, business_id: UUID, contact_id: UUID, why: str) -> None:
    rows = conn.execute(select(E.c.id, S.c.exit_on, S.c.kind).join(S, S.c.id == E.c.sequence_id).where(
        E.c.business_id == business_id, E.c.contact_id == contact_id, E.c.status == "active")).all()
    for eid, exit_on, kind in rows:
        if why in (exit_on or []) or (why == "unsubscribe" and kind == "marketing"):
            stop(conn, business_id, eid, why, "system")


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict, actor: str) -> None:
    """Called by services.log() for every event, inside its transaction: who gets in, who gets out."""
    if not subject_id:
        return
    exits = {"message.received": "reply", "appointment.booked": "booking", "contact.unsubscribed": "unsubscribe"}
    if type_ in exits:
        cid = subject_id
        if type_ == "message.received":
            cid = data.get("contact_id")
        elif type_ == "appointment.booked":
            cid = conn.execute(select(db.appointments.c.contact_id).where(db.appointments.c.id == subject_id)).scalar()
        if cid:
            _exit(conn, business_id, UUID(str(cid)), exits[type_])
    trig = {"contact.created": "lead.new", "form.submitted": "form.submitted", "appointment.booked": "booking.made",
            "contact.updated": "tag.added"}.get(type_)
    if not trig:
        return
    if trig == "lead.new" and (actor or "").startswith(NOT_LEADS):
        return
    if trig == "tag.added" and not data.get("tags_added"):
        return
    rows = conn.execute(select(S).where(S.c.business_id == business_id, S.c.active, S.c.trigger == trig)).mappings().all()
    if not rows or not blocks.on(blocks.state(conn, business_id), "followups"):
        return
    cid = subject_id
    if trig == "booking.made":
        cid = conn.execute(select(db.appointments.c.contact_id).where(db.appointments.c.id == subject_id)).scalar()
    if not cid:
        return
    for seq in rows:
        a = seq["trigger_arg"] or {}
        if trig == "form.submitted" and a.get("form_id") and a["form_id"] != str(data.get("form_id")):
            continue
        if trig == "tag.added" and a.get("tag") not in (data.get("tags_added") or []):
            continue
        with conn.begin_nested():
            enroll(conn, business_id, dict(seq), cid, f"sequence:{trig}")


# ---------------------------------------------------------------- the job: due steps into the outbox
def round_(now: datetime | None = None, limit: int = 300) -> dict:
    from .campaigns import new_token
    now = now or datetime.now(timezone.utc)
    stats = {"queued": 0, "done": 0}
    with db.tx() as conn:
        due = conn.execute(select(E).where(E.c.status == "active", E.c.next_at <= now).order_by(E.c.next_at).limit(limit)
                           .with_for_update(skip_locked=True)).mappings().all()
        for en in due:
            seq = conn.execute(select(S).where(S.c.id == en["sequence_id"])).mappings().one()
            if not seq["active"] or not blocks.on(blocks.state(conn, en["business_id"]), "followups"):
                continue                                    # paused: it waits where it is
            steps = seq["steps"] or []
            if en["step"] >= len(steps):
                conn.execute(update(E).where(E.c.id == en["id"]).values(status="done", next_at=None, updated_at=func.now()))
                stats["done"] += 1
                continue
            st = steps[en["step"]]
            conn.execute(pg_insert(SM).values(business_id=en["business_id"], enrollment_id=en["id"], step=en["step"],
                                              contact_id=en["contact_id"], send_at=now, token=new_token(),
                                              channel=None if st.get("channel") == "auto" else st.get("channel"))
                         .on_conflict_do_nothing(constraint="uq_sched_sequence"))
            nxt = en["step"] + 1
            if nxt >= len(steps):
                conn.execute(update(E).where(E.c.id == en["id"]).values(step=nxt, status="done", next_at=None, updated_at=func.now()))
                stats["done"] += 1
            else:
                conn.execute(update(E).where(E.c.id == en["id"]).values(
                    step=nxt, next_at=now + timedelta(minutes=int(steps[nxt].get("wait") or 0)), updated_at=func.now()))
            stats["queued"] += 1
    return stats


def send_row(row: dict, biz: dict, contact: dict, now: datetime, http) -> tuple:
    from . import campaigns as CG
    with db.tx() as conn:
        en = conn.execute(select(E).where(E.c.id == row["enrollment_id"])).mappings().first()
        seq = conn.execute(select(S).where(S.c.id == en["sequence_id"])).mappings().first() if en else None
    if not en or not seq:
        return "skipped", "sequence deleted", None, None
    if en["status"] in ("exited", "stopped"):
        return "skipped", en["exit_reason"] or "left", None, None
    steps = seq["steps"] or []
    if row["step"] is None or row["step"] >= len(steps):
        return "skipped", "step removed", None, None
    st = steps[row["step"]]
    content = {"channel": None if st.get("channel") == "auto" else st.get("channel"), "text": st.get("text") or "",
               "subject": st.get("subject") or "", "blocks": st.get("blocks") or [], "template": st.get("template"),
               "links": st.get("links") or []}
    return CG.deliver(row, biz, contact, content, kind=seq["kind"], track_opens=False, http=http, now=now, actor="sequence")


def after_send(row: dict, status: str, why: str | None) -> None:
    """A marketing step blocked for consent (they unsubscribed meanwhile) takes them out; the rest carry on."""
    if status == "skipped" and why in ("no_consent", "unsubscribed"):
        with db.tx() as conn:
            stop(conn, row["business_id"], row["enrollment_id"], "unsubscribe", "system")


def step_links(conn: Connection, enrollment_id: UUID, step: int | None) -> list[str]:
    seq = conn.execute(select(S.c.steps).join(E, E.c.sequence_id == S.c.id).where(E.c.id == enrollment_id)).scalar()
    if seq is None or step is None or step >= len(seq):
        return []
    return seq[step].get("links") or []
