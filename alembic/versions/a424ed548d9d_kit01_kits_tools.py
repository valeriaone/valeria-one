"""kit01 (v0.19.0): kits — each trade sees its own tools first; the owner's choices on top; suggestions said "no thanks" to

businesses.tools ({"invoices": true, "posts": false}: the owner's own choices on top of the trade's kit) and
businesses.tips_dismissed (["loyalty", …]). Data integrity: every business that exists keeps every tool that holds its
data — each such tool is pinned ON here, whatever its kit says (pinning one the kit already has is harmless). The SQL is
this release's copy of kits.REGISTRY[*].uses, frozen so the migration never changes with later code. Nothing is deleted.

Revision ID: a424ed548d9d
Revises: a18c0e5f7b91
Create Date: 2026-10-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a424ed548d9d'
down_revision: Union[str, Sequence[str], None] = 'a18c0e5f7b91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rows(table: str, where: str = "") -> str:
    return f"SELECT 1 FROM {table} x WHERE x.business_id = b.id{' AND x.' + where if where else ''}"


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
    "library": [_rows("catalog_items")],
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
}


def pin_statements() -> list[str]:
    """One UPDATE per tool: pin it ON for every business that holds data in it (tests run these too)."""
    return [f"UPDATE businesses b SET tools = b.tools || jsonb_build_object('{key}', true) WHERE EXISTS({' UNION ALL '.join(uses)})"
            for key, uses in USES.items()]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('businesses', sa.Column('tools', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"),
                                          nullable=False))
    op.add_column('businesses', sa.Column('tips_dismissed', postgresql.JSONB(astext_type=sa.Text()),
                                          server_default=sa.text("'[]'"), nullable=False))
    for sql in pin_statements():                         # what a business already uses never leaves its menu
        op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('businesses', 'tips_dismissed')
    op.drop_column('businesses', 'tools')
