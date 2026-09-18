"""Safety/PV: the figures a report stated, frozen at sign-off.

One JSON column. The next interval's QC compares its cumulative count against
the figure the previous report actually printed -- recomputing the previous
report's window over today's store can never produce a number larger than the
new report's own, so without a recorded figure "cumulative must not fall between
reports" is a check that cannot fire.

No RLS change: the column belongs to a table that already has its policy.

Revision ID: c3e8a1f5d207
Revises: a1f7c3e94b62
"""

import sqlalchemy as sa
from alembic import op

revision = "c3e8a1f5d207"
down_revision = "a1f7c3e94b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pv_report_instances",
                  sa.Column("figures_at_signoff", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pv_report_instances") as batch:
        batch.drop_column("figures_at_signoff")
