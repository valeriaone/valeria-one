"""Blocks: the parts of Valeria One a business switches on.

Today, Inbox and Customers are the core and always on. Everything else is a block, and every block is on
until the business switches it off, so a business only sees, and only runs, what it uses. Switching a block
off hides it and stops what it does by itself (automatic replies, reminders, public pages). The data stays,
and switching it back on brings everything back. The public API is not limited by blocks.

What a business calls things lives here too: the industry pack gives the default words and pipeline stage
names in every language; a business may replace any of them with its own word.
"""
from __future__ import annotations

from . import packs

BLOCKS = ("calendar", "pipeline", "payments", "quotes", "followups", "website", "comments", "ai")
WORD_MAX = 40


def on(biz: dict, name: str) -> bool:
    """Is this block working for this business? Never while the business is paused by its host."""
    if name not in BLOCKS:
        raise KeyError(name)
    if biz.get("suspended_at"):
        return False
    if (biz.get("features") or {}).get(name, True) is False:
        return False
    from .services import billing
    return billing.allows(biz, name)                       # on a server that charges: what the plan includes


def state(conn, business_id) -> dict:
    """Just what on() needs, for code that has only the business id."""
    from sqlalchemy import select

    from . import db
    B = db.businesses
    r = conn.execute(select(B.c.features, B.c.suspended_at, B.c.plan, B.c.trial_ends_at, B.c.paid_until, B.c.billing_exempt,
                            B.c.sandbox).where(B.c.id == business_id)).mappings().first()
    return dict(r) if r else {"features": {}, "suspended_at": None}


def chosen(biz: dict) -> bool:
    """Has the owner picked their blocks yet (rather than living with the defaults)?"""
    return bool(biz.get("features"))


def word_keys(industry: str) -> list[str]:
    """The words a business can rename: the pack's words, then one per pipeline stage ("stage:<key>")."""
    ip = packs.industry(industry)
    return sorted(ip.words) + [f"stage:{s.key}" for s in ip.stages]


def word(biz: dict, key: str, lang: str) -> str:
    """What this business calls `key`, for a reader in `lang`: its own word if it set one, else the pack's."""
    own = (biz.get("words") or {}).get(key)
    if own:
        return own
    ip = packs.industry(biz["industry"])
    if key.startswith("stage:"):
        st = ip.stage(key[6:])
        return packs.text(st.label, lang) if st else key[6:]
    return packs.text(ip.words[key], lang)
