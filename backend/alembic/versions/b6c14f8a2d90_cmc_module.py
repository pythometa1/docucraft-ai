"""The Quality/CMC module: deliverables, sources, and the structured quality
data store.

Sixteen tables, and the six that matter most are the ones a narrative module
has no equivalent for: materials, specifications, tests, batches, results and
batch formula. Those hold the numbers a dossier prints.

`cmc_results.value_text` is NOT NULL and `value_numeric` is nullable, which is
the schema stating the module's central rule: the string the source wrote is
the record, and the parsed number is a convenience for comparing against a
limit. A result reading "Complies" or "ND" has no numeric form and is not
lesser for it. Limits and quantities are String for the same reason -- storing
"0.050" as a float would decide, at insert time, that three significant
figures were two.

`RLS_TABLES` exported per convention; policy DDL restated, never imported.

Revision ID: b6c14f8a2d90
Revises: f4b83d1e9a72
"""

import sqlalchemy as sa
from alembic import op

from app.retrieval.vector import DEFAULT_DIMENSIONS
from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "b6c14f8a2d90"
down_revision = "f4b83d1e9a72"
branch_labels = None
depends_on = None

POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

RLS_TABLES = (
    "cmc_projects", "cmc_sites", "cmc_deliverables", "cmc_sections",
    "cmc_section_drafts", "cmc_citations", "cmc_documents", "cmc_chunks",
    "cmc_materials", "cmc_specifications", "cmc_tests", "cmc_batches",
    "cmc_results", "cmc_batch_formula", "cmc_changes", "cmc_exports",
)


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


def _stamps():
    return (sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False))


def upgrade() -> None:
    op.create_table(
        "cmc_projects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("inn_or_ds_name", sa.String(), nullable=True),
        sa.Column("dosage_form", sa.String(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("route_of_administration", sa.String(), nullable=True),
        sa.Column("submission_type", sa.String(), nullable=True),
        sa.Column("target_regions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("development_phase", sa.String(), nullable=True),
        sa.Column("baseline_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="setup"),
        sa.Column("created_by", sa.String(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_cmc_projects_project"),
    )
    op.create_index("ix_cmc_projects_org_id", "cmc_projects", ["org_id"])

    op.create_table(
        "cmc_sites",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("identifier", sa.String(), nullable=True),
        sa.Column("activities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("gmp_evidence_document_id", sa.String(), nullable=True),
        *_stamps(),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_sites_org_id", "cmc_sites", ["org_id"])
    op.create_index("ix_cmc_sites_org_project", "cmc_sites", ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_deliverables",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("doc_type_key", sa.String(), nullable=False),
        sa.Column("template_source", sa.String(), nullable=False, server_default="builtin"),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="ready"),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cmc_project_id", "doc_type_key",
                            name="uq_cmc_deliverables_project_key"),
    )
    op.create_index("ix_cmc_deliverables_org_id", "cmc_deliverables", ["org_id"])
    op.create_index("ix_cmc_deliverables_org_project", "cmc_deliverables",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_sections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_deliverable_id", sa.String(), nullable=False),
        sa.Column("section_code", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=_bool("true")),
        sa.Column("is_container", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("applicability", sa.String(), nullable=False, server_default="applicable"),
        sa.Column("applicability_justification", sa.Text(), nullable=True),
        sa.Column("guidance_text", sa.Text(), nullable=True),
        sa.Column("table_key", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("baseline_draft_id", sa.String(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_deliverable_id"], ["cmc_deliverables.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cmc_deliverable_id", "section_code",
                            name="uq_cmc_sections_deliverable_code"),
    )
    op.create_index("ix_cmc_sections_org_id", "cmc_sections", ["org_id"])
    op.create_index("ix_cmc_sections_org_deliverable", "cmc_sections",
                    ["org_id", "cmc_deliverable_id"])

    op.create_table(
        "cmc_section_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_section_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("generation_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cmc_section_id"], ["cmc_sections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cmc_section_id", "version",
                            name="uq_cmc_drafts_section_version"),
    )
    op.create_index("ix_cmc_section_drafts_org_id", "cmc_section_drafts", ["org_id"])
    op.create_index("ix_cmc_drafts_org_section", "cmc_section_drafts",
                    ["org_id", "cmc_section_id"])

    op.create_table(
        "cmc_citations",
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
        sa.ForeignKeyConstraint(["draft_id"], ["cmc_section_drafts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_citations_org_id", "cmc_citations", ["org_id"])
    op.create_index("ix_cmc_citations_draft", "cmc_citations", ["draft_id"])

    op.create_table(
        "cmc_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("material_id", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("processing_status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("value_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_documents_org_id", "cmc_documents", ["org_id"])
    op.create_index("ix_cmc_documents_org_project", "cmc_documents",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("material_id", sa.String(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_hint", sa.String(), nullable=True),
        sa.Column("is_table", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("table_id", sa.String(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["cmc_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    if _dialect() == "postgresql":
        op.execute(f"ALTER TABLE cmc_chunks ADD COLUMN embedding vector({DEFAULT_DIMENSIONS})")
    else:
        op.add_column("cmc_chunks", sa.Column("embedding", sa.Text(), nullable=True))
    op.create_index("ix_cmc_chunks_org_id", "cmc_chunks", ["org_id"])
    op.create_index("ix_cmc_chunks_org_project", "cmc_chunks", ["org_id", "cmc_project_id"])
    op.create_index("ix_cmc_chunks_document", "cmc_chunks", ["document_id"])

    # ---- the structured store ----
    op.create_table(
        "cmc_materials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("grade", sa.String(), nullable=True),
        sa.Column("compendial_ref", sa.String(), nullable=True),
        sa.Column("supplier", sa.String(), nullable=True),
        sa.Column("dmf_reference", sa.String(), nullable=True),
        *_stamps(),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_materials_org_id", "cmc_materials", ["org_id"])
    op.create_index("ix_cmc_materials_org_project", "cmc_materials",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_specifications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("material_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False, server_default="1.0"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["material_id"], ["cmc_materials.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_specifications_org_id", "cmc_specifications", ["org_id"])
    op.create_index("ix_cmc_specifications_org_project", "cmc_specifications",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_tests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("material_id", sa.String(), nullable=False),
        sa.Column("spec_version_id", sa.String(), nullable=True),
        sa.Column("test_name", sa.String(), nullable=False),
        sa.Column("method_id", sa.String(), nullable=True),
        sa.Column("method_type", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("acceptance_criterion_text", sa.Text(), nullable=True),
        sa.Column("limit_lower", sa.String(), nullable=True),
        sa.Column("limit_upper", sa.String(), nullable=True),
        sa.Column("limit_operator", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False, server_default="release"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_document_id", sa.String(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["material_id"], ["cmc_materials.id"]),
        sa.ForeignKeyConstraint(["spec_version_id"], ["cmc_specifications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_tests_org_id", "cmc_tests", ["org_id"])
    op.create_index("ix_cmc_tests_org_project", "cmc_tests", ["org_id", "cmc_project_id"])
    op.create_index("ix_cmc_tests_material", "cmc_tests", ["material_id"])

    op.create_table(
        "cmc_batches",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("material_id", sa.String(), nullable=False),
        sa.Column("batch_number", sa.String(), nullable=False),
        sa.Column("batch_size", sa.String(), nullable=True),
        sa.Column("batch_size_unit", sa.String(), nullable=True),
        sa.Column("manufacture_date", sa.Date(), nullable=True),
        sa.Column("site_id", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.Column("scale", sa.String(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["material_id"], ["cmc_materials.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["cmc_sites.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cmc_project_id", "material_id", "batch_number",
                            name="uq_cmc_batches_project_material_number"),
    )
    op.create_index("ix_cmc_batches_org_id", "cmc_batches", ["org_id"])
    op.create_index("ix_cmc_batches_org_project", "cmc_batches",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("test_id", sa.String(), nullable=False),
        sa.Column("storage_condition", sa.String(), nullable=True),
        sa.Column("timepoint_months", sa.Float(), nullable=True),
        sa.Column("orientation", sa.String(), nullable=True),
        # NOT NULL: the reported string is the record. The parsed number below
        # is nullable because "Complies" is a real result with no numeric form.
        sa.Column("value_text", sa.String(), nullable=False),
        sa.Column("value_numeric", sa.Numeric(20, 8), nullable=True),
        sa.Column("operator", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("table_ref", sa.String(), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("verified_by", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("conflict_with_id", sa.String(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["batch_id"], ["cmc_batches.id"]),
        sa.ForeignKeyConstraint(["test_id"], ["cmc_tests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_results_org_id", "cmc_results", ["org_id"])
    op.create_index("ix_cmc_results_org_project", "cmc_results",
                    ["org_id", "cmc_project_id"])
    op.create_index("ix_cmc_results_cell", "cmc_results",
                    ["batch_id", "test_id", "storage_condition", "timepoint_months"])

    op.create_table(
        "cmc_batch_formula",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("cmc_deliverable_id", sa.String(), nullable=True),
        sa.Column("component_material_id", sa.String(), nullable=True),
        sa.Column("component_name", sa.String(), nullable=False),
        sa.Column("function", sa.String(), nullable=True),
        sa.Column("quantity_per_unit", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("percent_ww", sa.String(), nullable=True),
        sa.Column("quantity_per_batch", sa.String(), nullable=True),
        sa.Column("reference_to_standard", sa.String(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("verified_by", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.ForeignKeyConstraint(["cmc_deliverable_id"], ["cmc_deliverables.id"]),
        sa.ForeignKeyConstraint(["component_material_id"], ["cmc_materials.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_batch_formula_org_id", "cmc_batch_formula", ["org_id"])
    op.create_index("ix_cmc_batch_formula_org_project", "cmc_batch_formula",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_changes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("change_reference", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("change_type", sa.String(), nullable=True),
        sa.Column("impacted_section_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("created_by", sa.String(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_changes_org_id", "cmc_changes", ["org_id"])
    op.create_index("ix_cmc_changes_org_project", "cmc_changes",
                    ["org_id", "cmc_project_id"])

    op.create_table(
        "cmc_exports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("cmc_project_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("granularity", sa.String(), nullable=False, server_default="combined"),
        sa.Column("options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cmc_project_id"], ["cmc_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cmc_exports_org_id", "cmc_exports", ["org_id"])
    op.create_index("ix_cmc_exports_org_project", "cmc_exports",
                    ["org_id", "cmc_project_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    for table in ("cmc_exports", "cmc_changes", "cmc_batch_formula", "cmc_results",
                  "cmc_batches", "cmc_tests", "cmc_specifications", "cmc_materials",
                  "cmc_chunks", "cmc_documents", "cmc_citations", "cmc_section_drafts",
                  "cmc_sections", "cmc_deliverables", "cmc_sites", "cmc_projects"):
        op.drop_table(table)
