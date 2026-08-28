"""What this organisation has already approved, and why that never leaves it.

A mapping that a reviewer approved once is the strongest evidence the compiler
will ever have for the same field in the next template. §13 lists it that way --
"historical approvals: same mapping approved 42 times", weight 0.60, the largest
single-signal weight in the table -- and §10 asks for the approved mapping to be
persisted "as structured relational/JSONB data, not only as an embedding",
because a vector can rank a candidate but cannot tell you a human accepted it
forty-two times. Nothing here recorded any of that. Every template compiled
against a blank history, so the ninth offer letter from the same customer got
the same guesses as the first, and a correction a reviewer made in March was
gone by April.

This module is that memory, and the whole of it is tenant-scoped. §16:

    "Mapping memory is tenant-scoped by default. Any cross-tenant learning is
    opt-in and limited to anonymised structural patterns -- never values, never
    template text, never field names."

Two mechanisms enforce that rather than describe it. First, the store has no
cross-tenant read that returns a `MappingEntry`: the only method that spans
organisations returns `StructuralKey`, a three-field record whose every value is
validated against a closed vocabulary, so a column name or a sentence of
template text cannot be carried out through it even by accident. Second, sharing
is off unless both the contributing organisation and the reading one have opted
in, and a pattern is only released once at least `MIN_CONTRIBUTING_ORGS`
distinct organisations have arrived at it independently -- a structure reported
from a single customer is that customer's structure, and anonymisation that
still identifies its source is not anonymisation.

Where the entries live is behind `MappingMemoryStore`, for the same reason:
`InMemoryMappingMemoryStore` for tests and previews,
`app.retrieval.store.SqlMappingMemoryStore` for a memory that is still there
after the next deploy. The anonymity guarantee holds either way -- the SQL
implementation does its projection in the query, so another tenant's entry is
never constructed in the process at all.

What comes back from `lookup` is ranked prior mappings carrying their approval
count. The count is deliberately raw rather than pre-mixed into a score: §13
combines signals itself, and `historical_approvals_signal` here is exactly the
saturating curve that table specifies, so the compiler feeds one number in
rather than reimplementing the arithmetic.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from app.generation.missing_policy import BLANK, ON_MISSING_VALUES
from app.retrieval.vector import (
    EmbeddingProvider,
    HashingEmbedder,
    TenantScopeRequired,
    embed_one,
    normalise_text,
)

# ------------------------------------------------------------------ vocabularies

#: Declared field types. §13 treats money, date and identifier fields specially
#: -- they carry a veto and a two-independent-signals rule -- so the type is a
#: closed set the confidence function can branch on, not free text.
FIELD_TYPES = frozenset({
    "string", "text", "date", "money", "number", "boolean", "enum", "identifier",
})

#: The shapes a mapping's transform can take. Closed for the same reason
#: `FIELD_TYPES` is, and for one more: this is one of the three values that
#: crosses a tenant boundary in an anonymised pattern, so the set of things it
#: can ever say has to be enumerable and inspectable on one screen.
TRANSFORMS = frozenset({
    "direct", "format_date", "format_money", "format_number",
    "uppercase", "titlecase", "concat", "lookup", "expression",
})

#: Below this, a stored mapping is not evidence about the field being asked
#: about -- it is a different field that happens to share a few characters.
#: Returning it would put a plausible-looking wrong column in front of a
#: reviewer, which is worse than returning nothing at all.
#: How far a single reviewer rejection pushes a candidate down the ranking.
#:
#: Applied to the score, not to whether the candidate appears. One correction
#: against two approvals is doubt, not deletion: the column stays available as
#: evidence, carrying its own rejection count, and a reviewer can still pick it.
#: `net_approvals` reaching zero is what removes a candidate, and that takes as
#: many rejections as it had approvals.
#:
#: It is deliberately larger than one approval is worth, because the two acts
#: are not equally considered. An approval is often a confirmation of something
#: the system had already pre-selected -- §13 puts the whole 0.80-0.97 band in
#: front of a reviewer that way, so an accept there is a click. A rejection is a
#: reviewer stopping, deciding the proposal is wrong, and naming a different
#: column instead.
#:
#: The failure this prevents is the one §14 keeps `reviewer_corrections` for: a
#: memory that weighs both the same re-proposes the column a reviewer declined
#: last month, and a reviewer who declines the same wrong mapping four times
#: stops reading the suggestions.
REJECTION_PENALTY = 0.25

MIN_CONTEXT_SIMILARITY = 0.25

#: How many distinct organisations must independently produce a structural
#: pattern before it may be shared. A pattern seen in one place describes that
#: place. There is no calibration behind the exact number yet; it is the
#: conservative floor, and it moves up rather than down.
MIN_CONTRIBUTING_ORGS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def historical_approvals_signal(approvals: int) -> float:
    """§13's historical-approvals signal: s = 1 - e^(-n/8).

    Saturating on purpose. The difference between zero approvals and three is
    most of what the signal knows; the difference between forty and fifty is
    noise, and a linear count would let one heavily-used field out-vote every
    other signal in the combination function.
    """
    if approvals < 0:
        raise ValueError(f"approvals={approvals} is not a count")
    return 1.0 - math.exp(-approvals / 8.0)


# ---------------------------------------------------------------- field context


@dataclass(frozen=True)
class FieldContext:
    """The thing being mapped, as much of it as the compiler knows.

    `sentence_context` is §13's "local sentence context" signal -- the sentence
    the placeholder sits in ("You will report to ..."), which is often the only
    thing that distinguishes two identically-named fields.
    """

    field_id: str
    label: str
    field_type: str = "string"
    doc_type: str | None = None
    sentence_context: str = ""
    template_family_id: str | None = None

    def __post_init__(self) -> None:
        if not self.field_id or not str(self.field_id).strip():
            raise ValueError("field context needs a field_id")
        if self.field_type not in FIELD_TYPES:
            raise ValueError(f"field_type={self.field_type!r} is not one of {sorted(FIELD_TYPES)}")

    @property
    def memory_key(self) -> str:
        """The normalised identity a prior mapping is filed under.

        Built from the label rather than the id: template compilers generate
        field ids, and the same field in next year's revision of the same letter
        routinely gets a different one. The human-readable label is what stays
        stable across revisions, which is exactly the case this memory exists to
        serve.
        """
        basis = normalise_text(self.label or self.field_id)
        if not basis:
            raise ValueError(f"field {self.field_id!r} has no label to key memory on")
        return basis.replace(" ", "_")

    @property
    def context_text(self) -> str:
        """What gets embedded for similarity: meaning, never values."""
        return " ".join(part for part in (self.label, self.field_type, self.sentence_context) if part)


# ---------------------------------------------------------------- stored shapes


@dataclass
class MappingEntry:
    """One (field, column, transform) triple this organisation has approved.

    Counts rather than a boolean: §13's signal is "approved 42 times", and a
    mapping a reviewer has accepted repeatedly is materially different evidence
    from one accepted once. `rejection_count` is the other half -- a candidate a
    reviewer replaced is negative evidence, and a memory that only remembers
    acceptances will keep proposing the thing that was rejected last month.
    """

    entry_id: str
    org_id: str
    field_key: str
    field_type: str
    source_column: str
    transform: str
    on_missing: str
    context_text: str
    doc_type: str | None = None
    template_family_id: str | None = None
    approval_count: int = 0
    rejection_count: int = 0
    first_seen_at: datetime = field(default_factory=_now)
    last_approved_at: datetime | None = None
    last_approved_by: str | None = None

    def __post_init__(self) -> None:
        if not self.org_id or not str(self.org_id).strip():
            raise TenantScopeRequired("a mapping memory entry without an org_id could never be filtered")
        if self.field_type not in FIELD_TYPES:
            raise ValueError(f"field_type={self.field_type!r} is not one of {sorted(FIELD_TYPES)}")
        if self.transform not in TRANSFORMS:
            raise ValueError(f"transform={self.transform!r} is not one of {sorted(TRANSFORMS)}")
        if self.on_missing not in ON_MISSING_VALUES:
            raise ValueError(f"on_missing={self.on_missing!r} is not one of {sorted(ON_MISSING_VALUES)}")

    @property
    def net_approvals(self) -> int:
        """Approvals a reviewer has not since taken back. Never negative."""
        return max(0, self.approval_count - self.rejection_count)

    def structural_key(self) -> "StructuralKey":
        """The only part of this entry that may ever cross a tenant boundary.

        Written as one method, reading three closed-vocabulary attributes, so
        the anonymisation is a single auditable place rather than a rule each
        caller applies. `source_column`, `context_text`, `field_key`,
        `template_family_id` and every timestamp are structurally unable to
        reach a `StructuralKey`, because it has nowhere to put them.
        """
        return StructuralKey(
            field_type=self.field_type,
            transform=self.transform,
            on_missing=self.on_missing,
        )


@dataclass(frozen=True)
class StructuralKey:
    """A mapping's shape with its content removed.

    "A date field, formatted, blocking when absent" is a structural pattern.
    "`start_date` maps to column `Joining Dt`" is a customer's template. Only
    the first of those is expressible here, and the validation below is what
    makes that a property of the type rather than an assurance about the caller.
    """

    field_type: str
    transform: str
    on_missing: str

    def __post_init__(self) -> None:
        if self.field_type not in FIELD_TYPES:
            raise ValueError(f"field_type={self.field_type!r} is not shareable; it must be one of {sorted(FIELD_TYPES)}")
        if self.transform not in TRANSFORMS:
            raise ValueError(f"transform={self.transform!r} is not shareable; it must be one of {sorted(TRANSFORMS)}")
        if self.on_missing not in ON_MISSING_VALUES:
            raise ValueError(f"on_missing={self.on_missing!r} is not shareable; it must be one of {sorted(ON_MISSING_VALUES)}")

    @property
    def fingerprint(self) -> str:
        raw = f"{self.field_type}|{self.transform}|{self.on_missing}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class StructuralPattern:
    """An aggregated `StructuralKey`, released only above the anonymity floor."""

    pattern_id: str
    field_type: str
    transform: str
    on_missing: str
    contributing_org_count: int
    approval_count: int

    def __post_init__(self) -> None:
        # Re-validated rather than trusted from the aggregation step: this is
        # the object that leaves the tenant boundary, so it checks itself.
        StructuralKey(self.field_type, self.transform, self.on_missing)
        if self.contributing_org_count < MIN_CONTRIBUTING_ORGS:
            raise ValueError(
                f"pattern seen in {self.contributing_org_count} organisation(s); "
                f"{MIN_CONTRIBUTING_ORGS} are required before it is anonymous"
            )


@dataclass(frozen=True)
class PriorMapping:
    """A ranked prior mapping, with the raw counts §13 needs to score it."""

    source_column: str
    transform: str
    on_missing: str
    field_type: str
    approval_count: int
    rejection_count: int
    similarity: float
    score: float
    doc_type: str | None
    template_family_id: str | None
    last_approved_at: datetime | None
    last_approved_by: str | None

    @property
    def net_approvals(self) -> int:
        return max(0, self.approval_count - self.rejection_count)

    @property
    def historical_approvals_signal(self) -> float:
        """Feed this straight into §13's combination function."""
        return historical_approvals_signal(self.net_approvals)


@dataclass(frozen=True)
class ReviewerCorrection:
    """What a correction changed, as two entries rather than a log line."""

    accepted: MappingEntry
    rejected: MappingEntry | None


@dataclass(frozen=True)
class CorrectionEvent:
    """One reviewer decision, kept as an event rather than two moved counters.

    §14 lists `reviewer_corrections` as a store beside `mapping_memory`, and the
    reason is that the counters lose the pairing. They record that one column
    gained an approval and another gained a rejection; they do not record that
    those were the same decision, and that cannot be reconstructed afterwards
    from two counters that moved on the same day. Calibrating §13's weights
    against real reviewer behaviour needs the pairing.
    """

    org_id: str
    field_key: str
    accepted_column: str
    rejected_column: str | None
    corrected_by: str
    at: datetime

    def __post_init__(self) -> None:
        if not self.org_id or not str(self.org_id).strip():
            raise TenantScopeRequired("a correction without an org_id could never be filtered")
        if not self.accepted_column or not str(self.accepted_column).strip():
            raise ValueError("a correction must name the column the reviewer accepted")
        if not self.corrected_by:
            raise ValueError("a correction must record who made it")


@dataclass(frozen=True)
class MemoryLookup:
    """Everything memory can say about one field, split by trust boundary."""

    field_key: str
    org_id: str
    prior_mappings: list
    structural_patterns: list = field(default_factory=list)

    @property
    def best(self) -> PriorMapping | None:
        return self.prior_mappings[0] if self.prior_mappings else None


# ----------------------------------------------------------------------- store


class MappingMemoryStore(Protocol):
    """Persistence for mapping memory, with one deliberately narrow cross-tenant read.

    Every method that returns a `MappingEntry` names exactly one organisation.
    The single method that spans organisations, `shared_structural_keys`,
    returns `StructuralKey` -- so the anonymisation happens inside the store,
    and no caller is ever holding another tenant's entry to be careful with.

    Two implementations satisfy this: `InMemoryMappingMemoryStore` below, for
    tests and previews, and `app.retrieval.store.SqlMappingMemoryStore`, which
    is the one production uses and the reason an approval a reviewer gave in
    March is still there in April.
    """

    def upsert(self, entry: MappingEntry) -> MappingEntry: ...

    def find(self, *, org_id: str, field_key: str, source_column: str, transform: str) -> MappingEntry | None: ...

    def entries_for_org(self, org_id: str) -> list: ...

    def record_correction(self, event: CorrectionEvent) -> None: ...

    def corrections_for_org(self, org_id: str) -> list: ...

    def set_sharing_opt_in(self, org_id: str, *, opted_in: bool, actor: str) -> None: ...

    def is_opted_in(self, org_id: str) -> bool: ...

    def shared_structural_keys(self, *, exclude_org_id: str) -> list: ...

    def forget_org(self, org_id: str) -> int: ...


class InMemoryMappingMemoryStore:
    """The reference implementation, partitioned by organisation.

    Same shape as `InMemoryVectorStore`: entries live under their org rather
    than in one table filtered on read, so a forgotten scope yields nothing
    instead of everything.
    """

    def __init__(self) -> None:
        self._by_org: dict[str, dict[tuple, MappingEntry]] = {}
        self._sharing: dict[str, bool] = {}
        self._corrections: dict[str, list[CorrectionEvent]] = {}

    @staticmethod
    def _key(field_key: str, source_column: str, transform: str) -> tuple:
        return (field_key, normalise_text(source_column), transform)

    def upsert(self, entry: MappingEntry) -> MappingEntry:
        bucket = self._by_org.setdefault(entry.org_id, {})
        bucket[self._key(entry.field_key, entry.source_column, entry.transform)] = entry
        return entry

    def find(self, *, org_id: str, field_key: str, source_column: str, transform: str) -> MappingEntry | None:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("find requires an org_id")
        return self._by_org.get(org_id, {}).get(self._key(field_key, source_column, transform))

    def entries_for_org(self, org_id: str) -> list:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("entries_for_org requires an org_id")
        return list(self._by_org.get(org_id, {}).values())

    def record_correction(self, event: CorrectionEvent) -> None:
        """Append one reviewer decision. Never updated, never deduplicated: the
        same reviewer making the same call twice is two data points about how
        the suggestions are behaving, not one."""
        self._corrections.setdefault(event.org_id, []).append(event)

    def corrections_for_org(self, org_id: str) -> list:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("corrections_for_org requires an org_id")
        return list(self._corrections.get(org_id, []))

    def set_sharing_opt_in(self, org_id: str, *, opted_in: bool, actor: str) -> None:
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("sharing policy needs an org_id")
        if not actor:
            # Opting a customer's data into a shared pool is a consent decision.
            # An unattributed one cannot be evidenced later, which is the whole
            # point of recording it.
            raise ValueError("cross-tenant sharing must record who opted in")
        self._sharing[org_id] = bool(opted_in)

    def is_opted_in(self, org_id: str) -> bool:
        return bool(self._sharing.get(org_id, False))

    def shared_structural_keys(self, *, exclude_org_id: str) -> list:
        """`(org_id, StructuralKey, approvals)` from opted-in organisations only.

        The org ids are here so the caller can count distinct contributors for
        the anonymity floor; they are consumed by that count and never returned
        further. Nothing else about the entry survives the trip.
        """
        rows = []
        for org_id, bucket in self._by_org.items():
            if org_id == exclude_org_id or not self.is_opted_in(org_id):
                continue
            for entry in bucket.values():
                if entry.net_approvals <= 0:
                    continue
                rows.append((org_id, entry.structural_key(), entry.net_approvals))
        return rows

    def forget_org(self, org_id: str) -> int:
        """§16 retention: offboarding deletes the memory too, not just the rows."""
        removed = self._by_org.pop(org_id, {})
        self._sharing.pop(org_id, None)
        self._corrections.pop(org_id, None)
        return len(removed)


# ---------------------------------------------------------------------- memory


class MappingMemory:
    """Record approvals and corrections; hand back ranked prior mappings.

    The ranking mixes two things and nothing else: how close the stored field
    context is to the one being asked about, and how much human agreement the
    mapping has behind it. It is not §13's confidence function and does not try
    to be -- confidence combines seven independent signals, and this is the
    supplier of one and a half of them. Keeping them separate is what stops the
    same evidence being counted twice.
    """

    #: Starting weights, in §13's sense of the word: values to calibrate against
    #: real reviewer decisions, not measured constants. Even weighting says a
    #: heavily-approved mapping for a loosely-related field and a perfect context
    #: match with one approval are worth about the same amount of a reviewer's
    #: attention, which is the honest position before there is data.
    SIMILARITY_WEIGHT = 0.5
    APPROVALS_WEIGHT = 0.5

    def __init__(
        self,
        store: MappingMemoryStore | None = None,
        provider: EmbeddingProvider | None = None,
    ):
        """The store is the seam. Pass `InMemoryMappingMemoryStore` for a test or
        a preview, `app.retrieval.store.SqlMappingMemoryStore` for anything whose
        answers have to still be there after a restart. The default stays
        in-memory so constructing a `MappingMemory` never silently opens a
        database connection -- a persistent memory is a decision the caller makes
        explicitly, with a session it owns."""
        self._store = store if store is not None else InMemoryMappingMemoryStore()
        self._provider = provider or HashingEmbedder()

    # ------------------------------------------------------------- recording

    def record_approved_mapping(
        self,
        *,
        org_id: str,
        field_context: FieldContext,
        source_column: str,
        approved_by: str,
        transform: str = "direct",
        on_missing: str = BLANK,
        at: datetime | None = None,
    ) -> MappingEntry:
        """Remember that a human approved this mapping. Idempotent per triple.

        Approving the same mapping again increments the count rather than
        creating a second row: "approved 42 times" is one entry with a counter,
        and forty-two rows would make the §13 signal a function of how the
        reviewer happened to click.
        """
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("an approved mapping must be recorded against an organisation")
        if not source_column or not str(source_column).strip():
            raise ValueError("an approved mapping needs the source column it approved")
        if not approved_by:
            raise ValueError("an approved mapping must record who approved it")

        field_key = field_context.memory_key
        existing = self._store.find(
            org_id=org_id, field_key=field_key, source_column=source_column, transform=transform,
        )
        moment = at or _now()
        if existing is None:
            entry = MappingEntry(
                entry_id=str(uuid.uuid4()),
                org_id=org_id,
                field_key=field_key,
                field_type=field_context.field_type,
                source_column=source_column,
                transform=transform,
                on_missing=on_missing,
                context_text=field_context.context_text,
                doc_type=field_context.doc_type,
                template_family_id=field_context.template_family_id,
                approval_count=1,
                first_seen_at=moment,
                last_approved_at=moment,
                last_approved_by=approved_by,
            )
        else:
            entry = existing
            entry.approval_count += 1
            entry.last_approved_at = moment
            entry.last_approved_by = approved_by
            # The most recent approval describes the field best: a template
            # revision that reworded the sentence around a placeholder should
            # move the stored context with it.
            entry.context_text = field_context.context_text
            if field_context.doc_type:
                entry.doc_type = field_context.doc_type
            if field_context.template_family_id:
                entry.template_family_id = field_context.template_family_id
        return self._store.upsert(entry)

    def record_reviewer_correction(
        self,
        *,
        org_id: str,
        field_context: FieldContext,
        accepted_column: str,
        corrected_by: str,
        rejected_column: str | None = None,
        transform: str = "direct",
        on_missing: str = BLANK,
        at: datetime | None = None,
    ) -> ReviewerCorrection:
        """A reviewer replaced a suggestion. Record both halves of that.

        §14 names `reviewer_corrections` as a first-class store beside mapping
        memory, and the reason is asymmetry: remembering only what was accepted
        leaves the system free to re-suggest the rejected column on the next
        template, and a reviewer who has to decline the same wrong mapping four
        times stops reading the suggestions.
        """
        accepted = self.record_approved_mapping(
            org_id=org_id,
            field_context=field_context,
            source_column=accepted_column,
            approved_by=corrected_by,
            transform=transform,
            on_missing=on_missing,
            at=at,
        )
        rejected_entry = None
        if rejected_column and normalise_text(rejected_column) != normalise_text(accepted_column):
            field_key = field_context.memory_key
            rejected_entry = self._store.find(
                org_id=org_id, field_key=field_key, source_column=rejected_column, transform=transform,
            )
            if rejected_entry is None:
                rejected_entry = MappingEntry(
                    entry_id=str(uuid.uuid4()),
                    org_id=org_id,
                    field_key=field_key,
                    field_type=field_context.field_type,
                    source_column=rejected_column,
                    transform=transform,
                    on_missing=on_missing,
                    context_text=field_context.context_text,
                    doc_type=field_context.doc_type,
                    template_family_id=field_context.template_family_id,
                    first_seen_at=at or _now(),
                )
            rejected_entry.rejection_count += 1
            self._store.upsert(rejected_entry)
        # Logged whether or not a column was displaced. A reviewer confirming a
        # suggestion outright is the control group for "was the proposal any
        # good?", and dropping it would leave the log holding only the failures.
        self._store.record_correction(CorrectionEvent(
            org_id=org_id,
            field_key=field_context.memory_key,
            accepted_column=accepted_column,
            rejected_column=rejected_entry.source_column if rejected_entry is not None else None,
            corrected_by=corrected_by,
            at=at or _now(),
        ))
        return ReviewerCorrection(accepted=accepted, rejected=rejected_entry)

    # --------------------------------------------------------------- lookup

    def lookup(
        self,
        field_context: FieldContext,
        org_id: str,
        *,
        k: int = 5,
        include_structural_patterns: bool = False,
    ) -> MemoryLookup:
        """Ranked prior mappings for one field, from this organisation only.

        `org_id` is required and positional: there is no "search everything"
        mode to reach by leaving an argument off. Cross-tenant structural
        patterns are off unless asked for, and asking is not enough on its own
        -- see `structural_patterns`.
        """
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("mapping memory lookup requires an org_id")
        if k < 1:
            raise ValueError(f"k={k} would ask for no prior mappings at all")

        field_key = field_context.memory_key
        query_vector = embed_one(self._provider, field_context.context_text) \
            if normalise_text(field_context.context_text) else None

        ranked: list[tuple[tuple, PriorMapping]] = []
        for entry in self._store.entries_for_org(org_id):
            if entry.net_approvals <= 0:
                # Rejected more often than approved. Negative evidence is worth
                # storing and worth not suggesting.
                continue
            similarity = self._similarity(field_key, query_vector, entry)
            if similarity < MIN_CONTEXT_SIMILARITY:
                continue
            score = (
                self.SIMILARITY_WEIGHT * similarity
                + self.APPROVALS_WEIGHT * historical_approvals_signal(entry.net_approvals)
                - REJECTION_PENALTY * entry.rejection_count
            )
            prior = PriorMapping(
                source_column=entry.source_column,
                transform=entry.transform,
                on_missing=entry.on_missing,
                field_type=entry.field_type,
                approval_count=entry.approval_count,
                rejection_count=entry.rejection_count,
                similarity=round(similarity, 4),
                score=round(score, 4),
                doc_type=entry.doc_type,
                template_family_id=entry.template_family_id,
                last_approved_at=entry.last_approved_at,
                last_approved_by=entry.last_approved_by,
            )
            # Same family and same document type are tiebreaks rather than score
            # terms. §13 scores family inheritance as its own signal, and adding
            # it here too would let one piece of evidence be counted twice; but
            # between two otherwise equal candidates the one from this family is
            # the better suggestion.
            same_family = bool(
                field_context.template_family_id
                and entry.template_family_id == field_context.template_family_id
            )
            same_doc_type = bool(field_context.doc_type and entry.doc_type == field_context.doc_type)
            ranked.append((
                (-score, not same_family, not same_doc_type, -entry.net_approvals, entry.source_column),
                prior,
            ))

        ranked.sort(key=lambda pair: pair[0])
        priors = [prior for _, prior in ranked[:k]]

        patterns = self.structural_patterns(org_id) if include_structural_patterns else []
        return MemoryLookup(
            field_key=field_key, org_id=org_id, prior_mappings=priors, structural_patterns=patterns,
        )

    def _similarity(self, field_key: str, query_vector, entry: MappingEntry) -> float:
        """1.0 for the same normalised field name, cosine of context otherwise."""
        if entry.field_key == field_key:
            return 1.0
        if query_vector is None or not normalise_text(entry.context_text):
            return 0.0
        entry_vector = embed_one(self._provider, entry.context_text)
        return float(max(0.0, min(1.0, float(query_vector @ entry_vector))))

    # ---------------------------------------------------- cross-tenant patterns

    def set_sharing_opt_in(self, org_id: str, *, opted_in: bool, actor: str) -> None:
        """Opt one organisation into the anonymised structural pool, or out again."""
        self._store.set_sharing_opt_in(org_id, opted_in=opted_in, actor=actor)

    def structural_patterns(self, org_id: str) -> list:
        """Anonymised structural patterns visible to this organisation.

        Three gates, all of which must pass:

          * the reading organisation has opted in -- a pool that can be read
            without contributing is a one-way export of other customers' shape;
          * the contributing organisation has opted in, checked inside the store
            so an un-opted tenant's rows are never even loaded;
          * at least `MIN_CONTRIBUTING_ORGS` distinct organisations produced the
            pattern, so no returned row describes a single identifiable customer.

        An organisation that has not opted in gets an empty list rather than an
        error: not sharing is a normal state, not a failure.
        """
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("structural patterns require the reading org_id")
        if not self._store.is_opted_in(org_id):
            return []

        aggregate: dict[str, dict] = {}
        for contributor_org_id, key, approvals in self._store.shared_structural_keys(exclude_org_id=org_id):
            bucket = aggregate.setdefault(key.fingerprint, {"key": key, "orgs": set(), "approvals": 0})
            bucket["orgs"].add(contributor_org_id)
            bucket["approvals"] += approvals

        patterns = [
            StructuralPattern(
                pattern_id=fingerprint,
                field_type=bucket["key"].field_type,
                transform=bucket["key"].transform,
                on_missing=bucket["key"].on_missing,
                contributing_org_count=len(bucket["orgs"]),
                approval_count=bucket["approvals"],
            )
            for fingerprint, bucket in aggregate.items()
            if len(bucket["orgs"]) >= MIN_CONTRIBUTING_ORGS
        ]
        patterns.sort(key=lambda p: (-p.approval_count, p.pattern_id))
        return patterns

    # ------------------------------------------------------------- retention

    def forget_org(self, org_id: str) -> int:
        """Drop everything this organisation taught the system."""
        if not org_id or not str(org_id).strip():
            raise TenantScopeRequired("forget_org requires an org_id")
        return self._store.forget_org(org_id)
