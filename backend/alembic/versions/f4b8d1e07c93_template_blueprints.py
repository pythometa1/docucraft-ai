"""Somewhere to keep a template that is being written.

The product could read a legacy `.docx` into a manifest and it could author
coloured tokens into HTML. Neither produced a template a person could *edit*:
the first gave back a JSON reading to argue with, the second produced markup no
manifest can be compiled from. These two tables are where "put a legacy template
in, get an editable one back, hand-edit it, save it for reuse" is kept.

Two tables rather than columns on `template_manifests`, for reasons that are in
the code rather than in taste. `StaticObject` carries `text_hash`, not text, so
no document can be emitted from a manifest alone; `ManifestEnvelope` requires a
non-empty `template_version_id`, which a template being written from scratch does
not yet have; and `routers/templates._manifest_summary` picks the highest
`version_no` from `template_manifests`, so every authoring save would show an
unpublished draft as the template's current reading.

`RLS_TABLES` is exported here rather than added to the row-level-security
revision, which states the convention itself: "A later revision that creates a
customer table is expected to enable RLS on it in the same breath, exporting its
own RLS_TABLES." `tests/test_rls.py` unions every revision's list and checks it
against the models, so a table added without this goes red rather than quietly
sitting outside the backstop. The policy DDL is restated rather than imported: a
migration has to keep doing what it did the day it ran, and importing a helper
that a later edit changes would rewrite history.

Revision ID: f4b8d1e07c93
Revises: e7a2c9b45d18
"""

import sqlalchemy as sa
from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC

revision = "f4b8d1e07c93"
down_revision = "e7a2c9b45d18"
branch_labels = None
depends_on = None

#: Identical strings to the ones `e5b26f0d71a4` uses. They have to be: a policy
#: that reads a setting nobody writes fails closed, which is an outage nobody can
#: explain rather than an obvious typo.
POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: The customer tables this revision creates. Read by `tests/test_rls.py`.
RLS_TABLES = ("template_blueprints", "template_blueprint_versions")


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
        "template_blueprints",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="blank"),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("current_version_id", sa.String(), nullable=True),
        sa.Column("source_template_version_id", sa.String(), nullable=True),
        sa.Column("template_file_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_template_blueprints_org_id", "template_blueprints", ["org_id"])

    op.create_table(
        "template_blueprint_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("blueprint_id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("body", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("objects", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("findings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("emitted_template_version_id", sa.String(), nullable=True),
        sa.Column("manifest_id", sa.String(), nullable=True),
        sa.Column("parent_version_id", sa.String(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["blueprint_id"], ["template_blueprints.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One row per (blueprint, version). Without it a concurrent save writes a
        # second version 3 and the two are told apart only by their timestamps,
        # which is not enough to say which one `revert-to 3` means.
        sa.UniqueConstraint("blueprint_id", "version_no",
                            name="uq_blueprint_version_no"),
    )
    op.create_index("ix_template_blueprint_versions_org_id",
                    "template_blueprint_versions", ["org_id"])
    op.create_index("ix_template_blueprint_versions_blueprint_id",
                    "template_blueprint_versions", ["blueprint_id"])

    if _dialect() == "postgresql":
        for statement in rls_statements():
            op.execute(statement)


def downgrade() -> None:
    if _dialect() == "postgresql":
        for statement in drop_statements():
            op.execute(statement)
    op.drop_index("ix_template_blueprint_versions_blueprint_id",
                  table_name="template_blueprint_versions")
    op.drop_index("ix_template_blueprint_versions_org_id",
                  table_name="template_blueprint_versions")
    op.drop_table("template_blueprint_versions")
    op.drop_index("ix_template_blueprints_org_id", table_name="template_blueprints")
    op.drop_table("template_blueprints")
