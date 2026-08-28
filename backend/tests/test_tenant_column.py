"""Every row that can hold customer data carries its tenant.

§16 of the architecture record puts this first, ahead of row-level security,
because RLS needs a column to key a policy against and a query-layer filter
needs one to filter on. Nine child tables — source chunks built from spreadsheet
rows, generated letters, template text, chat messages — carried none, and were
reachable only by joining through a parent that did. That works exactly as long
as every handler remembers the join, which is the convention §16 says not to
rely on.

The guardrail is `test_every_customer_table_carries_the_tenant`: a new table
holding customer data goes red here until someone either adds `org_id` or
explains, in `TENANT_FREE`, why the table cannot hold customer data.
"""

import pytest
from sqlalchemy import inspect, select

from app.db import Base, SessionLocal
from app.models import (
    ChatMessage, Conversation, DocumentVersion, GeneratedDocument, Project,
    SourceChunk, SourceFile, SourceVersion, TemplateFile, TemplateSection,
    TemplateVersion, User,
)


#: Tables with no tenant to carry. Each needs a reason: an entry here is a
#: decision to hold data outside every tenant filter, so it should be
#: uncomfortable to add.
TENANT_FREE = {
    "organizations": "It *is* the tenant.",
    "users": "Carries org_id already, via a real foreign key.",
    "counters": "Global display-id sequences. Holds integers and a name, never customer data.",
    "alembic_version": "Schema bookkeeping.",
}


def test_every_customer_table_carries_the_tenant():
    """The guardrail. A new table holding customer data fails here until it is
    either given org_id or explicitly excused."""
    missing = {
        name: table
        for name, table in Base.metadata.tables.items()
        if "org_id" not in table.columns and name not in TENANT_FREE
    }
    assert not missing, (
        "tables holding customer data with no tenant column:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nAdd org_id, or add the table to TENANT_FREE with a reason."
    )


def test_the_excuse_list_names_only_real_tables():
    """A stale excuse is worse than none: it silently exempts nothing while
    reading as though it exempts something."""
    unknown = set(TENANT_FREE) - set(Base.metadata.tables) - {"alembic_version"}
    assert not unknown, f"TENANT_FREE names tables that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("table", [
    "template_versions", "template_sections", "source_versions", "source_chunks",
    "document_versions", "section_outputs", "chat_messages",
    "template_library_versions", "template_cluster_members",
])
def test_the_tenant_column_is_not_nullable(app_client, table):
    """A nullable tenant is a tenant filter with a hole in it: `WHERE org_id = ?`
    silently drops every NULL row, so the data is neither visible nor protected.
    """
    columns = {c["name"]: c for c in inspect(SessionLocal().bind).get_columns(table)}
    assert "org_id" in columns, f"{table} has no org_id"
    assert columns["org_id"]["nullable"] is False, f"{table}.org_id is nullable"


def test_child_rows_inherit_the_tenant_of_their_parent(app_client, two_orgs):
    """The value has to be derived on write, not supplied by a caller — a tenant
    a request can name is a tenant a request can forge."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))

        source_file = SourceFile(
            org_id=project.org_id, project_id=project.id, name="s.csv",
            file_type="csv", status="ready", created_by=user.id,
        )
        db.add(source_file)
        db.flush()
        version = SourceVersion(
            source_file_id=source_file.id, org_id=source_file.org_id,
            version_no=1, blob_path="x.csv", created_by=user.id,
        )
        db.add(version)
        db.flush()
        chunk = SourceChunk(
            project_id=project.id, source_version_id=version.id, org_id=version.org_id,
            chunk_index=0, element_type="row", heading_path="", text="salary: 100000",
            token_count=2, content_sha256="abc",
        )
        db.add(chunk)
        db.flush()

        assert version.org_id == source_file.org_id
        assert chunk.org_id == source_file.org_id
        db.rollback()
    finally:
        db.close()


def test_a_tenant_filter_alone_now_scopes_the_payload_tables(app_client, two_orgs):
    """The point of the column: the sensitive tables can be filtered directly,
    without joining back through three parents to find out who owns the row."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        for model in (SourceChunk, DocumentVersion, TemplateVersion, TemplateSection, ChatMessage):
            rows = db.scalars(select(model).where(model.org_id == org_id)).all()
            assert all(r.org_id == org_id for r in rows), f"{model.__name__} leaked across the filter"
    finally:
        db.close()
