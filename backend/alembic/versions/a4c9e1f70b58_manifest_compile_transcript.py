"""Record how a manifest was compiled, and let a compile record its own failure.

The agentic compile reads every template with a model, in chunks, through a
writer/reviewer loop. Two things follow that the schema had nowhere to put.

`compile_transcript` is the round-by-round record: how many parts the template
was read in, what the document objected to each round, what the reviewer changed,
and why the loop stopped. Without it a reviewer looking at a low-confidence
manifest can see what it says but not how it came to say it.

`status="failed"` is the other. A compile that could not read the template used
to return the rule-based manifest instead -- fields, a confidence, and no way to
tell it apart from a compile that worked. The row is now written with the
transcript and refused at approval, so the attempt stays visible and nothing can
generate from it. No enum is changed here: `status` is a plain string column, and
`validate_manifest` is what enforces the meaning.

Revision ID: a4c9e1f70b58
Revises: b6d1f3a90c24
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c9e1f70b58"
down_revision = "b6d1f3a90c24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows read as an empty list rather than NULL:
    # every consumer iterates this, and `None` would turn a compile history that
    # predates the column into a TypeError at read time.
    op.add_column(
        "template_manifests",
        sa.Column("compile_transcript", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("template_manifests", "compile_transcript")
