"""gap03 (v0.42.0): a workflow can start when a customer's message has a word, or when they tap an option.

workflows.trigger gains 'message.keyword' (its trigger_arg: {"words": [...], "pick": "..."}).

Data integrity: only the CHECK constraint changes (a wider list); no row changes, nothing back-filled.
Downgrade refuses while any workflow uses the new trigger (it never deletes an owner's workflow).

Revision ID: c6f3b9d1e8a4
Revises: b5e2a8c4d9f3
Create Date: 2026-11-07 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c6f3b9d1e8a4'
down_revision: Union[str, Sequence[str], None] = 'b5e2a8c4d9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = ("'lead.new','deal.stage','booking.made','booking.done','booking.no_show','field.changed','form.submitted','tag.added',"
       "'payment.paid','contact.quiet','contact.inactive','date.field','manual'")


def upgrade() -> None:
    op.drop_constraint(op.f('ck_workflows_trigger'), 'workflows', type_='check')
    op.create_check_constraint(op.f('ck_workflows_trigger'), 'workflows', f"trigger IN ({OLD},'message.keyword')")


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from workflows where trigger = 'message.keyword'")).scalar()
    if n:
        raise RuntimeError(f"refusing to narrow the trigger list: {n} workflow(s) start on a keyword")
    op.drop_constraint(op.f('ck_workflows_trigger'), 'workflows', type_='check')
    op.create_check_constraint(op.f('ck_workflows_trigger'), 'workflows', f"trigger IN ({OLD})")
