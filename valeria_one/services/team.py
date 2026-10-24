"""Team: the owner invites people by link; staff answer the inbox and work customers, deals and the calendar,
while settings, channels, keys, AI, booking setup, webhooks and the team stay with owners."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import crypto, db
from . import Invalid, NotFound, log, users

INV, MB, U = db.invitations, db.memberships, db.users
INVITE_DAYS = 7


def invite(conn: Connection, biz: dict, actor: str, email: str, role: str = "staff") -> tuple[dict, str]:
    """-> (invitation, token for the link — shown once)."""
    e = users._email(email)
    if role not in ("owner", "staff"):
        raise Invalid("role is owner or staff", "role")
    already = conn.execute(select(MB.c.user_id).join(U, U.c.id == MB.c.user_id)
                           .where(MB.c.business_id == biz["id"], func.lower(U.c.email) == e)).first()
    if already:
        raise Invalid("that person is already on the team", "email")
    conn.execute(update(INV).where(INV.c.business_id == biz["id"], func.lower(INV.c.email) == e,
                                   INV.c.accepted_at.is_(None), INV.c.revoked_at.is_(None)).values(revoked_at=func.now()))
    token = secrets.token_urlsafe(24)
    row = conn.execute(insert(INV).values(business_id=biz["id"], email=e, role=role, token_hash=crypto.sha256(token),
                                          expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS))
                       .returning(INV)).mappings().one()
    log(conn, biz["id"], "team.invited", actor, row["id"], {"role": role})
    return _pub(row), token


def _pub(row) -> dict:
    d = dict(row)
    d.pop("token_hash", None)
    return d


def find(conn: Connection, token: str) -> dict:
    row = conn.execute(select(INV, db.businesses.c.name.label("business_name")).join(db.businesses, db.businesses.c.id == INV.c.business_id)
                       .where(INV.c.token_hash == crypto.sha256(token or ""))).mappings().first()
    if not row or row["accepted_at"] or row["revoked_at"] or row["expires_at"] < datetime.now(timezone.utc):
        raise NotFound("invitation")
    return dict(row)


def accept(conn: Connection, token: str, user: dict) -> dict:
    inv = find(conn, token)
    if users._email(user["email"]) != inv["email"]:
        raise Invalid(f"this invitation is for {inv['email']} — sign in with that email", "email")
    if not users.membership(conn, user["id"], inv["business_id"]):
        users.add_member(conn, inv["business_id"], user["id"], inv["role"])
    conn.execute(update(INV).where(INV.c.id == inv["id"]).values(accepted_at=func.now()))
    log(conn, inv["business_id"], "team.joined", f"user:{user['id']}", user["id"], {"role": inv["role"]})
    return inv


def join_new_user(conn: Connection, token: str, name: str, password: str) -> tuple[dict, dict]:
    """Someone without an account accepts: their account is created with the invited email."""
    inv = find(conn, token)
    if conn.execute(select(U.c.id).where(func.lower(U.c.email) == inv["email"])).first():
        raise Invalid("you already have an account — sign in to accept", "email")
    if len(password or "") < 10:
        raise Invalid("the password needs at least 10 characters", "password")
    user = conn.execute(insert(U).values(email=inv["email"], name=(name or "").strip()[:200],
                                         password_hash=users._ph.hash(password)).returning(U)).mappings().one()
    accept(conn, token, dict(user))
    return dict(user), inv


def members(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(U.c.id, U.c.email, U.c.name, U.c.last_login_at, MB.c.role, MB.c.created_at)
                                          .join(MB, MB.c.user_id == U.c.id).where(MB.c.business_id == business_id)
                                          .order_by(MB.c.created_at)).mappings()]


def pending(conn: Connection, business_id: UUID) -> list[dict]:
    return [_pub(r) for r in conn.execute(select(INV).where(INV.c.business_id == business_id, INV.c.accepted_at.is_(None),
                                                            INV.c.revoked_at.is_(None), INV.c.expires_at > func.now())
                                          .order_by(INV.c.created_at.desc())).mappings()]


def revoke(conn: Connection, business_id: UUID, actor: str, invitation_id: UUID) -> None:
    if not conn.execute(update(INV).where(INV.c.business_id == business_id, INV.c.id == invitation_id,
                                          INV.c.accepted_at.is_(None), INV.c.revoked_at.is_(None)).values(revoked_at=func.now())).rowcount:
        raise NotFound("invitation")
    log(conn, business_id, "team.invite_revoked", actor, invitation_id)


def remove(conn: Connection, business_id: UUID, actor: str, user_id: UUID) -> None:
    m = users.membership(conn, user_id, business_id)
    if not m:
        raise NotFound("member")
    if m["role"] == "owner":
        owners = conn.execute(select(func.count()).select_from(MB).where(MB.c.business_id == business_id, MB.c.role == "owner")).scalar()
        if owners <= 1:
            raise Invalid("a business always keeps at least one owner", "user")
    conn.execute(MB.delete().where(MB.c.business_id == business_id, MB.c.user_id == user_id))
    log(conn, business_id, "team.removed", actor, user_id)
