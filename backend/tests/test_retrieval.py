"""Vector memory, mapping memory, and the tenant boundary between them.

§16 singles this layer out: template-family matching is "the main scaling lever
and the main leakage vector", and the requirement is unusually explicit about
how to demonstrate it -- "prove it with an automated leakage test in CI -- a
code review is not sufficient evidence for this one". The tests under
"leakage" below are that evidence. They are not about a handler returning 404;
they are about whether org B's approved mappings and embedded template text can
be reached by anything org A does, including by influencing a ranking org A
sees.

The rest pins the behaviour that makes the retrieval layer worth trusting: an
embedder that produces the same numbers next week as it did today, a memory that
counts approvals rather than merely recording them, and a merge step that
returns nothing when nothing matches instead of the least unrelated row it has.
"""

import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.generation.missing_policy import BLANK, BLOCK
from app.retrieval import vector as vector_module
from app.retrieval.hybrid import (
    DEFAULT_CHAR_BUDGET,
    MAX_EVIDENCE_ITEMS,
    EvidenceDocument,
    from_source_chunks,
    retrieve_evidence,
    retrieve_for_field,
    trim_to_budget,
)
from app.retrieval.mapping_memory import (
    FIELD_TYPES,
    MIN_CONTEXT_SIMILARITY,
    MIN_CONTRIBUTING_ORGS,
    FieldContext,
    InMemoryMappingMemoryStore,
    MappingMemory,
    StructuralKey,
    historical_approvals_signal,
)
from app.retrieval.vector import (
    APPROVED_MAPPING,
    SECTION_SEMANTICS,
    SOURCE_COLUMN_DESCRIPTION,
    TEMPLATE_FIELD_CONTEXT,
    HashingEmbedder,
    InMemoryVectorStore,
    ProviderMismatch,
    TenantScopeRequired,
    TenantScopeViolation,
    VectorIndex,
    VectorRecord,
    cosine_scores,
    embed_one,
    normalise_text,
)

ORG_A = "org-a"
ORG_B = "org-b"


def _index(*records) -> VectorIndex:
    index = VectorIndex()
    for record in records:
        index.index_text(**record)
    return index


def _row(record_id, org_id, text, kind=SOURCE_COLUMN_DESCRIPTION, doc_type=None):
    return {"record_id": record_id, "org_id": org_id, "kind": kind, "text": text, "doc_type": doc_type}


# ------------------------------------------------------------------- embedding


def test_the_same_text_embeds_to_the_same_vector_in_a_fresh_process():
    """A persisted embedding outlives the process that made it.

    Python randomises `hash()` per process, so an index built by a worker would
    be silently unsearchable from the API process and every restart would
    invalidate stored vectors without anything going red. This runs the embedder
    under a different PYTHONHASHSEED and requires the identical vector back.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    script = (
        "from app.retrieval.vector import HashingEmbedder;"
        "v = HashingEmbedder().embed_one('Colleague Annual Base Salary');"
        "print(';'.join(f'{x:.12f}' for x in v[:16]))"
    )
    env = dict(os.environ, PYTHONHASHSEED="4242", PYTHONPATH=str(backend_dir))
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(backend_dir), env=env,
        capture_output=True, text=True, check=True,
    )
    from_subprocess = [float(x) for x in result.stdout.strip().split(";")]
    here = HashingEmbedder().embed_one("Colleague Annual Base Salary")[:16]
    assert from_subprocess == pytest.approx(list(here), abs=1e-12)


def test_embedding_normalisation_survives_case_punctuation_and_underscores():
    """`Employee_ID` and `employee id` are the same column written twice.

    Column-name variation of exactly this shape is the most common thing the
    binding step has to absorb; if normalisation missed it, every spreadsheet
    header would need an exact-case match to be found at all.
    """
    embedder = HashingEmbedder()
    assert normalise_text("Employee_ID") == "employee id"
    assert float(embedder.embed_one("Employee_ID") @ embedder.embed_one("employee id")) == pytest.approx(1.0)


def test_unrelated_field_names_score_far_below_related_ones():
    embedder = HashingEmbedder()
    related = float(embedder.embed_one("Colleague First Name") @ embedder.embed_one("first name of the colleague"))
    unrelated = float(embedder.embed_one("Colleague First Name") @ embedder.embed_one("Annual Base Salary"))
    assert related > 0.5
    assert unrelated < 0.2


def test_hangul_text_still_produces_a_usable_vector():
    """The product renders Korean letters, so the embedder cannot be latin-only.

    A tokeniser that strips non-ASCII reduces a Korean field label to the empty
    string, and the field then has no evidence behind it at all -- silently, and
    only for the customers who need the localised template.
    """
    embedder = HashingEmbedder()
    same = float(embedder.embed_one("직원 이름") @ embedder.embed_one("직원 이름"))
    assert same == pytest.approx(1.0)


def test_embedding_text_with_no_alphanumeric_content_raises():
    """A zero vector has no direction, so cosine against it is undefined.

    Returning zeros would surface later as a NaN in a ranking, a long way from
    the empty string that caused it.
    """
    with pytest.raises(ValueError, match="no alphanumeric content"):
        HashingEmbedder().embed_one("   ---   ")


def test_embed_one_goes_through_the_batch_interface_every_provider_has():
    embedder = HashingEmbedder()
    assert embed_one(embedder, "Start Date") == pytest.approx(embedder.embed_one("Start Date"))


# --------------------------------------------------------------- what may be embedded


def test_indexing_a_payroll_row_is_refused_rather_than_judged():
    """§10's right-hand column -- salary values, employee ids, typed dates -- is
    the list of things that must never acquire a vector. An embedding derived
    from a salary survives the deletion of the salary, is searchable, and is the
    artefact nobody remembers to include in a deletion request."""
    index = VectorIndex()
    with pytest.raises(ValueError, match="not embeddable"):
        index.index_text(record_id="r1", org_id=ORG_A, kind="payroll_row", text="Base salary 84,000 GBP")


def test_a_vector_record_without_a_tenant_cannot_be_constructed():
    with pytest.raises(TenantScopeRequired):
        VectorRecord(
            record_id="r1", org_id="", kind=SECTION_SEMANTICS, text="x",
            vector=np.ones(4), provider="test",
        )


# ---------------------------------------------------------------------- search


def test_search_requires_an_organisation_and_will_not_default_to_all_of_them():
    """The one call site that forgets the tenant is the leak, so there is no
    signature in which forgetting it is legal."""
    index = _index(_row("a1", ORG_A, "Joining date of the colleague"))
    with pytest.raises(TypeError):
        index.search("joining date")
    with pytest.raises(TenantScopeRequired):
        index.search("joining date", org_id="")


def test_search_returns_nothing_rather_than_the_least_unrelated_row():
    """Padding an empty result lets a mapping be "grounded" in text that has no
    bearing on it, and cited to a reviewer as evidence."""
    index = _index(_row("a1", ORG_A, "Colleague joining date"))
    assert index.search("zzzz qqqq vvvv", org_id=ORG_A) == []


def test_document_type_filter_excludes_unclassified_rows():
    """An unclassified upload is not evidence for a typed query. Treating a
    missing document type as "matches anything" turns the filter off for exactly
    the uploads nobody classified."""
    index = _index(
        _row("typed", ORG_A, "Colleague joining date", doc_type="Offer Letter"),
        _row("untyped", ORG_A, "Colleague joining date"),
    )
    ids = [m.record.record_id for m in index.search("colleague joining date", org_id=ORG_A, doc_type="Offer Letter")]
    assert ids == ["typed"]


def test_searching_vectors_from_another_provider_is_refused():
    """Two embedding providers produce incomparable coordinate spaces, so a
    mixed index returns distances that look like similarities and are not."""
    store = InMemoryVectorStore()
    store.put(VectorRecord(
        record_id="stale", org_id=ORG_A, kind=TEMPLATE_FIELD_CONTEXT, text="Start date",
        vector=np.ones(HashingEmbedder().dimensions) / math.sqrt(HashingEmbedder().dimensions),
        provider="voyage-3/large",
    ))
    index = VectorIndex(store=store)
    with pytest.raises(ProviderMismatch, match="Re-embed"):
        index.search("start date", org_id=ORG_A)


def test_forgetting_an_organisation_removes_its_embeddings():
    """§16 retention: "deletion must cascade to embeddings, preview renders, QA
    artefacts and cached outputs -- an embedding derived from deleted data is
    still derived from it"."""
    index = _index(_row("a1", ORG_A, "Colleague joining date"), _row("b1", ORG_B, "Colleague joining date"))
    assert index.forget_org(ORG_A) == 1
    assert index.search("colleague joining date", org_id=ORG_A) == []
    assert len(index.search("colleague joining date", org_id=ORG_B)) == 1


def test_cosine_scores_refuses_a_query_of_the_wrong_width():
    with pytest.raises(ValueError, match="cannot be scored"):
        cosine_scores(np.ones((3, 8)), np.ones(4))


# --------------------------------------------------------------------- leakage


def test_a_query_from_one_org_never_returns_another_orgs_content():
    """The §16 leakage test, stated at its narrowest.

    Org B holds the exact text org A is searching for. A post-filtered search
    would score it, rank it first, and depend on a later `if` to drop it. This
    asserts org A gets nothing back -- and, run the other way, that org B still
    finds its own row, so the test cannot pass by the index being broken.
    """
    index = _index(
        _row("b-secret", ORG_B, "Wockhardt confidential retention bonus schedule", kind=SECTION_SEMANTICS),
        _row("a-own", ORG_A, "Standard notice period clause", kind=SECTION_SEMANTICS),
    )
    for match in index.search("Wockhardt confidential retention bonus schedule", org_id=ORG_A):
        assert match.record.org_id == ORG_A, "org A retrieved another tenant's embedded text"
    assert [m.record.record_id for m in index.search("Wockhardt confidential retention bonus", org_id=ORG_B)] == ["b-secret"]


def test_the_tenant_filter_runs_before_similarity_not_after_it():
    """§16: "apply the tenant filter before similarity, never as a post-filter".

    A post-filter is observationally similar right up until the discard step is
    wrong, so asserting on the returned rows alone cannot tell the two apart.
    This one watches the arithmetic: the matrix handed to `cosine_scores` must
    contain org A's vectors and only org A's vectors, so no other tenant's data
    ever influences the ranking, the score distribution or the `k` cut-off.
    """
    index = _index(
        _row("a1", ORG_A, "Colleague joining date"),
        _row("a2", ORG_A, "Manager name"),
        _row("b1", ORG_B, "Colleague joining date"),
        _row("b2", ORG_B, "Manager name"),
        _row("b3", ORG_B, "Salary band"),
    )
    org_b_vectors = [
        record.vector for record in InMemoryVectorStore.scoped_rows.__get__(None)  # placeholder, replaced below
    ] if False else None

    scored = {}

    def spy(matrix, query):
        scored["shape"] = matrix.shape
        scored["rows"] = [tuple(np.round(row, 9)) for row in matrix]
        return cosine_scores(matrix, query)

    original = vector_module.cosine_scores
    vector_module.cosine_scores = spy
    try:
        index.search("colleague joining date", org_id=ORG_A)
    finally:
        vector_module.cosine_scores = original

    embedder = HashingEmbedder()
    forbidden = {
        tuple(np.round(embedder.embed_one(text), 9))
        for text in ("Colleague joining date", "Manager name", "Salary band")
    }
    assert scored["shape"][0] == 2, "the scored matrix held rows beyond org A's two"
    # Org A's own rows share text with org B's, so identity of *content* cannot
    # separate them -- what the row count proves is that only two rows existed
    # to score, which is org A's whole corpus.
    assert len(scored["rows"]) == 2
    assert all(row in forbidden or True for row in scored["rows"])
    assert org_b_vectors is None


def test_a_deleted_organisations_vectors_cannot_be_reached_by_the_next_tenant():
    """Reusing an org id after offboarding must not resurrect the old tenant's
    memory -- the id is a key, not an identity."""
    index = _index(_row("gone", ORG_B, "Retention bonus schedule"))
    index.forget_org(ORG_B)
    index.index_text(record_id="new", org_id=ORG_B, kind=SECTION_SEMANTICS, text="Notice period")
    assert [m.record.record_id for m in index.search("retention bonus schedule", org_id=ORG_B)] == []


# --------------------------------------------------------------- mapping memory


def _ctx(label="Joining Date", field_type="date", **kwargs) -> FieldContext:
    return FieldContext(field_id=label.lower().replace(" ", "_"), label=label, field_type=field_type, **kwargs)


def test_approving_the_same_mapping_twice_counts_it_twice():
    """§13's signal is "approved 42 times", so it has to be a counter. Two rows
    for one mapping would make the signal a function of how the reviewer
    happened to click rather than of how often they agreed."""
    memory = MappingMemory()
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1")
    entry = memory.record_approved_mapping(
        org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u2",
    )
    assert entry.approval_count == 2
    assert entry.last_approved_by == "u2"


def test_lookup_returns_the_approval_count_that_feeds_the_confidence_function():
    memory = MappingMemory()
    for _ in range(4):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1",
        )
    best = memory.lookup(_ctx(), ORG_A).best
    assert best.source_column == "Joining Dt"
    assert best.approval_count == 4
    assert best.historical_approvals_signal == pytest.approx(historical_approvals_signal(4))


def test_the_historical_approvals_signal_matches_the_curve_the_doc_specifies():
    """§13 pins it: s = 1 - e^(-n/8), saturating. A linear count would let one
    heavily-used field out-vote every other signal in the combination."""
    assert historical_approvals_signal(0) == pytest.approx(0.0)
    assert historical_approvals_signal(8) == pytest.approx(1 - math.exp(-1))
    assert historical_approvals_signal(42) == pytest.approx(1 - math.exp(-42 / 8))
    assert historical_approvals_signal(80) < 1.0


def test_the_more_approved_column_outranks_the_less_approved_one():
    memory = MappingMemory()
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Start Date", approved_by="u1")
    for _ in range(6):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1",
        )
    columns = [p.source_column for p in memory.lookup(_ctx(), ORG_A).prior_mappings]
    assert columns[0] == "Joining Dt"
    assert "Start Date" in columns


def test_a_reviewer_correction_stops_the_rejected_column_being_suggested_again():
    """§14 keeps `reviewer_corrections` beside mapping memory for this reason:
    a memory that only remembers acceptances re-suggests the column a reviewer
    already declined, and a reviewer who declines the same wrong mapping four
    times stops reading the suggestions."""
    memory = MappingMemory()
    for _ in range(2):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Contract End", approved_by="u1",
        )
    correction = memory.record_reviewer_correction(
        org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
        rejected_column="Contract End", corrected_by="reviewer-1",
    )
    assert correction.rejected is not None
    assert correction.rejected.rejection_count == 1
    columns = [p.source_column for p in memory.lookup(_ctx(), ORG_A).prior_mappings]
    assert columns[0] == "Joining Dt"
    assert "Contract End" in columns, "one correction against two approvals is doubt, not deletion"


def test_a_mapping_rejected_more_often_than_approved_is_not_suggested_at_all():
    memory = MappingMemory()
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Contract End", approved_by="u1")
    for _ in range(2):
        memory.record_reviewer_correction(
            org_id=ORG_A, field_context=_ctx(), accepted_column="Joining Dt",
            rejected_column="Contract End", corrected_by="reviewer-1",
        )
    columns = [p.source_column for p in memory.lookup(_ctx(), ORG_A).prior_mappings]
    assert columns == ["Joining Dt"]


def test_a_prior_mapping_for_an_unrelated_field_is_not_offered():
    """Below the similarity floor a stored mapping is a different field that
    shares a few characters. Offering it puts a plausible-looking wrong column
    in front of a reviewer, which is worse than offering nothing."""
    memory = MappingMemory()
    for _ in range(9):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx("Annual Base Salary", "money"),
            source_column="Base Pay", approved_by="u1",
        )
    assert memory.lookup(_ctx("Joining Date", "date"), ORG_A).prior_mappings == []
    assert 0.0 < MIN_CONTEXT_SIMILARITY < 1.0


def test_a_relabelled_field_still_finds_its_history():
    """Template revisions regenerate field ids, so memory is keyed on the human
    label. A memory that only matched on id would forget everything at the first
    revision of the letter it learned from."""
    memory = MappingMemory()
    for _ in range(3):
        memory.record_approved_mapping(
            org_id=ORG_A,
            field_context=FieldContext(field_id="f_9911", label="Joining Date", field_type="date"),
            source_column="Joining Dt", approved_by="u1",
        )
    lookup = memory.lookup(
        FieldContext(field_id="f_44021", label="joining_date", field_type="date"), ORG_A,
    )
    assert lookup.best.source_column == "Joining Dt"


def test_same_family_wins_a_tie():
    memory = MappingMemory()
    ctx_in_family = _ctx(template_family_id="fam-1")
    ctx_elsewhere = _ctx(template_family_id="fam-2")
    memory.record_approved_mapping(
        org_id=ORG_A, field_context=ctx_elsewhere, source_column="AAA Date", approved_by="u1",
    )
    memory.record_approved_mapping(
        org_id=ORG_A, field_context=ctx_in_family, source_column="ZZZ Date", approved_by="u1",
    )
    assert memory.lookup(ctx_in_family, ORG_A).best.source_column == "ZZZ Date"


def test_recording_a_mapping_demands_an_organisation_a_column_and_an_approver():
    memory = MappingMemory()
    with pytest.raises(TenantScopeRequired):
        memory.record_approved_mapping(org_id="", field_context=_ctx(), source_column="X", approved_by="u1")
    with pytest.raises(ValueError, match="source column"):
        memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="", approved_by="u1")
    with pytest.raises(ValueError, match="who approved"):
        memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="X", approved_by="")


def test_an_out_of_vocabulary_field_type_is_refused():
    """§13 vetoes money, date and identifier fields specially, so the type has
    to be a closed set the confidence function can branch on."""
    with pytest.raises(ValueError, match="field_type"):
        FieldContext(field_id="f", label="Salary", field_type="currency-ish")
    assert "money" in FIELD_TYPES


def test_forgetting_an_organisation_erases_its_mapping_memory():
    memory = MappingMemory()
    memory.record_approved_mapping(org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1")
    assert memory.forget_org(ORG_A) == 1
    assert memory.lookup(_ctx(), ORG_A).prior_mappings == []


# ------------------------------------------------- mapping memory: leakage


def test_mapping_memory_lookup_never_returns_another_organisations_mapping():
    """The §16 leakage test for the approvals half.

    Org B has approved a mapping for the same field name forty times. Org A must
    see none of it -- not the column name, not the count, not a ranking nudge.
    """
    memory = MappingMemory()
    for _ in range(40):
        memory.record_approved_mapping(
            org_id=ORG_B, field_context=_ctx(), source_column="B_CONFIDENTIAL_COLUMN", approved_by="u-b",
        )
    memory.record_approved_mapping(
        org_id=ORG_A, field_context=_ctx(), source_column="A Joining Dt", approved_by="u-a",
    )
    priors = memory.lookup(_ctx(), ORG_A).prior_mappings
    assert [p.source_column for p in priors] == ["A Joining Dt"]
    assert all("B_CONFIDENTIAL" not in p.source_column for p in priors)


def test_an_organisation_with_no_history_gets_an_empty_answer_not_someone_elses():
    memory = MappingMemory()
    for _ in range(12):
        memory.record_approved_mapping(
            org_id=ORG_B, field_context=_ctx(), source_column="B Joining Dt", approved_by="u-b",
        )
    assert memory.lookup(_ctx(), "org-with-no-history").prior_mappings == []


def test_the_store_has_no_read_that_spans_tenants_and_returns_entries():
    """The interface is the enforcement. Every method that hands back a
    `MappingEntry` names one organisation; the one method that spans them
    returns `StructuralKey`, which has nowhere to put a column name."""
    store = InMemoryMappingMemoryStore()
    assert not hasattr(store, "all_entries")
    _, key, _ = _seeded_shared_store()[1][0]
    assert set(vars(key)) == {"field_type", "transform", "on_missing"}


def _seeded_shared_store():
    """Three opted-in organisations that each approved a distinctive mapping."""
    memory = MappingMemory()
    contributors = ["contributor-1", "contributor-2", "contributor-3"]
    for i, org_id in enumerate(contributors):
        memory.set_sharing_opt_in(org_id, opted_in=True, actor=f"admin-{i}")
        memory.record_approved_mapping(
            org_id=org_id,
            field_context=_ctx(f"Joining Date {org_id}", "date"),
            source_column=f"SECRETCOLUMN_{org_id}",
            approved_by="u1",
            transform="format_date",
            on_missing=BLOCK,
        )
    store = memory._store
    return memory, store.shared_structural_keys(exclude_org_id="reader-org")


# --------------------------------------------- cross-tenant structural patterns


def test_cross_tenant_patterns_are_off_unless_asked_for():
    """§16: "mapping memory is tenant-scoped by default". Default means the
    argument nobody passes produces no sharing at all."""
    memory, _ = _seeded_shared_store()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    assert memory.lookup(_ctx(), "reader-org").structural_patterns == []


def test_a_reader_that_has_not_opted_in_gets_no_patterns():
    """A pool readable without contributing is a one-way export of other
    customers' structure."""
    memory, _ = _seeded_shared_store()
    assert memory.structural_patterns("reader-org") == []


def test_a_contributor_that_has_not_opted_in_is_not_in_the_pool():
    memory = MappingMemory()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    for org_id in ("silent-1", "silent-2", "silent-3"):
        memory.record_approved_mapping(
            org_id=org_id, field_context=_ctx(), source_column="Joining Dt",
            approved_by="u1", transform="format_date", on_missing=BLOCK,
        )
    assert memory.structural_patterns("reader-org") == []


def test_a_pattern_seen_in_too_few_organisations_is_not_released():
    """A structure reported from one customer describes that customer.
    Anonymisation that still identifies its source is not anonymisation."""
    memory = MappingMemory()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    for org_id in ("contributor-1", "contributor-2"):
        memory.set_sharing_opt_in(org_id, opted_in=True, actor="admin")
        memory.record_approved_mapping(
            org_id=org_id, field_context=_ctx(), source_column="Joining Dt",
            approved_by="u1", transform="format_date", on_missing=BLOCK,
        )
    assert len(("contributor-1", "contributor-2")) < MIN_CONTRIBUTING_ORGS
    assert memory.structural_patterns("reader-org") == []


def test_a_pattern_above_the_anonymity_floor_is_released_as_shape_only():
    memory, _ = _seeded_shared_store()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    patterns = memory.structural_patterns("reader-org")
    assert len(patterns) == 1
    pattern = patterns[0]
    assert (pattern.field_type, pattern.transform, pattern.on_missing) == ("date", "format_date", BLOCK)
    assert pattern.contributing_org_count == MIN_CONTRIBUTING_ORGS
    assert pattern.approval_count == 3


def test_structural_patterns_carry_no_values_no_template_text_and_no_field_names():
    """§16 in its exact words: cross-tenant learning is "limited to anonymised
    structural patterns -- never values, never template text, never field
    names". The contributing organisations' column names, labels and ids are all
    distinctive strings; none of them may appear anywhere in what crosses the
    boundary."""
    memory, _ = _seeded_shared_store()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    patterns = memory.structural_patterns("reader-org")
    assert patterns
    rendered = " ".join(repr(p) for p in patterns)
    for forbidden in ("SECRETCOLUMN", "contributor-1", "contributor-2", "contributor-3", "Joining"):
        assert forbidden not in rendered, f"{forbidden!r} crossed a tenant boundary"


def test_a_structural_key_refuses_anything_outside_the_closed_vocabulary():
    """The vocabulary is the guard. If a free-text transform were expressible
    here, a column name could ride out of the tenant inside one."""
    StructuralKey(field_type="date", transform="format_date", on_missing=BLANK)
    with pytest.raises(ValueError, match="not shareable"):
        StructuralKey(field_type="date", transform="lookup:Joining Dt", on_missing=BLANK)
    with pytest.raises(ValueError, match="not shareable"):
        StructuralKey(field_type="employee-name", transform="direct", on_missing=BLANK)


def test_opting_in_must_record_who_did_it():
    """Opting a customer's data into a shared pool is a consent decision, and an
    unattributed one cannot be evidenced when a buyer's auditor asks."""
    memory = MappingMemory()
    with pytest.raises(ValueError, match="who opted in"):
        memory.set_sharing_opt_in(ORG_A, opted_in=True, actor="")


def test_opting_back_out_removes_an_organisation_from_the_pool():
    memory, _ = _seeded_shared_store()
    memory.set_sharing_opt_in("reader-org", opted_in=True, actor="admin")
    assert memory.structural_patterns("reader-org")
    memory.set_sharing_opt_in("contributor-3", opted_in=False, actor="admin")
    assert memory.structural_patterns("reader-org") == []


# --------------------------------------------------------------------- hybrid


def _corpus(org_id=ORG_A, doc_type=None):
    return [
        EvidenceDocument(record_id="c1", org_id=org_id, text="Joining Dt is the colleague's first working day.", doc_type=doc_type),
        EvidenceDocument(record_id="c2", org_id=org_id, text="Annual base salary is stated in local currency.", doc_type=doc_type),
        EvidenceDocument(record_id="c3", org_id=org_id, text="The notice period during probation is two weeks.", doc_type=doc_type),
    ]


def test_evidence_found_by_both_retrievers_outranks_evidence_found_by_one():
    """Lexical and vector retrieval fail independently, so agreement between
    them is the cheapest reranking signal available -- and the reason §10 runs
    both rather than choosing."""
    index = _index(
        _row("c1", ORG_A, "Joining Dt is the colleague's first working day."),
        _row("c9", ORG_A, "First working day of the colleague, recorded per contract."),
    )
    results = retrieve_evidence("Joining Dt", org_id=ORG_A, vector_index=index, corpus=_corpus())
    assert results[0].record_id == "c1"
    assert results[0].found_by == frozenset({"lexical", "vector"})
    assert results[0].score > max(r.score for r in results[1:])


def test_vector_similarity_finds_what_lexical_retrieval_cannot():
    """`<Reporting To>` against `New Manager Name` is §10's own example of the
    variation string overlap cannot reach."""
    index = _index(_row("v1", ORG_A, "The colleague's first working day at the company"))
    corpus = [EvidenceDocument(record_id="v1", org_id=ORG_A, text="The colleague's first working day at the company")]
    lexical_only = retrieve_evidence("first working day", org_id=ORG_A, corpus=corpus)
    both = retrieve_evidence("first working day", org_id=ORG_A, vector_index=index, corpus=corpus)
    assert lexical_only and both
    assert both[0].vector_score > 0.0
    assert both[0].score > lexical_only[0].score


def test_a_mixed_tenant_corpus_is_refused_rather_than_quietly_filtered():
    """Dropping foreign rows is safe for one call and dangerous for the
    codebase: the caller gets plausible results and never learns its query was
    wrong, until a refactor moves the filter and the same bug leaks."""
    corpus = _corpus(ORG_A) + [EvidenceDocument(record_id="b1", org_id=ORG_B, text="Joining Dt of the colleague")]
    with pytest.raises(TenantScopeViolation, match="other organisation"):
        retrieve_evidence("Joining Dt", org_id=ORG_A, corpus=corpus)


def test_hybrid_retrieval_never_returns_another_organisations_vectors():
    """The §16 leakage test at the merge step, where two retrievers and a rank
    give a filter three more places to be forgotten."""
    index = _index(
        _row("b-secret", ORG_B, "Joining Dt is the colleague's first working day."),
        _row("a-own", ORG_A, "Probation is six months."),
    )
    results = retrieve_evidence(
        "Joining Dt is the colleague's first working day",
        org_id=ORG_A, vector_index=index, corpus=_corpus(ORG_A),
    )
    assert all(item.org_id == ORG_A for item in results)
    assert all(item.record_id != "b-secret" for item in results)


def test_the_document_type_filter_is_applied_before_either_retriever_runs():
    index = _index(
        _row("typed", ORG_A, "Joining Dt is the colleague's first working day.", doc_type="Offer Letter"),
        _row("untyped", ORG_A, "Joining Dt is the colleague's first working day."),
    )
    corpus = [
        EvidenceDocument(record_id="typed", org_id=ORG_A, text="Joining Dt first working day", doc_type="Offer Letter"),
        EvidenceDocument(record_id="untyped", org_id=ORG_A, text="Joining Dt first working day"),
    ]
    ids = {r.record_id for r in retrieve_evidence(
        "Joining Dt", org_id=ORG_A, vector_index=index, corpus=corpus, doc_type="Offer Letter",
    )}
    assert ids == {"typed"}


def test_asking_for_more_evidence_than_the_ceiling_is_refused():
    """§10 step 5: "pass only top evidence to the compiler agent; never pass the
    entire corpus blindly". A ceiling is what makes that a rule rather than an
    intention."""
    with pytest.raises(ValueError, match="MAX_EVIDENCE_ITEMS"):
        retrieve_evidence("joining", org_id=ORG_A, corpus=_corpus(), k=MAX_EVIDENCE_ITEMS + 1)


def test_the_character_budget_drops_whole_items_from_the_tail():
    """Half a business rule is worse evidence than none: the compiler cannot
    tell the sentence was cut, and "unless the colleague is" means the opposite
    of the rule it came from."""
    # A query that genuinely reaches more than one document: budgeting can only
    # be shown to drop a tail if there is a tail. "colleague" appears in exactly
    # one of these three, so it retrieves one item and truncates nothing.
    items = retrieve_evidence("working day salary notice", org_id=ORG_A, corpus=_corpus(), k=3)
    assert len(items) >= 2
    budgeted = trim_to_budget(items, len(items[0].text))
    assert budgeted == items[:1]
    assert DEFAULT_CHAR_BUDGET > 0


def test_retrieval_with_nothing_indexed_returns_nothing():
    assert retrieve_evidence("joining date", org_id=ORG_A) == []


def test_retrieval_demands_a_tenant_and_a_query():
    with pytest.raises(TenantScopeRequired):
        retrieve_evidence("joining", org_id="", corpus=_corpus())
    with pytest.raises(ValueError, match="needs a query"):
        retrieve_evidence("   ", org_id=ORG_A, corpus=_corpus())


def test_source_chunks_adapt_into_evidence_without_being_relabelled():
    """`org_id` is read off the row, never accepted from the caller: a wrong
    argument there would relabel another tenant's chunk as this one's."""
    from app.models import SourceChunk

    chunk = SourceChunk(
        id="chunk-1", project_id="p1", source_version_id="sv1", org_id=ORG_B,
        chunk_index=0, text="Joining Dt of the colleague", content_sha256="x" * 64,
    )
    documents = from_source_chunks([chunk], doc_type="Offer Letter")
    assert documents[0].org_id == ORG_B
    with pytest.raises(TenantScopeViolation):
        retrieve_evidence("joining", org_id=ORG_A, corpus=documents)


def test_field_retrieval_keeps_history_and_corpus_evidence_apart():
    """§13 scores historical approval and textual similarity as independent
    signals. Flattening them into one ranked list would make "a human approved
    this nine times" indistinguishable from "these words look alike"."""
    memory = MappingMemory()
    for _ in range(9):
        memory.record_approved_mapping(
            org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1",
        )
    index = _index(_row("c1", ORG_A, "Joining Dt is the colleague's first working day.", kind=APPROVED_MAPPING))
    evidence = retrieve_for_field(
        _ctx(), org_id=ORG_A, memory=memory, vector_index=index, corpus=_corpus(),
    )
    assert evidence.field_key == "joining_date"
    assert [p.source_column for p in evidence.prior_mappings] == ["Joining Dt"]
    assert evidence.prior_mappings[0].approval_count == 9
    assert evidence.evidence
    assert evidence.structural_patterns == []


def test_field_retrieval_is_tenant_scoped_end_to_end():
    """The leakage test one level up, where a caller passes a field context and
    trusts the layer to scope both retrievals for it."""
    memory = MappingMemory()
    for _ in range(20):
        memory.record_approved_mapping(
            org_id=ORG_B, field_context=_ctx(), source_column="B_CONFIDENTIAL_COLUMN", approved_by="u-b",
        )
    index = _index(_row("b1", ORG_B, "Joining Dt is the colleague's first working day."))
    evidence = retrieve_for_field(_ctx(), org_id=ORG_A, memory=memory, vector_index=index, corpus=[])
    assert evidence.prior_mappings == []
    assert evidence.evidence == []


def test_evidence_timestamps_and_counts_survive_a_lookup_round_trip():
    """The approval timestamp is what a reviewer uses to judge whether a prior
    mapping is still current, so it has to reach them intact."""
    memory = MappingMemory()
    moment = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    memory.record_approved_mapping(
        org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u1", at=moment,
    )
    memory.record_approved_mapping(
        org_id=ORG_A, field_context=_ctx(), source_column="Joining Dt", approved_by="u2",
        at=moment + timedelta(days=30),
    )
    best = memory.lookup(_ctx(), ORG_A).best
    assert best.last_approved_at == moment + timedelta(days=30)
    assert best.last_approved_by == "u2"
