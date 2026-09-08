"""CSR module, milestone M1: the project extension, its template choice, and
the ICH E3 section tree.

A csr_project row extends a portal project rather than replacing it -- the
portal project supplies org scoping, membership and the audit surface; this
table adds the study link and the CSR-specific descriptors. Sections are
seeded per project from the built-in ICH E3 structure (app/csr/ich_e3.py)
when the template is chosen, so the table starts empty here.

`RLS_TABLES` exported per convention; policy DDL restated, never imported.
Later milestones add csr_documents/chunks/drafts/citations/exports with their
own revisions.

Revision ID: e2a91c5f7b48
Revises: c7f2a94e8d31
"""

import sqlalchemy as sa
from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "e2a91c5f7b48"
down_revision = "c7f2a94e8d31"
branch_labels = None
depends_on = None

POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: Read by `tests/test_rls.py`.
RLS_TABLES = ("csr_projects", "csr_templates", "csr_sections")


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


def _bool_default(value: str) -> sa.sql.elements.TextClause:
    return sa.text({"true": "1", "false": "0"}[value]) if _dialect() == "sqlite" \
        else sa.text(value)


def upgrade() -> None:
    op.create_table(
        "csr_projects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("study_id", sa.String(), nullable=True),
        sa.Column("compound_name", sa.String(), nullable=True),
        sa.Column("therapeutic_area", sa.String(), nullable=True),
        sa.Column("blinding", sa.String(), nullable=True),
        sa.Column("study_design_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="setup"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_csr_projects_project"),
    )
    op.create_index("ix_csr_projects_org_id", "csr_projects", ["org_id"])

    op.create_table(
        "csr_templates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("csr_project_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="builtin_ich_e3"),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("parsed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["csr_project_id"], ["csr_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_csr_templates_org_id", "csr_templates", ["org_id"])

    op.create_table(
        "csr_sections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("csr_project_id", sa.String(), nullable=False),
        sa.Column("section_number", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=_bool_default("true")),
        sa.Column("is_container", sa.Boolean(), nullable=False, server_default=_bool_default("false")),
        sa.Column("status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("guidance_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["csr_project_id"], ["csr_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("csr_project_id", "section_number",
                            name="uq_csr_sections_project_number"),
    )
    op.create_index("ix_csr_sections_org_id", "csr_sections", ["org_id"])
    op.create_index("ix_csr_sections_org_project", "csr_sections",
                    ["org_id", "csr_project_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    op.drop_index("ix_csr_sections_org_project", "csr_sections")
    op.drop_index("ix_csr_sections_org_id", "csr_sections")
    op.drop_table("csr_sections")
    op.drop_index("ix_csr_templates_org_id", "csr_templates")
    op.drop_table("csr_templates")
    op.drop_index("ix_csr_projects_org_id", "csr_projects")
    op.drop_table("csr_projects")
