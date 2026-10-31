"""des01 (v0.26.1): design work files in a project — AutoCAD (.dwg, .dxf), SketchUp (.skp), Photoshop (.psd)

files: a new kind 'work' (kept byte for byte, never shrunk or converted), sha256 (of the bytes kept; checked on every
download of a work file), preview_id (one of the project's pictures or PDFs shown in its place; SET NULL when that
picture is deleted) and client_download (the customer may download the work file itself: false until the designer ticks it).

Data integrity: nothing existing changes and nothing is deleted — every file keeps its kind, size and bytes; the new
columns are empty (sha256 and preview_id NULL) or false (client_download). Downgrade refuses while any work file exists
(it never deletes one).

Revision ID: a6f2c9e4d8b1
Revises: d5a9c3e7b1f4
Create Date: 2026-10-31 23:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a6f2c9e4d8b1'
down_revision: Union[str, Sequence[str], None] = 'd5a9c3e7b1f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS = "kind IN ('photo','render','plan','doc','canva','site','post')"
NEW_KINDS = "kind IN ('photo','render','plan','doc','canva','site','post','work')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('ck_files_kind'), 'files', type_='check')
    op.create_check_constraint(op.f('ck_files_kind'), 'files', NEW_KINDS)
    op.add_column('files', sa.Column('sha256', sa.String(length=64), nullable=True))
    op.add_column('files', sa.Column('preview_id', sa.UUID(), nullable=True))
    op.add_column('files', sa.Column('client_download', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_foreign_key(op.f('fk_files_preview_id_files'), 'files', 'files', ['preview_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM files WHERE kind = 'work'")).scalar()
    if n:
        raise RuntimeError(f"{n} design work files are kept here; downgrading would lose them, so it stops (nothing changed)")
    op.drop_constraint(op.f('fk_files_preview_id_files'), 'files', type_='foreignkey')
    op.drop_column('files', 'client_download')
    op.drop_column('files', 'preview_id')
    op.drop_column('files', 'sha256')
    op.drop_constraint(op.f('ck_files_kind'), 'files', type_='check')
    op.create_check_constraint(op.f('ck_files_kind'), 'files', OLD_KINDS)
