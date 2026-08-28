"""Retrieval memory that is still there tomorrow, and a tenant filter made of SQL.

Two claims are under test here and they pull in different directions.

The first is that §10's memory is real. PostgreSQL is the system of record and
pgvector is "semantic memory within the same data platform"; approved mappings
are persisted "as structured relational/JSONB data, not only as an embedding".
Both stores used to be process dictionaries, so the failure being pinned is
concrete: a reviewer approves a mapping in March, the service is redeployed in
April, and the same template compiles against a blank history as though the
approval never happened. Every "outlives the session that recorded it" test
below reads through a connection the writer never touched.

The second is that persisting it changed nothing about the boundary. §16 says
the tenant filter runs "before similarity, never as a post-filter on results",
and says in as many words that a code review is not sufficient evidence for that
one. So the leakage tests here do not merely check what came back: they check
that the SQL carried org A's id and not org B's, and that the matrix handed to
the similarity function held org A's rows and only org A's rows.
"""

from __future__ import annotations

import contextlib
import io
import os
from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.generation.missing_policy import BLOCK
from app.models import Embedding as EmbeddingRow
from app.models import MappingMemoryEntry as MappingMemoryRow
from app.retrieval import embeddings as embeddings_module
from app.retrieval import vector as vector_module
from app.retrieval.embeddings import (
    HNSW_OPS,
    PgvectorUnavailable,
    VectorColumn,
    encode_vector,
    hnsw_index_ddl,
)
from app.retrieval.mapping_memory import (
    MIN_CONTRIBUTING_ORGS,
    CorrectionEvent,
    FieldContext,
    InMemoryMappingMemoryStore,
    MappingMemory,
)
from app.retrieval.store import SqlMappingMemoryStore, SqlVectorStore
from app.retrieval.vector import (
    DEFAULT_DIMENSIONS,
    SOURCE_COLUMN_DESCRIPTION,
    TEMPLATE_FIELD_CONTEXT,
    HashingEmbedder,
    InMemoryVectorStore,
    TenantScopeRequired,
    VectorIndex,
    cosine_scores,
)

#: Deliberately not the ids the other suites use. The test database is shared
#: across the session, and a leakage test that accidentally shared an org with
#: another module would be proving something about that module instead.
ORG_A = "persist-org-a"
ORG_B = "persist-org-b"

WHEN = datetime(2026, 3, 4, 9, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------------- fixtures


@pytest.fixture()
def session(app_client):
    """A session against the migrated test database, rolled back afterwards."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def clean_orgs(session):
    """Both tenants emptied before and after, so tests cannot seed each other."""
    def _wipe():
        SqlVectorStore(session).forget_org(ORG_A)
        SqlVectorStore(session).forget_org(ORG_B)
        SqlMappingMemoryStore(session).forget_org(ORG_A)
        SqlMappingMemoryStore(session).forget_org(ORG_B)
        session.commit()

    _wipe()
    yield
    _wipe()


@contextlib.contextmanager
def fresh_connection():
    """A session from an engine this process has not used before.

    The point is not tidiness. Reading back through the same `Session` proves
    nothing about persistence -- the identity map would answer from memory. A
    new engine opens a new connection to the same file, which is the closest a
    single-process test gets to "the service was restarted".
    """
    engine = create_engine(os.environ["DATABASE_URL"])
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = maker()
    # A brand-new PostgreSQL connection has no tenant scope, and row-level
    # security answers an unscoped session with zero rows -- which is the
    # fail-closed behaviour §16 asks for, and would look here like "the data did
    # not persist". A real restarted service is not unscoped either: the next
    # request sets the tenant before it reads. Seeding privileges, because these
    # tests read across both organisations to prove isolation.
    if engine.dialect.name == "postgresql":
        db.execute(text("SET app.rls_bypass = 'on'"))
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _ctx(label="Joining Date", field_type="date", **kwargs) -> FieldContext:
    return FieldContext(
        field_id=label.lower().replace(" ", "_"), label=label, field_type=field_type, **kwargs,
    )


# ------------------------------------------------------- embeddings on disk


def test_an_embedding_outlives_the_session_that_indexed_it(session, clean_orgs):
    """The failure this fixes: a deploy erases the index, and the compiler that
    had evidence for a field yesterday has none today."""
    index = VectorIndex(store=SqlVectorStore(session))
    index.index_text(
        record_id="col-joining-dt",
        org_id=ORG_A,
        kind=SOURCE_COLUMN_DESCRIPTION,
        text="Colleague joining date",
    )
    session.commit()

    with fresh_connection() as reader:
        reopened = VectorIndex(store=SqlVectorStore(reader))
        matches = reopened.search("colleague joining date", org_id=ORG_A)

    assert [m.record.record_id for m in matches] == ["col-joining-dt"]
    assert matches[0].score > 0.9
    assert matches[0].record.text == "Colleague joining date"


def test_the_stored_vector_is_the_one_that_was_indexed_not_a_re_embedding(session, clean_orgs):
    """Coordinates round-trip through the column exactly. A JSON fallback that
    lost precision would move every score by a little and nothing would say so."""
    index = VectorIndex(store=SqlVectorStore(session))
    record = index.index_text(
        record_id="col-manager",
        org_id=ORG_A,
        kind=SOURCE_COLUMN_DESCRIPTION,
        text="Reporting manager name",
    )
    session.commit()

    with fresh_connection() as reader:
        rows = SqlVectorStore(reader).scoped_rows(org_id=ORG_A)

    assert len(rows) == 1
    # pgvector's `vector` is float4, so a float64 vector comes back rounded to
    # single precision. That is the type's design, not a defect -- and it is why
    # this is a closeness assertion rather than an equality one. The tolerance is
    # float32 epsilon: anything looser would stop catching a genuinely wrong
    # vector, which is what the test is actually for.
    assert np.allclose(rows[0].vector, record.vector, rtol=0, atol=1e-6)
    assert rows[0].provider == record.provider


def test_re_indexing_a_record_replaces_it_instead_of_accumulating_rows(session, clean_orgs):
    """A second embedding of the same column is a correction of the first.
    Keeping both would let a stale vector out-rank the current one by the simple
    trick of being returned twice."""
    store = SqlVectorStore(session)
    index = VectorIndex(store=store)
    index.index_text(record_id="c1", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Basic pay")
    index.index_text(record_id="c1", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Annual basic pay")
    session.commit()

    rows = store.scoped_rows(org_id=ORG_A)
    assert [r.text for r in rows] == ["Annual basic pay"]


def test_the_same_record_id_in_two_organisations_is_two_rows(session, clean_orgs):
    """Uniqueness is per tenant. Two customers may legitimately use the same
    field id, and neither should be able to overwrite the other's row by
    choosing one."""
    store = SqlVectorStore(session)
    index = VectorIndex(store=store)
    index.index_text(record_id="shared-id", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Grade")
    index.index_text(record_id="shared-id", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Band")
    session.commit()

    assert [r.text for r in store.scoped_rows(org_id=ORG_A)] == ["Grade"]
    assert [r.text for r in store.scoped_rows(org_id=ORG_B)] == ["Band"]


def test_metadata_and_document_type_survive_the_round_trip(session, clean_orgs):
    store = SqlVectorStore(session)
    VectorIndex(store=store).index_text(
        record_id="f1",
        org_id=ORG_A,
        kind=TEMPLATE_FIELD_CONTEXT,
        text="You will report to",
        doc_type="Offer Letter",
        metadata={"template_version_id": "tv-9", "paragraph": 4},
    )
    session.commit()

    with fresh_connection() as reader:
        row = SqlVectorStore(reader).scoped_rows(org_id=ORG_A)[0]

    assert row.doc_type == "Offer Letter"
    assert row.metadata == {"template_version_id": "tv-9", "paragraph": 4}
    assert row.kind == TEMPLATE_FIELD_CONTEXT


def test_an_unclassified_row_is_not_evidence_for_a_typed_query(session, clean_orgs):
    """The doc_type predicate is SQL equality, so `doc_type IS NULL` does not
    satisfy it. Letting NULL match would stop the filter filtering the moment
    somebody forgets to classify an upload."""
    index = VectorIndex(store=SqlVectorStore(session))
    index.index_text(
        record_id="typed", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION,
        text="Colleague joining date", doc_type="Offer Letter",
    )
    index.index_text(
        record_id="untyped", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION,
        text="Colleague joining date",
    )
    session.commit()

    matches = index.search("colleague joining date", org_id=ORG_A, doc_type="Offer Letter")
    assert [m.record.record_id for m in matches] == ["typed"]


def test_a_vector_of_the_wrong_width_is_refused_before_the_database_sees_it(session, clean_orgs):
    """pgvector columns are fixed-width and reject a mismatch with an error that
    names neither the record nor the provider. Checking here means the message
    names both, at the call that made the mistake."""
    narrow = HashingEmbedder(dimensions=64)
    index = VectorIndex(provider=narrow, store=SqlVectorStore(session))
    with pytest.raises(ValueError, match=r"64 dimensions but the column is declared vector\(1024\)"):
        index.index_text(record_id="x", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Grade")


def test_forgetting_an_organisation_deletes_its_embeddings(session, clean_orgs):
    """§16 retention: "deletion must cascade to embeddings"."""
    store = SqlVectorStore(session)
    index = VectorIndex(store=store)
    index.index_text(record_id="a1", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Retention bonus")
    index.index_text(record_id="b1", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Retention bonus")
    session.commit()

    assert index.forget_org(ORG_A) == 1
    session.commit()

    with fresh_connection() as reader:
        assert SqlVectorStore(reader).scoped_rows(org_id=ORG_A) == []
        assert len(SqlVectorStore(reader).scoped_rows(org_id=ORG_B)) == 1


def test_a_reused_org_id_finds_nothing_of_the_previous_tenant(session, clean_orgs):
    """An org id is a key, not an identity. Offboarding then re-onboarding under
    the same id must not resurrect the old tenant's memory."""
    index = VectorIndex(store=SqlVectorStore(session))
    index.index_text(record_id="gone", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Retention bonus schedule")
    session.commit()
    index.forget_org(ORG_B)
    index.index_text(record_id="new", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Notice period")
    session.commit()

    assert index.search("retention bonus schedule", org_id=ORG_B) == []


def test_the_database_backed_store_still_refuses_an_unscoped_read(session):
    """There is no global read to reach by leaving an argument off."""
    store = SqlVectorStore(session)
    with pytest.raises(TenantScopeRequired):
        store.scoped_rows(org_id="")
    with pytest.raises(TenantScopeRequired):
        store.forget_org("   ")


def test_size_reports_capacity_without_handing_back_anybody_s_rows(session, clean_orgs):
    store = SqlVectorStore(session)
    before = store.size()
    VectorIndex(store=store).index_text(
        record_id="s1", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Cost centre",
    )
    session.commit()
    assert store.size() == before + 1


# ------------------------------------------------------------------ leakage


def _seed_both_orgs(session) -> VectorIndex:
    index = VectorIndex(store=SqlVectorStore(session))
    index.index_text(record_id="a1", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Colleague joining date")
    index.index_text(record_id="a2", org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text="Manager name")
    index.index_text(record_id="b1", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Colleague joining date")
    index.index_text(record_id="b2", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Manager name")
    index.index_text(record_id="b3", org_id=ORG_B, kind=SOURCE_COLUMN_DESCRIPTION, text="Salary band")
    session.commit()
    return index


def test_org_a_cannot_reach_org_bs_row_through_the_database_backed_store(session, clean_orgs):
    """§16 asks for this one to be automated evidence rather than a code review.

    Both organisations hold a row with identical text, so the query matches org
    B's data just as well as org A's -- which is exactly the condition under
    which a missing filter is invisible in the returned scores.
    """
    index = _seed_both_orgs(session)

    matches = index.search("colleague joining date", org_id=ORG_A)

    assert [m.record.record_id for m in matches] == ["a1"]
    assert all(m.record.org_id == ORG_A for m in matches)
    assert not any(m.record.record_id.startswith("b") for m in matches)


def test_the_tenant_filter_is_a_where_clause_and_not_a_discard_step(session, clean_orgs):
    """The difference between filtering in SQL and filtering afterwards is
    invisible in the results and total in the consequences.

    Two observations, both necessary. The statement the database executed has to
    carry org A's id and not org B's -- that is the filter being *in the query*.
    And the matrix handed to `cosine_scores` has to hold two rows, org A's whole
    corpus -- that is the filter running *before* similarity, so no other
    tenant's vectors influenced the ranking, the score distribution or the `k`
    cut-off.
    """
    index = _seed_both_orgs(session)

    statements: list[tuple[str, object]] = []

    def record_sql(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, parameters))

    scored: dict = {}

    def spy(matrix, query):
        scored["shape"] = matrix.shape
        return cosine_scores(matrix, query)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record_sql)
    original = vector_module.cosine_scores
    vector_module.cosine_scores = spy
    try:
        index.search("colleague joining date", org_id=ORG_A)
    finally:
        vector_module.cosine_scores = original
        event.remove(engine, "before_cursor_execute", record_sql)

    selects = [(sql, params) for sql, params in statements if "FROM embeddings" in sql]
    assert selects, "the search did not read the embeddings table at all"
    for sql, params in selects:
        assert "org_id" in sql and "WHERE" in sql, f"no tenant predicate in: {sql}"
        flat = str(params)
        assert ORG_A in flat, f"the statement did not carry org A's id: {sql} {params}"
        assert ORG_B not in flat, f"the statement carried org B's id: {sql} {params}"

    assert scored["shape"][0] == 2, (
        "the scored matrix held rows beyond org A's two, so another tenant's "
        "vectors were part of the ranking arithmetic"
    )


def test_no_read_on_the_mapping_store_returns_another_tenants_entry(session, clean_orgs):
    memory_a = MappingMemory(store=SqlMappingMemoryStore(session))
    memory_a.record_approved_mapping(
        org_id=ORG_B, field_context=_ctx(), source_column="SECRET_JOINING_DT",
        approved_by="reviewer-b", at=WHEN,
    )
    session.commit()

    lookup = memory_a.lookup(_ctx(), ORG_A)
    assert lookup.prior_mappings == []
    assert SqlMappingMemoryStore(session).entries_for_org(ORG_A) == []


# ------------------------------------------------------- mapping memory on disk


def test_an_approved_mapping_outlives_the_session_that_recorded_it(session, clean_orgs):
    """The concrete failure: a reviewer approves in March, the service is
    deployed in April, and the ninth offer letter from the same customer gets
    the same guesses as the first."""
    MappingMemory(store=SqlMappingMemoryStore(session)).record_approved_mapping(
        org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt",
        approved_by="reviewer-a", transform="format_date", on_missing=BLOCK, at=WHEN,
    )
    session.commit()

    with fresh_connection() as reader:
        best = MappingMemory(store=SqlMappingMemoryStore(reader)).lookup(_ctx(), ORG_A).best

    assert best is not None
    assert best.source_column == "Joining Dt"
    assert best.transform == "format_date"
    assert best.on_missing == BLOCK
    assert best.approval_count == 1
    assert best.last_approved_by == "reviewer-a"
    assert best.last_approved_at == WHEN


def test_approving_the_same_mapping_twice_is_one_row_carrying_a_count_of_two(session, clean_orgs):
    """§13's largest single-signal weight is "approved 42 times". Forty-two rows
    would make the signal a function of how the reviewer happened to click."""
    memory = MappingMemory(store=SqlMappingMemoryStore(session))
    for _ in range(2):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1", at=WHEN,
        )
    session.commit()

    rows = session.execute(
        select(MappingMemoryRow).where(MappingMemoryRow.org_id == ORG_A)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].approval_count == 2


def test_a_column_spelled_two_ways_is_one_mapping_not_two(session, clean_orgs):
    """"Joining Dt" and "joining_dt" are the same column. Two rows would split
    the approval count between them and halve the signal §13 leans on hardest."""
    memory = MappingMemory(store=SqlMappingMemoryStore(session))
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1", at=WHEN)
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="joining_dt", approved_by="u1", at=WHEN)
    session.commit()

    entries = SqlMappingMemoryStore(session).entries_for_org(ORG_A)
    assert len(entries) == 1
    assert entries[0].approval_count == 2


def test_a_rejection_survives_the_restart_that_a_naive_memory_would_forget(session, clean_orgs):
    """A reviewer who has to decline the same wrong mapping four times stops
    reading the suggestions. That only holds if the rejection is still there
    after the next deploy."""
    memory = MappingMemory(store=SqlMappingMemoryStore(session))
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Start Dt", approved_by="u1", at=WHEN)
    memory.record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        rejected_column="Start Dt", corrected_by="reviewer-a", at=WHEN,
    )
    session.commit()

    with fresh_connection() as reader:
        priors = MappingMemory(store=SqlMappingMemoryStore(reader)).lookup(_ctx(), ORG_A).prior_mappings

    assert [p.source_column for p in priors] == ["Joining Dt"], (
        "the rejected column was suggested again after a restart"
    )


def test_a_reviewer_correction_is_logged_as_a_pair_not_as_two_moved_counters(session, clean_orgs):
    """§14 keeps `reviewer_corrections` as a store in its own right because the
    counters lose the pairing: they record that one column gained an approval
    and another gained a rejection, not that those were the same decision."""
    store = SqlMappingMemoryStore(session)
    MappingMemory(store=store).record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        rejected_column="Start Dt", corrected_by="reviewer-a", at=WHEN,
    )
    session.commit()

    with fresh_connection() as reader:
        logged = SqlMappingMemoryStore(reader).corrections_for_org(ORG_A)

    assert len(logged) == 1
    assert logged[0].accepted_column == "Joining Dt"
    assert logged[0].rejected_column == "Start Dt"
    assert logged[0].corrected_by == "reviewer-a"
    assert logged[0].at == WHEN


def test_a_confirmation_with_nothing_displaced_is_logged_too(session, clean_orgs):
    """The control group. A log holding only the corrections cannot answer "were
    the suggestions any good?", which is what it is being kept for."""
    store = SqlMappingMemoryStore(session)
    MappingMemory(store=store).record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        corrected_by="reviewer-a", at=WHEN,
    )
    session.commit()

    logged = store.corrections_for_org(ORG_A)
    assert len(logged) == 1
    assert logged[0].rejected_column is None


def test_forgetting_an_organisation_erases_the_memory_the_consent_and_the_log(session, clean_orgs):
    """§16 retention. Leaving the correction log behind would leave a record of
    which columns a departed customer's reviewers accepted and rejected, field
    by field -- which is their data."""
    store = SqlMappingMemoryStore(session)
    memory = MappingMemory(store=store)
    memory.set_sharing_opt_in(ORG_A, opted_in=True, actor="admin")
    memory.record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        rejected_column="Start Dt", corrected_by="reviewer-a", at=WHEN,
    )
    session.commit()

    assert memory.forget_org(ORG_A) == 2
    session.commit()

    with fresh_connection() as reader:
        gone = SqlMappingMemoryStore(reader)
        assert gone.entries_for_org(ORG_A) == []
        assert gone.corrections_for_org(ORG_A) == []
        assert gone.is_opted_in(ORG_A) is False


def test_the_mapping_store_refuses_a_read_with_no_tenant(session):
    store = SqlMappingMemoryStore(session)
    for call in (
        lambda: store.entries_for_org(""),
        lambda: store.corrections_for_org("  "),
        lambda: store.find(org_id="", field_key="k", source_column="c", transform="direct"),
        lambda: store.forget_org(""),
    ):
        with pytest.raises(TenantScopeRequired):
            call()


# ------------------------------------------------- consent and shared patterns


def test_sharing_consent_outlives_the_session_that_recorded_it(session, clean_orgs):
    SqlMappingMemoryStore(session).set_sharing_opt_in(ORG_A, opted_in=True, actor="admin-1")
    session.commit()

    with fresh_connection() as reader:
        assert SqlMappingMemoryStore(reader).is_opted_in(ORG_A) is True


def test_opting_back_out_is_recorded_as_a_new_decision_not_a_deleted_row(session, clean_orgs):
    store = SqlMappingMemoryStore(session)
    store.set_sharing_opt_in(ORG_A, opted_in=True, actor="admin-1")
    store.set_sharing_opt_in(ORG_A, opted_in=False, actor="admin-2")
    session.commit()

    assert store.is_opted_in(ORG_A) is False
    from app.models import MappingMemorySharing

    row = session.execute(
        select(MappingMemorySharing).where(MappingMemorySharing.org_id == ORG_A)
    ).scalar_one()
    assert row.actor == "admin-2", "the row does not say who made the current decision"


def test_consent_must_name_who_gave_it(session, clean_orgs):
    """Opting a customer's data into a shared pool is a consent decision, and an
    unattributed one cannot be evidenced later."""
    store = SqlMappingMemoryStore(session)
    with pytest.raises(ValueError, match="who opted in"):
        store.set_sharing_opt_in(ORG_A, opted_in=True, actor="")
    with pytest.raises(TenantScopeRequired):
        store.set_sharing_opt_in("", opted_in=True, actor="admin")


def test_an_organisation_with_no_row_at_all_is_opted_out(session, clean_orgs):
    """Absence means no. A pool that defaults to sharing shares whatever nobody
    has got round to configuring."""
    assert SqlMappingMemoryStore(session).is_opted_in("never-heard-of-this-org") is False
    assert SqlMappingMemoryStore(session).is_opted_in("") is False


def _seed_shared_pool(session, contributors: int) -> MappingMemory:
    memory = MappingMemory(store=SqlMappingMemoryStore(session))
    for i in range(contributors):
        org_id = f"persist-contributor-{i}"
        memory.forget_org(org_id)
        memory.set_sharing_opt_in(org_id, opted_in=True, actor=f"admin-{i}")
        memory.record_approved_mapping(
            org_id=org_id,
            field_context=_ctx(f"Joining Date {i}", "date"),
            source_column=f"SECRETCOLUMN_{i}",
            approved_by="u1",
            transform="format_date",
            on_missing=BLOCK,
            at=WHEN,
        )
    memory.forget_org(ORG_A)
    memory.set_sharing_opt_in(ORG_A, opted_in=True, actor="admin-reader")
    session.commit()
    return memory


def test_the_cross_tenant_pool_leaves_the_database_as_shape_only(session, clean_orgs):
    """The projection is the anonymisation: three closed-vocabulary columns and
    a count. Another tenant's entry is never constructed in this process, so
    there is no object anybody has to be careful with."""
    memory = _seed_shared_pool(session, MIN_CONTRIBUTING_ORGS)
    try:
        rows = SqlMappingMemoryStore(session).shared_structural_keys(exclude_org_id=ORG_A)
        assert rows, "the pool came back empty; the rest of this test proves nothing"
        for _org_id, key, approvals in rows:
            assert set(vars(key)) == {"field_type", "transform", "on_missing"}
            assert approvals >= 1
        rendered = repr(rows)
        assert "SECRETCOLUMN" not in rendered
        assert "Joining Date" not in rendered
    finally:
        for i in range(MIN_CONTRIBUTING_ORGS):
            memory.forget_org(f"persist-contributor-{i}")
        session.commit()


def test_a_pattern_below_the_anonymity_floor_is_not_released_from_the_database(session, clean_orgs):
    """A structure reported by two customers still describes those two
    customers. Anonymisation that identifies its source is not anonymisation."""
    memory = _seed_shared_pool(session, MIN_CONTRIBUTING_ORGS - 1)
    try:
        assert memory.structural_patterns(ORG_A) == []
    finally:
        for i in range(MIN_CONTRIBUTING_ORGS - 1):
            memory.forget_org(f"persist-contributor-{i}")
        session.commit()


def test_a_pattern_at_the_floor_is_released_as_shape_only(session, clean_orgs):
    memory = _seed_shared_pool(session, MIN_CONTRIBUTING_ORGS)
    try:
        patterns = memory.structural_patterns(ORG_A)
        assert len(patterns) == 1
        pattern = patterns[0]
        assert (pattern.field_type, pattern.transform, pattern.on_missing) == ("date", "format_date", BLOCK)
        assert pattern.contributing_org_count == MIN_CONTRIBUTING_ORGS
    finally:
        for i in range(MIN_CONTRIBUTING_ORGS):
            memory.forget_org(f"persist-contributor-{i}")
        session.commit()


def test_a_contributor_that_has_not_opted_in_never_has_a_row_read(session, clean_orgs):
    """The opt-in join runs in SQL, which is a stronger statement than reading
    the row and declining to use it."""
    memory = _seed_shared_pool(session, MIN_CONTRIBUTING_ORGS)
    try:
        memory.set_sharing_opt_in("persist-contributor-0", opted_in=False, actor="admin-0")
        session.commit()
        rows = SqlMappingMemoryStore(session).shared_structural_keys(exclude_org_id=ORG_A)
        assert all(org_id != "persist-contributor-0" for org_id, _key, _n in rows)
        assert memory.structural_patterns(ORG_A) == [], (
            "two contributors is below the anonymity floor and should release nothing"
        )
    finally:
        for i in range(MIN_CONTRIBUTING_ORGS):
            memory.forget_org(f"persist-contributor-{i}")
        session.commit()


# ---------------------------------------------------------------- conformance


def test_the_two_stores_answer_the_same_lookup_identically(session, clean_orgs):
    """The seam only holds if swapping the store does not move a score.

    A persistence layer that quietly re-ranks is worse than none: the reviewer
    sees one order in a preview and another in production, and nothing in either
    says why.
    """
    def run(memory: MappingMemory) -> list[tuple]:
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt",
            approved_by="u1", transform="format_date", on_missing=BLOCK, at=WHEN,
        )
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt",
            approved_by="u1", transform="format_date", on_missing=BLOCK, at=WHEN,
        )
        memory.record_reviewer_correction(
            org_id=ORG_A, field_context=_ctx(), accepted_column="Start Date",
            rejected_column="Commencement", corrected_by="u2", at=WHEN,
        )
        lookup = memory.lookup(_ctx(), ORG_A)
        return [
            (p.source_column, p.transform, p.on_missing, p.approval_count,
             p.rejection_count, p.similarity, p.score)
            for p in lookup.prior_mappings
        ]

    in_memory = run(MappingMemory(store=InMemoryMappingMemoryStore()))
    persisted = run(MappingMemory(store=SqlMappingMemoryStore(session)))
    session.commit()

    # Everything but the two floats has to match exactly: the columns, the
    # transform, the missing-value policy and both counters are the reviewer's
    # decisions, and a store that changed one of those would be changing what
    # was approved.
    assert [row[:5] for row in persisted] == [row[:5] for row in in_memory]

    # Similarity and score agree to single precision rather than exactly. The
    # persisted side's context vector has been through pgvector's float4 column
    # and the in-memory side's has not, so the last couple of decimals differ.
    # What must not differ is the ORDER -- a store that quietly re-ranks shows a
    # reviewer one order in a preview and another in production.
    assert [row[0] for row in persisted] == [row[0] for row in in_memory]
    for stored_row, memory_row in zip(persisted, in_memory):
        assert abs(stored_row[5] - memory_row[5]) < 1e-6
        assert abs(stored_row[6] - memory_row[6]) < 1e-6


def test_the_two_vector_stores_rank_a_search_identically(session, clean_orgs):
    def run(index: VectorIndex) -> list[tuple]:
        for record_id, text in (
            ("v1", "Colleague joining date"),
            ("v2", "Manager name"),
            ("v3", "Start date of employment"),
        ):
            index.index_text(record_id=record_id, org_id=ORG_A, kind=SOURCE_COLUMN_DESCRIPTION, text=text)
        return [(m.record.record_id, round(m.score, 9)) for m in index.search("joining date", org_id=ORG_A)]

    in_memory = run(VectorIndex(store=InMemoryVectorStore()))
    persisted = run(VectorIndex(store=SqlVectorStore(session)))
    session.commit()

    assert [record_id for record_id, _ in persisted] == [record_id for record_id, _ in in_memory]
    # Same ranking, and the scores agree to single precision. They cannot agree
    # exactly: the persisted vectors have been through pgvector's float4 column
    # and the in-memory ones have not. What must hold is that the rounding never
    # reorders the results, which the ranking assertion above is what pins.
    for (_, persisted_score), (_, memory_score) in zip(persisted, in_memory):
        assert abs(persisted_score - memory_score) < 1e-6


# ------------------------------------------------------------- the column type


def test_the_vector_column_matches_the_backend_it_is_on(session):
    """Two real column types behind one Python attribute.

    JSON on SQLite so the suite runs on a laptop with nothing installed;
    pgvector's own type on PostgreSQL so the HNSW index and the distance
    operators mean anything. Asserting whichever one is actually in front of us
    keeps both halves honest -- the fallback is the path the whole suite runs
    on, and the real one is the path production runs on.
    """
    bind = session.get_bind()
    rendered = str({c["name"]: c for c in inspect(bind).get_columns("embeddings")}["vector"]["type"]).upper()
    if bind.dialect.name == "postgresql":
        assert "VECTOR" in rendered
    else:
        assert "JSON" in rendered


def test_the_vector_column_is_pgvector_on_postgresql():
    """CI runs the migrations against Postgres 16, so this DDL has to be valid
    there even though no test in this suite ever connects to it."""
    from sqlalchemy.dialects import postgresql

    ddl = str(CreateTable(EmbeddingRow.__table__).compile(dialect=postgresql.dialect()))
    assert "VECTOR(1024)" in ddl.upper()


def test_a_postgres_vector_column_without_pgvector_says_what_to_install(monkeypatch):
    """The guard is the point: the application boots without the package, and
    the failure arrives where it is actionable rather than at import time."""
    from sqlalchemy.dialects import postgresql

    monkeypatch.setattr(embeddings_module, "_PgVector", None)
    with pytest.raises(PgvectorUnavailable) as excinfo:
        VectorColumn(16).load_dialect_impl(postgresql.dialect())

    message = str(excinfo.value)
    assert "pip install pgvector" in message
    assert "CREATE EXTENSION IF NOT EXISTS vector" in message


def test_the_sqlite_fallback_still_works_when_pgvector_is_missing(monkeypatch):
    """Absence of the package must not take the fallback down with it -- that
    would make a missing optional dependency fatal for everyone."""
    from sqlalchemy.dialects import sqlite

    monkeypatch.setattr(embeddings_module, "_PgVector", None)
    impl = VectorColumn(16).load_dialect_impl(sqlite.dialect())
    assert "JSON" in str(impl).upper()


@pytest.mark.parametrize("bad, message", [
    (None, "cannot hold NULL"),
    (np.zeros((2, 4)), "one-dimensional"),
    (np.zeros(3), "the column is declared"),
    (np.array([1.0, float("nan"), 3.0, 4.0]), "NaN or infinity"),
])
def test_the_column_refuses_coordinates_it_cannot_honestly_store(bad, message):
    with pytest.raises(ValueError, match=message):
        encode_vector(bad, 4)


def test_encoding_accepts_the_width_it_was_given():
    assert encode_vector(np.array([0.5, -0.5, 0.0, 1.0]), 4) == [0.5, -0.5, 0.0, 1.0]


def test_a_vector_column_of_no_width_is_not_a_vector_column():
    with pytest.raises(ValueError, match="does not describe a vector"):
        VectorColumn(0)


def test_the_hnsw_index_is_built_for_the_metric_the_search_ranks_by():
    """`VectorIndex` ranks by cosine. An index built for another operator class
    is not an error -- Postgres just stops using it, and the only symptom is a
    query that gets slower as the corpus grows."""
    assert HNSW_OPS == "vector_cosine_ops"
    ddl = hnsw_index_ddl("embeddings", "vector")
    assert "USING hnsw" in ddl
    assert HNSW_OPS in ddl


# ---------------------------------------------------------------- the migration


def _offline_sql(url: str) -> str:
    """The DDL this revision emits for one dialect, without a server.

    Alembic's offline mode compiles and prints the statements instead of running
    them, which is the only way to assert that the PostgreSQL path is right from
    a suite that runs on SQLite.
    """
    from unittest.mock import patch

    from alembic import command
    from alembic.config import Config

    from app.config import settings

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    buffer = io.StringIO()
    # `alembic/env.py` overwrites the config URL from `settings.database_url`, so
    # the dialect has to be chosen there rather than here -- setting the option
    # and being quietly ignored is exactly the kind of test that passes while
    # asserting nothing.
    with patch.object(settings, "database_url", url), contextlib.redirect_stdout(buffer):
        command.upgrade(cfg, "9f3a6c1d8b25:b2f47c9e1a63", sql=True)
    return buffer.getvalue()


def test_the_migration_creates_the_extension_and_the_hnsw_index_on_postgres():
    """Failing at migration time, in front of whoever is deploying, is the whole
    reason the extension is created here rather than discovered at the first
    INSERT in front of a customer."""
    sql = _offline_sql("postgresql+psycopg://u:p@localhost/nowhere")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "VECTOR(1024)" in sql.upper()
    assert "USING hnsw" in sql
    assert HNSW_OPS in sql


def test_the_migration_emits_neither_on_sqlite():
    """SQLite has no extensions and no HNSW. Emitting either would take the
    whole suite down for a control SQLite cannot express."""
    sql = _offline_sql("sqlite://")
    assert "CREATE EXTENSION" not in sql
    assert "hnsw" not in sql
    assert "CREATE TABLE embeddings" in sql


@pytest.mark.parametrize("table", ["embeddings", "mapping_memory", "mapping_memory_sharing", "reviewer_corrections"])
def test_the_tenant_column_on_the_new_tables_is_not_nullable(app_client, table):
    """A nullable tenant is a tenant filter with a hole in it: `WHERE org_id = ?`
    silently drops every NULL row, so the data is neither visible nor
    protected."""
    from app.db import SessionLocal

    columns = {c["name"]: c for c in inspect(SessionLocal().bind).get_columns(table)}
    assert "org_id" in columns, f"{table} has no org_id"
    assert columns["org_id"]["nullable"] is False, f"{table}.org_id is nullable"


def test_the_embeddings_table_carries_a_per_tenant_index(app_client):
    """§18 wants per-tenant partitioning planned before the corpus reaches the
    low millions. The index leading on org_id is the first step of that, and it
    is also what keeps the scoped read from being a full scan today."""
    from app.db import SessionLocal

    indexes = inspect(SessionLocal().bind).get_indexes("embeddings")
    leading = {tuple(ix["column_names"])[0] for ix in indexes if ix["column_names"]}
    assert "org_id" in leading, f"no index leads on org_id: {indexes}"


# ------------------------------------------------------------ correction events


def test_a_correction_event_without_a_tenant_cannot_be_constructed():
    with pytest.raises(TenantScopeRequired):
        CorrectionEvent(
            org_id="", field_key="joining_date", accepted_column="Joining Dt",
            rejected_column=None, corrected_by="u1", at=WHEN,
        )


@pytest.mark.parametrize("kwargs, message", [
    ({"accepted_column": " "}, "column the reviewer accepted"),
    ({"corrected_by": ""}, "who made it"),
])
def test_a_correction_event_records_the_decision_and_the_decider(kwargs, message):
    """A correction with no accepted column describes nothing, and one with no
    author cannot be evidenced later."""
    base = dict(
        org_id=ORG_A, field_key="joining_date", accepted_column="Joining Dt",
        rejected_column=None, corrected_by="u1", at=WHEN,
    )
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        CorrectionEvent(**base)


def test_the_in_memory_store_keeps_corrections_as_an_append_only_log():
    """The same reviewer making the same call twice is two data points about how
    the suggestions are behaving, not one."""
    store = InMemoryMappingMemoryStore()
    memory = MappingMemory(store=store)
    for _ in range(2):
        memory.record_reviewer_correction(
            org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
            rejected_column="Start Dt", corrected_by="reviewer-a", at=WHEN,
        )
    assert len(store.corrections_for_org(ORG_A)) == 2
    assert store.corrections_for_org(ORG_B) == []
    with pytest.raises(TenantScopeRequired):
        store.corrections_for_org("")


def test_forgetting_an_organisation_takes_its_in_memory_corrections_with_it():
    store = InMemoryMappingMemoryStore()
    memory = MappingMemory(store=store)
    memory.record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        corrected_by="reviewer-a", at=WHEN,
    )
    memory.forget_org(ORG_A)
    assert store.corrections_for_org(ORG_A) == []


def test_the_default_provider_width_matches_the_column_width():
    """A provider that produced a different width would be rejected by every
    insert, which is a deployment failure rather than a runtime one -- worth
    noticing here instead."""
    assert HashingEmbedder().dimensions == DEFAULT_DIMENSIONS == 1024


def test_a_null_never_becomes_a_vector_of_zeros_on_the_way_through_the_column():
    """A zero vector has no direction, so a cosine against it is undefined
    rather than "no match". Quietly substituting one for a NULL would turn a
    missing embedding into a row that ranks against every query."""
    from sqlalchemy.dialects import sqlite

    column = VectorColumn(4)
    dialect = sqlite.dialect()
    assert column.process_bind_param(None, dialect) is None
    assert column.process_result_value(None, dialect) is None


def test_coordinates_survive_both_halves_of_the_json_fallback():
    from sqlalchemy.dialects import sqlite

    column = VectorColumn(4)
    dialect = sqlite.dialect()
    stored = column.process_bind_param(np.array([0.25, -0.5, 0.75, -1.0]), dialect)
    assert stored == [0.25, -0.5, 0.75, -1.0]
    assert np.array_equal(column.process_result_value(stored, dialect), np.array([0.25, -0.5, 0.75, -1.0]))


def test_the_hnsw_index_can_be_dropped_by_the_name_it_was_created_under():
    """A downgrade that names a different index leaves the real one behind, and
    the next upgrade fails on a name that already exists."""
    from app.retrieval.embeddings import drop_hnsw_index_ddl

    created = hnsw_index_ddl("embeddings", "vector")
    dropped = drop_hnsw_index_ddl("embeddings", "vector")
    assert "ix_embeddings_vector_hnsw" in created
    assert "ix_embeddings_vector_hnsw" in dropped
    assert dropped.startswith("DROP INDEX IF EXISTS")


def test_a_timestamp_that_already_carries_a_zone_is_converted_not_relabelled():
    """Re-attaching UTC to a naive column value restores information the column
    dropped. Doing the same to an aware value in another zone would move the
    moment, which is a different thing entirely."""
    from datetime import timedelta

    from app.retrieval.store import _as_utc

    tokyo = datetime(2026, 3, 4, 18, 30, tzinfo=timezone(timedelta(hours=9)))
    assert _as_utc(tokyo) == WHEN
    assert _as_utc(None) is None


def test_the_index_reports_and_clears_a_tenant_through_the_persistent_store(session, clean_orgs):
    """`VectorIndex`'s own surface has to keep working across the seam -- the
    store swap is not allowed to make `size` or `forget_org` mean something
    different."""
    index = VectorIndex(store=SqlVectorStore(session))
    before = index.size()
    index.index_many([
        {"record_id": "m1", "org_id": ORG_A, "kind": SOURCE_COLUMN_DESCRIPTION, "text": "Cost centre"},
        {"record_id": "m2", "org_id": ORG_A, "kind": SOURCE_COLUMN_DESCRIPTION, "text": "Legal entity"},
    ])
    session.commit()

    assert index.size() == before + 2
    assert index.provider.name.startswith("hashing-char-ngram")
    with pytest.raises(TenantScopeRequired):
        index.forget_org("")
    assert index.forget_org(ORG_A) == 2
    session.commit()
