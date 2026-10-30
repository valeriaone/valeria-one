"""mon01: the owner's own fields, "when this, do that" recipes, the board, forms

Custom fields on customers and deals (custom_fields; values in contacts.custom / deals.custom), who looks after a
customer (contacts.assignee_id), recipes and what they did (recipes, recipe_runs), the task board (tasks.started_at)
and to-dos about a customer or a deal (tasks.contact_id, deal_id, due_at), the owner's own forms (forms,
form_submissions: request consent and marketing consent kept apart).

Revision ID: e7c1a9d3f5b2
Revises: b13a7e5c0d21
Create Date: 2026-10-30 04:16:17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e7c1a9d3f5b2'
down_revision: Union[str, Sequence[str], None] = 'b13a7e5c0d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('custom_fields',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('object', sa.String(length=8), nullable=False),
    sa.Column('key', sa.String(length=24), nullable=False),
    sa.Column('label', sa.String(length=60), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('options', postgresql.ARRAY(sa.String(length=60)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('show_in_list', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('show_on_card', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('text','number','money','date','yesno','choice','phone','link')", name=op.f('ck_custom_fields_kind')),
    sa.CheckConstraint("object IN ('contact','deal')", name=op.f('ck_custom_fields_object')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_custom_fields_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_custom_fields')),
    sa.UniqueConstraint('business_id', 'object', 'key', name='uq_custom_field')
    )
    op.create_table('forms',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('intro', sa.String(length=500), server_default='', nullable=False),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('thanks', sa.String(length=500), server_default='', nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=True),
    sa.Column('tag', sa.String(length=60), nullable=True),
    sa.Column('on_site', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('submissions', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_forms_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_forms')),
    sa.UniqueConstraint('slug', name=op.f('uq_forms_slug'))
    )
    op.create_table('recipes',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('preset', sa.String(length=30), nullable=True),
    sa.Column('name', sa.String(length=160), server_default='', nullable=False),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('trigger_arg', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('condition', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('actions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('ask_first', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("trigger IN ('lead.new','deal.stage','booking.made','field.changed','form.submitted','contact.quiet')", name=op.f('ck_recipes_trigger')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_recipes_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recipes'))
    )
    op.create_table('recipe_runs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('recipe_id', sa.UUID(), nullable=False),
    sa.Column('dedupe', sa.String(length=120), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=True),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('lines', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('done','failed','loop')", name=op.f('ck_recipe_runs_status')),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_recipe_runs_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_recipe_runs_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], name=op.f('fk_recipe_runs_recipe_id_recipes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recipe_runs')),
    sa.UniqueConstraint('recipe_id', 'dedupe', name='uq_recipe_run')
    )
    op.create_index('ix_recipe_runs_biz', 'recipe_runs', ['business_id', 'created_at'], unique=False)
    op.create_index('ix_recipe_runs_subject', 'recipe_runs', ['recipe_id', 'subject_id', 'created_at'], unique=False)
    op.create_table('form_submissions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('form_id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('deal_id', sa.UUID(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('request_consent', sa.Boolean(), nullable=False),
    sa.Column('marketing_consent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_form_submissions_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_form_submissions_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], name=op.f('fk_form_submissions_deal_id_deals'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['form_id'], ['forms.id'], name=op.f('fk_form_submissions_form_id_forms'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_form_submissions'))
    )
    op.create_index('ix_form_submissions_form', 'form_submissions', ['form_id', 'created_at'], unique=False)
    op.add_column('contacts', sa.Column('custom', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('contacts', sa.Column('assignee_id', sa.UUID(), nullable=True))
    op.create_foreign_key(op.f('fk_contacts_assignee_id_users'), 'contacts', 'users', ['assignee_id'], ['id'], ondelete='SET NULL')
    op.add_column('deals', sa.Column('custom', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('tasks', sa.Column('contact_id', sa.UUID(), nullable=True))
    op.add_column('tasks', sa.Column('deal_id', sa.UUID(), nullable=True))
    op.add_column('tasks', sa.Column('due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(op.f('fk_tasks_deal_id_deals'), 'tasks', 'deals', ['deal_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_tasks_contact_id_contacts'), 'tasks', 'contacts', ['contact_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('fk_tasks_contact_id_contacts'), 'tasks', type_='foreignkey')
    op.drop_constraint(op.f('fk_tasks_deal_id_deals'), 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'started_at')
    op.drop_column('tasks', 'due_at')
    op.drop_column('tasks', 'deal_id')
    op.drop_column('tasks', 'contact_id')
    op.drop_column('deals', 'custom')
    op.drop_constraint(op.f('fk_contacts_assignee_id_users'), 'contacts', type_='foreignkey')
    op.drop_column('contacts', 'assignee_id')
    op.drop_column('contacts', 'custom')
    op.drop_index('ix_form_submissions_form', table_name='form_submissions')
    op.drop_table('form_submissions')
    op.drop_index('ix_recipe_runs_subject', table_name='recipe_runs')
    op.drop_index('ix_recipe_runs_biz', table_name='recipe_runs')
    op.drop_table('recipe_runs')
    op.drop_table('recipes')
    op.drop_table('forms')
    op.drop_table('custom_fields')
