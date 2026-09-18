"""A workflow lane on a generated document.

`generated_documents.status` answers what the engine and the reviewers say about
a letter -- it failed QA, somebody objected, the engine parked a question. It has
no room for what a *person* says about it: picked up, finished, given up on. And
overloading it would put the two in a fight the QA verdict has to win, which
means the person's answer would keep being erased by a recompute they did not
ask for.

So this is a second axis, and only the person writes it. Only the three a person
can assert are storable -- `work_in_progress`, `completed`, `cancelled`.
`approved` and `blocked` are layered over this column on read by
`generation.workflow_status.effective`, so neither can be claimed by a PATCH: a
signature stays something the approve endpoint records, and a QA verdict stays
something the fill engine found.

Revision ID: 4a1c8d2e6f57
Revises: b83e6d417c92
"""

import sqlalchemy as sa
from alembic import op

revision = "4a1c8d2e6f57"
down_revision = "b83e6d417c92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One statement, NOT NULL and DEFAULT together -- deliberately *not*
    # add-nullable-then-`alter_column(nullable=False)`. That is the shape
    # `8e2d5b7c9a04` had to be fixed for: SQLite has no ALTER COLUMN, alembic's
    # generic implementation emits `ALTER TABLE t ALTER COLUMN c SET NOT NULL`,
    # and every SQLite older than 3.53 -- CI's included -- rejects it as a syntax
    # error while the laptop it was written on accepts it.
    #
    # `ADD COLUMN ... NOT NULL DEFAULT <constant>` is one statement both engines
    # take, and it backfills every existing row as it goes, so there is no
    # UPDATE to write either. Every document that already exists becomes
    # `work_in_progress`, which is the truth about it: nobody has picked it up,
    # because until now there was nothing to pick it up with.
    op.add_column(
        "generated_documents",
        sa.Column("workflow_status", sa.String(), nullable=False,
                  server_default="work_in_progress"),
    )
    # The documents screen filters by this, always within one project. A lane
    # across the whole estate is not a view the product has, so the index is
    # composite rather than on the column alone.
    op.create_index(
        "ix_generated_documents_project_workflow",
        "generated_documents", ["project_id", "workflow_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_generated_documents_project_workflow",
                  table_name="generated_documents")
    # Batch mode rather than a bare drop, for the same portability reason as
    # above: SQLite gained ALTER TABLE DROP COLUMN only in 3.35 and still refuses
    # when any index names the column. Batch rebuilds the table and is correct
    # whatever else is on it; it renders as a plain ALTER on PostgreSQL.
    with op.batch_alter_table("generated_documents") as batch:
        batch.drop_column("workflow_status")
