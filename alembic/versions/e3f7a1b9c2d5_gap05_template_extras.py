"""gap05 (v0.42.0): WhatsApp templates with a header, a footer and buttons (quick replies or a link).

wa_templates.extras   {"header", "footer", "buttons": [{"type": "quick"|"url", "text", "url"?}]}  — empty for every existing row.

Data integrity: one new column with a default ('{}'); no row changes meaning, nothing is lost.
Downgrade refuses while any template carries extras (it never drops what an owner wrote).

Revision ID: e3f7a1b9c2d5
Revises: d8a2c5e7f1b3
Create Date: 2026-11-07 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e3f7a1b9c2d5'
down_revision: Union[str, Sequence[str], None] = 'd8a2c5e7f1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('wa_templates', sa.Column('extras', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"),
                                            nullable=False))


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from wa_templates where extras <> '{}'::jsonb")).scalar()
    if n:
        raise RuntimeError(f"refusing to drop wa_templates.extras: {n} template(s) have a header, footer or buttons")
    op.drop_column('wa_templates', 'extras')
