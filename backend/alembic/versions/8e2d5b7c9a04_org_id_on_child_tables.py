"""carry the tenant on every row that can hold customer data

§16 of the architecture record: "Every row capable of holding customer data
carries organization_id, enforced at the query layer rather than by convention
inside endpoint handlers", with row-level security as the backstop "so a
forgotten WHERE clause fails closed instead of leaking".

Nine child tables held real customer payload -- source chunks built from
spreadsheet rows, generated letters, template text, chat messages -- and carried
no tenant at all. They were reachable only through a parent that did, which
works exactly as long as every handler remembers to join through it. That is the
convention §16 says not to rely on, and it is also why row-level security could
not be turned on: there was no column to key a policy against.

The backfill derives each row's tenant from its parent, so it is exact rather
than a guess. The column is then made NOT NULL, because a nullable tenant is a
tenant filter with a hole in it.

Revision ID: 8e2d5b7c9a04
Revises: 7c1f4a9e2b31
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e2d5b7c9a04'
down_revision: Union[str, None] = '7c1f4a9e2b31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: table -> the SELECT that yields this row's tenant, joined from its parent.
#: Ordered so a table whose own backfill reads another table's freshly-filled
#: org_id runs after it (template_versions before template_sections,
#: source_versions before source_chunks).
BACKFILL = [
    ("template_versions", """
        UPDATE template_versions SET org_id = (
            SELECT tf.org_id FROM template_files tf WHERE tf.id = template_versions.template_file_id
        )
    """),
    ("template_sections", """
        UPDATE template_sections SET org_id = (
            SELECT tv.org_id FROM template_versions tv WHERE tv.id = template_sections.template_version_id
        )
    """),
    ("source_versions", """
        UPDATE source_versions SET org_id = (
            SELECT sf.org_id FROM source_files sf WHERE sf.id = source_versions.source_file_id
        )
    """),
    ("source_chunks", """
        UPDATE source_chunks SET org_id = (
            SELECT sv.org_id FROM source_versions sv WHERE sv.id = source_chunks.source_version_id
        )
    """),
    ("document_versions", """
        UPDATE document_versions SET org_id = (
            SELECT gd.org_id FROM generated_documents gd WHERE gd.id = document_versions.document_id
        )
    """),
    ("section_outputs", """
        UPDATE section_outputs SET org_id = (
            SELECT gj.org_id FROM generation_jobs gj WHERE gj.id = section_outputs.job_id
        )
    """),
    ("chat_messages", """
        UPDATE chat_messages SET org_id = (
            SELECT c.org_id FROM conversations c WHERE c.id = chat_messages.conversation_id
        )
    """),
    ("template_library_versions", """
        UPDATE template_library_versions SET org_id = (
            SELECT tl.org_id FROM template_library tl WHERE tl.id = template_library_versions.template_library_id
        )
    """),
    ("template_cluster_members", """
        UPDATE template_cluster_members SET org_id = (
            SELECT tc.org_id FROM template_clusters tc WHERE tc.id = template_cluster_members.cluster_id
        )
    """),
]

TABLES = [table for table, _ in BACKFILL]


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column('org_id', sa.String(), nullable=True))

    for _table, statement in BACKFILL:
        op.execute(statement)

    # An orphan -- a child whose parent is gone -- would keep a NULL tenant and
    # so sit outside every filter. There should be none, but deleting silently
    # would destroy data and leaving it nullable would defeat the column, so
    # the constraint is what decides: if any row is still NULL this migration
    # fails and someone looks at why.
    for table in TABLES:
        # `batch_alter_table`, not a bare `alter_column`. SQLite has no
        # ALTER COLUMN, so alembic's generic implementation emits
        # `ALTER TABLE t ALTER COLUMN c SET NOT NULL` and the database rejects it
        # as a syntax error. Batch mode rebuilds the table instead, and renders
        # as a plain ALTER on PostgreSQL, so one line is correct on both.
        #
        # This was invisible on the machine it was written on: SQLite 3.53 --
        # very recent, and what Homebrew's Python happens to bundle -- accepts
        # the statement. Every older SQLite, CI's included, does not. So a
        # migration the project describes as runnable "on a laptop with nothing
        # installed" in fact required one specific bleeding-edge SQLite.
        with op.batch_alter_table(table) as batch:
            batch.alter_column('org_id', existing_type=sa.String(), nullable=False)
        op.create_index(f'ix_{table}_org_id', table, ['org_id'])


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f'ix_{table}_org_id', table_name=table)
        op.drop_column(table, 'org_id')
