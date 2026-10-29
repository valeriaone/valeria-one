"""grow02: directory, google business profile, posts, tiktok

The public directory (opt-in listings), a business's own Google Business Profile with its reviews, and posts
for Facebook / Instagram / Google. TikTok needs no table: it is a channel account like the others.

Revision ID: 9d3e7b1a5c20
Revises: 15ae2c4b553b
Create Date: 2026-10-29 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

OLD_KINDS = "kind IN ('photo','render','plan','doc','canva','site')"
NEW_KINDS = "kind IN ('photo','render','plan','doc','canva','site','post')"

# revision identifiers, used by Alembic.
revision: str = '9d3e7b1a5c20'
down_revision: Union[str, Sequence[str], None] = '15ae2c4b553b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('ck_files_kind'), 'files', type_='check')
    op.create_check_constraint(op.f('ck_files_kind'), 'files', NEW_KINDS)
    op.create_table('directory_listings',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('listed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('category', sa.String(length=60), server_default='', nullable=False),
    sa.Column('city', sa.String(length=100), server_default='', nullable=False),
    sa.Column('area', sa.String(length=100), server_default='', nullable=False),
    sa.Column('summary', sa.String(length=300), server_default='', nullable=False),
    sa.Column('show_hours', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('show_phone', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('show_whatsapp', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('show_address', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_directory_listings_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_directory_listings'))
    )
    op.create_index('ix_directory_listed_city', 'directory_listings', ['listed', sa.literal_column('lower(city)')], unique=False)
    op.create_table('google_links',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('account_email', sa.String(length=320), nullable=True),
    sa.Column('tokens', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('account', sa.String(length=100), nullable=True),
    sa.Column('location', sa.String(length=100), nullable=True),
    sa.Column('location_title', sa.String(length=300), nullable=True),
    sa.Column('info', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('insights', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('insights_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='ok', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('backoff_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('polled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connected_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ok','reconnect')", name=op.f('ck_google_links_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_google_links_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('business_id', name=op.f('pk_google_links'))
    )
    op.create_table('reviews',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('author', sa.String(length=200), server_default='', nullable=False),
    sa.Column('rating', sa.Integer(), nullable=True),
    sa.Column('comment', sa.Text(), server_default='', nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reply', sa.Text(), nullable=True),
    sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('draft', sa.Text(), nullable=True),
    sa.Column('draft_by_ai', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('reply_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('rating IS NULL OR rating BETWEEN 1 AND 5', name=op.f('ck_reviews_rating')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_reviews_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reviews')),
    sa.UniqueConstraint('business_id', 'source', 'external_id', name='uq_review')
    )
    op.create_table('posts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), server_default='idea', nullable=False),
    sa.Column('idea', sa.Text(), server_default='', nullable=False),
    sa.Column('networks', postgresql.ARRAY(sa.String(length=20)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('texts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='draft', nullable=False),
    sa.Column('publish_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('results', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('by_ai', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('idea','offer','product','holiday','review')", name=op.f('ck_posts_kind')),
    sa.CheckConstraint("status IN ('draft','scheduled','publishing','published','failed','cancelled')", name=op.f('ck_posts_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_posts_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_posts_file_id_files'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_posts'))
    )
    op.create_index('ix_posts_due', 'posts', ['publish_at'], unique=False, postgresql_where=sa.text("status = 'scheduled'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_posts_due', table_name='posts', postgresql_where=sa.text("status = 'scheduled'"))
    op.drop_table('posts')
    op.drop_table('reviews')
    op.drop_table('google_links')
    op.drop_index('ix_directory_listed_city', table_name='directory_listings')
    op.drop_table('directory_listings')
    op.execute("DELETE FROM files WHERE kind = 'post'")            # pictures the old code can't show
    op.drop_constraint(op.f('ck_files_kind'), 'files', type_='check')
    op.create_check_constraint(op.f('ck_files_kind'), 'files', OLD_KINDS)
