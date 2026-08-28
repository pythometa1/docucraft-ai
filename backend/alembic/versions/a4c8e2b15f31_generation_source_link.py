"""link a generation to the upload it came from, and give lineage a key to keep

`ManifestGeneration.source_record` holds a verbatim copy of one spreadsheet row
-- salary, identifiers, name -- captured at generation time so a reviewer could
see what the letter was filled from. Nothing linked it back to the upload, so
when a source file was destroyed on its retention schedule that copy survived
indefinitely. §16 is unambiguous about what that is: "an embedding derived from
deleted data is still derived from it", and a verbatim copy is the least derived
thing there is.

`source_version_id` gives the cascade something to find the row by.
`source_record_key` is what §17 actually asks lineage to carry -- "source
file/version + row/record key", not a copy of every value -- so a letter stays
traceable to the record that produced it after the record itself is scrubbed.

Both are nullable and there is no backfill. A generation written before this
migration has no link to recover: inferring one from timestamps would produce a
cascade that deletes rows on a guess, and the honest state for those rows is
"unknown provenance" rather than a plausible-looking answer.

Revision ID: a4c8e2b15f31
Revises: e5b26f0d71a4
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c8e2b15f31'
down_revision: Union[str, None] = 'e5b26f0d71a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('manifest_generations', sa.Column('source_version_id', sa.String(), nullable=True))
    op.add_column('manifest_generations', sa.Column('source_record_key', sa.String(), nullable=True))
    op.create_index(
        'ix_manifest_generations_source_version_id',
        'manifest_generations',
        ['source_version_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_manifest_generations_source_version_id', table_name='manifest_generations')
    op.drop_column('manifest_generations', 'source_record_key')
    op.drop_column('manifest_generations', 'source_version_id')
