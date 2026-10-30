"""Memberships, members and the community (ars02, v0.21).

A membership is something the business offers — "Gold members", "The 30-day plan", "Clients' club" — free, or paid
through one of its OWN plans (services/invoices.py: a recurring invoice with the business's own pay link; Valeria One
never holds the money). A customer becomes a member only by the owner's yes: the owner adds them, or a visitor asks
to join from the member area and the owner says yes (then, for a paid one, their subscription starts the usual way).

The member area lives at the business's own address /s/<business>/members and wears its look. Members sign in with a
one-time link (no password to forget): they type their email; if they are an active member, a link valid LINK_HOURS is
sent through the business's OWN email account (it is the member who asked: nothing is sent to anyone who didn't) — or,
when the business has none connected, through the server's own email in the business's name, replies going to the
owner (mem01) — and when neither can send, the owner hears that this member asked (push + Memberships) and shares the
link with one tap. The owner can also copy a member's link and share it by hand (WhatsApp…). Only the link's hash is kept, and it works once.
A signed-in member sees only that business's area: the session says which business and which customer, and every page
checks both.

Community: each membership can have its board — members post and comment; the owner posts as the business and hides
anything (hidden = kept, not shown; one tap shows it again). Nothing is deleted.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, NotFound, log

MP, MB, ML = db.member_plans, db.members, db.member_links
CP, CC = db.community_posts, db.community_comments
C = db.contacts
LINK_HOURS = 24                   # a link sent by email
SHARE_DAYS = 7                    # a link the owner copies and shares by hand
MAX_POSTS_DAY = 20                # per member, posts + comments (a bot or a quarrel never floods the board)
STATUSES = ("pending", "active", "paused", "removed")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- memberships
def plans(conn: Connection, business_id: UUID, active_only: bool = False) -> list[dict]:
    q = select(MP).where(MP.c.business_id == business_id)
    if active_only:
        q = q.where(MP.c.active)
    rows = [dict(r) for r in conn.execute(q.order_by(MP.c.created_at)).mappings()]
    n = dict(conn.execute(select(MB.c.member_plan_id, func.count()).where(MB.c.business_id == business_id, MB.c.status == "active")
                          .group_by(MB.c.member_plan_id)).all())
    w = dict(conn.execute(select(MB.c.member_plan_id, func.count()).where(MB.c.business_id == business_id, MB.c.status == "pending")
                          .group_by(MB.c.member_plan_id)).all())
    prices = {p["id"]: p for p in conn.execute(select(db.plans).where(db.plans.c.business_id == business_id)).mappings()}
    for r in rows:
        r.update(active_n=n.get(r["id"], 0), waiting=w.get(r["id"], 0), price=dict(prices[r["plan_id"]]) if r["plan_id"] in prices else None)
    return rows


def plan(conn: Connection, business_id: UUID, mp_id: UUID) -> dict:
    r = conn.execute(select(MP).where(MP.c.business_id == business_id, MP.c.id == mp_id)).mappings().first()
    if not r:
        raise NotFound("membership")
    return dict(r)


def save_plan(conn: Connection, biz: dict, actor: str, data: dict, mp_id: UUID | None = None) -> dict:
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise Invalid("give the membership a name", "name")
    pid = data.get("plan_id") or None
    if pid:
        try:
            pid = UUID(str(pid))
        except ValueError:
            raise Invalid("pick one of your plans", "plan_id") from None
        if not conn.execute(select(db.plans.c.id).where(db.plans.c.business_id == biz["id"], db.plans.c.id == pid)).first():
            raise Invalid("pick one of your plans", "plan_id")
    vals = {"name": name, "about": str(data.get("about") or "").strip()[:4000], "plan_id": pid,
            "community": bool(data.get("community")), "open_join": bool(data.get("open_join")), "active": bool(data.get("active", True))}
    if mp_id:
        plan(conn, biz["id"], mp_id)
        conn.execute(update(MP).where(MP.c.business_id == biz["id"], MP.c.id == mp_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "membership.updated", actor, mp_id)
        return plan(conn, biz["id"], mp_id)
    if not biz.get("slug"):                                    # the member area needs the business's public address
        from .funnels import _need_slug
        _need_slug(conn, biz, actor)
    row = conn.execute(insert(MP).values(business_id=biz["id"], **vals).returning(MP)).mappings().one()
    log(conn, biz["id"], "membership.created", actor, row["id"])
    return dict(row)


# ---------------------------------------------------------------- members
def members(conn: Connection, business_id: UUID, mp_id: UUID | None = None, status: str | None = None) -> list[dict]:
    q = (select(MB, C.c.name, C.c.email, C.c.phone, MP.c.name.label("plan_name"))
         .join(C, C.c.id == MB.c.contact_id).join(MP, MP.c.id == MB.c.member_plan_id).where(MB.c.business_id == business_id))
    if mp_id:
        q = q.where(MB.c.member_plan_id == mp_id)
    if status:
        q = q.where(MB.c.status == status)
    return [dict(r) for r in conn.execute(q.order_by(MB.c.status != "pending", C.c.name)).mappings()]


def get(conn: Connection, business_id: UUID, member_id: UUID) -> dict:
    r = conn.execute(select(MB).where(MB.c.business_id == business_id, MB.c.id == member_id)).mappings().first()
    if not r:
        raise NotFound("member")
    return dict(r)


def _start(conn: Connection, biz: dict, actor: str, m: dict, auto_send: bool) -> dict:
    """In: active from now; a paid membership starts its subscription (its invoices go the usual way)."""
    from . import invoices as IVS
    mp = plan(conn, biz["id"], m["member_plan_id"])
    sub = m["subscription_id"]
    if mp["plan_id"] and not sub:
        sub = IVS.subscribe(conn, biz, actor, plan_id=mp["plan_id"], contact_id=m["contact_id"], auto_send=auto_send)["id"]
    conn.execute(update(MB).where(MB.c.id == m["id"]).values(status="active", joined_at=func.coalesce(MB.c.joined_at, func.now()),
                                                             subscription_id=sub))
    log(conn, biz["id"], "member.joined", actor, m["contact_id"], {"member_id": str(m["id"]), "membership": mp["name"]})
    return get(conn, biz["id"], m["id"])


def add(conn: Connection, biz: dict, actor: str, mp_id: UUID, contact_id: UUID, auto_send: bool = True) -> dict:
    """The owner puts a customer in (or says yes to one who asked)."""
    from . import contacts
    plan(conn, biz["id"], mp_id)
    contacts.get(conn, biz["id"], contact_id)
    conn.execute(pg_insert(MB).values(business_id=biz["id"], member_plan_id=mp_id, contact_id=contact_id, status="pending",
                                      created_by=actor).on_conflict_do_nothing(constraint="uq_member"))
    m = dict(conn.execute(select(MB).where(MB.c.member_plan_id == mp_id, MB.c.contact_id == contact_id)).mappings().one())
    if m["status"] == "active":
        return m
    return _start(conn, biz, actor, m, auto_send)


def ask_to_join(conn: Connection, biz: dict, mp_id: UUID, *, name: str, email: str, phone: str = "", note: str = "") -> dict:
    """A visitor asks from the member area: a customer (found by email/phone or new) waiting for the owner's yes."""
    from . import site as ST
    mp = plan(conn, biz["id"], mp_id)
    if not (mp["active"] and mp["open_join"]):
        raise NotFound("membership")
    if not (email or "").strip():
        raise Invalid(_t("ms_need_email", biz["language"]), "email")
    res = ST.contact_form(conn, biz, name=name, email=email, phone=phone,
                          message=f"🔑 {mp['name']}" + (f"\n{note.strip()[:1000]}" if (note or "").strip() else ""),
                          source="members", event="lead.members")
    conn.execute(pg_insert(MB).values(business_id=biz["id"], member_plan_id=mp_id, contact_id=res["contact"]["id"], status="pending",
                                      created_by="member_area").on_conflict_do_nothing(constraint="uq_member"))
    m = dict(conn.execute(select(MB).where(MB.c.member_plan_id == mp_id, MB.c.contact_id == res["contact"]["id"])).mappings().one())
    if m["status"] == "pending":
        log(conn, biz["id"], "member.requested", "member_area", res["contact"]["id"], {"member_id": str(m["id"])})
    return {"member": m, "contact": res["contact"]}


def set_status(conn: Connection, biz: dict, actor: str, member_id: UUID, act: str) -> dict:
    """yes (a request) · pause · resume · remove. A paid one's subscription pauses / resumes / ends with it."""
    from . import invoices as IVS
    m = get(conn, biz["id"], member_id)
    if act == "yes" and m["status"] == "pending":
        return _start(conn, biz, actor, m, True)
    moves = {"pause": ("active", "paused", "pause"), "resume": ("paused", "active", "resume"), "remove": (None, "removed", "cancel")}
    if act not in moves:
        raise Invalid("?", "act")
    want, to, sub_act = moves[act]
    if (want and m["status"] != want) or m["status"] == "removed":
        raise Invalid(_t("ms_cant", biz["language"]), "status")
    conn.execute(update(MB).where(MB.c.id == member_id).values(status=to))
    if m["subscription_id"]:
        try:
            IVS.set_subscription(conn, biz, actor, m["subscription_id"], sub_act)
        except (Invalid, NotFound):
            pass                                               # already that way (the owner changed it on Invoices)
    log(conn, biz["id"], f"member.{act}", actor, m["contact_id"], {"member_id": str(member_id)})
    return get(conn, biz["id"], member_id)


def of_contact(conn: Connection, business_id: UUID, contact_id: UUID, active_only: bool = True) -> list[dict]:
    """The memberships a customer is in (active: their member area shows these)."""
    q = (select(MB, MP.c.name.label("plan_name"), MP.c.community, MP.c.about).join(MP, MP.c.id == MB.c.member_plan_id)
         .where(MB.c.business_id == business_id, MB.c.contact_id == contact_id))
    if active_only:
        q = q.where(MB.c.status == "active", MP.c.active)
    return [dict(r) for r in conn.execute(q.order_by(MP.c.created_at)).mappings()]


# ---------------------------------------------------------------- signing in with a one-time link
def _new_link(conn: Connection, biz: dict, m: dict, hours: int) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute(insert(ML).values(business_id=biz["id"], member_id=m["id"], token_hash=crypto.sha256(token),
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=hours)))
    return f"{config.PUBLIC_URL()}/s/{biz['slug']}/members/in/{token}"


def share_link(conn: Connection, biz: dict, actor: str, member_id: UUID) -> str:
    """The owner copies a member's sign-in link to send it by hand (works once, for SHARE_DAYS)."""
    m = get(conn, biz["id"], member_id)
    if m["status"] != "active":
        raise Invalid(_t("ms_not_active", biz["language"]), "status")
    log(conn, biz["id"], "member.link_shared", actor, m["contact_id"], {"member_id": str(member_id)})
    return _new_link(conn, biz, m, SHARE_DAYS * 24)


def send_link(biz: dict, email: str, http=None) -> bool:
    """A member typed their email on the sign-in page: if they are an active member, their link goes to that address
    through the business's OWN email account, else through the server's email in the business's name (mem01: a member
    of a business without email connected could never sign in alone). When nothing can send, the owner is told this
    member asked. -> sent. The page says the same either way (nobody learns who is a member)."""
    from . import campaigns as CG
    from . import inbox as X
    from . import mailer
    mail = (email or "").strip().lower()[:320]
    if "@" not in mail:
        return False
    with db.tx() as conn:
        row = conn.execute(select(MB, C.c.name).join(C, C.c.id == MB.c.contact_id).join(MP, MP.c.id == MB.c.member_plan_id).where(
            MB.c.business_id == biz["id"], MB.c.status == "active", MP.c.active, func.lower(C.c.email) == mail,
            C.c.deleted_at.is_(None)).order_by(MB.c.created_at)).mappings().first()
        if not row or biz.get("suspended_at"):
            return False
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = conn.execute(select(func.count()).select_from(ML).where(ML.c.member_id == row["id"], ML.c.created_at > since)).scalar()
        recent += conn.execute(select(func.count()).select_from(db.events).where(
            db.events.c.business_id == biz["id"], db.events.c.type == "member.link_asked", db.events.c.created_at > since,
            db.events.c.data["member_id"].astext == str(row["id"]))).scalar()
        if recent >= 3:                                        # a few a time: never a flood into someone's mailbox (or the owner's phone)
            return False
        acc = CG.ready_channels(conn, biz).get("email")
        server = not acc and mailer.configured()
        if acc or server:
            link = _new_link(conn, biz, dict(row), LINK_HOURS)
            owner_mail = None if acc else conn.execute(
                select(db.users.c.email).join(db.memberships, db.memberships.c.user_id == db.users.c.id).where(
                    db.memberships.c.business_id == biz["id"], db.memberships.c.role == "owner").order_by(db.memberships.c.created_at)).scalar()
        else:                                                  # nothing can send: the owner hears it and shares it by hand
            log(conn, biz["id"], "member.link_asked", "member", row["contact_id"], {"member_id": str(row["id"])})
    lang = biz["language"]
    if not (acc or server):
        from . import push
        push.notify(biz["id"], _t("ms_push_title", lang), _t("ms_push_body", lang).format(name=row["name"] or mail),
                    f"/app/members#m-{row['id']}", http=http)
        return False
    subject = _t("ms_mail_subject", lang).format(business=biz["name"])
    body = _t("ms_mail_body", lang).format(business=biz["name"], link=link, hours=LINK_HOURS)
    if server:
        return mailer.send(mail, subject, body, reply_to=owner_mail, from_name=biz["name"])
    import html as _h
    page = (f'<p style="font:16px/1.5 Arial,Helvetica,sans-serif">{_h.escape(body).replace(chr(10), "<br>")}</p>'
            f'<p><a href="{_h.escape(link)}" style="font:700 16px Arial,Helvetica,sans-serif">{_h.escape(_t("ms_sign_in", lang))}</a></p>')
    try:
        X.send(biz, "member_area", contact_id=row["contact_id"], body=body, kind="service", channel_account_id=acc, http=http,
               email={"subject": subject, "html": page})
    except Exception:                                          # noqa: BLE001 — the page never tells; the owner can share it by hand
        return False
    return True


def asked(conn: Connection, business_id: UUID, days: int = 3) -> list[dict]:
    """Active members who asked for their sign-in link when nothing could send it, and haven't been given one since."""
    E = db.events
    last: dict[str, datetime] = {}                            # member -> when they last asked (a few rows: at most 3 each per 10 min)
    for d, at in conn.execute(select(E.c.data, E.c.created_at).where(
            E.c.business_id == business_id, E.c.type == "member.link_asked",
            E.c.created_at > datetime.now(timezone.utc) - timedelta(days=days))):
        k = str((d or {}).get("member_id") or "")
        last[k] = max(at, last.get(k, at))
    out = []
    for k, at in last.items():
        try:
            mid = UUID(k)
        except ValueError:
            continue
        given = conn.execute(select(func.count()).select_from(ML).where(ML.c.member_id == mid, ML.c.created_at > at)).scalar()
        m = conn.execute(select(MB.c.id, MB.c.status, C.c.name, C.c.email).join(C, C.c.id == MB.c.contact_id).where(
            MB.c.id == mid, MB.c.business_id == business_id)).mappings().first()
        if m and m["status"] == "active" and not given:
            out.append({**dict(m), "at": at})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def use_link(slug: str, token: str) -> tuple[dict, dict]:
    """The link was opened: once, before it runs out, for an active member of THIS business. -> (business, member)."""
    if not token or len(token) > 80:
        raise NotFound("link")
    with db.tx() as conn:
        biz = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower()[:60])).mappings().first()
        if not biz or biz["suspended_at"]:
            raise NotFound("link")
        ln = conn.execute(select(ML).where(ML.c.token_hash == crypto.sha256(token), ML.c.business_id == biz["id"])
                          .with_for_update()).mappings().first()
        if not ln or ln["used_at"] or ln["expires_at"] < datetime.now(timezone.utc):
            raise NotFound("link")
        m = get(conn, biz["id"], ln["member_id"])
        if m["status"] != "active":
            raise NotFound("link")
        conn.execute(update(ML).where(ML.c.id == ln["id"]).values(used_at=func.now()))
        conn.execute(update(MB).where(MB.c.id == m["id"]).values(last_seen_at=func.now()))
    return dict(biz), m


def session_value(biz: dict, m: dict) -> dict:
    return {"b": str(biz["id"]), "c": str(m["contact_id"]), "at": datetime.now(timezone.utc).isoformat()}


def signed_in(conn: Connection, biz: dict, value) -> dict | None:
    """The member signed in at THIS business (from the session), with their active memberships — or None."""
    if not isinstance(value, dict) or value.get("b") != str(biz["id"]):
        return None
    try:
        cid = UUID(str(value.get("c")))
    except ValueError:
        return None
    mine = of_contact(conn, biz["id"], cid)
    if not mine:
        return None
    c = conn.execute(select(C.c.id, C.c.name).where(C.c.business_id == biz["id"], C.c.id == cid, C.c.deleted_at.is_(None))).mappings().first()
    if not c:
        return None
    return {"contact_id": cid, "name": c["name"], "memberships": mine, "plan_ids": {m["member_plan_id"] for m in mine},
            "member_ids": {m["member_plan_id"]: m["id"] for m in mine}, "joined": {m["member_plan_id"]: m["joined_at"] for m in mine}}


# ---------------------------------------------------------------- the community
def board(conn: Connection, biz: dict, mp_id: UUID, with_hidden: bool = False, limit: int = 50) -> list[dict]:
    """Posts, newest first, each with its comments (oldest first); authors by first name, or the business's name."""
    def who(r) -> str:
        return biz["name"] if r["by_business"] else ((r["author"] or "").split(" ")[0] or "—")
    q = (select(CP, C.c.name.label("author")).outerjoin(MB, MB.c.id == CP.c.member_id).outerjoin(C, C.c.id == MB.c.contact_id)
         .where(CP.c.business_id == biz["id"], CP.c.member_plan_id == mp_id))
    if not with_hidden:
        q = q.where(CP.c.hidden_at.is_(None))
    posts = [dict(r) for r in conn.execute(q.order_by(CP.c.created_at.desc()).limit(limit)).mappings()]
    ids = [p["id"] for p in posts]
    cq = (select(CC, C.c.name.label("author")).outerjoin(MB, MB.c.id == CC.c.member_id).outerjoin(C, C.c.id == MB.c.contact_id)
          .where(CC.c.business_id == biz["id"], CC.c.post_id.in_(ids or [None])))
    if not with_hidden:
        cq = cq.where(CC.c.hidden_at.is_(None))
    comments: dict = {}
    for r in conn.execute(cq.order_by(CC.c.created_at)).mappings():
        comments.setdefault(r["post_id"], []).append({**dict(r), "who": who(r)})
    for p in posts:
        p.update(who=who(p), comments=comments.get(p["id"], []))
    return posts


def _body(text: str) -> str:
    body = (text or "").strip()[:2000]
    if not body:
        raise Invalid("write something first", "body")
    return body


def _flood(conn: Connection, member_id: UUID) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    n = (conn.execute(select(func.count()).select_from(CP).where(CP.c.member_id == member_id, CP.c.created_at > since)).scalar()
         + conn.execute(select(func.count()).select_from(CC).where(CC.c.member_id == member_id, CC.c.created_at > since)).scalar())
    if n >= MAX_POSTS_DAY:
        raise Invalid("that's a lot for one day — try again tomorrow", "body")


def post(conn: Connection, biz: dict, mp_id: UUID, text: str, member_id: UUID | None, actor: str) -> dict:
    """A post on the board: by a member (member_id) or by the business (None: the owner)."""
    mp = plan(conn, biz["id"], mp_id)
    if not mp["community"]:
        raise NotFound("community")
    if member_id:
        m = get(conn, biz["id"], member_id)
        if m["status"] != "active" or m["member_plan_id"] != mp_id:
            raise NotFound("community")
        _flood(conn, member_id)
    row = conn.execute(insert(CP).values(business_id=biz["id"], member_plan_id=mp_id, member_id=member_id, by_business=not member_id,
                                         body=_body(text))
                       .returning(CP)).mappings().one()
    log(conn, biz["id"], "community.posted", actor, row["id"], {"membership": str(mp_id)})
    return dict(row)


def comment(conn: Connection, biz: dict, post_id: UUID, text: str, member_id: UUID | None, actor: str) -> dict:
    p = conn.execute(select(CP).where(CP.c.business_id == biz["id"], CP.c.id == post_id, CP.c.hidden_at.is_(None))).mappings().first()
    if not p:
        raise NotFound("post")
    if member_id:
        m = get(conn, biz["id"], member_id)
        if m["status"] != "active" or m["member_plan_id"] != p["member_plan_id"]:
            raise NotFound("post")
        _flood(conn, member_id)
    row = conn.execute(insert(CC).values(business_id=biz["id"], post_id=post_id, member_id=member_id, by_business=not member_id,
                                         body=_body(text))
                       .returning(CC)).mappings().one()
    return dict(row)


def hide(conn: Connection, biz: dict, actor: str, what: str, item_id: UUID, on: bool = True) -> None:
    """The owner hides a post or a comment (kept, not shown) — or shows it again."""
    T = CP if what == "post" else CC
    if not conn.execute(update(T).where(T.c.business_id == biz["id"], T.c.id == item_id).values(
            hidden_at=func.now() if on else None, hidden_by=actor if on else None)).rowcount:
        raise NotFound(what)
    log(conn, biz["id"], "community.hidden" if on else "community.shown", actor, item_id, {"what": what})
