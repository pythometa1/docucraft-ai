"""The five object types the manifest table could not hold.

§6 defines ten semantic object types and `ManifestEnvelope` models all ten, but
the row has three JSON lists -- `fields`, `conditions`, `blocks`. So STATIC,
NARRATIVE, HEADER, FOOTER and SIGNATURE had nowhere to go. `to_row_values`
reported them in `unmapped` rather than dropping them quietly, and
`POST /templates/{id}/inherit-manifest` refused outright with
MANIFEST_OBJECTS_UNSTORABLE the moment an approved manifest carried one -- which
is the honest behaviour available to a writer that cannot store what it is given,
and also the reason inheriting a real template with a signature block failed.

`objects` is the lossless envelope: every object, in §6 shape, whatever its type.
The three legacy lists stay exactly as they are, because `fill_template`,
`validate_manifest`, `source_resolver` and `build_workbook` all read them and
none of them should have to change to gain a column. A writer that opts in emits
both projections from one envelope; it never emits one without the other.

No backfill. `envelope_from_row` reads `objects` when it is non-empty and falls
back to the three lists otherwise, so every existing row keeps producing exactly
the envelope it produces today. Backfilling would mean deciding, per row, which
of ten types a legacy entry had always been -- and a guess written into the
column that exists to be authoritative is worse than the fallback that is
already correct.

Revision ID: e7a2c9b45d18
Revises: c1d5a83e9042
"""

import sqlalchemy as sa
from alembic import op

revision = "e7a2c9b45d18"
down_revision = "c1d5a83e9042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so every pre-existing row reads as an empty list rather
    # than NULL -- `envelope_from_row` tests this for emptiness to decide
    # whether to fall back to the legacy lists, and `None` would make that
    # decision a TypeError instead of a branch.
    op.add_column(
        "template_manifests",
        sa.Column("objects", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("template_manifests", "objects")
