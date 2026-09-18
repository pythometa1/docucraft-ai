"""The Safety/Pharmacovigilance module: products, reporting intervals, and the
case store.

One migration for the whole of §4 rather than a table per milestone. The
foreign-key graph runs case -> product -> report -> RSI and back again, and a
schema delivered in pieces means the purge cascade is rewritten every time a
piece lands. Later milestones add columns; they do not add the shape.

`pv_case_originals` is created here although M3 is what fills it. A table that
keeps un-masked source text apart from everything that reads has to exist
before any un-masked text arrives, or the separation it provides is a
separation that began late.

`RLS_TABLES` exported per convention; policy DDL restated, never imported.

Revision ID: a1f7c3e94b62
Revises: b6c14f8a2d90
"""

import sqlalchemy as sa
from alembic import op

from app.retrieval.vector import DEFAULT_DIMENSIONS
from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "a1f7c3e94b62"
down_revision = "b6c14f8a2d90"
branch_labels = None
depends_on = None

POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

RLS_TABLES = (
    "pv_mapping_profiles", "pv_products", "pv_approval_statuses",
    "pv_cases", "pv_deid_items", "pv_documents", "pv_literature_refs",
    "pv_members", "pv_report_instances", "pv_rsi_versions",
    "pv_safety_actions", "pv_safety_concerns", "pv_signals", "pv_studies",
    "pv_case_drugs", "pv_case_events", "pv_case_labs",
    "pv_case_narratives", "pv_case_originals", "pv_chunks", "pv_due_dates",
    "pv_duplicate_candidates", "pv_exports", "pv_exposures",
    "pv_rsi_listed_terms", "pv_sections", "pv_section_drafts",
    "pv_citations",
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


def upgrade() -> None:
    op.create_table(
        "pv_mapping_profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_system", sa.String(), nullable=True),
        sa.Column("column_map", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_mapping_profiles_org_id", "pv_mapping_profiles", ["org_id"])

    op.create_table(
        "pv_products",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("inn", sa.String(), nullable=True),
        sa.Column("mah_name", sa.String(), nullable=True),
        sa.Column("atc_code", sa.String(), nullable=True),
        sa.Column("ibd", sa.Date(), nullable=True),
        sa.Column("dibd", sa.Date(), nullable=True),
        sa.Column("formulations", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("routes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("approved_indications", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("development_indications", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("regions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="setup"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_pv_products_project"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_products_org_id", "pv_products", ["org_id"])

    op.create_table(
        "pv_approval_statuses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("country", sa.String(), nullable=False),
        sa.Column("approval_date", sa.Date(), nullable=True),
        sa.Column("indication", sa.Text(), nullable=True),
        sa.Column("formulation", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="approved"),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.UniqueConstraint("pv_product_id", "country", "formulation", name="uq_pv_approval_statuses_product_country_form"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_approval_statuses_org_id", "pv_approval_statuses", ["org_id"])
    op.create_index("ix_pv_approval_statuses_org_product", "pv_approval_statuses", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_cases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("worldwide_case_id", sa.String(), nullable=True),
        sa.Column("local_case_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("case_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("report_source", sa.String(), nullable=True),
        sa.Column("study_id", sa.String(), nullable=True),
        sa.Column("country_of_occurrence", sa.String(), nullable=True),
        sa.Column("primary_reporter_qualification", sa.String(), nullable=True),
        sa.Column("initial_receipt_date", sa.Date(), nullable=True),
        sa.Column("latest_receipt_date", sa.Date(), nullable=True),
        sa.Column("is_medically_confirmed", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("is_serious", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("seriousness_criteria", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("case_outcome", sa.String(), nullable=True),
        sa.Column("patient_age", sa.Float(), nullable=True),
        sa.Column("patient_age_group", sa.String(), nullable=True),
        sa.Column("patient_sex", sa.String(), nullable=True),
        sa.Column("is_pregnancy_case", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("is_special_situation", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("special_situation_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("deidentification_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("imported_from", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_cases_org_id", "pv_cases", ["org_id"])
    op.create_index("ix_pv_cases_org_product", "pv_cases", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_cases_product_receipt", "pv_cases", ["pv_product_id", "latest_receipt_date"])
    op.create_index("ix_pv_cases_product_serious", "pv_cases", ["pv_product_id", "is_serious"])
    op.create_index("ix_pv_cases_product_source", "pv_cases", ["pv_product_id", "report_source"])
    op.create_index("ix_pv_cases_worldwide_id", "pv_cases", ["pv_product_id", "worldwide_case_id"])

    op.create_table(
        "pv_deid_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("identifier_type", sa.String(), nullable=False),
        sa.Column("detected_text", sa.Text(), nullable=True),
        sa.Column("context_snippet", sa.Text(), nullable=True),
        sa.Column("proposed_mask", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_deid_items_org_id", "pv_deid_items", ["org_id"])
    op.create_index("ix_pv_deid_items_org_product", "pv_deid_items", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_deid_items_product_status", "pv_deid_items", ["pv_product_id", "status"])

    op.create_table(
        "pv_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=True),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("input_type", sa.String(), nullable=False, server_default="document"),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("blob_path", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("processing_status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_documents_org_id", "pv_documents", ["org_id"])
    op.create_index("ix_pv_documents_org_product", "pv_documents", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_literature_refs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("citation", sa.Text(), nullable=False),
        sa.Column("database", sa.String(), nullable=True),
        sa.Column("search_date", sa.Date(), nullable=True),
        sa.Column("search_strategy_ref", sa.Text(), nullable=True),
        sa.Column("relevance", sa.Text(), nullable=True),
        sa.Column("linked_case_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_literature_refs_org_id", "pv_literature_refs", ["org_id"])
    op.create_index("ix_pv_literature_refs_org_product", "pv_literature_refs", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_members",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("pv_role", sa.String(), nullable=False),
        sa.Column("granted_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.UniqueConstraint("pv_product_id", "user_id", name="uq_pv_members_product_user"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_members_org_id", "pv_members", ["org_id"])
    op.create_index("ix_pv_members_org_product", "pv_members", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_report_instances",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("doc_type_key", sa.String(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("data_lock_point", sa.Date(), nullable=False),
        sa.Column("rsi_version_id", sa.String(), nullable=True),
        sa.Column("meddra_version", sa.String(), nullable=True),
        sa.Column("baseline_report_id", sa.String(), nullable=True),
        sa.Column("regions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="setup"),
        sa.Column("qppv_signoff_by", sa.String(), nullable=True),
        sa.Column("qppv_signoff_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_report_instances_dlp", "pv_report_instances", ["pv_product_id", "data_lock_point"])
    op.create_index("ix_pv_report_instances_org_id", "pv_report_instances", ["org_id"])
    op.create_index("ix_pv_report_instances_org_product", "pv_report_instances", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_rsi_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("rsi_type", sa.String(), nullable=False),
        sa.Column("version_label", sa.String(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("superseded_by", sa.String(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.UniqueConstraint("pv_product_id", "rsi_type", "version_label", name="uq_pv_rsi_versions_product_type_label"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_rsi_versions_org_id", "pv_rsi_versions", ["org_id"])
    op.create_index("ix_pv_rsi_versions_org_product", "pv_rsi_versions", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_safety_actions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("action_date", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_safety_actions_org_id", "pv_safety_actions", ["org_id"])
    op.create_index("ix_pv_safety_actions_org_product", "pv_safety_actions", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_safety_actions_product_date", "pv_safety_actions", ["pv_product_id", "action_date"])

    op.create_table(
        "pv_safety_concerns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("concern_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("meddra_terms", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("first_added_report_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="current"),
        sa.Column("rmp_part_reference", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_safety_concerns_org_id", "pv_safety_concerns", ["org_id"])
    op.create_index("ix_pv_safety_concerns_org_product", "pv_safety_concerns", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_signals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("signal_reference", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("meddra_terms", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("detection_source", sa.String(), nullable=True),
        sa.Column("detection_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("priority", sa.String(), nullable=True),
        sa.Column("evaluation_summary", sa.Text(), nullable=True),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("action_taken", sa.Text(), nullable=True),
        sa.Column("closure_date", sa.Date(), nullable=True),
        sa.Column("linked_case_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("linked_report_instance_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_signals_org_id", "pv_signals", ["org_id"])
    op.create_index("ix_pv_signals_org_product", "pv_signals", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_signals_product_status", "pv_signals", ["pv_product_id", "status"])

    op.create_table(
        "pv_studies",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("study_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("population", sa.Text(), nullable=True),
        sa.Column("planned_enrolment", sa.Integer(), nullable=True),
        sa.Column("actual_enrolment", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("completion_date", sa.Date(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.UniqueConstraint("pv_product_id", "study_id", name="uq_pv_studies_product_study"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_studies_org_id", "pv_studies", ["org_id"])
    op.create_index("ix_pv_studies_org_product", "pv_studies", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_case_drugs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("drug_name", sa.String(), nullable=True),
        sa.Column("is_company_product", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("dose", sa.String(), nullable=True),
        sa.Column("dose_unit", sa.String(), nullable=True),
        sa.Column("frequency", sa.String(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("indication", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("action_taken", sa.String(), nullable=True),
        sa.Column("dechallenge", sa.String(), nullable=True),
        sa.Column("rechallenge", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_case_drugs_case", "pv_case_drugs", ["case_id"])
    op.create_index("ix_pv_case_drugs_org_id", "pv_case_drugs", ["org_id"])
    op.create_index("ix_pv_case_drugs_org_product", "pv_case_drugs", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_case_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("verbatim_term", sa.Text(), nullable=True),
        sa.Column("meddra_llt", sa.String(), nullable=True),
        sa.Column("meddra_pt", sa.String(), nullable=True),
        sa.Column("meddra_hlt", sa.String(), nullable=True),
        sa.Column("meddra_hlgt", sa.String(), nullable=True),
        sa.Column("meddra_soc", sa.String(), nullable=True),
        sa.Column("meddra_version", sa.String(), nullable=True),
        sa.Column("is_serious", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("seriousness_criteria", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("expectedness", sa.String(), nullable=False, server_default="not_assessed"),
        sa.Column("expectedness_rsi_version_id", sa.String(), nullable=True),
        sa.Column("causality_reporter", sa.String(), nullable=True),
        sa.Column("causality_company", sa.String(), nullable=True),
        sa.Column("onset_date", sa.Date(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("is_aesi", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("coding_required", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("suggested_by_system_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_case_events_case", "pv_case_events", ["case_id"])
    op.create_index("ix_pv_case_events_org_id", "pv_case_events", ["org_id"])
    op.create_index("ix_pv_case_events_org_product", "pv_case_events", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_case_events_product_expectedness", "pv_case_events", ["pv_product_id", "expectedness"])
    op.create_index("ix_pv_case_events_product_pt", "pv_case_events", ["pv_product_id", "meddra_pt"])
    op.create_index("ix_pv_case_events_product_soc", "pv_case_events", ["pv_product_id", "meddra_soc"])

    op.create_table(
        "pv_case_labs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("test_name", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("reference_range", sa.String(), nullable=True),
        sa.Column("test_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_case_labs_case", "pv_case_labs", ["case_id"])
    op.create_index("ix_pv_case_labs_org_id", "pv_case_labs", ["org_id"])
    op.create_index("ix_pv_case_labs_org_product", "pv_case_labs", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_case_narratives",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("raw_text_redacted", sa.Text(), nullable=True),
        sa.Column("generated_text", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_case_narratives_case", "pv_case_narratives", ["case_id"])
    op.create_index("ix_pv_case_narratives_org_id", "pv_case_narratives", ["org_id"])
    op.create_index("ix_pv_case_narratives_org_product", "pv_case_narratives", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_case_originals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="narrative"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("last_accessed_by", sa.String(), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_case_originals_case", "pv_case_originals", ["case_id"])
    op.create_index("ix_pv_case_originals_org_id", "pv_case_originals", ["org_id"])
    op.create_index("ix_pv_case_originals_org_product", "pv_case_originals", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=True),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_hint", sa.String(), nullable=True),
        sa.Column("is_table", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("table_id", sa.String(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["pv_documents.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    if _dialect() == "postgresql":
        op.execute(f"ALTER TABLE pv_chunks ADD COLUMN embedding vector({DEFAULT_DIMENSIONS})")
    else:
        op.add_column("pv_chunks", sa.Column("embedding", sa.Text(), nullable=True))
    op.create_index("ix_pv_chunks_document", "pv_chunks", ["document_id"])
    op.create_index("ix_pv_chunks_org_id", "pv_chunks", ["org_id"])
    op.create_index("ix_pv_chunks_org_product", "pv_chunks", ["org_id", "pv_product_id"])

    op.create_table(
        "pv_due_dates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("submission_due_date", sa.Date(), nullable=True),
        sa.Column("basis_note", sa.Text(), nullable=True),
        sa.Column("is_informational", sa.Boolean(), nullable=False, server_default=_bool("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_instance_id"], ["pv_report_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_due_dates_org_id", "pv_due_dates", ["org_id"])
    op.create_index("ix_pv_due_dates_org_report", "pv_due_dates", ["org_id", "report_instance_id"])

    op.create_table(
        "pv_duplicate_candidates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("other_case_id", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("matched_on", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["other_case_id"], ["pv_cases.id"]),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_duplicate_candidates_org_id", "pv_duplicate_candidates", ["org_id"])
    op.create_index("ix_pv_duplicate_candidates_org_product", "pv_duplicate_candidates", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_duplicate_candidates_status", "pv_duplicate_candidates", ["pv_product_id", "status"])

    op.create_table(
        "pv_exports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=False),
        sa.Column("granularity", sa.String(), nullable=False, server_default="combined"),
        sa.Column("options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("blob_path", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.ForeignKeyConstraint(["report_instance_id"], ["pv_report_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_exports_org_id", "pv_exports", ["org_id"])
    op.create_index("ix_pv_exports_org_product", "pv_exports", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_exports_report", "pv_exports", ["report_instance_id"])

    op.create_table(
        "pv_exposures",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_product_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("population_descriptor", sa.Text(), nullable=True),
        sa.Column("measure", sa.String(), nullable=False),
        sa.Column("value_text", sa.String(), nullable=True),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("calculation_method_note", sa.Text(), nullable=True),
        sa.Column("source_document_id", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_product_id"], ["pv_products.id"]),
        sa.ForeignKeyConstraint(["report_instance_id"], ["pv_report_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_exposures_org_id", "pv_exposures", ["org_id"])
    op.create_index("ix_pv_exposures_org_product", "pv_exposures", ["org_id", "pv_product_id"])
    op.create_index("ix_pv_exposures_report", "pv_exposures", ["report_instance_id"])

    op.create_table(
        "pv_rsi_listed_terms",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("rsi_version_id", sa.String(), nullable=False),
        sa.Column("meddra_pt", sa.String(), nullable=False),
        sa.Column("meddra_soc", sa.String(), nullable=True),
        sa.Column("condition_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["rsi_version_id"], ["pv_rsi_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_rsi_listed_terms_org_id", "pv_rsi_listed_terms", ["org_id"])
    op.create_index("ix_pv_rsi_listed_terms_version_pt", "pv_rsi_listed_terms", ["rsi_version_id", "meddra_pt"])

    op.create_table(
        "pv_sections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("report_instance_id", sa.String(), nullable=False),
        sa.Column("section_code", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_container", sa.Boolean(), nullable=False, server_default=_bool("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=_bool("true")),
        sa.Column("guidance_text", sa.Text(), nullable=True),
        sa.Column("table_key", sa.String(), nullable=True),
        sa.Column("source_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("delta_status", sa.String(), nullable=False, server_default="fresh"),
        sa.Column("baseline_section_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_instance_id"], ["pv_report_instances.id"]),
        sa.UniqueConstraint("report_instance_id", "section_code", name="uq_pv_sections_report_code"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_sections_org_id", "pv_sections", ["org_id"])
    op.create_index("ix_pv_sections_org_report", "pv_sections", ["org_id", "report_instance_id"])

    op.create_table(
        "pv_section_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("pv_section_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False, server_default="model"),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("generation_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pv_section_id"], ["pv_sections.id"]),
        sa.UniqueConstraint("pv_section_id", "version", name="uq_pv_section_drafts_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_section_drafts_org_id", "pv_section_drafts", ["org_id"])
    op.create_index("ix_pv_section_drafts_org_section", "pv_section_drafts", ["org_id", "pv_section_id"])

    op.create_table(
        "pv_citations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column("marker", sa.String(), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_id", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("quoted_number", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["pv_section_drafts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pv_citations_org_draft", "pv_citations", ["org_id", "draft_id"])
    op.create_index("ix_pv_citations_org_id", "pv_citations", ["org_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    # Children before parents: the reverse of the order they were created in.
    for table in (
        "pv_citations",
        "pv_section_drafts",
        "pv_sections",
        "pv_rsi_listed_terms",
        "pv_exposures",
        "pv_exports",
        "pv_duplicate_candidates",
        "pv_due_dates",
        "pv_chunks",
        "pv_case_originals",
        "pv_case_narratives",
        "pv_case_labs",
        "pv_case_events",
        "pv_case_drugs",
        "pv_studies",
        "pv_signals",
        "pv_safety_concerns",
        "pv_safety_actions",
        "pv_rsi_versions",
        "pv_report_instances",
        "pv_members",
        "pv_literature_refs",
        "pv_documents",
        "pv_deid_items",
        "pv_cases",
        "pv_approval_statuses",
        "pv_products",
        "pv_mapping_profiles",
    ):
        op.drop_table(table)
