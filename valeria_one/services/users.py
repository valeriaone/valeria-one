"""People who sign in to the web app, and which businesses they belong to."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, businesses, log

U, MB = db.users, db.memberships
_ph = PasswordHasher()
MAX_TRIES, LOCK_MINUTES = 5, 15
_DUMMY = _ph.hash("timing-equaliser")


def _email(v: str) -> str:
    e = (v or "").strip().lower()
    if not e or "@" not in e or "." not in e.split("@")[-1] or " " in e or len(e) > 320:
        raise Invalid("enter a valid email", "email")
    return e


def register(conn: Connection, *, email: str, name: str, password: str, business_name: str, country: str,
             industry: str, sandbox: bool = False) -> tuple[dict, dict]:
    """New owner + new business, in one go."""
    e = _email(email)
    if len(password or "") < 10:
        raise Invalid("the password needs at least 10 characters", "password")
    if conn.execute(select(U.c.id).where(func.lower(U.c.email) == e)).first():
        raise Invalid("there is already an account with this email — sign in instead", "email")
    biz = businesses.create(conn, name=business_name, country=country, industry=industry, sandbox=sandbox)
    user = conn.execute(insert(U).values(email=e, name=(name or "").strip()[:200], password_hash=_ph.hash(password))
                        .returning(U)).mappings().one()
    conn.execute(insert(MB).values(business_id=biz["id"], user_id=user["id"], role="owner"))
    log(conn, biz["id"], "user.joined", f"user:{user['id']}", user["id"], {"role": "owner"})
    return dict(user), biz


def authenticate(conn: Connection, email: str, password: str, now: datetime | None = None) -> dict | None:
    """The user, or None. Five wrong passwords lock the account for 15 minutes. Same timing whether or
    not the email exists, so nobody can probe which emails have accounts."""
    now = now or datetime.now(timezone.utc)
    try:
        e = _email(email)
    except Invalid:
        _verify(_DUMMY, password)
        return None
    u = conn.execute(select(U).where(func.lower(U.c.email) == e)).mappings().first()
    if not u:
        _verify(_DUMMY, password)
        return None
    if u["locked_until"] and u["locked_until"] > now:
        _verify(_DUMMY, password)
        return None
    if not _verify(u["password_hash"], password):
        n = u["failed_attempts"] + 1
        conn.execute(update(U).where(U.c.id == u["id"]).values(
            failed_attempts=0 if n >= MAX_TRIES else n,
            locked_until=now + timedelta(minutes=LOCK_MINUTES) if n >= MAX_TRIES else None))
        return None
    conn.execute(update(U).where(U.c.id == u["id"]).values(failed_attempts=0, locked_until=None, last_login_at=now))
    return dict(u)


def _verify(h: str, pw: str) -> bool:
    try:
        return _ph.verify(h, pw or "")
    except (VerificationError, InvalidHashError):
        return False


def get(conn: Connection, user_id: UUID) -> dict | None:
    r = conn.execute(select(U).where(U.c.id == user_id)).mappings().first()
    return dict(r) if r else None


def membership(conn: Connection, user_id: UUID, business_id: UUID) -> dict | None:
    r = conn.execute(select(MB).where(MB.c.user_id == user_id, MB.c.business_id == business_id)).mappings().first()
    return dict(r) if r else None


def businesses_of(conn: Connection, user_id: UUID) -> list[dict]:
    B = db.businesses
    return [dict(r) for r in conn.execute(select(B, MB.c.role).join(MB, MB.c.business_id == B.c.id)
                                          .where(MB.c.user_id == user_id).order_by(B.c.created_at)).mappings()]


def add_member(conn: Connection, business_id: UUID, user_id: UUID, role: str = "owner") -> None:
    conn.execute(insert(MB).values(business_id=business_id, user_id=user_id, role=role))


def set_password(conn: Connection, user_id: UUID, password: str) -> None:
    if len(password or "") < 10:
        raise Invalid("the password needs at least 10 characters", "password")
    conn.execute(update(U).where(U.c.id == user_id).values(password_hash=_ph.hash(password), failed_attempts=0, locked_until=None))


# ---------------------------------------------------------------- forgot my password
RESET_MINUTES = 60
PR = db.password_resets


def start_reset(conn: Connection, email: str) -> tuple[dict, str] | None:
    """-> (user, token for the link) or None when there is no such account. The caller says the same thing
    either way, so nobody can find out which emails have accounts. Three links an hour at most."""
    import secrets
    from .. import crypto
    try:
        e = _email(email)
    except Invalid:
        return None
    u = conn.execute(select(U).where(func.lower(U.c.email) == e)).mappings().first()
    if not u:
        return None
    recent = conn.execute(select(func.count()).select_from(PR).where(
        PR.c.user_id == u["id"], PR.c.created_at > func.now() - timedelta(hours=1))).scalar()
    if recent >= 3:
        return None
    token = secrets.token_urlsafe(32)
    conn.execute(insert(PR).values(user_id=u["id"], token_hash=crypto.sha256(token),
                                   expires_at=datetime.now(timezone.utc) + timedelta(minutes=RESET_MINUTES)))
    return dict(u), token


def reset_user(conn: Connection, token: str) -> dict | None:
    from .. import crypto
    r = conn.execute(select(PR).where(PR.c.token_hash == crypto.sha256(token or ""), PR.c.used_at.is_(None),
                                      PR.c.expires_at > func.now())).mappings().first()
    return get(conn, r["user_id"]) if r else None


def finish_reset(conn: Connection, token: str, password: str) -> dict:
    from .. import crypto
    r = conn.execute(select(PR).where(PR.c.token_hash == crypto.sha256(token or ""), PR.c.used_at.is_(None),
                                      PR.c.expires_at > func.now()).with_for_update()).mappings().first()
    if not r:
        raise Invalid("this link has expired or was already used — ask for a new one", "token")
    set_password(conn, r["user_id"], password)
    conn.execute(update(PR).where(PR.c.user_id == r["user_id"], PR.c.used_at.is_(None)).values(used_at=func.now()))
    u = get(conn, r["user_id"])
    for m in conn.execute(select(MB.c.business_id).where(MB.c.user_id == u["id"])):
        log(conn, m[0], "user.password_reset", f"user:{u['id']}", u["id"])
    return u
