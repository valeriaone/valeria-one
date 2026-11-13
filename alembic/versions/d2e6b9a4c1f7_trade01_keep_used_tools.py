"""trade01 (v0.65.0): each trade's kit is now its 5 tools — every business keeps every tool that holds its data

The kits got smaller (a gym sees Members, Calendar, Payments, Follow-ups, Website; the rest one tap away in Add more). Data
integrity: before that shows, every tool that holds a business's data is pinned ON for it (businesses.tools), so nothing
anyone uses leaves their menu. A tool the owner hid on purpose (tools.<key> = false) stays hidden — their choice. The SQL
is this release's copy of kits.REGISTRY[*].uses, frozen so the migration never changes with later code. No schema change,
nothing deleted; downgrade leaves the pins (harmless: they only show tools that hold data).

Revision ID: d2e6b9a4c1f7
Revises: c8f5a1d9e2b6
Create Date: 2026-11-13 04:00:00

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2e6b9a4c1f7'
down_revision: Union[str, Sequence[str], None] = 'c8f5a1d9e2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND ' + where if where else ''}"


def _self(where: str) -> str:
    return f"SELECT 1 WHERE {where}"


USES = {
    "studio": [_self("b.studio_host IS NOT NULL OR b.features->>'studio' = 'true' OR b.apps_ready <> '[]'::jsonb")],
    "projects": [_rows("projects")],
    "tasks": [_rows("tasks")],
    "pipeline": [_rows("deals")],
    "calendar": [_rows("appointments"), _rows("booking_services"), _self("b.booking_enabled")],
    "invoices": [_rows("invoices"), _rows("plans"), _rows("subscriptions"), _rows("payments"), _rows("payment_accounts")],
    "suppliers": [_rows("suppliers"), _rows("purchase_orders")],
    "library": [_rows("catalog_items", "NOT in_inventory")],
    "followups": [_rows("automations")],
    "campaigns": [_rows("campaigns"), _rows("sequences")],
    "recipes": [_rows("recipes")],
    "workflows": [_rows("workflows")],
    "grow": [_rows("review_requests"), _rows("referral_links"), _rows("growth_settings")],
    "posts": [_rows("posts")],
    "google": [_rows("google_links")],
    "website": [_self("b.site_enabled")],
    "forms": [_rows("forms")],
    "ai_team": [_self("b.ai_staff <> '{}'::jsonb")],
    "channels": [_rows("channel_accounts")],
    "directory": [_rows("directory_listings", "listed")],
    "comments": [_rows("comment_rules"), _rows("social_comments")],
    "tiktok": [_rows("channel_accounts", "channel = 'tiktok'")],
    "voice_agent": [_rows("voice_agents"), _rows("calls")],
    "pos": [_rows("pos_sales")],
    "inventory": [_rows("catalog_items", "in_inventory"), _rows("stock_moves")],
    "staff": [_rows("staff_members"), _rows("shifts")],
    "books": [_rows("book_entries")],
    "funnels": [_rows("funnels")],
    "emails": [_rows("email_designs")],
    "schedule": [_rows("posts", "publish_at IS NOT NULL")],
    "members": [_rows("member_plans"), _rows("members")],
    "courses": [_rows("courses")],
    "affiliates": [_rows("affiliates")],
    "ads": [_rows("ads")],
    "connect_apps": [_rows("inbound_hooks"), _rows("webhook_endpoints", "active")],
    "marketplace": [_rows("marketplace_items")],
    "contracts": [_rows("contracts"), _rows("contract_templates")],
    "leads": [_rows("lead_sources"), _rows("leads")],
}


def pin_statements() -> list[str]:
    """One UPDATE per tool: pin it ON for every business that holds data in it and never chose it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) "
            f"WHERE NOT (b.tools ? '{key}') AND EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    pass
