"""The `kind` filter, and why it has to run before the merge rather than after.

A mature index holds more than one class of record for the same tenant: the
columns of every source file it has ingested, and the field context of every
template it has compiled. A query drawn from a template resembles the template
records far more than it resembles a spreadsheet header, so a ranked list that
mixes the two is entirely field context by the time it reaches the caller.

Trimming that list to the kind you wanted then returns nothing at all. Measured
on a live tenant before this filter existed: 470 records, 24 retrieved, 0 of
them the source columns the compiler had asked for. So the filter belongs in
the retrieval, applied to both halves of the merge, and these tests pin it
there.
"""

import math

import numpy as np
import pytest

from app.retrieval.hybrid import EvidenceDocument, retrieve_evidence
from app.retrieval.vector import (
    SOURCE_COLUMN_DESCRIPTION,
    TEMPLATE_FIELD_CONTEXT,
    HashingEmbedder,
    InMemoryVectorStore,
    VectorIndex,
    VectorRecord,
    embed_one,
)

ORG = "org-kind"


def _record(record_id: str, kind: str, text: str, *, org_id: str = ORG) -> VectorRecord:
    embedder = HashingEmbedder()
    return VectorRecord(
        record_id=record_id, org_id=org_id, kind=kind, text=text,
        vector=embed_one(embedder, text), provider=embedder.name,
    )


def _seeded_store() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    store.put(_record("col:start", SOURCE_COLUMN_DESCRIPTION, "Start Date (date), for example 01/02/2024"))
    store.put(_record("col:name", SOURCE_COLUMN_DESCRIPTION, "Colleague First Name (person_name)"))
    store.put(_record("fld:start", TEMPLATE_FIELD_CONTEXT, "start_date: the start date of the assignment"))
    store.put(_record("fld:name", TEMPLATE_FIELD_CONTEXT, "colleague_first_name: the colleague's first name"))
    return store


def test_scoped_rows_filters_by_kind():
    rows = _seeded_store().scoped_rows(org_id=ORG, kind=SOURCE_COLUMN_DESCRIPTION)
    assert {r.record_id for r in rows} == {"col:start", "col:name"}


def test_scoped_rows_without_kind_returns_every_kind():
    """The filter is opt-in. Every existing caller passes no kind and must keep
    seeing the whole tenant, or adding this argument silently narrowed them."""
    rows = _seeded_store().scoped_rows(org_id=ORG)
    assert len(rows) == 4


def test_search_filters_by_kind():
    index = VectorIndex(store=_seeded_store())
    matches = index.search("start date", org_id=ORG, kind=SOURCE_COLUMN_DESCRIPTION, k=8, min_score=-1.0)
    assert matches, "a kind-filtered search over matching records should still return them"
    assert {m.record.kind for m in matches} == {SOURCE_COLUMN_DESCRIPTION}


def test_retrieve_evidence_filters_both_halves():
    """The point of the filter: neither retriever may contribute a foreign kind.

    A vector-only leak is the one that actually happened -- the lexical corpus
    was filtered by the caller and the index was not -- so the assertion is on
    `found_by` as well as on `kind`.
    """
    store = _seeded_store()
    corpus = [
        EvidenceDocument(record_id=r.record_id, org_id=r.org_id, text=r.text, kind=r.kind)
        for r in store.scoped_rows(org_id=ORG)
    ]
    ranked = retrieve_evidence(
        "start date of the assignment", org_id=ORG, vector_index=VectorIndex(store=store),
        corpus=corpus, kind=SOURCE_COLUMN_DESCRIPTION, k=8,
    )
    assert ranked, "the source columns match this query and must survive the filter"
    assert {item.kind for item in ranked} == {SOURCE_COLUMN_DESCRIPTION}
    assert not any("fld:" in item.record_id for item in ranked)


def test_unfiltered_retrieval_is_dominated_by_the_other_kind():
    """The measurement that motivates the filter, kept as a regression.

    Without `kind`, template field context outranks the source columns for a
    template-shaped query. If this ever stops being true the filter is still
    correct, but the docstrings explaining *why* it exists would be wrong -- so
    this asserts the premise rather than assuming it.
    """
    store = _seeded_store()
    corpus = [
        EvidenceDocument(record_id=r.record_id, org_id=r.org_id, text=r.text, kind=r.kind)
        for r in store.scoped_rows(org_id=ORG)
    ]
    ranked = retrieve_evidence(
        "start_date: the start date of the assignment", org_id=ORG,
        vector_index=VectorIndex(store=store), corpus=corpus, k=2,
    )
    assert ranked[0].kind == TEMPLATE_FIELD_CONTEXT
