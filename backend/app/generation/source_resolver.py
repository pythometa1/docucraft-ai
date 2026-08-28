"""Mapping a source file's columns onto a manifest's fields -- and saying why.

Several tiers propose a column for each bindable target, because no single
strategy covers a real spreadsheet: a MERGEFIELD code, an alias a reviewer
confirmed elsewhere in the estate, an exact slug, an embedding above §13's
similarity floor, a column this organisation has approved for the field before,
a near match for the truncated headers real exports are full of ("GBS Dalian
Recruiting Delive" for `gbs_dalian_recruiting_delivery_apac_services`), and --
for whatever is still unmatched -- the model.

A proposal is not a confidence, and until this module was wired to §13's
scoring function it graded its own proposals with a label for the tier that
produced them: 0.95 for every exact slug match, 0.97 for every alias, 0.80 for
anything a model suggested. The same 0.95 covered a mapping this organisation
has approved forty-two times and one that matched a column name nobody has ever
confirmed, so an approval queue built on it could not be sorted by risk. A
reviewer facing two hundred identical 0.95s either accepts the lot or re-reads
the lot, and both of those end with a salary in the wrong paragraph.

The tiers therefore now do one job -- propose candidates -- and
`app.compiler.confidence` does the other: combine the *independent* signals
each candidate carries, apply §13's vetoes, and place the result in an approval
band. Every target gets a ranked list rather than a winner, because §13's
REVIEW band is defined as "presented with ranked alternatives and the reasoning
behind each", and its fourth veto -- two candidates within 0.05 of each other --
is a property of the list that no single winner can express.

Which of §13's seven signals this path can compute honestly is bounded by what
the estate actually has today. The three it cannot are named in
`SIGNALS_NOT_COMPUTED` with the reason, rather than filled in with a plausible
number: a fabricated signal moves the score, is indistinguishable from a real
one afterwards, and the whole point of the combination formula is that it
combines evidence that exists.

One consequence is worth stating out loud rather than discovering in
production: with four of seven signals live the score cannot exceed
`max_attainable_score()` (~0.92), which is below §13's 0.97 auto-accept floor.
Nothing auto-accepts today. That is the record's stated day-one posture -- "the
goal on day one is a near-zero escaped-error rate, not a high automation
percentage" -- and it lifts when the missing signals arrive, not when a
threshold is lowered.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.compiler import confidence as cf
from app.compiler.rule_compiler import _slug
from app.expressions.token_parser import _condition_identifiers
from app.generation.value_format import parse_date
from app.retrieval.vector import HashingEmbedder, embed_one, normalise_text

# Below this, a name similarity is noise rather than a hint. Tuned so a header
# an export truncated still finds its field while unrelated columns do not. It
# is a *proposal* threshold and nothing else: what a near match is worth is
# decided by the signals the candidate carries, not by this number.
FUZZY_THRESHOLD = 0.62

#: How many of a column's values are read to decide what type it holds. A type
#: is a property of the column, and two hundred rows settle it; reading a
#: hundred thousand to reach the same answer would make the binding screen slow
#: for no gain.
TYPE_SAMPLE_ROWS = 200

#: The §13 signals this path can compute today, and what each one is read from.
#: Surfaced on the API response so a reviewer knows the score is a combination
#: of four signals rather than seven.
SIGNALS_COMPUTED: dict[str, str] = {
    "exact_name_match": (
        "The column's normalised slug equals the field id, matches its MERGEFIELD code, "
        "or reaches it through an alias a reviewer confirmed in this organisation's "
        "field dictionary."
    ),
    "semantic_similarity": (
        "Cosine between the field's label and the column name, from the embedding "
        "provider in app.retrieval.vector. §13 floors it at 0.75."
    ),
    "historical_approvals": (
        "How many times this organisation has already approved this field against this "
        "column, from app.retrieval.mapping_memory. Omitted entirely when the caller "
        "consulted no memory."
    ),
    "type_compatibility": (
        "The field's declared type against the type measured from the column's actual "
        "values -- never the file's storage type, which is text for every CSV column."
    ),
}

#: The three §13 signals this path does not compute, and why. An entry here is
#: a gap with a reason, which is a different thing from a signal that was
#: computed and found nothing -- and both are different from a number invented
#: to fill the hole.
SIGNALS_NOT_COMPUTED: dict[str, str] = {
    "sentence_context_match": (
        "§13 scores this as local-window entailment against the sentence the placeholder "
        "sits in. The binding path holds the manifest, not the template document, so the "
        "sentence is not in reach here -- and no entailment model is wired up to judge it "
        "if it were."
    ),
    "family_inheritance": (
        "The only family evidence available is template clustering over this organisation's "
        "own saved bindings -- the same manifest_bindings rows that produce "
        "historical_approvals. Scoring both would count one reviewer's action twice, and the "
        "combination formula is only sound over independent signals."
    ),
    "business_rule_consistency": (
        "Nothing measures whether a proposed source object coheres with the other objects in "
        "the manifest yet; §7's expression layer knows the references, not their semantics."
    ),
}


def max_attainable_score() -> float:
    """The highest score reachable while three of the seven signals are missing.

    Reported rather than assumed: a band whose floor sits above this number is
    unreachable, and a reviewer told "nothing auto-accepts" deserves to see the
    arithmetic behind it.
    """
    residual = 1.0
    for name in SIGNALS_COMPUTED:
        residual *= 1.0 - cf.INITIAL_WEIGHTS[name]
    return 1.0 - residual


@dataclass
class BindableTarget:
    """Something that needs a value before generation can run."""

    field_id: str
    type: str = "string"
    origin: str = "field"  # field | condition | formula
    required: bool = False
    source_hint: str | None = None
    mergefield_code: str | None = None
    # The type the manifest actually *declares*, as opposed to `type`, which
    # falls back to "string" so the binding UI always has something to show. A
    # condition variable has no declared type at all, and telling the type gate
    # "string" -- which accepts every value on earth -- would be inventing a
    # declaration nobody made.
    declared_type: str | None = None
    # The manifest field object itself, for the classifiers in §13 that read
    # more than the id (money / identifier / date fields carry extra rules).
    spec: dict = field(default_factory=dict)
    # The human label the placeholder carries in the document ("<Colleague
    # First Name>"). Field ids are slugs; labels are what a column name
    # actually resembles, so this is what gets embedded.
    label: str | None = None


@dataclass
class Suggestion:
    """One proposed binding, with the §13 verdict on it.

    `confidence` is the score from `app.compiler.confidence`, not a per-tier
    constant. `method` survives from the tier that proposed the column, because
    provenance is still worth showing -- but it no longer decides the number.
    """

    field_id: str
    column: str | None
    confidence: float
    method: str  # dictionary | exact_slug | mergefield | fuzzy | memory | llm | unmatched
    rationale: str = ""
    band: str = cf.Band.BLOCK.value
    vetoes: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    #: Every other candidate for this target, ranked, each with its own score,
    #: band, vetoes and evidence. §13's REVIEW band is defined in terms of this
    #: list existing.
    alternatives: list = field(default_factory=list)
    declared_type: str | None = None
    observed_type: str | None = None

    @property
    def auto_applicable(self) -> bool:
        """Whether §13 permits applying this without a human looking at it."""
        return self.band == cf.Band.AUTO_ACCEPT.value


@dataclass
class UnmatchedConditionValue:
    """A source value that selects none of the branches the template offers.

    Binding a column is only half of matching. If the sheet says "Casual" and the
    template branches on Full time / Part time / Fixed Term, that row produces a
    letter with a section silently missing -- the fill engine's branch gate
    catches it, but only after generation. Reported here it is a one-line
    `value_map` entry a reviewer confirms once for the whole family.

    Deliberately keyed on the value, not the literal: a batch of one Full Time
    row does not mean the Part time branch is broken, and reporting it that way
    would bury the real case in noise nobody reads.
    """

    field_id: str
    column: str | None
    observed_value: str
    offered: list = field(default_factory=list)


@dataclass
class ColumnProfile:
    """A source column as measured, not as declared.

    Every cell in a CSV is text on disk, and every cell out of openpyxl arrives
    here as a string too. Reporting that as the column's type would veto every
    mapping in the estate on a type mismatch, so the type is read from the
    values themselves -- and is None, meaning UNKNOWN, when the column is empty.
    A guess there would either hand out the type weight or fire the veto on
    evidence nobody has.
    """

    name: str
    slug: str
    observed_type: str | None = None
    non_empty: int = 0
    sample_value: str | None = None


@dataclass
class BindingPlan:
    suggestions: list = field(default_factory=list)
    unmatched_fields: list = field(default_factory=list)
    unused_columns: list = field(default_factory=list)
    unmatched_condition_values: list = field(default_factory=list)
    #: field_id -> `confidence.Decision`, the ranked verdict for that target.
    decisions: dict = field(default_factory=dict)
    #: The columns as measured, for the reviewer and for the type gate.
    column_profiles: list = field(default_factory=list)

    def as_field_bindings(self) -> dict:
        """The proposed field -> column map, whatever band each proposal is in.

        Deliberately not filtered by band. This is what a preview or a test fill
        runs on, and a preview exists precisely so a human can look at a mapping
        the gate has not cleared. What the bands govern is *approval* -- see
        `auto_applicable_bindings`, and the band on every suggestion.
        """
        return {s.field_id: s.column for s in self.suggestions if s.column}

    def auto_applicable_bindings(self) -> dict:
        """Only the mappings §13 allows to be applied without a human.

        Empty until the missing signals arrive: `max_attainable_score()` is
        below the auto-accept floor, and money and identifier fields are
        excluded from the band whatever they score.
        """
        return {s.field_id: s.column for s in self.suggestions if s.column and s.auto_applicable}

    def band_counts(self) -> dict:
        """How the approval summary breaks down, for the reviewer's overview."""
        counts = {band.value: 0 for band in cf.Band}
        for suggestion in self.suggestions:
            if suggestion.column:
                counts[suggestion.band] = counts.get(suggestion.band, 0) + 1
        return counts


def bindable_targets(manifest: dict) -> list[BindableTarget]:
    """Every name that must resolve before a document can be produced.

    Critically this is *not* just `manifest["fields"]`. Condition expressions
    reference variables that are never placeholders anywhere in the document --
    the Hospira template's three conditions all hinge on `colleague_type`,
    which has no slot and therefore no field entry. A binding UI built only
    from the field list leaves it permanently unbound, every condition silently
    evaluates false, and every conditional block is deleted from the output.
    """
    targets: dict[str, BindableTarget] = {}

    for f in manifest.get("fields", []):
        slots = f.get("slots", []) or []
        mergefield_code = next(
            (slot.get("code") for slot in slots if slot.get("kind") == "mergefield"),
            None,
        )
        targets[f["id"]] = BindableTarget(
            field_id=f["id"],
            type=f.get("type", "string"),
            origin="formula" if f.get("kind") == "computed" else "field",
            required=bool(f.get("required")),
            source_hint=f.get("source_hint"),
            mergefield_code=mergefield_code,
            declared_type=f.get("type"),
            spec=f,
            label=next((slot.get("text") for slot in slots if slot.get("text")), None),
        )

    for cond in manifest.get("conditions", []):
        for name in _condition_identifiers(cond.get("expression", "")):
            if name not in targets:
                targets[name] = BindableTarget(field_id=name, origin="condition")

    # A computed field's inputs must resolve too, even when nothing else names them.
    for f in manifest.get("fields", []):
        for name in f.get("inputs", []) or []:
            if name not in targets:
                targets[name] = BindableTarget(field_id=name, origin="formula")

    return list(targets.values())


# ---------------------------------------------------------------- column types

_BOOLEAN_WORDS = frozenset({"true", "false", "yes", "no", "y", "n"})
_CURRENCY_SYMBOLS = "$£€₹¥₩"
_NUMBER_BODY = re.compile(r"^\d+(\.\d+)?$")


def _looks_numeric(text: str) -> bool:
    """Whether one cell is a number a human wrote: separators, symbol, sign.

    Explicitly not `float(text)`, which accepts "nan" and "inf" -- neither of
    which is a number in a spreadsheet, and both of which would type a column
    of prose as numeric on one stray cell.
    """
    cleaned = text.strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    cleaned = cleaned.lstrip("+-").strip()
    for symbol in _CURRENCY_SYMBOLS:
        cleaned = cleaned.replace(symbol, "")
    # Both spaces: a thousands separator arrives as an ordinary space from a
    # CSV and as a non-breaking one from a spreadsheet that formatted it.
    cleaned = cleaned.rstrip("%").replace(",", "").replace(" ", "").replace(" ", "").strip()
    return bool(cleaned) and bool(_NUMBER_BODY.match(cleaned))


def _value_type(text: str) -> str:
    if parse_date(text) is not None:
        return "date"
    if _looks_numeric(text):
        return "number"
    if text.strip().casefold() in _BOOLEAN_WORDS:
        return "boolean"
    return "text"


def infer_column_type(values) -> str | None:
    """The type a column's values actually hold, or None when they say nothing.

    Every non-empty sampled value has to agree. A column that is numbers with
    three "N/A"s in it is text, and a money field bound to it is a type
    mismatch -- which is exactly the finding a reviewer needs, because those
    three rows are the ones that would render "N/A" where an amount belongs.
    Returning "number" on a majority vote would hide them.
    """
    seen: set[str] = set()
    read = 0
    for value in values:
        text = "" if value is None else str(value)
        if not text.strip():
            continue
        seen.add(_value_type(text))
        read += 1
        if len(seen) > 1 or read >= TYPE_SAMPLE_ROWS:
            break
    if not seen:
        # An empty column is not a type mismatch and not a type match. UNKNOWN
        # withholds the weight and the veto, which is the honest answer.
        return None
    return seen.pop() if len(seen) == 1 else "text"


def profile_columns(columns: list[str], records: list | None = None) -> list[ColumnProfile]:
    """Measure every column once, so the type gate reads values and not headers."""
    records = records or []
    profiles = []
    for column in columns:
        values = [r.get(column) for r in records if isinstance(r, dict)]
        non_empty = [v for v in values if v is not None and str(v).strip()]
        profiles.append(ColumnProfile(
            name=column,
            slug=_slug(column),
            observed_type=infer_column_type(non_empty),
            non_empty=len(non_empty),
            sample_value=str(non_empty[0]) if non_empty else None,
        ))
    return profiles


# ------------------------------------------------------------------ similarity


def _similarity(a: str, b: str) -> float:
    """Ratio that also rewards prefixes, so a truncated spreadsheet header
    still finds its full field id."""
    if not a or not b:
        return 0.0
    if a.startswith(b) or b.startswith(a):
        shorter, longer = sorted((len(a), len(b)))
        # A short prefix matches too many things; require real overlap.
        return max(0.75, shorter / longer) if shorter >= 8 else shorter / longer
    return SequenceMatcher(None, a, b).ratio()


def _target_text(target: BindableTarget) -> str:
    """What gets embedded for a target: its label if the document gave it one.

    `<Colleague First Name>` resembles the column "Colleague First Name"; the
    slug `colleague_first_name` resembles it slightly less, and a condition
    variable has nothing but its slug. The source hint is deliberately left out
    -- it is a sentence of instruction prose ("the Colleague first and last name
    from source file"), and mixing a sentence into a two-word label moves the
    cosine for reasons that have nothing to do with the column.
    """
    label = normalise_text(target.label or "")
    return label or target.field_id.replace("_", " ")


def _embed(embedder, text: str):
    """A vector, or None when there is nothing embeddable in the text.

    A column named "#" has no alphanumeric content, and the embedder refuses it
    rather than returning a zero vector with no direction. That refusal is
    right; it just means this candidate has no semantic signal, not that the
    request fails.
    """
    if not normalise_text(text):
        return None
    return embed_one(embedder, text)


def _cosine(left, right) -> float:
    if left is None or right is None:
        return 0.0
    # Clamped rather than passed through: signed feature hashing can produce a
    # slightly negative cosine for unrelated strings, and §13's signal is a
    # strength in [0, 1] where "less than none" has no meaning.
    return max(0.0, min(1.0, float(left @ right)))


# ------------------------------------------------------------------ proposals

_EXACT_METHODS = ("mergefield", "dictionary", "exact_slug")


def _exact_match(target: BindableTarget, profile: ColumnProfile, dictionary: dict) -> tuple[str, str] | None:
    """Whether the names agree after normalisation or a reviewer-confirmed fold.

    All three routes produce §13's `exact_name_match` at full strength. A
    dictionary alias is a human-approved equivalence, which is what "synonym
    folding" means in the record's signal table -- it is not a weaker kind of
    guess than a literal match.
    """
    if target.mergefield_code and profile.name.strip().casefold() == target.mergefield_code.strip().casefold():
        return "mergefield", f"MERGEFIELD code {target.mergefield_code}"
    if dictionary.get(profile.slug) == target.field_id:
        return "dictionary", "alias confirmed in this organisation's field dictionary"
    if profile.slug == target.field_id:
        return "exact_slug", "column name normalises to the field id"
    return None


#: `MappingMemory.lookup` ranks prior mappings for the field asked about *and*
#: for fields whose stored context resembles it, and reports how close each one
#: was: exactly 1.0 when the field itself matched, a context cosine otherwise.
#: Only the first kind is §13's signal, which is worded "same mapping approved
#: 42 times". Crediting `start_date` with the approvals a reviewer gave
#: `effective_date` would inflate the evidence for a column nobody approved
#: here, on the fields where two names differ by one word -- which is exactly
#: where mapping mistakes live.
SAME_FIELD_PRECEDENT = 1.0


def _approvals_index(lookup) -> dict:
    """Prior approvals *for this field*, keyed by normalised column name.

    Takes whatever `MappingMemory.lookup` returned rather than a count, so the
    approval arithmetic (approvals minus the rejections a reviewer has since
    made) stays where it is defined instead of being re-derived here.
    """
    index: dict[str, int] = {}
    for prior in getattr(lookup, "prior_mappings", []) or []:
        if float(getattr(prior, "similarity", SAME_FIELD_PRECEDENT)) < SAME_FIELD_PRECEDENT:
            continue
        key = normalise_text(prior.source_column)
        index[key] = max(index.get(key, 0), int(prior.net_approvals))
    return index


def _build_candidate(
    target: BindableTarget,
    profile: ColumnProfile,
    *,
    exact: bool,
    cosine: float,
    approvals: int | None,
) -> cf.Candidate:
    """One (target, column) pair with the signals that genuinely support it.

    `type_compatibility` is not in this list on purpose: it is a hard gate as
    well as a weight, so `confidence.Candidate` derives it once from the
    declared and observed types rather than trusting a caller to pass the same
    verdict to both.
    """
    signals = [
        cf.exact_name_match(exact),
        cf.semantic_similarity(cosine),
    ]
    if approvals is not None:
        # Zero approvals is evidence: it says memory was asked and had nothing,
        # which is a different statement from never having asked. §13's
        # no-precedent veto reads it that way too.
        signals.append(cf.historical_approvals(approvals))
    return cf.Candidate(
        target=target.field_id,
        source_ref=profile.name,
        signals=tuple(signals),
        target_field=target.spec or {"id": target.field_id},
        declared_type=target.declared_type,
        source_type=profile.observed_type,
    )


def _rationale(why: str, scored) -> str:
    """One line a reviewer can act on: what proposed it, and what backs it."""
    parts = [why] if why else []
    for signal in scored.candidate.scoring_signals():
        if not signal.supports or signal.name == "exact_name_match":
            continue  # an exact match is already what `why` says
        if signal.name == "historical_approvals":
            parts.append(f"approved before in this organisation ({signal.detail.replace('_', ' ')})")
        elif signal.name == "semantic_similarity":
            parts.append(f"name similarity {signal.strength:.0%}")
        elif signal.name == "type_compatibility":
            parts.append(f"type {signal.detail} checks out")
    if not scored.candidate.supporting_signals():
        # Said out loud rather than left as a low number. A proposal from the
        # near-match tier or from the model reads like a finding otherwise, and
        # it is the one a reviewer most needs to look at properly.
        parts.append("nothing independent supports it")
    text = "; ".join(parts)
    # Only the first character: `str.capitalize` lower-cases everything after
    # it, and "MERGEFIELD code LAB__FT_SALARY__38_HR_" is not a sentence.
    return text[:1].upper() + text[1:]


def suggest_bindings(
    manifest: dict,
    columns: list[str],
    dictionary: dict[str, str] | None = None,
    llm_matcher=None,
    records: list | None = None,
    memory_lookups: dict | None = None,
    embedder=None,
) -> BindingPlan:
    """Propose ranked columns for each bindable target, scored by §13.

    `dictionary` maps a normalised alias -> canonical field id, accumulated from
    previous reviewer corrections across the estate. `llm_matcher` is called
    only with whatever the deterministic tiers could not match, so the cost is
    proportional to the residue rather than the template. `records`, when given,
    types the columns from their real values and additionally reports condition
    literals no row's value equals.

    `memory_lookups` maps field_id -> the `MemoryLookup` that
    `app.retrieval.mapping_memory` returned for that field. Passing it is what
    makes §13's historical-approvals signal available; leaving it off omits the
    signal rather than substituting a zero, because "nobody asked" and "asked,
    and this organisation has never approved it" are different findings and the
    no-precedent veto turns on which one it is.
    """
    dictionary = dictionary or {}
    records = records or []
    targets = bindable_targets(manifest)
    profiles = profile_columns(columns, records)
    by_name = {p.name: p for p in profiles}

    embedder = embedder or HashingEmbedder()
    column_vectors = {p.name: _embed(embedder, p.name) for p in profiles}
    # Normalised once per column rather than once per (target, column): the
    # inner loop below runs fields x columns times, and a five-hundred-column
    # export is not a rare shape.
    column_keys = {p.name: normalise_text(p.name) for p in profiles}

    # field_id -> {column name: (method, why)}. A column reaches this map once;
    # the tier that got there first names it, and the signals decide its worth.
    #
    # Deliberately not every (field, column) pair. A candidate whose only
    # support is the type gate is not a candidate: every text column is
    # type-compatible with a string field, so admitting them would put a
    # hundred columns inside the 0.05 ambiguity margin of each other and veto
    # every target in the manifest as a tie. A pair reaches the ranking when
    # something -- a name, an embedding, a prior approval, a model -- actually
    # proposed it.
    proposals: dict[str, dict[str, tuple[str, str]]] = {t.field_id: {} for t in targets}
    cosines: dict[tuple, float] = {}
    approvals: dict[tuple, int] = {}
    consulted_memory: set[str] = set()
    exactly_claimed: set[str] = set()

    for target in targets:
        target_vector = _embed(embedder, _target_text(target))
        lookup = (memory_lookups or {}).get(target.field_id)
        index = _approvals_index(lookup) if lookup is not None else {}
        if memory_lookups is not None and target.field_id in memory_lookups:
            consulted_memory.add(target.field_id)

        for profile in profiles:
            cosine = _cosine(target_vector, column_vectors.get(profile.name))
            cosines[(target.field_id, profile.name)] = cosine
            approved = index.get(column_keys[profile.name], 0)
            approvals[(target.field_id, profile.name)] = approved

            hit = _exact_match(target, profile, dictionary)
            if hit:
                proposals[target.field_id][profile.name] = hit
                exactly_claimed.add(profile.name)
            elif cosine >= cf.SEMANTIC_SIMILARITY_FLOOR:
                proposals[target.field_id][profile.name] = (
                    "fuzzy", f"column name is {cosine:.0%} similar to the field",
                )
            elif approved > 0:
                proposals[target.field_id][profile.name] = (
                    "memory", "this organisation has approved this mapping before",
                )

    # A near match is a last resort, so it only runs for targets nothing else
    # proposed anything for, and never over a column that is some other field's
    # exact match. Below §13's cosine floor it contributes no signal at all --
    # it puts a candidate in front of a reviewer, with a band that says so.
    for target in targets:
        if proposals[target.field_id]:
            continue
        best_name, best_score = None, 0.0
        for profile in profiles:
            if profile.name in exactly_claimed:
                continue
            score = _similarity(profile.slug, target.field_id)
            if score > best_score:
                best_name, best_score = profile.name, score
        if best_name and best_score >= FUZZY_THRESHOLD:
            proposals[target.field_id][best_name] = (
                "fuzzy", f"closest remaining column ({best_score:.0%} similar by name shape)",
            )

    # The model sees only the residue, and its own confidence is not evidence:
    # §13 excludes a self-reported score from the combination twice over, so an
    # LLM proposal is scored on the independent signals it happens to carry and
    # lands in REVIEW or BLOCK when it carries none.
    unresolved_ids = [t.field_id for t in targets if not proposals[t.field_id]]
    if llm_matcher and unresolved_ids:
        remaining = [c for c in columns if c not in exactly_claimed]
        if remaining:
            for field_id, column, rationale in llm_matcher(unresolved_ids, remaining):
                if column not in by_name or field_id not in proposals or proposals[field_id]:
                    continue
                proposals[field_id][column] = ("llm", rationale or "proposed by the mapping model")

    decisions: dict[str, cf.Decision] = {}
    for target in targets:
        candidates = [
            _build_candidate(
                target,
                by_name[column],
                exact=method in _EXACT_METHODS,
                cosine=cosines[(target.field_id, column)],
                approvals=(
                    approvals[(target.field_id, column)]
                    if target.field_id in consulted_memory else None
                ),
            )
            for column, (method, _why) in proposals[target.field_id].items()
        ]
        if candidates:
            decisions[target.field_id] = cf.decide(candidates)

    chosen = _assign_columns(targets, decisions)

    suggestions: list[Suggestion] = []
    unmatched: list[str] = []
    for target in targets:
        decision = decisions.get(target.field_id)
        scored = chosen.get(target.field_id)
        profile = by_name.get(scored.candidate.source_ref) if scored else None
        if scored is None or profile is None:
            unmatched.append(target.field_id)
            suggestions.append(Suggestion(
                field_id=target.field_id, column=None, confidence=0.0, method="unmatched",
                rationale=(
                    # Two different findings, and a reviewer acts on them
                    # differently: one needs a column, the other needs the
                    # contested column taken off whichever field has it wrong.
                    "Every column that matched is already bound to another field"
                    if decision else "No column matched -- bind manually or leave blank"
                ),
                band=cf.Band.BLOCK.value,
                alternatives=[s.as_dict() for s in decision.ranked] if decision else [],
                declared_type=target.declared_type,
            ))
            continue

        method, why = proposals[target.field_id][profile.name]
        suggestions.append(Suggestion(
            field_id=target.field_id,
            column=profile.name,
            confidence=round(scored.score, 4),
            method=method,
            rationale=_rationale(why, scored),
            band=scored.band.value,
            vetoes=list(scored.vetoes),
            evidence=list(scored.candidate.evidence()),
            alternatives=[
                s.as_dict() for s in decision.ranked if s.candidate.source_ref != profile.name
            ],
            declared_type=target.declared_type,
            observed_type=profile.observed_type,
        ))

    taken = {s.column for s in suggestions if s.column}
    return BindingPlan(
        suggestions=suggestions,
        unmatched_fields=unmatched,
        unused_columns=[c for c in columns if c not in taken and not c.startswith("_")],
        unmatched_condition_values=_unmatched_condition_values(
            manifest, {s.field_id: s.column for s in suggestions if s.column}, records
        ),
        decisions=decisions,
        column_profiles=profiles,
    )


def _assign_columns(targets: list, decisions: dict) -> dict:
    """Give each target the best-scoring column no better-evidenced target wants.

    One column, one field: two fields bound to the same column means one of
    them is wrong, and a document that repeats the same value in two places is
    the visible half of that mistake. Best evidence goes first so a contested
    column lands with the field that has the stronger case for it, and the
    loser drops to its next-ranked alternative rather than being dropped.
    """
    leaders = {
        field_id: decision.leader.score for field_id, decision in decisions.items()
    }
    order = sorted(
        (t for t in targets if t.field_id in decisions),
        # field_id breaks ties so the same inputs always produce the same
        # assignment; an approval summary that reshuffles between runs is
        # unreviewable.
        key=lambda t: (-leaders[t.field_id], t.field_id),
    )

    taken: set[str] = set()
    chosen: dict = {}
    for target in order:
        for scored in decisions[target.field_id].ranked:
            if scored.candidate.source_ref in taken:
                continue
            chosen[target.field_id] = scored
            taken.add(scored.candidate.source_ref)
            break
    return chosen


def _unmatched_condition_values(manifest: dict, field_bindings: dict, records: list) -> list:
    from app.generation.docx_renderer import EQUALITY_CONDITION_RE
    from app.expressions.token_parser import _normalise_scalar

    if not records:
        return []

    offered: dict[str, list[str]] = {}
    for cond in manifest.get("conditions", []):
        m = EQUALITY_CONDITION_RE.match(cond.get("expression", ""))
        if m and m.group(2) not in offered.setdefault(m.group(1), []):
            offered[m.group(1)].append(m.group(2))

    out: list[UnmatchedConditionValue] = []
    for field_id, literals in sorted(offered.items()):
        column = field_bindings.get(field_id)
        if column is None:
            continue  # already reported as an unmatched field; not a value problem
        wanted = {_normalise_scalar(v) for v in literals}
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get(column)
            if value in (None, "") or str(value) in seen:
                continue
            seen.add(str(value))
            if _normalise_scalar(value) not in wanted:
                out.append(UnmatchedConditionValue(
                    field_id=field_id, column=column,
                    observed_value=str(value), offered=list(literals),
                ))
    return out


def apply_binding(record: dict, field_bindings: dict, value_map: dict | None = None) -> dict:
    """Turn one source row into a field-keyed record the fill engine can use.

    `value_map` closes gaps normalisation cannot: a source that writes "FT"
    where the manifest condition expects "Full time" is a vocabulary
    difference, not a formatting one.
    """
    value_map = value_map or {}
    out: dict = {}
    for field_id, column in field_bindings.items():
        if not column or column not in record:
            continue
        value = record[column]
        mapped = value_map.get(field_id, {})
        if value in mapped:
            value = mapped[value]
        elif isinstance(value, str):
            # Case-insensitive fallback so a reviewer doesn't have to enumerate casings.
            lowered = {str(k).casefold(): v for k, v in mapped.items()}
            if value.casefold() in lowered:
                value = lowered[value.casefold()]
        out[field_id] = value
    return out
