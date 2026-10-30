"""Kits (kit01, v0.19): one toolbox, and each trade sees its own tools first.

Every tool stays in the app. The registry below is the one place that lists them — its menu address, icon, group, words
and what it needs — so a new tool only registers here. A kit (the `kit:` part of the industry pack, packs/industries/
<code>.yaml) says which tools start ON for that trade, in what menu order, and what the menu calls them (a gym's
"Customers" are "Members"). An industry without a kit gets the generic one; adding a trade = adding its YAML.

The owner's own choices sit on top (businesses.tools: {"invoices": true, "posts": false}) from "Add more" (/app/add).
OFF only HIDES a tool from the menu and Today: its data stays, its address still opens, one tap brings it back. What a
tool does by itself (replies, reminders, public pages) is the blocks' job (blocks.py) and is not touched by hiding it.

A business never loses what it uses: a tool that holds its data is pinned ON (the v0.19 migration did it for every
business that existed; changing the trade in Settings does it again — see keep_in_use).

Core (Today, Valeria, Connect, Inbox, Customers, Settings, Add more, Start, Billing, Server) is always there.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Connection

from . import blocks, db, packs

GROUPS = ("sell", "book", "talk", "market", "online", "work")


@dataclass(frozen=True)
class Tool:
    key: str
    href: str
    icon: str                                   # a name in templates/_icons.html
    group: str = ""                             # "" = core (always on)
    label: str = ""                             # locale key (default: the key itself, as the menu always said)
    needs: tuple[str, ...] = ()                 # blocks it needs switched on (blocks.py)
    owner: bool = False                         # only the owner sees it
    studio_hides: bool = False                  # the Studio door replaces it while the studio is on
    only: str = ""                              # core extras: "billing" (a server that charges) | "admin" (who runs the server)
    uses: tuple[str, ...] = ()                  # SQL (bound :b = business id): any row = the business uses this tool

    @property
    def core(self) -> bool:
        return not self.group

    @property
    def word(self) -> str:
        return self.label or self.key

    @property
    def about(self) -> str:
        return f"ft_{self.key}_d"


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} WHERE business_id = :b{' AND ' + where if where else ''}"


def _biz(where: str) -> str:
    return f"SELECT 1 FROM businesses WHERE id = :b AND ({where})"


# the menu as it was before kits, in its order; then the tools that lived inside other pages
REGISTRY: tuple[Tool, ...] = (
    Tool("today", "/app", "today"),
    Tool("valeria", "/app/valeria", "valeria", needs=("assistant",)),
    Tool("connect", "/app/connect", "connect", owner=True),
    Tool("inbox", "/app/inbox", "inbox"),
    Tool("contacts", "/app/contacts", "contacts"),
    Tool("studio", "/app/studio", "wand", "work", label="ft_studio", needs=("studio", "projects"),
         uses=(_biz("studio_host IS NOT NULL OR features->>'studio' = 'true' OR apps_ready <> '[]'::jsonb"),)),
    Tool("projects", "/app/projects", "projects", "work", needs=("projects",), studio_hides=True, uses=(_rows("projects"),)),
    Tool("tasks", "/app/tasks", "tasks", "work", uses=(_rows("tasks"),)),
    Tool("pipeline", "/app/pipeline", "pipeline", "sell", needs=("pipeline",), uses=(_rows("deals"),)),
    Tool("calendar", "/app/calendar", "calendar", "book", needs=("calendar",),
         uses=(_rows("appointments"), _rows("booking_services"), _biz("booking_enabled"))),
    Tool("invoices", "/app/invoices", "invoices", "sell", needs=("payments",),
         uses=(_rows("invoices"), _rows("plans"), _rows("subscriptions"), _rows("payments"), _rows("payment_accounts"))),
    Tool("suppliers", "/app/suppliers", "suppliers", "work", needs=("projects",), studio_hides=True,
         uses=(_rows("suppliers"), _rows("purchase_orders"))),
    Tool("library", "/app/library", "library", "work", needs=("projects",), studio_hides=True,
         uses=(_rows("catalog_items", "NOT in_inventory"),)),          # v0.20: items listed in Inventory are Inventory's
    Tool("followups", "/app/followups", "followups", "talk", needs=("followups",), owner=True, uses=(_rows("automations"),)),
    Tool("campaigns", "/app/campaigns", "campaigns", "market", needs=("followups",), owner=True,
         uses=(_rows("campaigns"), _rows("sequences"))),
    Tool("recipes", "/app/recipes", "recipes", "talk", needs=("followups",), owner=True, uses=(_rows("recipes"),)),
    Tool("workflows", "/app/workflows", "workflows", "talk", needs=("followups",), owner=True, uses=(_rows("workflows"),)),
    Tool("grow", "/app/grow", "grow", "market", needs=("grow",), owner=True,
         uses=(_rows("review_requests"), _rows("referral_links"), _rows("growth_settings"))),
    Tool("posts", "/app/posts", "posts", "market", needs=("posts",), owner=True, uses=(_rows("posts"),)),
    Tool("google", "/app/google", "google", "online", owner=True, uses=(_rows("google_links"),)),
    Tool("website", "/app/website", "website", "online", needs=("website",), owner=True, uses=(_biz("site_enabled"),)),
    Tool("forms", "/app/forms", "forms", "sell", owner=True, uses=(_rows("forms"),)),
    Tool("ai_team", "/app/ai-team", "ai_team", "work", needs=("projects",), owner=True, uses=(_biz("ai_staff <> '{}'::jsonb"),)),
    Tool("channels", "/app/channels", "channels", "talk", owner=True, uses=(_rows("channel_accounts"),)),
    Tool("directory", "/app/directory", "directory", "online", owner=True, uses=(_rows("directory_listings", "listed"),)),
    Tool("comments", "/app/comments", "comments", "talk", needs=("comments",),
         uses=(_rows("comment_rules"), _rows("social_comments"))),
    Tool("tiktok", "/app/tiktok", "tiktok", "market", owner=True, uses=(_rows("channel_accounts", "channel = 'tiktok'"),)),
    Tool("voice_agent", "/app/voice-agent", "phone", "talk", label="vc_title", owner=True,
         uses=(_rows("voice_agents"), _rows("calls"))),
    # ars01 (v0.20): the register, the shelf, the team's hours, the money book
    Tool("pos", "/app/pos", "pos", "sell", uses=(_rows("pos_sales"),)),
    Tool("inventory", "/app/inventory", "inventory", "sell",
         uses=(_rows("catalog_items", "in_inventory"), _rows("stock_moves"))),
    Tool("staff", "/app/staff", "staff", "work", uses=(_rows("staff_members"), _rows("shifts"))),
    Tool("books", "/app/books", "books", "sell", owner=True, uses=(_rows("book_entries"),)),
    # ars02 (v0.21): pages that sell, emails that look good, a posting calendar, members and courses, affiliates, ads,
    # other apps connected
    Tool("funnels", "/app/funnels", "funnel", "online", owner=True, uses=(_rows("funnels"),)),
    Tool("emails", "/app/emails", "mail", "market", needs=("followups",), owner=True, uses=(_rows("email_designs"),)),
    Tool("schedule", "/app/schedule", "schedule", "market", needs=("posts",), owner=True,
         uses=(_rows("posts", "publish_at IS NOT NULL"),)),
    Tool("members", "/app/members", "members", "sell", label="ms_menu", owner=True,
         uses=(_rows("member_plans"), _rows("members"))),
    Tool("courses", "/app/courses", "courses", "sell", owner=True, uses=(_rows("courses"),)),
    Tool("affiliates", "/app/affiliates", "affiliates", "market", owner=True, uses=(_rows("affiliates"),)),
    Tool("ads", "/app/ads", "ads", "market", owner=True, uses=(_rows("ads"),)),
    Tool("connect_apps", "/app/connect-apps", "plug", "online", owner=True,
         uses=(_rows("inbound_hooks"), _rows("webhook_endpoints", "active"))),
    Tool("settings", "/app/settings", "settings", owner=True),
    Tool("add_more", "/app/add", "plus", owner=True),
    Tool("start", "/app/start", "start", owner=True),
    Tool("billing", "/app/billing", "card", owner=True, only="billing"),
    Tool("server", "/app/server", "server", only="admin"),
)
TOOLS = {t.key: t for t in REGISTRY}
TOP = ("today", "valeria", "connect", "inbox", "contacts")            # core, first in the menu
BOTTOM = ("settings", "add_more", "start", "billing", "server")      # core, last in the menu
OPTIONAL = tuple(t.key for t in REGISTRY if not t.core)


# ---------------------------------------------------------------- the kit and what is on
def kit(industry: str | None) -> packs.Kit:
    """The trade's kit; the generic one when the trade has none (or is unknown)."""
    try:
        k = packs.industry(industry or "generic").kit
    except KeyError:
        k = None
    return k or packs.industry("generic").kit


def check_kits() -> list[str]:
    """Problems in the shipped kits (tests: none) — every tool and label a kit names is a registered, optional tool."""
    out = []
    for code, ip in packs.industries().items():
        if not ip.kit:
            continue
        out += [f"{code}: unknown tool {k!r}" for k in ip.kit.tools if k not in OPTIONAL]
        out += [f"{code}: label for unknown tool {k!r}" for k in ip.kit.labels if k not in TOOLS]
    return out


def is_on(biz: dict, key: str) -> bool:
    """Is this tool in the business's menu (its own choice, else its kit)? Core is always on."""
    t = TOOLS[key]
    if t.core:
        return True
    own = (biz.get("tools") or {}).get(key)
    if own is not None:
        return bool(own)
    return key in kit(biz.get("industry")).tools


def visible(biz: dict, key: str) -> bool:
    """On, and every block it needs is working (a block off, or not in the plan, hides it as before)."""
    return is_on(biz, key) and all(blocks.on(biz, b) for b in TOOLS[key].needs)


def label(biz: dict, key: str) -> str:
    """The locale key the menu says for this tool: the kit's word for this trade, else the tool's own."""
    return kit(biz.get("industry")).labels.get(key) or TOOLS[key].word


def _order(biz: dict) -> list[str]:
    """Core first, then the kit's tools in its order, then any tool the owner added (registry order), then core last.
    The Studio door, when it shows, stays at the very top (as it always was)."""
    k = kit(biz.get("industry")).tools
    rest = [x for x in OPTIONAL if x not in k]
    return ["studio"] + [x for x in list(TOP) + [x for x in k if x in TOOLS] + rest + list(BOTTOM) if x != "studio"]


def menu(biz: dict, *, role: str, admin: bool, billing: bool, signed_in: bool) -> list[dict]:
    """What the office menu shows this person, in order: [{key, href, icon, label}] (label = a locale key)."""
    out = []
    studio = blocks.on(biz, "studio")
    for key in _order(biz):
        t = TOOLS[key]
        if t.owner and role != "owner":
            continue
        if t.only == "admin" and not admin:
            continue
        if t.only == "billing" and not (billing and signed_in):
            continue
        if not visible(biz, key) or (studio and t.studio_hides):
            continue
        out.append({"key": key, "href": t.href, "icon": t.icon, "label": label(biz, key)})
    return out


def catalog(biz: dict) -> list[dict]:
    """Add more: every optional tool, grouped — [{group, tools: [{key, href, icon, label, about, on, shown, plan}]}]."""
    from .services import billing as BL
    groups = []
    for g in GROUPS:
        rows = []
        for t in REGISTRY:
            if t.group != g:
                continue
            rows.append({"key": t.key, "href": t.href, "icon": t.icon, "label": label(biz, t.key), "about": t.about,
                         "on": is_on(biz, t.key), "shown": visible(biz, t.key),
                         "plan": all(BL.allows(biz, b) for b in t.needs)})      # False = the plan doesn't include it
        rows.sort(key=lambda r: (not r["on"], OPTIONAL.index(r["key"])))
        groups.append({"group": g, "tools": rows})
    return groups


def set_tool(conn: Connection, biz: dict, actor: str, key: str, on: bool) -> dict:
    """Show (on) or hide (off) a tool. Showing one also switches on any block it needs that the owner had switched off,
    so one tap is enough; hiding never touches blocks or data."""
    from .services import Invalid, log
    from .services import billing as BL
    t = TOOLS.get(key)
    if not t or t.core:
        raise Invalid(f"unknown tool {key!r}", "tool")
    B = db.businesses
    row = dict(conn.execute(select(B).where(B.c.id == biz["id"]).with_for_update()).mappings().one())
    vals = {"tools": {**(row["tools"] or {}), key: bool(on)}}
    if on:
        missing = [b for b in t.needs if not BL.allows(row, b)]
        if missing:
            raise Invalid("this needs a plan that includes it", "tool")
        switched = [b for b in t.needs if not blocks.on(row, b)]
        if switched:
            vals["features"] = {**(row["features"] or {}), **{b: True for b in switched}}
    else:
        switched = []
    conn.execute(update(B).where(B.c.id == biz["id"]).values(**vals, updated_at=func.now()))
    log(conn, biz["id"], "business.tool", actor, biz["id"], {"tool": key, "on": bool(on), "blocks_on": switched})
    return dict(conn.execute(select(B).where(B.c.id == biz["id"])).mappings().one())


# ---------------------------------------------------------------- never lose what is used
def in_use(conn: Connection, business_id) -> set[str]:
    """The tools that hold this business's data (one query)."""
    keyed = [t for t in REGISTRY if t.uses]
    cols = ", ".join(f"EXISTS({' UNION ALL '.join(t.uses)}) AS {t.key}" for t in keyed)
    row = conn.execute(text(f"SELECT {cols}"), {"b": business_id}).mappings().one()
    return {k for k, v in row.items() if v}


def keep_in_use(conn: Connection, business_id, actor: str) -> list[str]:
    """Pin ON every tool that holds data and isn't on (after the trade changed: the new kit may not have it). A tool the
    owner hid on purpose stays hidden: that was their choice, not the kit's."""
    from .services import log
    B = db.businesses
    row = dict(conn.execute(select(B).where(B.c.id == business_id).with_for_update()).mappings().one())
    pin = sorted(k for k in in_use(conn, business_id) if (row["tools"] or {}).get(k) is None and not is_on(row, k))
    if pin:
        conn.execute(update(B).where(B.c.id == business_id).values(tools={**(row["tools"] or {}), **{k: True for k in pin}}))
        log(conn, business_id, "business.tools_kept", actor, business_id, {"tools": pin})
    return pin


# ---------------------------------------------------------------- Valeria suggests (rules, no AI, one at a time)
TIP_NO_SHOWS = 2           # no-shows in the last 90 days -> reminders
TIP_QUOTES = 3             # quotes sent -> invoices
TIP_REPEAT = 5             # customers who came back (2+ visits or payments) -> loyalty
TIPS = ("restock", "reminders", "invoices", "loyalty")


def suggestion(conn: Connection, biz: dict) -> dict | None:
    """The one thing worth suggesting now, from the business's own numbers — never one the owner dismissed.
    -> {"key", "tool" (turn this tool on) or "href" (go set it up)} or None."""
    from .services import inventory as INV
    from .services import loyalty as LY
    from .services import reminders as RM
    no = set(biz.get("tips_dismissed") or [])
    bid = biz["id"]
    if "restock" not in no and visible(biz, "inventory"):                 # v0.20: what is running low, before it runs out
        n = len(INV.low(conn, bid))
        if n:
            return {"key": "restock", "href": "/app/inventory?low=1", "n": n}
    if "reminders" not in no and blocks.on(biz, "calendar") and blocks.on(biz, "assistant") and not RM.rule(conn, bid):
        n = conn.execute(text("SELECT count(*) FROM appointments WHERE business_id = :b AND status = 'no_show' "
                              "AND starts_at > now() - interval '90 days'"), {"b": bid}).scalar()
        if n >= TIP_NO_SHOWS:
            return {"key": "reminders", "href": "/app/valeria#reminders", "n": n}
    if "invoices" not in no and not visible(biz, "invoices"):
        n = conn.execute(text("SELECT count(*) FROM quotes WHERE business_id = :b"), {"b": bid}).scalar()
        if n >= TIP_QUOTES:
            return {"key": "invoices", "tool": "invoices", "n": n}
    if "loyalty" not in no and blocks.on(biz, "loyalty") and blocks.on(biz, "assistant") and not LY.rule(conn, bid)["on"]:
        n = conn.execute(text(
            "SELECT count(*) FROM (SELECT contact_id FROM ("
            " SELECT contact_id FROM appointments WHERE business_id = :b AND status = 'done' AND contact_id IS NOT NULL"
            " UNION ALL SELECT contact_id FROM payments WHERE business_id = :b AND status = 'paid' AND contact_id IS NOT NULL"
            ") v GROUP BY contact_id HAVING count(*) >= 2) r"), {"b": bid}).scalar()
        if n >= TIP_REPEAT:
            return {"key": "loyalty", "href": "/app/valeria#loyalty", "n": n}
    return None


def dismiss(conn: Connection, biz: dict, actor: str, key: str) -> None:
    """"No thanks": that suggestion never comes back."""
    from .services import Invalid, log
    if key not in TIPS:
        raise Invalid(f"unknown suggestion {key!r}", "tip")
    B = db.businesses
    cur = list(conn.execute(select(B.c.tips_dismissed).where(B.c.id == biz["id"]).with_for_update()).scalar() or [])
    if key not in cur:
        conn.execute(update(B).where(B.c.id == biz["id"]).values(tips_dismissed=cur + [key]))
        log(conn, biz["id"], "business.tip_dismissed", actor, biz["id"], {"tip": key})
