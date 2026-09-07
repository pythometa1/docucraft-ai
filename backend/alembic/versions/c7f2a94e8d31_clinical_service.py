"""The clinical service: a study book and the numbered document registry.

The second per-industry vertical with domain records of its own. A study
document is not just a generated file: it is a numbered, study-addressed
record (CSR-0001, PA-0003) whose snapshot must keep saying what it said even
after the study-book row is edited -- so it gets a registry row linking the
number, the study as reported, and the document version that carries it.

Number sequences reuse the `number_sequences` table the invoice revision
created; each clinical document type allocates under its own key, so the
counters never collide and never share.

Unlike `a9d4f7e21c85`, there is NO taxonomy backfill here: the Clinical
function and its four document types have shipped in `app.bootstrap` since the
first backend commit, so every existing install already carries the lookup
rows. The asymmetry is deliberate, not an omission.

`RLS_TABLES` is exported here rather than added to the row-level-security
revision, per the convention that revision itself states. The policy DDL is
restated rather than imported: a migration has to keep doing what it did the
day it ran.

Revision ID: c7f2a94e8d31
Revises: a9d4f7e21c85
"""

import sqlalchemy as sa
from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "c7f2a94e8d31"
down_revision = "a9d4f7e21c85"
branch_labels = None
depends_on = None

#: Identical strings to the ones `e5b26f0d71a4` uses. They have to be: a policy
#: that reads a setting nobody writes fails closed.
POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: The customer tables this revision creates. Read by `tests/test_rls.py`.
RLS_TABLES = ("studies", "clinical_documents")


def rls_statements(tables=RLS_TABLES) -> list[str]:
    statements: list[str] = []
    for table in tables:
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        statements.append(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING (org_id = current_setting('{ORG_GUC}', true)) "
            f"WITH CHECK (org_id = current_setting('{ORG_GUC}', true))"
        )
        statements.append(
            f"CREATE POLICY {MAINTENANCE_POLICY} ON {table} "
            f"USING (current_setting('{MAINTENANCE_GUC}', true) = 'on') "
            f"WITH CHECK (current_setting('{MAINTENANCE_GUC}', true) = 'on')"
        )
    return statements


def drop_statements(tables=RLS_TABLES) -> list[str]:
    statements: list[str] = []
    for table in tables:
        statements.append(f"DROP POLICY IF EXISTS {MAINTENANCE_POLICY} ON {table}")
        statements.append(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        statements.append(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    return statements


def _dialect() -> str:
    return op.get_context().dialect.name


def upgrade() -> None:
    op.create_table(
        "studies",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("protocol_number", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("sponsor", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("indication", sa.String(), nullable=True),
        sa.Column("principal_investigator", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_studies_org_id", "studies", ["org_id"])

    op.create_table(
        "clinical_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("number", sa.String(), nullable=False),
        sa.Column("study_id", sa.String(), nullable=True),
        sa.Column("study_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("version_label", sa.String(), nullable=True),
        sa.Column("manifest_id", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("document_version_id", sa.String(), nullable=True),
        sa.Column("document_date", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="final"),
        sa.Column("qa_passed", sa.Boolean(), nullable=False, server_default=sa.text("1")
                  if op.get_context().dialect.name == "sqlite" else sa.text("true")),
        sa.Column("source_record", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "number", name="uq_clinical_documents_org_number"),
    )
    op.create_index("ix_clinical_documents_org_id", "clinical_documents", ["org_id"])
    op.create_index("ix_clinical_documents_org_study", "clinical_documents",
                    ["org_id", "study_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    op.drop_index("ix_clinical_documents_org_study", "clinical_documents")
    op.drop_index("ix_clinical_documents_org_id", "clinical_documents")
    op.drop_table("clinical_documents")
    op.drop_index("ix_studies_org_id", "studies")
    op.drop_table("studies")
