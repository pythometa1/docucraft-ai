"""Deleting a safety product takes its data with it.

Acceptance criterion 10, and the hardest promise in the module to keep by hand.
The purge is a written list of models in child-before-parent order, and a
written list is a thing that falls behind the schema: every milestone from M2
to M9 adds tables, and the one that gets forgotten leaves case-level safety
data -- patient ages, event terms, narratives -- in a database a customer was
told had been emptied.

So this file does not check the list. It walks `Base.metadata`, writes a row
into EVERY `pv_` table there is, deletes the product through the endpoint, and
asserts nothing is left. A table added in a later milestone and forgotten in
the purge fails here without anybody remembering to come back and add a case.
"""

from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pv_tables():
    from app.models import Base

    return [t for t in Base.metadata.sorted_tables if t.name.startswith("pv_")]


def _value_for(column, *, ids, org_id, product_id):
    """Something a NOT NULL column will accept.

    Foreign keys are resolved against rows already written, which is why the
    caller walks `sorted_tables` -- parents first, so a child always has
    something real to point at.
    """
    for fk in column.foreign_keys:
        target = fk.column.table.name
        if target in ids:
            return ids[target]
    if column.name == "org_id":
        return org_id
    if column.name == "pv_product_id":
        return product_id
    kind = column.type
    if isinstance(kind, sa.JSON):
        default = getattr(column.default, "arg", None)
        return {} if default is dict else []
    if isinstance(kind, sa.Boolean):
        return False
    if isinstance(kind, sa.Integer):
        return 1
    if isinstance(kind, sa.Float):
        return 1.0
    if isinstance(kind, sa.Date):
        return date(2026, 1, 1)
    if isinstance(kind, sa.DateTime):
        return datetime.now(timezone.utc)
    return "seeded"


@pytest.fixture
def seeded(app_client, two_orgs):
    """A product with one row in every `pv_` table the schema declares."""
    import uuid

    from app.db import SessionLocal
    from app.models import PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Purge safety", "function": "Safety", "document_type": "PSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Purgeazine",
        "ibd": "2020-01-01", "dibd": "2016-01-01"}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    ids = {"pv_products": product["id"]}
    written = []
    for table in _pv_tables():
        if table.name == "pv_products":
            continue
        row_id = str(uuid.uuid4())
        values = {"id": row_id}
        for column in table.columns:
            if column.name == "id" or column.nullable:
                continue
            values[column.name] = _value_for(
                column, ids=ids, org_id=org_id, product_id=product["id"])
        # A couple of columns are nullable but carry the link the purge walks,
        # so they are filled deliberately rather than left out.
        for optional in ("pv_product_id", "report_instance_id", "case_id",
                         "rsi_version_id", "blob_path"):
            if optional in table.columns and optional not in values:
                values[optional] = _value_for(
                    table.columns[optional], ids=ids, org_id=org_id,
                    product_id=product["id"])
        db.execute(table.insert().values(**values))
        ids[table.name] = row_id
        written.append(table.name)
    db.commit()

    # Every table really did get a row, or the test proves nothing.
    for name in [t.name for t in _pv_tables()]:
        assert name in ids, f"{name} was not seeded"
    db.close()
    # The ids are returned, not just the names: the suite shares one database,
    # so "no pv rows anywhere" would be a statement about every other test's
    # products too. What this file asserts is that THESE rows are gone.
    return token, product["id"], ids


def test_every_pv_table_is_declared_by_the_schema_walk(seeded):
    """The guard on the guard: if the walk found nothing, the purge test below
    would pass by testing nothing."""
    _token, _product_id, ids = seeded
    assert len(ids) >= 28, sorted(ids)
    assert "pv_case_originals" in ids, "the un-masked store must be covered"
    assert "pv_cases" in ids and "pv_case_events" in ids


def test_deleting_the_product_leaves_no_pv_row_behind(app_client, seeded):
    from app.db import SessionLocal

    token, product_id, ids = seeded
    deleted = app_client.delete(f"/api/v1/pv/products/{product_id}",
                                headers=_auth(token))
    assert deleted.status_code == 200, deleted.text

    db = SessionLocal()
    try:
        survivors = []
        for table in _pv_tables():
            row_id = ids[table.name]
            found = db.scalar(sa.select(sa.func.count()).select_from(table)
                              .where(table.c.id == row_id))
            if found:
                survivors.append(table.name)
    finally:
        db.close()
    assert survivors == [], (
        "these tables survived a purge that reported success: "
        + ", ".join(sorted(survivors)))


def test_the_purge_reports_what_it_removed(app_client, seeded):
    """A receipt somebody can check, not a bare `deleted: true`."""
    token, product_id, _ids = seeded
    body = app_client.delete(f"/api/v1/pv/products/{product_id}",
                             headers=_auth(token)).json()
    assert body["deleted"] is True
    purged = body["purged"]
    assert purged["pv_cases"] == 1
    assert purged["pv_case_events"] == 1
    assert purged["pv_case_originals"] == 1
    assert purged["pv_report_instances"] == 1
    assert purged["pv_rsi_versions"] == 1


def test_the_deletion_is_audited_as_a_warning(app_client, seeded):
    from app.db import SessionLocal
    from app.models import AuditLog

    token, product_id, _ids = seeded
    app_client.delete(f"/api/v1/pv/products/{product_id}", headers=_auth(token))
    db = SessionLocal()
    try:
        entries = db.query(AuditLog).filter(
            AuditLog.entity_type == "pv_product",
            AuditLog.entity_id == product_id).all()
    finally:
        db.close()
    deleted = [e for e in entries if "Deleted" in (e.event or "")]
    assert deleted, [e.event for e in entries]
    assert deleted[0].severity == "warning"
    assert "cases" in (deleted[0].target or "")


def test_another_products_rows_are_untouched(app_client, seeded, two_orgs):
    """The purge is scoped. A second product in the same organisation keeps
    everything it had."""
    from app.db import SessionLocal
    from app.models import PvCase, PvProduct

    token, product_id, _ids = seeded
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Neighbour safety", "function": "Safety", "document_type": "PSUR",
        "region": "Global", "language": "English"}).json()
    neighbour = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Neighbourazine"}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, neighbour["id"]).org_id
    db.add(PvCase(org_id=org_id, pv_product_id=neighbour["id"],
                  worldwide_case_id="KEEP-1",
                  initial_receipt_date=date(2026, 2, 2)))
    db.commit()
    db.close()

    app_client.delete(f"/api/v1/pv/products/{product_id}", headers=_auth(token))

    db = SessionLocal()
    try:
        kept = db.query(PvCase).filter(
            PvCase.pv_product_id == neighbour["id"]).all()
        assert [c.worldwide_case_id for c in kept] == ["KEEP-1"]
        assert db.get(PvProduct, neighbour["id"]) is not None
    finally:
        db.close()


def test_the_blob_column_is_the_one_retention_sweeps_for(seeded):
    """`app.retention` finds a deleted organisation's files by looking for a
    column named exactly `blob_path`. The CMC module called its column
    `storage_path` and is outside that sweep, relying entirely on its own
    hand-written purge; this module joins the sweep by being named the way the
    sweep looks."""
    from app.retention import BLOB_COLUMN

    tables = {t.name: t for t in _pv_tables()}
    for name in ("pv_documents", "pv_exports"):
        assert BLOB_COLUMN in tables[name].columns, (
            f"{name} stores a file under a column {BLOB_COLUMN!r} sweeps for")
