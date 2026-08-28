"""The §10 retrieval sequence, in order, with the filter that has to come first.

§10 does not describe lexical and vector retrieval as alternatives. It gives an
ordered recipe, and the order is the design:

    1. Apply tenant/company and document-type metadata filters first.
    2. Run lexical retrieval (BM25/keyword) for exact terms and placeholder names.
    3. Run pgvector similarity for semantic naming variation.
    4. Merge candidates and optionally rerank using context/type/business-rule signals.
    5. Pass only top evidence to the compiler agent; never pass the entire corpus blindly.

Each step exists because the one before it is insufficient. Lexical retrieval
alone cannot connect `<Reporting To>` to `New Manager Name`; vector similarity
alone loses to lexical on exact placeholder codes like `LAB__FT_SALARY__38_HR_`,
where a hash of character n-grams is a worse instrument than string equality.
Running both and merging is what makes the compiler's evidence cover both
failure shapes.

Step 5 is a constraint rather than an optimisation. A compiler prompt built from
"every chunk we have" costs money proportional to the customer's estate, blows
past the context window on the estates that most need this system, and buries
the two paragraphs that mattered among four hundred that did not. So `k` has a
hard ceiling and the returned evidence can be trimmed to a character budget
before it ever reaches a prompt.

Step 1 is the one that is not about quality at all. The tenant filter runs
before any scoring, in the stores, and a corpus containing another
organisation's rows is refused outright rather than quietly filtered: silently
dropping foreign rows would make the mistake invisible, and the next caller
would make it in a place where nothing was checking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.retrieval import lexical
from app.retrieval.mapping_memory import FieldContext, MappingMemory, MemoryLookup
from app.retrieval.vector import (
    SOURCE_COLUMN_DESCRIPTION,
    TenantScopeRequired,
    TenantScopeViolation,
    VectorIndex,
)

#: The hard ceiling on how much evidence any one retrieval may return. §10's
#: "never pass the entire corpus blindly" needs a number somewhere, and a
#: request for more than this is a caller trying to do exactly that.
MAX_EVIDENCE_ITEMS = 24

#: Vector floor used while merging. Below the index's own noise floor on
#: purpose: a weak vector score that lands on the same record as a strong
#: lexical hit is corroboration, and dropping it before the merge would throw
#: away the agreement the rerank exists to notice. Nothing reaches the compiler
#: on this score alone -- `_rank` still has to place it.
VECTOR_RECALL_FLOOR = 0.0

#: A rough prompt budget for evidence text. Characters rather than tokens on
#: purpose: the tokeniser belongs to whichever provider is configured, and this
#: layer must not need to know which one that is.
DEFAULT_CHAR_BUDGET = 8000

#: Merge weights. Starting values in §13's sense -- to be calibrated against
#: reviewer decisions, not treated as measured constants. Vector is weighted
#: slightly higher because the lexical half of the pair is already strong
#: wherever it fires at all, so the marginal information usually comes from the
#: semantic side.
LEXICAL_WEIGHT = 0.45
VECTOR_WEIGHT = 0.55

#: Found by both retrievers is a different quality of evidence from found by
#: one: the two methods fail independently, so agreement between them is the
#: cheapest reranking signal available.
AGREEMENT_BONUS = 0.10


@dataclass(frozen=True)
class EvidenceDocument:
    """One lexically searchable item, carrying the metadata step 1 filters on.

    A plain dataclass rather than a database row so this module stays usable
    from the compiler, from a test, and from a future worker that has already
    loaded its corpus. `from_source_chunks` adapts the rows that exist today.
    """

    record_id: str
    org_id: str
    text: str
    kind: str = SOURCE_COLUMN_DESCRIPTION
    doc_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.org_id or not str(self.org_id).strip():
            raise TenantScopeRequired("an evidence document without an org_id could never be filtered")


def from_source_chunks(chunks: Sequence[Any], *, doc_type: str | None = None) -> list[EvidenceDocument]:
    """Adapt `SourceChunk` rows into the shape this module filters and ranks.

    Duck-typed on `id`, `org_id` and `text` so a caller can pass ORM rows or
    anything else with those attributes. `org_id` is read from the row rather
    than accepted as an argument -- taking it from the caller would let a wrong
    value relabel another tenant's chunk as this one's, which is the failure
    this whole module exists to prevent.
    """
    documents = []
    for chunk in chunks:
        documents.append(EvidenceDocument(
            record_id=str(chunk.id),
            org_id=str(chunk.org_id),
            text=chunk.text,
            doc_type=doc_type,
            metadata={"heading_path": getattr(chunk, "heading_path", None)},
        ))
    return documents


@dataclass(frozen=True)
class RankedEvidence:
    """One piece of evidence, with the provenance a reviewer can act on.

    Both component scores survive into the result. A reviewer asking "why was
    this suggested?" gets "the column name matched exactly" or "nothing matched
    the name, but the description is semantically close", which are different
    answers deserving different amounts of scepticism.
    """

    record_id: str
    org_id: str
    kind: str
    text: str
    score: float
    lexical_score: float
    vector_score: float
    found_by: frozenset
    doc_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldEvidence:
    """Everything the compiler is allowed to see about one field."""

    field_key: str
    org_id: str
    prior_mappings: list
    structural_patterns: list
    evidence: list


# ------------------------------------------------------------------ step 1


def _filter_corpus(
    corpus: Sequence[EvidenceDocument],
    *,
    org_id: str,
    doc_type: str | None,
) -> list[EvidenceDocument]:
    """Tenant and document-type metadata filters, before anything is scored.

    A foreign row raises rather than being dropped. Dropping is safe for this
    call and dangerous for the codebase: it means a caller can assemble a
    mixed-tenant corpus, get plausible results, and never learn that its query
    was wrong -- until a later refactor moves the filter and the same bug leaks.
    """
    foreign = {d.org_id for d in corpus} - {org_id}
    if foreign:
        raise TenantScopeViolation(
            f"corpus for org {org_id!r} contains documents belonging to {len(foreign)} other "
            "organisation(s). Retrieval must be handed one tenant's rows, not filtered into one."
        )
    if doc_type is None:
        return list(corpus)
    # An unclassified document is not evidence for a typed query. Treating a
    # missing document type as "matches anything" would turn the filter off for
    # exactly the uploads nobody got round to classifying.
    return [d for d in corpus if d.doc_type == doc_type]


# ------------------------------------------------------------------ steps 2-4


def retrieve_evidence(
    query: str,
    *,
    org_id: str,
    vector_index: VectorIndex | None = None,
    corpus: Sequence[EvidenceDocument] = (),
    doc_type: str | None = None,
    kind: str | None = None,
    k: int = 8,
    char_budget: int | None = None,
) -> list[RankedEvidence]:
    """Ranked evidence for one query, following §10's sequence in order.

    Returns at most `k` items, and never more than `MAX_EVIDENCE_ITEMS`. An
    empty list is a legitimate answer and means what it says: nothing in this
    organisation's corpus bears on the query. It does not mean "here is the
    least unrelated thing we have".
    """
    if not org_id or not str(org_id).strip():
        raise TenantScopeRequired("retrieve_evidence requires an org_id; there is no global corpus")
    if k < 1:
        raise ValueError(f"k={k} would ask for no evidence at all")
    if k > MAX_EVIDENCE_ITEMS:
        raise ValueError(
            f"k={k} exceeds MAX_EVIDENCE_ITEMS={MAX_EVIDENCE_ITEMS}. §10: pass only top evidence "
            "to the compiler agent, never the entire corpus."
        )
    if not query or not query.strip():
        raise ValueError("retrieve_evidence needs a query")

    # Step 1 -- metadata filters first, for both retrievers. `kind` is applied
    # to the lexical corpus here and passed into the vector index below, so the
    # two halves of the merge are drawn from the same population. Filtering only
    # one of them would let a record win on a signal the other retriever was
    # never allowed to disagree with.
    scoped = _filter_corpus(corpus, org_id=org_id, doc_type=doc_type)
    if kind is not None:
        scoped = [d for d in scoped if d.kind == kind]

    # Step 2 -- lexical. `lexical.retrieve` reads only `.text` off each item and
    # hands the item back, so an EvidenceDocument passes through unchanged.
    lexical_hits: dict[str, tuple[EvidenceDocument, float]] = {}
    if scoped:
        for document, score in lexical.retrieve(scoped, query, k=MAX_EVIDENCE_ITEMS):
            lexical_hits[document.record_id] = (document, float(score))

    # TF-IDF cosine already lands in 0..1, but `lexical.retrieve` falls back to
    # raw keyword-overlap counts when the corpus is too small to vectorise.
    # Dividing by a max that is never below 1.0 rescales the fallback without
    # inflating genuine cosines.
    lexical_ceiling = max([1.0] + [s for _, s in lexical_hits.values()])

    # Step 3 -- vector similarity, tenant-filtered inside the index.
    vector_hits: dict[str, tuple[Any, float]] = {}
    if vector_index is not None:
        # A lower floor than a bare `search` would use, deliberately. The index's
        # default keeps a direct caller from citing the least-unrelated row as
        # evidence; here the vector score is one of two signals feeding a rerank,
        # and a weak-but-real overlap that agrees with the lexical hit is exactly
        # what "found by both" is meant to reward. The merge, not the index, is
        # what decides relevance on this path.
        for match in vector_index.search(
            query, org_id=org_id, doc_type=doc_type, kind=kind, k=MAX_EVIDENCE_ITEMS,
            min_score=VECTOR_RECALL_FLOOR,
        ):
            vector_hits[match.record.record_id] = (match.record, float(match.score))

    # Step 4 -- merge and rank.
    merged: list[RankedEvidence] = []
    for record_id in sorted(set(lexical_hits) | set(vector_hits)):
        lexical_document, raw_lexical = lexical_hits.get(record_id, (None, 0.0))
        vector_record, raw_vector = vector_hits.get(record_id, (None, 0.0))
        normalised_lexical = raw_lexical / lexical_ceiling
        normalised_vector = max(0.0, raw_vector)

        found_by = set()
        if lexical_document is not None:
            found_by.add("lexical")
        if vector_record is not None:
            found_by.add("vector")

        score = LEXICAL_WEIGHT * normalised_lexical + VECTOR_WEIGHT * normalised_vector
        if len(found_by) == 2:
            score = min(1.0, score + AGREEMENT_BONUS)

        source = lexical_document if lexical_document is not None else vector_record
        merged.append(RankedEvidence(
            record_id=record_id,
            org_id=source.org_id,
            kind=source.kind,
            text=source.text,
            score=round(score, 4),
            lexical_score=round(normalised_lexical, 4),
            vector_score=round(normalised_vector, 4),
            found_by=frozenset(found_by),
            doc_type=source.doc_type,
            metadata=dict(getattr(source, "metadata", {}) or {}),
        ))

    merged.sort(key=lambda item: (-item.score, item.record_id))
    top = merged[:k]

    # Step 5 -- the budget, applied last so ranking decides what survives it.
    if char_budget is not None:
        top = trim_to_budget(top, char_budget)
    return top


def trim_to_budget(items: Sequence[RankedEvidence], char_budget: int) -> list[RankedEvidence]:
    """Drop from the tail until the evidence text fits `char_budget`.

    Whole items, never truncated ones. Half a paragraph of a business rule is
    worse evidence than none of it: the compiler cannot tell that the sentence
    was cut, and a rule that reads "unless the colleague is" means the opposite
    of the rule it came from.
    """
    if char_budget < 1:
        raise ValueError(f"char_budget={char_budget} leaves room for no evidence at all")
    kept: list[RankedEvidence] = []
    used = 0
    for item in items:
        cost = len(item.text)
        if used + cost > char_budget:
            break
        kept.append(item)
        used += cost
    return kept


def retrieve_for_field(
    field_context: FieldContext,
    *,
    org_id: str,
    memory: MappingMemory | None = None,
    vector_index: VectorIndex | None = None,
    corpus: Sequence[EvidenceDocument] = (),
    kind: str | None = None,
    k: int = 8,
    memory_k: int = 5,
    char_budget: int | None = DEFAULT_CHAR_BUDGET,
    include_structural_patterns: bool = False,
) -> FieldEvidence:
    """Everything §5 step 6 asks for about one unresolved object, in one call.

    "Retrieve historical mappings, synonyms and approved business rules relevant
    to each unresolved object" is two retrievals, not one: mapping memory
    answers "what has this customer approved for this field before", and hybrid
    retrieval answers "what in this customer's corpus is about this field". They
    are kept as separate fields on the result because §13 scores them as
    separate signals, and flattening them into one ranked list would make
    historical approval indistinguishable from textual similarity.
    """
    if not org_id or not str(org_id).strip():
        raise TenantScopeRequired("retrieve_for_field requires an org_id")

    lookup: MemoryLookup | None = None
    if memory is not None:
        lookup = memory.lookup(
            field_context, org_id, k=memory_k, include_structural_patterns=include_structural_patterns,
        )

    evidence = retrieve_evidence(
        field_context.context_text,
        org_id=org_id,
        vector_index=vector_index,
        corpus=corpus,
        doc_type=field_context.doc_type,
        kind=kind,
        k=k,
        char_budget=char_budget,
    )
    return FieldEvidence(
        field_key=field_context.memory_key,
        org_id=org_id,
        prior_mappings=list(lookup.prior_mappings) if lookup else [],
        structural_patterns=list(lookup.structural_patterns) if lookup else [],
        evidence=evidence,
    )
