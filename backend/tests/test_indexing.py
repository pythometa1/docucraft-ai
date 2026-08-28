"""What reaches the embeddings table when a real upload happens.

The §10 tables and the vector store were built and tested before anything wrote
to them, which meant the semantic memory was a schema rather than a memory. The
question these tests answer is not "does the store work" -- `test_retrieval.py`
covers that -- but "does a customer uploading a spreadsheet leave anything in
it, and is what it leaves the thing §10 asked for or a second copy of their
payroll".
"""

import io
import os

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Embedding, Project, User
from app.retrieval.indexing import (
    SOURCE_COLUMN, TEMPLATE_FIELD, describe_column, index_manifest_fields,
    index_source_columns,
)
from app.retrieval.store import SqlVectorStore
from app.retrieval.vector import VectorIndex


PAYROLL = [
    {"employee_id": "EMP-000417", "full_name": "Dana Ruiz", "annual_salary": "118400",
     "joining_dt": "14/03/2024", "colleague_type": "Full time"},
    {"employee_id": "EMP-000418", "full_name": "Sam Okafor", "annual_salary": "96250",
     "joining_dt": "02/09/2023", "colleague_type": "Part time"},
]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------- what a column becomes

def test_a_column_description_carries_the_shape_and_not_the_value():
    """§10 embeds "source column name + description + type + samples". The
    sample is what makes a six-figure salary distinguishable from a four-digit
    reference; whose salary it is has no bearing on that."""
    text = describe_column("annual_salary", ["118400", "96250"])

    assert "annual_salary" in text
    assert "money" in text
    assert "118400" not in text and "96250" not in text


def test_a_column_with_nothing_in_it_says_so_rather_than_inventing_a_sample():
    text = describe_column("bonus_amount", [None, ""])
    assert "no value present" in text
    assert "bonus_amount" in text


def test_the_customers_own_vocabulary_is_what_stays_searchable():
    """The column name is the strongest signal and is the customer's own
    wording, which is exactly the thing that has to be findable."""
    assert "Colleague Joining Dt" in describe_column("Colleague Joining Dt", ["14/03/2024"])


# --------------------------------------------------- one vector per column

def test_an_upload_contributes_one_vector_per_column_not_per_row(app_client, two_orgs):
    """The design decision §10 turns on. Fifty thousand employees must not
    become fifty thousand vectors: that is a second, unindexed copy of the
    payroll, and every deletion then has to find it."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        index = VectorIndex(store=SqlVectorStore(db))
        written = index_source_columns(
            index, org_id=org_id, source_version_id="sv-index-1",
            columns=list(PAYROLL[0]), records=PAYROLL * 500,  # 1,000 rows
        )
        db.commit()
    finally:
        db.close()

    assert len(written) == 5, "one per column, whatever the row count"


def test_no_customer_value_reaches_the_embeddings_table(app_client, two_orgs):
    """The rule this module enforces rather than documents."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        index_source_columns(
            VectorIndex(store=SqlVectorStore(db)),
            org_id=org_id, source_version_id="sv-index-2",
            columns=list(PAYROLL[0]), records=PAYROLL,
        )
        db.commit()
        stored = " ".join(
            row.text for row in db.scalars(
                select(Embedding).where(Embedding.org_id == org_id)
            ).all()
        )
    finally:
        db.close()

    for secret in ("Dana Ruiz", "Sam Okafor", "118400", "96250", "EMP-000417", "EMP-000418"):
        assert secret not in stored, f"{secret!r} was written into the vector store"


def test_bookkeeping_columns_are_not_indexed(app_client, two_orgs):
    """`_row_index` is how the ingestion tracks position; it is not schema, and
    a compiler offered it as a mapping candidate would be offered nonsense."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        written = index_source_columns(
            VectorIndex(store=SqlVectorStore(db)),
            org_id=org_id, source_version_id="sv-index-3",
            columns=["full_name", "_row_index"], records=[{"full_name": "Dana Ruiz", "_row_index": 1}],
        )
        db.commit()
    finally:
        db.close()

    assert [w.record_id.split(":", 1)[1] for w in written] == ["full_name"]


def test_indexing_refuses_without_a_tenant():
    """An untenanted vector is unreachable by every search and invisible to
    every deletion -- it is not stored data, it is litter."""
    with pytest.raises(ValueError, match="org_id"):
        index_source_columns(VectorIndex(), org_id="", source_version_id="sv", columns=["a"])


# ------------------------------------------------- the template side

def test_a_field_is_indexed_by_the_sentence_it_sits_in(app_client, two_orgs):
    """§10's own example: `<Reporting To>` finding `New Manager Name` is the
    variation string overlap cannot reach, and the sentence is what carries it.
    The token alone is two words half the estate shares."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        index = VectorIndex(store=SqlVectorStore(db))
        index_manifest_fields(
            index, org_id=org_id, manifest_id="mf-index-1",
            fields=[{"id": "new_manager_name", "compiled_from": "You will report to <New Reporting To>."}],
        )
        db.commit()
        hits = index.search("who does the colleague report to", org_id=org_id, k=3)
    finally:
        db.close()

    assert hits, "the sentence has to be findable, or indexing it bought nothing"
    assert hits[0].record.kind == TEMPLATE_FIELD


def test_the_two_kinds_are_distinguishable(app_client, two_orgs):
    """A column description and a template sentence answer different questions,
    so a caller has to be able to ask for one without the other."""
    assert SOURCE_COLUMN != TEMPLATE_FIELD


# --------------------------------------------------- end to end, over the API

def test_uploading_a_spreadsheet_populates_the_semantic_memory(app_client, two_orgs):
    """The gap this closes: the tables existed and nothing wrote to them, so the
    memory was a schema."""
    token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        before = len(db.scalars(select(Embedding).where(Embedding.org_id == org_id)).all())
    finally:
        db.close()

    csv = b"employee_id,full_name,annual_salary\nEMP-000417,Dana Ruiz,118400\n"
    res = app_client.post(
        f"/api/v1/projects/{project_a}/sources",
        files={"file": ("payroll.csv", csv, "text/csv")},
        headers=_auth(token_a),
    )
    assert res.status_code in (200, 201), res.text

    db = SessionLocal()
    try:
        rows = db.scalars(select(Embedding).where(Embedding.org_id == org_id)).all()
    finally:
        db.close()

    assert len(rows) > before, "an upload must leave something in the semantic memory"
    stored = " ".join(r.text for r in rows)
    assert "annual_salary" in stored
    assert "Dana Ruiz" not in stored and "118400" not in stored
