"""The invoice service: a client book, org-scoped number sequences, and the
invoice registry itself.

The first per-industry vertical with domain records of its own. An invoice is
not just a generated document: it is a numbered, customer-addressed,
money-bearing record, so it gets a registry row linking the number, the
customer as billed, the totals and the document version that carries them.

`number_sequences` exists because the global `counters` table is keyed by bare
name with no org_id -- it cannot sit behind row-level security, and two tenants
would share one numbering. Allocation locks the row for the transaction on
PostgreSQL; SQLite's single writer makes the plain path equivalent there.

The revision also backfills the Finance taxonomy (`function` "Finance" and its
document types) into `lookup_values` for organisations that already exist --
`python -m app.bootstrap` refuses to re-run against a populated database, so a
seed-time-only addition would reach new installs and skip every live one. The
inserts run under the maintenance GUC because `lookup_values` is behind FORCE
row-level security and a migration connects as the owner, whom FORCE does not
exempt.

`RLS_TABLES` is exported here rather than added to the row-level-security
revision, per the convention that revision itself states. The policy DDL is
restated rather than imported: a migration has to keep doing what it did the
day it ran.

Revision ID: a9d4f7e21c85
Revises: 4a1c8d2e6f57
"""

import sqlalchemy as sa
from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "a9d4f7e21c85"
down_revision = "4a1c8d2e6f57"
branch_labels = None
depends_on = None

#: Identical strings to the ones `e5b26f0d71a4` uses. They have to be: a policy
#: that reads a setting nobody writes fails closed.
POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: The customer tables this revision creates. Read by `tests/test_rls.py`.
RLS_TABLES = ("customers", "number_sequences", "invoices")

#: The vertical's taxonomy, backfilled for existing organisations. Mirrors
#: `app.bootstrap.FUNCTIONS` / `DOCUMENT_TYPES` at the time this revision was
#: written -- restated rather than imported, because a migration has to keep
#: doing what it did the day it ran.
FINANCE_FUNCTION = "Finance"
FINANCE_DOCUMENT_TYPES = ("Invoice", "Quotation", "Purchase Order")


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


def _backfill_finance_taxonomy() -> None:
    """One `lookup_values` row per org for Finance and each of its doc types.

    Idempotent by construction: existing rows are read first and only the
    missing ones inserted, so re-running the migration (or racing a bootstrap
    that has since learned the same values) cannot duplicate an entry.
    """
    import uuid

    bind = op.get_bind()
    if _dialect() == "postgresql":
        bind.execute(sa.text(f"SELECT set_config('{MAINTENANCE_GUC}', 'on', true)"))

    org_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM organizations"))]
    if not org_ids:
        return

    existing = {
        (row[0], row[1], row[2])
        for row in bind.execute(sa.text(
            "SELECT org_id, kind, value FROM lookup_values "
            "WHERE kind IN ('function', 'document_type')"
        ))
    }
    insert = sa.text(
        "INSERT INTO lookup_values (id, org_id, kind, value, parent_value, sort_order, is_active) "
        "VALUES (:id, :org_id, :kind, :value, :parent_value, :sort_order, :is_active)"
    )
    for org_id in org_ids:
        if (org_id, "function", FINANCE_FUNCTION) not in existing:
            bind.execute(insert, {
                "id": str(uuid.uuid4()), "org_id": org_id, "kind": "function",
                "value": FINANCE_FUNCTION, "parent_value": None,
                "sort_order": 9, "is_active": True,
            })
        for i, doc_type in enumerate(FINANCE_DOCUMENT_TYPES):
            if (org_id, "document_type", doc_type) not in existing:
                bind.execute(insert, {
                    "id": str(uuid.uuid4()), "org_id": org_id, "kind": "document_type",
                    "value": doc_type, "parent_value": FINANCE_FUNCTION,
                    "sort_order": i, "is_active": True,
                })


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("tax_id", sa.String(), nullable=True),
        sa.Column("default_currency", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customers_org_id", "customers", ["org_id"])

    op.create_table(
        "number_sequences",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False, server_default="INV-"),
        sa.Column("padding", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("next_value", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
        # One sequence per (org, key). Without it two concurrent first
        # allocations each create a sequence and both hand out number 1.
        sa.UniqueConstraint("org_id", "key", name="uq_number_sequences_org_key"),
    )
    op.create_index("ix_number_sequences_org_id", "number_sequences", ["org_id"])

    op.create_table(
        "invoices",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("number", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=True),
        sa.Column("customer_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("manifest_id", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("document_version_id", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False, server_default="USD"),
        sa.Column("subtotal", sa.Numeric(18, 2), nullable=True),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("total", sa.Numeric(18, 2), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="issued"),
        sa.Column("qa_passed", sa.Boolean(), nullable=False, server_default=sa.text("1")
                  if op.get_context().dialect.name == "sqlite" else sa.text("true")),
        sa.Column("source_record", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "number", name="uq_invoices_org_number"),
    )
    op.create_index("ix_invoices_org_id", "invoices", ["org_id"])
    op.create_index("ix_invoices_org_customer", "invoices", ["org_id", "customer_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)

    _backfill_finance_taxonomy()


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    op.drop_index("ix_invoices_org_customer", "invoices")
    op.drop_index("ix_invoices_org_id", "invoices")
    op.drop_table("invoices")
    op.drop_index("ix_number_sequences_org_id", "number_sequences")
    op.drop_table("number_sequences")
    op.drop_index("ix_customers_org_id", "customers")
    op.drop_table("customers")
