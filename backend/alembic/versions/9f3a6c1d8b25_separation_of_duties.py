"""four-eyes flag on templates, and the approval trail that makes it checkable

§16: "Four-eyes approval on manifest lock for any template flagged as legally
binding." Two columns are needed for that to be more than an intention.

`template_files.legally_binding` is the flag. It is per template rather than
global because an offer letter and an internal memo do not carry the same risk,
and a separation rule applied to everything is one teams learn to route around.

`template_manifests.approvals` is the trail. A single `approved_by` records who
approved last; four eyes needs to know whether a *different* person has already
signed off, which one column cannot answer.

Revision ID: 9f3a6c1d8b25
Revises: 8e2d5b7c9a04
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f3a6c1d8b25'
down_revision: Union[str, None] = '8e2d5b7c9a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defaults to false: enabling four-eyes on an existing estate is a decision
    # someone makes per template, not something a migration does to them.
    op.add_column(
        'template_files',
        sa.Column('legally_binding', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'template_manifests',
        sa.Column('approvals', sa.JSON(), nullable=False, server_default='[]'),
    )

    # An already-approved manifest has exactly one approval on record, and it is
    # the one already stored. Backfilling it keeps the trail honest rather than
    # showing every historical approval as having happened with nobody watching.
    op.execute(
        """
        UPDATE template_manifests
           SET approvals = json_array(json_object('user_id', approved_by, 'at', approved_at))
         WHERE approved_by IS NOT NULL
        """
        if op.get_bind().dialect.name == "sqlite"
        else
        """
        UPDATE template_manifests
           SET approvals = jsonb_build_array(
                   jsonb_build_object('user_id', approved_by, 'at', approved_at)
               )::json
         WHERE approved_by IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column('template_manifests', 'approvals')
    op.drop_column('template_files', 'legally_binding')
