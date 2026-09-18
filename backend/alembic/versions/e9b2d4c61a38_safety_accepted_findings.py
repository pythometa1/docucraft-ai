"""Safety/PV: the QC findings a qualified person accepted rather than fixed.

One JSON column on the report. A small set of blockers read prose with a
pattern -- "a published series of 12 patients" is a count the case store did
not produce and is still correct -- and without somewhere to record a reasoned
acceptance the only way past one is to change the text until the pattern stops
matching.

No RLS change: the column belongs to a table that already has its policy.

Revision ID: e9b2d4c61a38
Revises: c3e8a1f5d207
"""

import sqlalchemy as sa
from alembic import op

revision = "e9b2d4c61a38"
down_revision = "c3e8a1f5d207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pv_report_instances",
                  sa.Column("accepted_findings", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pv_report_instances") as batch:
        batch.drop_column("accepted_findings")
