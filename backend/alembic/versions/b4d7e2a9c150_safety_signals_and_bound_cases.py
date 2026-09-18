"""Safety/PV M9: signals that remember why they were raised, and narratives
bound to their cases.

`pv_signals.detection_basis` holds the screening statistic behind a candidate
raised from disproportionality -- counts, interval, window, background -- so
the reviewer triaging it reads the evidence rather than a sentence about it.

`pv_report_instances.case_ids` binds an ICSR narrative report to the case, or
batch of cases, it narrates. Every other report type describes an interval
and leaves it empty.

No RLS change: both columns belong to tables that already have their policy.

Revision ID: b4d7e2a9c150
Revises: e9b2d4c61a38
"""

import sqlalchemy as sa
from alembic import op

revision = "b4d7e2a9c150"
down_revision = "e9b2d4c61a38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pv_signals", sa.Column("detection_basis", sa.JSON(), nullable=True))
    op.add_column("pv_report_instances", sa.Column("case_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pv_report_instances") as batch:
        batch.drop_column("case_ids")
    with op.batch_alter_table("pv_signals") as batch:
        batch.drop_column("detection_basis")
