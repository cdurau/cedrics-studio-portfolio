"""initial

Revision ID: 0001
Revises:
Create Date: 2026-08-21 11:39:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table('entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('content', sa.String(), nullable=True),
    sa.Column('author', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_entries_author'), 'entries', ['author'], unique=False)
    op.create_index(op.f('ix_entries_content'), 'entries', ['content'], unique=False)
    op.create_index(op.f('ix_entries_id'), 'entries', ['id'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_entries_id'), table_name='entries')
    op.drop_index(op.f('ix_entries_content'), table_name='entries')
    op.drop_index(op.f('ix_entries_author'), table_name='entries')
    op.drop_table('entries')
