"""row-level security, so a forgotten WHERE clause fails closed instead of leaking

§16 names this exactly: "PostgreSQL row-level security as the backstop, so a
forgotten WHERE clause fails closed instead of leaking." Every tenant filter in
this codebase is currently a hand-written `WHERE org_id = user.org_id` inside a
handler. There are hundreds of them, they are correct today, and the failure
mode of the one that is added next month without the clause is a cross-tenant
disclosure that no test catches and no reviewer notices -- §19 ranks that as
"contract breach; potentially existential".

Migration 8e2d5b7c9a04 gave every customer-data table an `org_id`, which is what
a policy needs to key on. This turns that column into an enforced boundary.

How it works. Each covered table gets RLS enabled, FORCEd, and one policy
comparing `org_id` against the session GUC `app.current_org`, which
`app/tenancy.py` sets from the authenticated user's organisation. When the GUC
is unset, `current_setting('app.current_org', true)` is NULL, `org_id = NULL` is
NULL, and NULL is not TRUE -- so an unscoped session sees zero rows rather than
everything. That asymmetry is the whole point: the way to get this wrong now
returns nothing instead of returning somebody else's salary letters.

FORCE matters as much as ENABLE. A table's owner bypasses its own policies
unless RLS is forced, and this application connects as the schema owner in every
deployment that exists today. Without FORCE the policies below would be real,
correct and completely inert.

What this is and is not. The GUC is set by the application, so this does not
defend against application code that deliberately names the wrong tenant -- it
defends against application code that names no tenant at all, which is the
mistake people actually make. §16 calls it a backstop, and a backstop is what it
is; the per-handler filters stay.

The maintenance policy. FORCE applies to data migrations too, so a future
backfill that runs `UPDATE source_chunks SET ...` would silently touch zero rows
-- a worse failure than the one being fixed, because it looks like success. The
second policy makes the escape hatch explicit and greppable rather than
accidental: a migration sets `app.rls_bypass` to 'on' for its own transaction
(see `app.tenancy.maintenance_bypass`) and nothing else ever does. It grants no
authority the application did not already have, since anything able to set
`app.rls_bypass` could equally set `app.current_org` to any tenant it liked.

SQLite is a no-op. The test suite runs on SQLite and SQLite has no row-level
security, so `upgrade()` checks the dialect and returns. That means RLS itself
cannot be exercised by the suite -- which is why `tests/test_rls.py` pins the
regression that actually matters instead: that this migration covers every table
carrying `org_id`, so a customer table added next year cannot be forgotten.

Revision ID: e5b26f0d71a4
Revises: d3f81a5c47b9
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

from app.tenancy import MAINTENANCE_GUC, ORG_GUC


# revision identifiers, used by Alembic.
revision: str = 'e5b26f0d71a4'
down_revision: Union[str, None] = 'd3f81a5c47b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The GUC names are imported from `app.tenancy` rather than restated here.
#: They have to be identical in both places or the policy reads a setting nobody
#: writes -- which fails closed, and therefore fails as an outage nobody can
#: explain rather than as an obvious typo.
POLICY = "org_isolation"
MAINTENANCE_POLICY = "rls_maintenance"

#: Every table carrying `org_id` that no earlier revision has already covered,
#: as of this revision. Written out rather than derived from `Base.metadata` at
#: run time, because a migration has to keep doing what it did the day it ran --
#: a list that follows the models would change the meaning of an already-applied
#: revision.
#:
#: A later revision that creates a customer table is expected to enable RLS on
#: it in the same breath, exporting its own `RLS_TABLES`. `tests/test_rls.py`
#: unions every revision's list and checks the result against the models, so
#: neither this list nor that convention can quietly stop covering something.
RLS_TABLES = (
    "audit_logs",
    "chat_messages",
    "conversations",
    "deletion_certificates",
    "document_versions",
    "draft_documents",
    "embeddings",
    "field_dictionary",
    "generated_documents",
    "generation_jobs",
    "lookup_values",
    "manifest_bindings",
    "manifest_generations",
    "mapping_memory",
    "mapping_memory_sharing",
    "mappings",
    "operation_timings",
    "org_data_policies",
    "projects",
    "qa_failure_logs",
    "review_tasks",
    "reviewer_corrections",
    "section_outputs",
    "source_chunks",
    "source_files",
    "source_versions",
    "suggestion_logs",
    "template_cluster_members",
    "template_clusters",
    "template_families",
    "template_files",
    "template_library",
    "template_library_versions",
    "template_manifests",
    "template_sections",
    "template_versions",
)

#: Tables carrying `org_id` that are deliberately left outside RLS. Each needs a
#: reason, and the reason has to be about a chicken-and-egg problem rather than
#: convenience -- an entry here is a decision to leave a table outside the
#: backstop, so it should be uncomfortable to add.
RLS_EXEMPT = {
    "users": (
        "Authentication has to find the user before it knows which tenant to "
        "scope to. A policy here would make login unsolvable without a bypass, "
        "and a bypass on the identity table is worse than no policy on it. "
        "`users.org_id` is a real foreign key and every application read of it "
        "is already org-scoped."
    ),
}


def rls_statements(tables=RLS_TABLES) -> list[str]:
    """The exact DDL `upgrade()` runs, as strings.

    Returned rather than executed inline so a test can assert what the migration
    emits without needing a PostgreSQL server -- the suite runs on SQLite, where
    none of this can be observed by running it.
    """
    statements: list[str] = []
    for table in tables:
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Without FORCE the schema owner -- which is who this application
        # connects as -- silently bypasses every policy below.
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
    # `op.get_context().dialect` rather than `op.get_bind().dialect`: there is no
    # bind in Alembic's offline (--sql) mode, and this DDL is worth being able to
    # generate and read without a server to run it against.
    return op.get_context().dialect.name


def upgrade() -> None:
    if _dialect() != "postgresql":
        # SQLite has no row-level security. Erroring here would take the whole
        # test suite down for a control it cannot express; skipping keeps the
        # migration chain runnable everywhere and leaves the Postgres path
        # unchanged.
        return
    for statement in rls_statements():
        op.execute(statement)


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    for statement in drop_statements():
        op.execute(statement)
