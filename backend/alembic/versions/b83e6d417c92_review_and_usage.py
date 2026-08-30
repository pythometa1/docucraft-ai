"""Somewhere to record what a person thought, and what a model call cost.

Two features, one revision, because they share a column.

**Document review.** A person who is not happy with a finished letter had
nowhere to say so. `review_tasks` is the *engine* parking a question it could
not answer; every row is written by `batch_runner` and nothing a person does can
create one. `:revoke` was the closest thing to a rejection and was not one -- no
reason, no audit row, and it hardcoded `"draft"`, which laundered a `blocked`
document into a clean one that `:approve` would then wave through.
`document_reviews` and `review_comments` are where the objection and the
conversation about it live.

**Usage.** Every provider already returns `input_tokens` and `output_tokens`;
all thirteen call sites discarded them, and `generation_jobs.token_usage` -- the
column the analytics page summed -- was never written by anything, so the token
figure was structurally always zero. `llm_calls` is where they land, with the
rate that was in force frozen onto the row so a later price change cannot
restate last quarter's spend.

**The shared column** is `manifest_generations.document_version_id`. The only
link between a generation and the document it produced was a matching
`blob_path` string, and that link is wrong three ways: it breaks the moment a
document is edited, because `apply_version_text` writes a different path; it is
not unique, because every preview of a row writes the same one; and
`retention.delete_generated_document` deletes by it, so two documents sharing a
path means deleting one destroys the other's lineage. A review needs to reach
the generation, and analytics needs to reach the template, so both need this.

Revision ID: b83e6d417c92
Revises: f4b8d1e07c93
"""

import sqlalchemy as sa
from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "b83e6d417c92"
down_revision = "f4b8d1e07c93"
branch_labels = None
depends_on = None

#: Identical strings to `e5b26f0d71a4`. They have to be: a policy that reads a
#: setting nobody writes fails closed, which is an outage nobody can explain
#: rather than an obvious typo.
POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: The customer tables this revision creates. `tests/test_rls.py` unions every
#: revision's list and checks it against the models, so a table added without
#: this goes red rather than sitting quietly outside the backstop.
RLS_TABLES = ("document_reviews", "review_comments", "llm_calls", "org_model_rates")


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


def _backfill_generation_document(conn) -> None:
    """Point each generation at the version it produced, matched on blob path.

    The lowest `version_no` wins: the original render is the one the generation
    record describes, and later versions are edits of it.

    **This must run with the maintenance bypass held.** Row-level security is
    `FORCE`d on `manifest_generations`, and a migration connection has no
    `app.current_org` set -- so without the bypass the UPDATE matches zero rows
    on PostgreSQL and the migration reports success having done nothing.
    """
    if _dialect() == "postgresql":
        conn.execute(sa.text("""
            UPDATE manifest_generations g
               SET document_version_id = sub.id
              FROM (SELECT DISTINCT ON (org_id, blob_path) id, org_id, blob_path
                      FROM document_versions
                     WHERE blob_path IS NOT NULL
                     ORDER BY org_id, blob_path, version_no ASC) sub
             WHERE sub.blob_path = g.blob_path
               AND sub.org_id = g.org_id
               AND g.document_version_id IS NULL
        """))
    else:
        conn.execute(sa.text("""
            UPDATE manifest_generations
               SET document_version_id = (
                   SELECT v.id FROM document_versions v
                    WHERE v.blob_path = manifest_generations.blob_path
                      AND v.org_id = manifest_generations.org_id
                    ORDER BY v.version_no ASC LIMIT 1)
             WHERE document_version_id IS NULL
               AND blob_path IS NOT NULL
        """))


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column("document_versions", sa.Column("finalized_at", sa.DateTime(), nullable=True))
    op.add_column("manifest_generations",
                  sa.Column("document_version_id", sa.String(), nullable=True))
    op.create_index("ix_manifest_generations_document_version_id",
                    "manifest_generations", ["document_version_id"])
    op.add_column("review_tasks", sa.Column("document_version_id", sa.String(), nullable=True))
    op.add_column("review_tasks", sa.Column("created_by", sa.String(), nullable=True))
    op.create_index("ix_review_tasks_document_version_id",
                    "review_tasks", ["document_version_id"])
    # The unified queue filters on both at once; the existing index is on status
    # alone.
    op.create_index("ix_review_tasks_org_status", "review_tasks", ["org_id", "status"])

    conn = op.get_bind()
    if _dialect() == "postgresql":
        from app.tenancy import maintenance_bypass

        with maintenance_bypass(conn):
            _backfill_generation_document(conn)
    else:
        _backfill_generation_document(conn)

    op.create_table(
        "document_reviews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("document_version_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("assigned_to", sa.String(), nullable=True),
        sa.Column("authored_by", sa.String(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["generated_documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_reviews_org_id", "document_reviews", ["org_id"])
    op.create_index("ix_document_reviews_document_id", "document_reviews", ["document_id"])
    op.create_index("ix_document_reviews_document_version_id",
                    "document_reviews", ["document_version_id"])
    op.create_index("ix_document_reviews_state", "document_reviews", ["state"])
    op.create_index("ix_document_reviews_assigned_to", "document_reviews", ["assigned_to"])
    if _dialect() == "postgresql":
        # One open review per version. Two would mean two people approving
        # independently, which defeats the point of counting approvals at all.
        # The handler refuses it too; this is the backstop that survives a race.
        op.execute(
            "CREATE UNIQUE INDEX uq_one_open_review_per_version "
            "ON document_reviews (document_version_id) WHERE state = 'open'"
        )

    op.create_table(
        "review_comments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("review_id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("span_index", sa.Integer(), nullable=True),
        sa.Column("quoted_text", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["document_reviews.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_comments_org_id", "review_comments", ["org_id"])
    op.create_index("ix_review_comments_review_id", "review_comments", ["review_id"])

    op.create_table(
        "llm_calls",
        # No foreign keys, deliberately. The row records what a call cost and
        # must outlive the document, project or template it was made for --
        # otherwise tidying up a document quietly reduces a bill. It leaves on
        # offboarding with the rest of the org, like every other table.
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False, server_default="generate"),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("subject_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(), nullable=False, server_default="ok"),
        sa.Column("input_rate_micro_usd_per_ktok", sa.Integer(), nullable=True),
        sa.Column("output_rate_micro_usd_per_ktok", sa.Integer(), nullable=True),
        # Nullable on purpose: an unpriced model has NO cost, which is a
        # different statement from a cost of zero.
        sa.Column("cost_micro_usd", sa.Integer(), nullable=True),
        sa.Column("rate_source", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_org_id", "llm_calls", ["org_id"])
    op.create_index("ix_llm_calls_project_id", "llm_calls", ["project_id"])
    op.create_index("ix_llm_calls_operation", "llm_calls", ["operation"])
    op.create_index("ix_llm_calls_subject_id", "llm_calls", ["subject_id"])
    op.create_index("ix_llm_calls_model", "llm_calls", ["model"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_org_created", "llm_calls", ["org_id", "created_at"])

    op.create_table(
        "org_model_rates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_micro_usd_per_ktok", sa.Integer(), nullable=False),
        sa.Column("output_micro_usd_per_ktok", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "model", name="uq_org_model_rate"),
    )
    op.create_index("ix_org_model_rates_org_id", "org_model_rates", ["org_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
        op.execute("DROP INDEX IF EXISTS uq_one_open_review_per_version")

    op.drop_table("org_model_rates")
    op.drop_table("llm_calls")
    op.drop_table("review_comments")
    op.drop_table("document_reviews")

    op.drop_index("ix_review_tasks_org_status", table_name="review_tasks")
    op.drop_index("ix_review_tasks_document_version_id", table_name="review_tasks")
    op.drop_column("review_tasks", "created_by")
    op.drop_column("review_tasks", "document_version_id")
    op.drop_index("ix_manifest_generations_document_version_id",
                  table_name="manifest_generations")
    op.drop_column("manifest_generations", "document_version_id")
    op.drop_column("document_versions", "finalized_at")
    op.drop_column("document_versions", "status_reason")
