"""embeddings, mapping memory and reviewer corrections -- §10's memory, on disk

§10 makes PostgreSQL the system of record and pgvector "semantic memory within
the same data platform", and asks for the approved mapping to be persisted "as
structured relational/JSONB data, not only as an embedding". None of that
existed. Both stores were process dictionaries, so a deploy erased every
embedded field description and every mapping a reviewer had approved: the ninth
offer letter from a customer was compiled against the same blank history as the
first, and a correction made in March was gone by April.

Three tables, each carrying org_id NOT NULL, because §16 wants the tenant filter
in the query rather than in whoever remembered to join back to a parent.

The vector column renders as pgvector's `vector(1024)` on PostgreSQL and as a
JSON array of floats on SQLite -- see app/retrieval/embeddings.py for why both
paths exist. The dimension is written here as a literal rather than imported
from `DEFAULT_DIMENSIONS`: a migration is a historical record of the schema at
one point in time, and a constant that later changes must not retroactively
rewrite what this revision built.

Two PostgreSQL-only statements, both guarded on the dialect so the suite's
SQLite run does not trip over them:

  * `CREATE EXTENSION IF NOT EXISTS vector` -- the extension is what makes the
    column type exist at all, and creating it here means a database that cannot
    have it fails at migration time, in front of whoever is deploying, instead
    of at the first INSERT in front of a customer. It needs a superuser (or
    rds_superuser); that is a deployment prerequisite, not something this
    migration can work around.
  * an HNSW index over the vector column. §18 lists "pgvector recall degrades as
    mapping memory grows -- plan an HNSW index strategy and per-tenant
    partitioning before the corpus approaches the low millions of vectors" as a
    scaling limit worth watching. Building it on an empty table costs one
    statement; building it later is an index build over a table every tenant is
    reading.

These three tables carry `org_id` and therefore belong inside the row-level
security backstop revision e5b26f0d71a4 sets up. They are not covered here: that
revision runs *after* this one and owns one frozen list of covered tables, so the
coverage belongs in its `RLS_TABLES` rather than in a second, competing set of
`CREATE POLICY` statements here -- two migrations creating the same policy is a
hard failure on PostgreSQL.

Revision ID: b2f47c9e1a63
Revises: 9f3a6c1d8b25
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.retrieval.embeddings import VectorColumn, drop_hnsw_index_ddl, hnsw_index_ddl


# revision identifiers, used by Alembic.
revision: str = 'b2f47c9e1a63'
down_revision: Union[str, None] = '9f3a6c1d8b25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Fixed at the width `HashingEmbedder` produces today. pgvector columns are
#: fixed-width, so changing this is a new migration and a re-embed, never an
#: edit here.
DIMENSIONS = 1024

def _dialect() -> str:
    # `op.get_context().dialect` rather than `op.get_bind().dialect`: there is no
    # bind in Alembic's offline (--sql) mode, and the Postgres DDL this revision
    # emits is worth being able to generate and inspect without a server.
    return op.get_context().dialect.name


def upgrade() -> None:
    is_postgres = _dialect() == "postgresql"

    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'embeddings',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('record_id', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('vector', VectorColumn(DIMENSIONS), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('dims', sa.Integer(), nullable=False),
        sa.Column('doc_type', sa.String(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # Unique within the tenant, not globally: two customers may legitimately
        # use the same field id, and neither should be able to overwrite the
        # other's row by choosing one.
        sa.UniqueConstraint('org_id', 'record_id', name='uq_embeddings_org_record'),
    )
    op.create_index('ix_embeddings_org_id', 'embeddings', ['org_id'])
    op.create_index('ix_embeddings_org_doc_type', 'embeddings', ['org_id', 'doc_type'])
    if is_postgres:
        op.execute(hnsw_index_ddl('embeddings', 'vector'))

    op.create_table(
        'mapping_memory',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('field_key', sa.String(), nullable=False),
        sa.Column('source_column', sa.String(), nullable=False),
        # The normalised form the uniqueness constraint keys on. Without it
        # "Joining Dt" and "joining_dt" are two rows, the approval count splits
        # between them, and §13's largest single-signal weight becomes a
        # function of how the reviewer happened to type the column name.
        sa.Column('source_column_key', sa.String(), nullable=False),
        sa.Column('transform', sa.String(), nullable=False),
        sa.Column('on_missing', sa.String(), nullable=False),
        sa.Column('field_type', sa.String(), nullable=False),
        sa.Column('context_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('doc_type', sa.String(), nullable=True),
        sa.Column('template_family_id', sa.String(), nullable=True),
        sa.Column('approval_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rejection_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_approved_at', sa.DateTime(), nullable=True),
        sa.Column('last_approved_by', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'org_id', 'field_key', 'source_column_key', 'transform',
            name='uq_mapping_memory_triple',
        ),
    )
    op.create_index('ix_mapping_memory_org_id', 'mapping_memory', ['org_id'])
    op.create_index('ix_mapping_memory_org_field', 'mapping_memory', ['org_id', 'field_key'])

    op.create_table(
        'mapping_memory_sharing',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        # Defaults to false at the column level as well as in the application:
        # a row that somehow arrives without a value is opted out, because the
        # failure mode of the other default is a customer's structural patterns
        # in a pool they never agreed to.
        sa.Column('opted_in', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', name='uq_mapping_memory_sharing_org'),
    )
    op.create_index('ix_mapping_memory_sharing_org_id', 'mapping_memory_sharing', ['org_id'])

    op.create_table(
        'reviewer_corrections',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('field_key', sa.String(), nullable=False),
        sa.Column('accepted_column', sa.String(), nullable=False),
        sa.Column('rejected_column', sa.String(), nullable=True),
        sa.Column('corrected_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_reviewer_corrections_org_id', 'reviewer_corrections', ['org_id'])
    op.create_index(
        'ix_reviewer_corrections_org_field', 'reviewer_corrections', ['org_id', 'field_key'],
    )


def downgrade() -> None:
    is_postgres = _dialect() == "postgresql"

    op.drop_index('ix_reviewer_corrections_org_field', table_name='reviewer_corrections')
    op.drop_index('ix_reviewer_corrections_org_id', table_name='reviewer_corrections')
    op.drop_table('reviewer_corrections')

    op.drop_index('ix_mapping_memory_sharing_org_id', table_name='mapping_memory_sharing')
    op.drop_table('mapping_memory_sharing')

    op.drop_index('ix_mapping_memory_org_field', table_name='mapping_memory')
    op.drop_index('ix_mapping_memory_org_id', table_name='mapping_memory')
    op.drop_table('mapping_memory')

    if is_postgres:
        op.execute(drop_hnsw_index_ddl('embeddings', 'vector'))
    op.drop_index('ix_embeddings_org_doc_type', table_name='embeddings')
    op.drop_index('ix_embeddings_org_id', table_name='embeddings')
    op.drop_table('embeddings')

    # The extension is deliberately not dropped. It is database-wide, it may be
    # in use by something this revision knows nothing about, and dropping it
    # would take those columns with it.
