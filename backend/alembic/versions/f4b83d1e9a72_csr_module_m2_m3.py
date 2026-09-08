"""CSR module, milestones M2-M3: uploaded sources, their retrievable chunks,
and the versioned drafts with resolved citations.

`csr_chunks` is the per-project index the generation engine retrieves from.
The project id is on the row (not just the document's) because retrieval
filters by project BEFORE similarity: a CSR that could score another study's
chunks has already leaked, whatever it does with the ranking afterwards.

`csr_section_drafts` keeps every version -- model output and human edit alike
append rather than overwrite -- because "what did the model write, and what
did the writer change?" is the question a regulator asks about an AI-assisted
document. `generation_params` holds the retrieved chunk ids, k and prompt
version, so the evidence behind a paragraph is recoverable years later.

Revision ID: f4b83d1e9a72
Revises: e2a91c5f7b48
"""

import sqlalchemy as sa
from alembic import op

from app.retrieval.vector import DEFAULT_DIMENSIONS
from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "f4b83d1e9a72"
down_revision = "e2a91c5f7b48"
branch_labels = None
depends_on = None

POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

RLS_TABLES = ("csr_documents", "csr_chunks", "csr_section_drafts", "csr_citations")


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


def _bool(value: str):
    return sa.text({"true": "1", "false": "0"}[value]) if _dialect() == "sqlite" \
        else sa.text(value)


def upgrade() -> None:
    op.create_table(
        "csr_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("csr_project_id", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("processing_status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["csr_project_id"], ["csr_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_csr_documents_org_id", "csr_documents", ["org_id"])
    op.create_index("ix_csr_documents_org_project", "csr_documents",
                    ["org_id", "csr_project_id"])

    op.create_table(
        "csr_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("csr_project_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_hint", sa.String(), nullable=True),
        sa.Column("is_table", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("table_id", sa.String(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["csr_project_id"], ["csr_projects.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["csr_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # The embedding column's type differs by dialect exactly as VectorColumn does.
    if _dialect() == "postgresql":
        op.execute(f"ALTER TABLE csr_chunks ADD COLUMN embedding vector({DEFAULT_DIMENSIONS})")
    else:
        op.add_column("csr_chunks", sa.Column("embedding", sa.Text(), nullable=True))
    op.add_column("csr_chunks", sa.Column("created_at", sa.DateTime(), nullable=True))
    op.create_index("ix_csr_chunks_org_id", "csr_chunks", ["org_id"])
    op.create_index("ix_csr_chunks_org_project", "csr_chunks", ["org_id", "csr_project_id"])
    op.create_index("ix_csr_chunks_document", "csr_chunks", ["document_id"])

    op.create_table(
        "csr_section_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("csr_section_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("generation_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["csr_section_id"], ["csr_sections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("csr_section_id", "version",
                            name="uq_csr_drafts_section_version"),
    )
    op.create_index("ix_csr_drafts_org_id", "csr_section_drafts", ["org_id"])
    op.create_index("ix_csr_drafts_org_section", "csr_section_drafts",
                    ["org_id", "csr_section_id"])

    op.create_table(
        "csr_citations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column("marker", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("chunk_id", sa.String(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("table_ref", sa.String(), nullable=True),
        sa.Column("cited_value", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["csr_section_drafts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_csr_citations_org_id", "csr_citations", ["org_id"])
    op.create_index("ix_csr_citations_draft", "csr_citations", ["draft_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    op.drop_index("ix_csr_citations_draft", "csr_citations")
    op.drop_index("ix_csr_citations_org_id", "csr_citations")
    op.drop_table("csr_citations")
    op.drop_index("ix_csr_drafts_org_section", "csr_section_drafts")
    op.drop_index("ix_csr_drafts_org_id", "csr_section_drafts")
    op.drop_table("csr_section_drafts")
    op.drop_index("ix_csr_chunks_document", "csr_chunks")
    op.drop_index("ix_csr_chunks_org_project", "csr_chunks")
    op.drop_index("ix_csr_chunks_org_id", "csr_chunks")
    op.drop_table("csr_chunks")
    op.drop_index("ix_csr_documents_org_project", "csr_documents")
    op.drop_index("ix_csr_documents_org_id", "csr_documents")
    op.drop_table("csr_documents")
