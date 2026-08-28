"""Semantic memory, and the tenant filter that has to run before it.

§10 of the architecture record splits retrieval in two: lexical retrieval finds
exact terms and placeholder names, and vector similarity finds the semantic
naming variation lexical retrieval cannot -- `<Reporting To>` against
`New Manager Name`. Only the first half existed here (`lexical.py`, TF-IDF over
a project's chunks), so every mapping the compiler could not match by string
overlap had no evidence at all behind it.

The half that matters more, though, is the filter in front of it. §16 names
template-family matching "the main scaling lever and the main leakage vector",
and is specific about the mechanism:

    "pgvector searches apply the tenant filter before similarity, never as a
    post-filter on results."

The difference is not stylistic. A post-filter computes one customer's salary
letter against every other customer's corpus and then discards the rows it
should never have scored: the ranking, the score distribution, the timing and
any `k` cut-off have already been influenced by data belonging to someone else,
and a single missing `if` in the discard step turns a near-miss into a
disclosure. So the store here has no unscoped read at all. `scoped_rows`
requires an org, the in-memory implementation partitions by org so a missing
filter is an empty result rather than a full one, and the similarity computation
only ever sees a matrix that was assembled from one tenant's rows.

What gets embedded is also constrained. §10's table lists what earns a vector --
field context, conditional instruction text, section semantics, source column
descriptions, approved mappings, organisation terminology -- and what does not:
exact salary values, employee identifiers, typed dates, booleans, and every row
of a clean structured spreadsheet. `EMBEDDABLE_KINDS` is that left-hand column
as a closed vocabulary, so indexing a payroll row is a `ValueError` rather than
a judgement call made at a call site.

Where the vectors live is a second decision, kept behind `VectorStore`. The
in-memory implementation below is the reference one and the one the suite runs
against; `app.retrieval.store.SqlVectorStore` is the same contract over
PostgreSQL and pgvector, where §10 says semantic memory belongs -- "within the
same data platform" as transactional truth, so a tenant filter, a backup and a
deletion request all reach the vectors by the route they already reach the rows.
Neither implementation has a read that spans tenants, so the boundary above does
not depend on which one is plugged in.

The provider is a protocol with a local, deterministic default. There is no
external embedding API and no new dependency: `HashingEmbedder` is a signed
hashing vectoriser over word unigrams and character n-grams, which handles the
morphological variation ("emp_id" / "Employee ID") that matters most for column
and field naming, and works on Hangul and CJK text where whitespace tokenisation
does not. A hosted provider drops in by implementing `EmbeddingProvider`; every
record records which provider produced it, and a search across records embedded
by a different provider fails loudly instead of returning nonsense distances.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np

# ---------------------------------------------------------------------- errors


class RetrievalError(Exception):
    """Base for every refusal in the retrieval layer."""


class TenantScopeRequired(RetrievalError):
    """A retrieval call arrived without the organisation to scope it to."""


class TenantScopeViolation(RetrievalError):
    """A retrieval call was handed data belonging to another organisation."""


class ProviderMismatch(RetrievalError):
    """Vectors in the index were produced by a different embedding provider."""


# ------------------------------------------------------------ what may be embedded

#: §10 "What to embed". A closed vocabulary rather than a free-text label,
#: because the right-hand column of that table -- exact salary values, employee
#: identifiers, already-typed dates, enum values, every row of a clean
#: spreadsheet -- is the list of things that must never acquire a vector. An
#: embedding derived from a salary is still derived from it: it survives row
#: deletion, it is searchable, and it is the one artefact nobody thinks to
#: include in a deletion request. Making the caller name the kind means adding a
#: new class of embedded content is a deliberate edit here.
TEMPLATE_FIELD_CONTEXT = "template_field_context"
CONDITIONAL_INSTRUCTION = "conditional_instruction"
SECTION_SEMANTICS = "section_semantics"
SOURCE_COLUMN_DESCRIPTION = "source_column_description"
APPROVED_MAPPING = "approved_mapping"
ORG_TERMINOLOGY = "org_terminology"

EMBEDDABLE_KINDS = frozenset({
    TEMPLATE_FIELD_CONTEXT,
    CONDITIONAL_INSTRUCTION,
    SECTION_SEMANTICS,
    SOURCE_COLUMN_DESCRIPTION,
    APPROVED_MAPPING,
    ORG_TERMINOLOGY,
})


# ------------------------------------------------------------------- embedding

#: Wide enough that hash collisions stay rare across a template estate's
#: vocabulary, small enough that a record costs 8KB of float64 in memory and a
#: `vector(1024)` column in Postgres.
DEFAULT_DIMENSIONS = 1024

#: Cosine below which a match is noise rather than a weak signal.
#:
#: A hashing character-n-gram embedder gives unrelated English text a small but
#: non-zero cosine, because two sentences share spaces and common letters
#: whatever they mean. Measured against a one-row index holding "Colleague
#: joining date": unrelated queries ("zzzz qqqq vvvv", "salary") land at
#: 0.04-0.05, a loosely related one ("start date of employment") at 0.17, and
#: related ones at 0.62-1.00. 0.10 sits above the noise and below anything with
#: real overlap.
#:
#: This is a recall floor, not the §13 confidence floor. §13 puts
#: semantic_similarity's floor at 0.75 before it contributes to a mapping's
#: score at all -- retrieval's job is to offer candidates, and the confidence
#: function's job is to decide whether one is good enough to act on.
NOISE_FLOOR = 0.10

#: Character n-gram widths. Three catches stems and abbreviations; four keeps
#: unrelated short words from colliding. Both are needed for scripts that do not
#: separate words with whitespace, where the word tokens below yield one token
#: for an entire clause.
_NGRAM_WIDTHS = (3, 4)

#: Anything that is not a word character, plus the underscore. Deliberately not
#: `[^a-z0-9]`: that reduces Hangul, CJK and accented Latin text to an empty
#: string, and this product renders Korean letters.
_SEPARATORS = re.compile(r"[\W_]+", re.UNICODE)


def normalise_text(text: str) -> str:
    """Case-folded, punctuation-stripped, single-spaced.

    `Employee_ID` and `employee id` must produce the same features or column
    matching fails on formatting alone, which is the most common shape of the
    naming variation §10 asks vector search to absorb.
    """
    return _SEPARATORS.sub(" ", text.casefold()).strip()


@lru_cache(maxsize=200_000)
def _feature_hash(feature: str) -> int:
    """A stable 64-bit hash for one feature.

    Explicitly not `hash()`. Python randomises string hashing per process, so an
    index built in a worker would be unsearchable from the API process and every
    restart would silently invalidate stored vectors. blake2b is deterministic
    across processes, machines and releases, which is the property a persisted
    embedding needs.
    """
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")


def _features(text: str) -> dict[str, int]:
    """Raw feature counts for one string: word unigrams plus character n-grams."""
    normalised = normalise_text(text)
    if not normalised:
        return {}
    counts: dict[str, int] = {}
    for token in normalised.split():
        counts[f"w:{token}"] = counts.get(f"w:{token}", 0) + 1
    padded = f" {normalised} "
    for width in _NGRAM_WIDTHS:
        for start in range(len(padded) - width + 1):
            gram = f"c{width}:{padded[start:start + width]}"
            counts[gram] = counts.get(gram, 0) + 1
    return counts


@runtime_checkable
class EmbeddingProvider(Protocol):
    """What the index needs from an embedder, hosted or local.

    `name` is recorded on every vector. Two providers produce incomparable
    coordinate spaces, so mixing them inside one index yields distances that
    look like similarities and are not; the index refuses that rather than
    ranking on noise. It is the same reasoning §16 applies to model pinning for
    the compiler: a provider change must never silently alter results that were
    reviewed and approved under the previous one.
    """

    name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """One L2-normalised row per input text, shaped (len(texts), dimensions)."""
        ...


def embed_one(provider: EmbeddingProvider, text: str) -> np.ndarray:
    """One vector for one string, through the batch interface every provider has.

    Hosted providers batch; a single-text convenience method is not something a
    protocol should demand of them. Keeping this here means `EmbeddingProvider`
    stays a one-method contract that a real client satisfies in a few lines.
    """
    matrix = provider.embed([text])
    if matrix.shape != (1, provider.dimensions):
        raise ValueError(
            f"provider {provider.name!r} returned {matrix.shape} for one text; "
            f"expected (1, {provider.dimensions})"
        )
    return matrix[0]


class HashingEmbedder:
    """A deterministic local embedder: signed feature hashing, no network call.

    The signed part matters. Hashing thousands of features into 1024 buckets
    guarantees collisions; adding every colliding feature with the same sign
    makes collisions accumulate into spurious similarity, while a sign drawn
    from the hash itself makes them cancel in expectation. Term frequencies are
    dampened logarithmically so a word repeated twenty times in a long clause
    does not dominate the vector.

    This is a real vectoriser, not a placeholder: it is deterministic across
    processes, it has no external dependency, and it produces the sub-word
    overlap that column-name matching lives on. What it does not have is
    semantic knowledge -- "salary" and "compensation" share no characters and so
    score near zero. That is the ceiling a hosted embedding provider lifts, and
    the reason `EmbeddingProvider` exists.
    """

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS, *, name: str | None = None):
        if dimensions < 16:
            raise ValueError(f"dimensions={dimensions} is too small to hash into meaningfully")
        self.dimensions = int(dimensions)
        self.name = name or f"hashing-char-ngram/1:{self.dimensions}"

    def embed_one(self, text: str) -> np.ndarray:
        counts = _features(text)
        if not counts:
            # A vector of zeros has no direction, so cosine against it is
            # undefined rather than "no match". Callers get told, at the point
            # the empty string was introduced, instead of downstream where the
            # NaN surfaces as a ranking bug.
            raise ValueError("cannot embed text with no alphanumeric content")
        vector = np.zeros(self.dimensions, dtype=np.float64)
        for feature, count in counts.items():
            digest = _feature_hash(feature)
            bucket = digest % self.dimensions
            sign = 1.0 if (digest >> 63) & 1 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            # Possible in principle: every feature cancelled against a collision.
            raise ValueError("embedding collapsed to the zero vector; text carries no usable signal")
        return vector / norm

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float64)
        return np.vstack([self.embed_one(t) for t in texts])


# ---------------------------------------------------------------------- records


@dataclass(frozen=True)
class VectorRecord:
    """One embedded item, inseparable from the tenant that owns it.

    `org_id` is not optional and not defaulted. A record that reaches the store
    without one would be unfilterable, and the only safe thing to do with an
    unfilterable record is refuse to create it.
    """

    record_id: str
    org_id: str
    kind: str
    text: str
    vector: np.ndarray
    provider: str
    doc_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id or not str(self.record_id).strip():
            raise ValueError("vector record needs an id")
        if not self.org_id or not str(self.org_id).strip():
            raise TenantScopeRequired("vector record has no org_id; it could never be filtered")
        if self.kind not in EMBEDDABLE_KINDS:
            raise ValueError(
                f"kind={self.kind!r} is not embeddable. §10 permits "
                f"{sorted(EMBEDDABLE_KINDS)}; values, identifiers and typed "
                "source data are deliberately excluded."
            )
        if not isinstance(self.vector, np.ndarray) or self.vector.ndim != 1:
            raise ValueError("vector must be a one-dimensional numpy array")
        if not self.provider:
            raise ValueError("vector record must record the provider that produced it")


@dataclass(frozen=True)
class VectorMatch:
    record: VectorRecord
    score: float


# ----------------------------------------------------------------------- store


class VectorStore(Protocol):
    """Persistence for vectors, with no way to read across tenants.

    There is deliberately no `all_rows()`. Every read names an organisation, so
    the tenant filter is a property of the interface rather than a discipline
    each caller has to remember -- which is precisely what §16 means by
    "enforced at the query layer rather than by convention inside endpoint
    handlers".

    Two implementations satisfy this: `InMemoryVectorStore` below, for tests and
    previews, and `app.retrieval.store.SqlVectorStore`, which puts the same
    contract on top of PostgreSQL/pgvector so `scoped_rows` becomes a literal
    `WHERE org_id = ...` -- the filter in the query, before similarity, rather
    than a discard step after it.
    """

    def put(self, record: VectorRecord) -> None: ...

    def scoped_rows(self, *, org_id: str, doc_type: str | None = None, kind: str | None = None) -> list[VectorRecord]: ...

    def forget_org(self, org_id: str) -> int: ...

    def size(self) -> int: ...


class InMemoryVectorStore:
    """The reference implementation, and what the tests run against.

    Rows are partitioned by organisation rather than held in one list with an
    `org_id` attribute, so the failure mode of a forgotten filter is an empty
    result rather than the whole corpus. That is the same fail-closed shape §16
    asks PostgreSQL row-level security to provide behind a forgotten WHERE
    clause.
    """

    def __init__(self) -> None:
        self._by_org: dict[str, dict[str, VectorRecord]] = {}

    def put(self, record: VectorRecord) -> None:
        self._by_org.setdefault(record.org_id, {})[record.record_id] = record

    def scoped_rows(self, *, org_id: str, doc_type: str | None = None, kind: str | None = None) -> list[VectorRecord]:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("scoped_rows requires an org_id")
        rows = list(self._by_org.get(org_id, {}).values())
        if doc_type is not None:
            # An unclassified row is not evidence for a typed query. Including it
            # would let a document type filter quietly stop filtering the moment
            # someone forgets to classify an upload.
            rows = [r for r in rows if r.doc_type == doc_type]
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        return rows

    def forget_org(self, org_id: str) -> int:
        """§16 retention: "deletion must cascade to embeddings"."""
        removed = self._by_org.pop(org_id, {})
        return len(removed)

    def size(self) -> int:
        return sum(len(rows) for rows in self._by_org.values())


# ---------------------------------------------------------------------- search


def cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine similarity of one query against a matrix of unit row vectors.

    Stored vectors are normalised on write, so this is a single dot product
    rather than a per-query renormalisation of the corpus. Kept as a module
    function on purpose: it is the one place similarity is computed, which makes
    "what did the search actually score?" an answerable question in a test
    rather than an argument in review.
    """
    if matrix.ndim != 2:
        raise ValueError(f"expected a 2-D matrix, got shape {matrix.shape}")
    if query.shape != (matrix.shape[1],):
        raise ValueError(f"query of shape {query.shape} cannot be scored against {matrix.shape}")
    return np.clip(matrix @ query, -1.0, 1.0)


class VectorIndex:
    """Cosine search with a mandatory, pre-applied tenant filter.

    `search` takes `org_id` as a required keyword argument. It is not defaulted
    and not inferred from the query, because the one call site that forgets it
    is the leak. The candidate rows are fetched from the store by organisation
    and only then assembled into a matrix -- another tenant's vectors are never
    part of the arithmetic, so there is no post-filter step to get wrong.
    """

    def __init__(self, provider: EmbeddingProvider | None = None, store: VectorStore | None = None):
        """The store is the seam. Pass `InMemoryVectorStore` for a test or a
        preview, `app.retrieval.store.SqlVectorStore` for anything whose index
        has to survive a restart. The default stays in-memory so constructing a
        `VectorIndex` never silently opens a database connection -- persistence
        is a decision the caller makes explicitly, with a session it owns."""
        self._provider = provider or HashingEmbedder()
        self._store = store if store is not None else InMemoryVectorStore()

    @property
    def provider(self) -> EmbeddingProvider:
        return self._provider

    def index_text(
        self,
        *,
        record_id: str,
        org_id: str,
        kind: str,
        text: str,
        doc_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> VectorRecord:
        """Embed one item and store it under its tenant."""
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("cannot index a record without an org_id")
        vector = embed_one(self._provider, text)
        record = VectorRecord(
            record_id=record_id,
            org_id=org_id,
            kind=kind,
            text=text,
            vector=vector,
            provider=self._provider.name,
            doc_type=doc_type,
            metadata=dict(metadata or {}),
        )
        self._store.put(record)
        return record

    def index_many(self, items: Sequence[Mapping[str, Any]]) -> list[VectorRecord]:
        return [self.index_text(**item) for item in items]

    def search(
        self,
        query: str,
        *,
        org_id: str,
        doc_type: str | None = None,
        kind: str | None = None,
        k: int = 8,
        min_score: float = NOISE_FLOOR,
    ) -> list[VectorMatch]:
        """The top `k` records of this organisation, ranked by cosine similarity.

        `min_score` defaults to `NOISE_FLOOR` and the comparison is strict, so a
        record that shares nothing with the query is not returned as the best of
        a bad set. Handing back the least-unrelated row would let the compiler cite
        evidence that has no bearing on the field it is mapping -- the same
        reasoning `lexical.retrieve` applies when it declines to pad an empty
        result.
        """
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("search requires an org_id; a global vector search is never correct here")
        if k < 1:
            raise ValueError(f"k={k} would ask for no results at all")

        # `kind` narrows to one class of record -- source columns, template
        # fields -- before anything is scored. It is a filter and not a post-hoc
        # trim because the two kinds compete: an index holding 267 template
        # fields and 203 source columns returns almost nothing but fields for a
        # query drawn from a template, and filtering the ranked output then
        # yields an empty list rather than the columns that were asked for.
        candidates = self._store.scoped_rows(org_id=org_id, doc_type=doc_type, kind=kind)
        if not candidates:
            return []

        foreign_providers = {r.provider for r in candidates} - {self._provider.name}
        if foreign_providers:
            raise ProviderMismatch(
                f"index holds vectors from {sorted(foreign_providers)} but the active provider is "
                f"{self._provider.name!r}; distances between the two are meaningless. Re-embed before searching."
            )

        matrix = np.vstack([r.vector for r in candidates])
        scores = cosine_scores(matrix, embed_one(self._provider, query))
        ranked = sorted(
            zip(candidates, (float(s) for s in scores)),
            # record_id breaks ties deterministically: identical scores must not
            # reorder between runs, or a golden test becomes flaky for no reason.
            key=lambda pair: (-pair[1], pair[0].record_id),
        )
        return [VectorMatch(record=r, score=s) for r, s in ranked[:k] if s > min_score]

    def forget_org(self, org_id: str) -> int:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("forget_org requires an org_id")
        return self._store.forget_org(org_id)

    def size(self) -> int:
        return self._store.size()
