"""Businesses and their API keys."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import insert, select, update, func
from sqlalchemy.engine import Connection

from .. import blocks, crypto, db, packs
from . import Invalid, NotFound, log


def create(conn: Connection, *, name: str, country: str, industry: str = "generic", language: str | None = None,
           timezone: str | None = None, sandbox: bool = False, demo: bool = False) -> dict:
    name = (name or "").strip()
    if not name:
        raise Invalid("name is required", "name")
    try:
        cp = packs.country(country)
    except KeyError:
        raise Invalid(f"unknown country {country!r}; see GET /v1/packs/countries", "country") from None
    try:
        ip = packs.industry(industry)
    except KeyError:
        raise Invalid(f"unknown industry {industry!r}; see GET /v1/packs/industries", "industry") from None
    lang = language or cp.languages[0]
    if demo and not sandbox:
        raise Invalid("a public demo must be a sandbox business", "demo")
    tz = timezone or cp.timezone
    _check_tz(tz)
    row = conn.execute(insert(db.businesses).values(
        name=name[:200], country=cp.code, industry=ip.code, language=lang, timezone=tz,
        currency=cp.currency, sandbox=sandbox, demo=demo).returning(db.businesses)).mappings().one()
    from . import billing
    billing.on_create(conn, row["id"], sandbox)                 # its reference; a trial where the server charges
    log(conn, row["id"], "business.created", "system", row["id"], {"country": cp.code, "industry": ip.code})
    return get(conn, row["id"])


def _check_tz(tz: str) -> None:
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(tz)
    except Exception:
        raise Invalid(f"unknown time zone {tz!r}", "timezone") from None


def get(conn: Connection, business_id: UUID) -> dict:
    row = conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().first()
    if not row:
        raise NotFound("business")
    return dict(row)


def patch(conn: Connection, business_id: UUID, actor: str, **fields) -> dict:
    allowed = {k: v for k, v in fields.items() if k in ("name", "language", "timezone", "industry") and v is not None}
    if "industry" in allowed:
        try:
            packs.industry(allowed["industry"])
        except KeyError:
            raise Invalid(f"unknown industry {allowed['industry']!r}", "industry") from None
    if "timezone" in allowed:
        _check_tz(allowed["timezone"])
    if "name" in allowed and not allowed["name"].strip():
        raise Invalid("name is required", "name")
    if allowed:
        conn.execute(update(db.businesses).where(db.businesses.c.id == business_id)
                     .values(**allowed, updated_at=func.now()))
        log(conn, business_id, "business.updated", actor, business_id, {"fields": sorted(allowed)})
    return get(conn, business_id)


def set_blocks(conn: Connection, business_id: UUID, actor: str, enabled) -> dict:
    """Switch blocks on (those in `enabled`) and off (the rest). Switching follow-ups off also cancels the
    messages and campaigns waiting to go out, so nothing is sent by a part the owner turned off."""
    want = set(enabled or ())
    unknown = want - set(blocks.BLOCKS)
    if unknown:
        raise Invalid(f"unknown block {sorted(unknown)[0]!r}; blocks are {', '.join(blocks.BLOCKS)}", "blocks")
    biz = get(conn, business_id)
    features = {b: b in want for b in blocks.BLOCKS}
    before = {b: blocks.on(biz, b) for b in blocks.BLOCKS}
    conn.execute(update(db.businesses).where(db.businesses.c.id == business_id).values(features=features, updated_at=func.now()))
    if before["followups"] and not features["followups"]:
        SM, CP = db.scheduled_messages, db.campaigns
        conn.execute(update(SM).where(SM.c.business_id == business_id, SM.c.status == "pending")
                     .values(status="skipped", error="follow-ups switched off"))
        conn.execute(update(CP).where(CP.c.business_id == business_id, CP.c.status.in_(("scheduled", "sending")))
                     .values(status="cancelled"))
    changed = sorted(b for b in blocks.BLOCKS if before[b] != features[b])
    log(conn, business_id, "business.blocks", actor, business_id,
        {"on": sorted(want), "off": sorted(set(blocks.BLOCKS) - want), "changed": changed})
    return get(conn, business_id)


def set_words(conn: Connection, business_id: UUID, actor: str, words: dict) -> dict:
    """What the business calls things: only the words given change; an empty value goes back to the pack's word."""
    biz = get(conn, business_id)
    allowed = blocks.word_keys(biz["industry"])
    cur = dict(biz["words"] or {})
    for k, v in (words or {}).items():
        if k not in allowed:
            raise Invalid(f"unknown word {k!r}", "words")
        v = " ".join(str(v or "").split())
        if len(v) > blocks.WORD_MAX:
            raise Invalid(f"keep words under {blocks.WORD_MAX} characters", "words")
        if v:
            cur[k] = v
        else:
            cur.pop(k, None)
    conn.execute(update(db.businesses).where(db.businesses.c.id == business_id).values(words=cur, updated_at=func.now()))
    log(conn, business_id, "business.words", actor, business_id, {"words": sorted(cur)})
    return get(conn, business_id)


def new_key(conn: Connection, business_id: UUID, name: str = "default") -> tuple[str, dict]:
    """-> (the full key — shown ONCE, never stored —, the stored row)."""
    biz = get(conn, business_id)
    key, prefix, h = crypto.new_api_key(biz["sandbox"])
    row = conn.execute(insert(db.api_keys).values(business_id=business_id, name=name[:100], prefix=prefix, key_hash=h)
                       .returning(db.api_keys.c.id, db.api_keys.c.prefix, db.api_keys.c.name, db.api_keys.c.created_at)
                       ).mappings().one()
    log(conn, business_id, "api_key.created", "system", row["id"], {"prefix": prefix})
    return key, dict(row)


def by_key(conn: Connection, key: str) -> tuple[dict, dict] | None:
    """API key -> (business, key row), or None if unknown or revoked."""
    if not key or not key.startswith(("vo_live_", "vo_test_")):
        return None
    k = conn.execute(select(db.api_keys).where(db.api_keys.c.key_hash == crypto.sha256(key),
                                               db.api_keys.c.revoked_at.is_(None))).mappings().first()
    if not k:
        return None
    conn.execute(update(db.api_keys).where(db.api_keys.c.id == k["id"]).values(last_used_at=func.now()))
    return get(conn, k["business_id"]), dict(k)


def keys(conn: Connection, business_id: UUID) -> list[dict]:
    K = db.api_keys
    return [dict(r) for r in conn.execute(select(K.c.id, K.c.name, K.c.prefix, K.c.created_at, K.c.last_used_at, K.c.revoked_at)
                                          .where(K.c.business_id == business_id).order_by(K.c.created_at.desc())).mappings()]


def revoke_key(conn: Connection, business_id: UUID, actor: str, key_id: UUID) -> None:
    K = db.api_keys
    n = conn.execute(update(K).where(K.c.business_id == business_id, K.c.id == key_id, K.c.revoked_at.is_(None))
                     .values(revoked_at=func.now())).rowcount
    if not n:
        raise NotFound("api key")
    log(conn, business_id, "api_key.revoked", actor, key_id)
