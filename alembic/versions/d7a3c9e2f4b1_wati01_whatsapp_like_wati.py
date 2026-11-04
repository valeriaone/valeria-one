"""wati01 (v0.29.0): WhatsApp as complete as the paid inboxes — templates, team chats, away message

wa_templates        the business's WhatsApp message templates on its OWN WhatsApp account: written here, sent to Meta for
                    approval, their status read back (APPROVED / PENDING / REJECTED + why). Campaigns and the inbox pick
                    from the approved ones instead of typing a name.
conversation_notes  the team's own notes on a chat (never sent to the customer).
conversations.assignee_id   who on the team has this chat (anyone may take it; NULL = nobody yet).
assistant_settings.away / away_text   out of the business's hours, one "we're closed, we'll answer at …" reply.

Data integrity: two new tables and three new columns with safe defaults; nothing existing changes, nothing is back-filled.
Downgrade refuses while any template or note exists (it never deletes the business's words).

Revision ID: d7a3c9e2f4b1
Revises: c4d8e1f6a2b7
Create Date: 2026-11-04 10:06:09.897857

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd7a3c9e2f4b1'
down_revision: Union[str, Sequence[str], None] = 'c4d8e1f6a2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('wa_templates',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('channel_account_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('language', sa.String(length=15), nullable=False),
    sa.Column('category', sa.String(length=14), server_default='UTILITY', nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('example', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='DRAFT', nullable=False),
    sa.Column('meta_id', sa.String(length=40), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('MARKETING','UTILITY','AUTHENTICATION')", name=op.f('ck_wa_templates_category')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_wa_templates_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['channel_account_id'], ['channel_accounts.id'], name=op.f('fk_wa_templates_channel_account_id_channel_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wa_templates')),
    sa.UniqueConstraint('channel_account_id', 'name', 'language', name='uq_wa_template')
    )
    op.create_table('conversation_notes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('author_id', sa.UUID(), nullable=True),
    sa.Column('author_name', sa.String(length=200), server_default='', nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], name=op.f('fk_conversation_notes_author_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_conversation_notes_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_conversation_notes_conversation_id_conversations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversation_notes'))
    )
    op.create_index('ix_conversation_notes_conv', 'conversation_notes', ['conversation_id', 'created_at'], unique=False)
    op.add_column('assistant_settings', sa.Column('away', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('assistant_settings', sa.Column('away_text', sa.String(length=500), server_default='', nullable=False))
    op.add_column('conversations', sa.Column('assignee_id', sa.UUID(), nullable=True))
    op.create_foreign_key(op.f('fk_conversations_assignee_id_users'), 'conversations', 'users', ['assignee_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    b = op.get_bind()
    n = b.execute(sa.text("SELECT (SELECT count(*) FROM wa_templates) + (SELECT count(*) FROM conversation_notes)")).scalar()
    if n:
        raise RuntimeError(f"{n} WhatsApp templates or chat notes exist; downgrading would lose them, so it stops (nothing changed)")
    op.drop_constraint(op.f('fk_conversations_assignee_id_users'), 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'assignee_id')
    op.drop_column('assistant_settings', 'away_text')
    op.drop_column('assistant_settings', 'away')
    op.drop_index('ix_conversation_notes_conv', table_name='conversation_notes')
    op.drop_table('conversation_notes')
    op.drop_table('wa_templates')
