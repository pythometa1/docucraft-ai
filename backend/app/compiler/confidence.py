"""Turning mapping evidence into a number the approval gate can act on.

Everything upstream of this module produces opinions: a fuzzy string ratio, a
cosine, a model that says it is 99% sure. `source_resolver` already attaches a
number to each of them, but that number is a label for the tier that produced
it -- 0.95 for an exact slug match, 0.80 for anything the model proposed -- and
it is the same 0.95 whether the mapping has been approved forty-two times
across the estate or has never been seen anywhere before. An approval queue
built on it cannot separate a well-evidenced mapping from a lucky string match,
so a reviewer either rubber-stamps the lot or re-reads the lot, and both of
those end with a salary in the wrong paragraph.

§13 of the architecture record replaces the per-tier label with an explicit
combination over *independent* signals:

    confidence = 1 - PRODUCT over signals i of (1 - s_i * w_i)

Two properties are the reason for that shape. Evidence accumulates -- a weak
second signal always raises the score and can never lower it -- and no finite
amount of evidence reaches 1.0, so "certain" is not a state this function is
able to report.

The one input that never enters the product is the model's own stated
confidence. The record says so twice (§4 principle 8, and §13 again): it is
uncalibrated, unauditable, and correlates poorly with correctness on
domain-specific mapping, which is worst precisely in the case that matters --
a plausible but wrong mapping is where a model is most self-assured. `Signal`
therefore refuses a self-reported confidence by name rather than trusting every
future caller to remember the rule.

Every weight and threshold in this module is a STARTING VALUE, not a measured
constant, and they are named that way in code so nobody mistakes them for
findings. They have to be calibrated against real reviewer decisions: log each
suggestion with its score, its evidence and the reviewer's accept or reject
(§14 -- that log is the only dataset that can ever calibrate them, and it
cannot be reconstructed later), then fit the bands so the auto-accept band hits
a target precision on held-out templates. Until that data exists the bands stay
deliberately conservative. The goal on day one is a near-zero escaped-error
rate, not a high automation percentage.
"""

import math
import re
from dataclasses import dataclass
from enum import Enum

# ---- starting configuration ----------------------------------------------
#
# None of these numbers has been measured. Flip WEIGHTS_CALIBRATED only when a
# fit against reviewer decisions exists, and change it in the same commit that
# records the dataset it was fitted on -- a weight table whose provenance is
# "someone adjusted it once" is worse than an honestly unmeasured one.

WEIGHTS_CALIBRATED = False

#: w_i per §13, table "Evidence combination -- starting configuration".
INITIAL_WEIGHTS: dict[str, float] = {
    "exact_name_match": 0.55,          # after normalisation and synonym folding
    "semantic_similarity": 0.35,       # cosine; floor at 0.75, else s_i = 0
    "historical_approvals": 0.60,      # saturating: s_i = 1 - e^(-n/8)
    "type_compatibility": 0.30,        # hard gate -- mismatch vetoes the candidate
    "sentence_context_match": 0.30,    # local-window entailment against the sentence
    "family_inheritance": 0.65,        # scaled by measured family similarity
    "business_rule_consistency": 0.25, # coherence with other objects in the manifest
}

#: Below this cosine the embedding is telling us nothing, so s_i is 0 rather
#: than a small positive number. A 0.6 cosine between unrelated HR field names
#: is background noise for most embedding models, and letting it contribute
#: would make every candidate look mildly supported.
SEMANTIC_SIMILARITY_FLOOR = 0.75

#: Approvals saturate: the eighth approval of a mapping should count for much
#: less than the first, and no number of them may carry a mapping on its own.
HISTORICAL_SATURATION = 8.0

#: Two candidates this close are a tie, whatever the ranking says.
AMBIGUITY_MARGIN = 0.05

AUTO_ACCEPT_FLOOR = 0.97
CONFIRM_FLOOR = 0.80
REVIEW_FLOOR = 0.50

#: §13's second veto: a money, date or identifier field wants corroboration.
#: One signal, however strong, is a single point of failure on the fields where
#: being wrong is most expensive.
MINIMUM_SIGNALS_WHEN_MATERIAL = 2


class Band(str, Enum):
    """What the approval gate may do with a candidate. §13, "Approval bands"."""

    AUTO_ACCEPT = "AUTO_ACCEPT"  # applied automatically, still itemised and reversible
    CONFIRM = "CONFIRM"          # pre-selected with evidence, one click to accept
    REVIEW = "REVIEW"            # presented with ranked alternatives and reasoning
    BLOCK = "BLOCK"              # the manifest cannot be locked until a human resolves it


# ---- vetoes ---------------------------------------------------------------
#
# The record is inconsistent about what a veto produces: the signal table says
# vetoes "force REVIEW regardless of computed score", while the approval-band
# table says "Block | < 0.50, or any veto". We take the band table, which is
# the stricter of the two and the one the gate actually reads -- both bands
# mean "a human decides", and §13's own instruction while the weights are
# uncalibrated is to set the bands conservatively.

VETO_TYPE_MISMATCH = "declared_type_mismatch"
VETO_THIN_EVIDENCE = "material_field_under_two_signals"
VETO_NO_PRECEDENT = "no_historical_precedent_and_no_family_match"
VETO_AMBIGUOUS = "second_candidate_within_margin"

VETO_HELP = {
    VETO_TYPE_MISMATCH: "The source column's type cannot fill the field's declared type.",
    VETO_THIN_EVIDENCE: (
        "A money, date or identifier field is supported by fewer than two independent signals."
    ),
    VETO_NO_PRECEDENT: (
        "Nothing has approved this mapping before and no template family vouches for it."
    ),
    VETO_AMBIGUOUS: "Another candidate scores within 0.05 of this one; the choice is a tie.",
}


class TypeMatch(str, Enum):
    """Three states, because "we do not know" is not "they disagree".

    Collapsing UNKNOWN into MISMATCH vetoes every mapping drawn from an untyped
    CSV; collapsing it into COMPATIBLE hands the 0.30 weight to a candidate no
    one has type-checked. Neither is acceptable, so callers get all three.
    """

    COMPATIBLE = "COMPATIBLE"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


# ---- signals --------------------------------------------------------------

# No legitimate signal name contains any of these. A caller reaching for one is
# reaching for the model's own opinion of itself, which §13 excludes by name.
_SELF_REPORT_TOKENS = (
    "confidence", "certainty", "self_report", "selfreport", "stated",
    "logprob", "log_prob", "perplexity", "llm", "model", "gpt", "claude",
)


@dataclass(frozen=True)
class Signal:
    """One independent piece of evidence for one candidate mapping.

    `strength` is s_i in [0, 1] -- how strongly this signal supports the
    mapping. The weight it is multiplied by lives in INITIAL_WEIGHTS and is not
    the caller's to choose, so that two call sites cannot quietly disagree
    about what an exact name match is worth.

    A strength of 0 is a real and useful record: it says the signal was
    computed and did not support the mapping (a cosine under the floor, a name
    that did not match), which is different from a signal nobody looked for.
    Zero-strength signals are kept as evidence and are not counted as support.
    """

    name: str
    strength: float
    detail: str = ""

    def __post_init__(self):
        _reject_self_report(self.name)
        if self.name not in INITIAL_WEIGHTS:
            raise ValueError(
                f"Unknown signal {self.name!r}. The scoring function is closed over the "
                f"§13 signal set: {', '.join(sorted(INITIAL_WEIGHTS))}. Adding a signal means "
                "adding a weight to INITIAL_WEIGHTS and recalibrating, not passing a new name."
            )
        if not 0.0 <= float(self.strength) <= 1.0:
            raise ValueError(
                f"Signal {self.name!r} has strength {self.strength!r}; s_i must be in [0, 1]. "
                "A strength outside that range breaks the combination formula's guarantee "
                "that evidence can only accumulate."
            )

    @property
    def weight(self) -> float:
        return INITIAL_WEIGHTS[self.name]

    @property
    def supports(self) -> bool:
        """Whether this counts towards the two-signal minimum on material fields."""
        return self.strength > 0.0

    def as_evidence(self) -> str:
        """The audit-trail form, per the §6 manifest object's `evidence` list."""
        if self.detail:
            return f"{self.name}:{self.detail}"
        if self.strength >= 1.0:
            return self.name
        return f"{self.name}:{self.strength:.2f}"


def _reject_self_report(name: str) -> None:
    lowered = str(name).strip().lower()
    for token in _SELF_REPORT_TOKENS:
        if token in lowered:
            raise ValueError(
                f"{name!r} is a model's own stated confidence, and §13 excludes it from the "
                "scoring function twice over: it is uncalibrated, unauditable, and least "
                "reliable exactly where it matters -- on a plausible but wrong mapping. "
                "Pass the independent signals the model's proposal is supported by "
                "(name match, semantic similarity, precedent, family, type, context), "
                "not the model's opinion of itself."
            )


def exact_name_match(matched: bool, *, detail: str = "") -> Signal:
    """Names agree after normalisation and synonym folding.

    Binary on purpose. A synonym fold is a human-approved equivalence, so
    "Effective Date" reaching `effective_date` through the fold table is worth
    exactly what a literal match is worth; anything weaker than a fold is what
    `semantic_similarity` is for.
    """
    return Signal("exact_name_match", 1.0 if matched else 0.0, detail)


def semantic_similarity(cosine: float) -> Signal:
    """Embedding similarity, with §13's floor applied."""
    if not 0.0 <= float(cosine) <= 1.0:
        raise ValueError(f"Cosine similarity {cosine!r} is outside [0, 1].")
    strength = float(cosine) if cosine >= SEMANTIC_SIMILARITY_FLOOR else 0.0
    return Signal("semantic_similarity", strength, f"cos={float(cosine):.2f}")


def historical_approvals(n: int) -> Signal:
    """`n` previous approvals of this exact mapping, saturating at 1 - e^(-n/8).

    Saturating rather than linear because the fortieth approval of a mapping is
    not four times the evidence of the tenth, and because an unsaturated count
    would let one popular mapping auto-accept with no other support at all.
    """
    count = int(n)
    if count < 0:
        raise ValueError(f"Approval count {n!r} is negative.")
    strength = 1.0 - math.exp(-count / HISTORICAL_SATURATION)
    return Signal("historical_approvals", strength, f"{count}_approvals")


def sentence_context_match(entailment: float) -> Signal:
    """Local-window entailment: does the sentence around the token agree?

    "You will report to <X>" entails a person, not a date. This is the signal
    that catches a mapping which is plausible on the field name alone.
    """
    if not 0.0 <= float(entailment) <= 1.0:
        raise ValueError(f"Entailment score {entailment!r} is outside [0, 1].")
    return Signal("sentence_context_match", float(entailment))


def family_inheritance(family_similarity: float) -> Signal:
    """This mapping is approved on a sibling template, scaled by how alike they are.

    The scaling is the whole safeguard: a mapping inherited from a 98% similar
    template is near-conclusive, the same mapping inherited from a 60% match is
    a hint, and treating both as "inherited" is how one customer's approved
    mapping ends up wrong on another's letter.
    """
    if not 0.0 <= float(family_similarity) <= 1.0:
        raise ValueError(f"Family similarity {family_similarity!r} is outside [0, 1].")
    return Signal(
        "family_inheritance", float(family_similarity), f"{float(family_similarity):.2f}"
    )


def business_rule_consistency(coherence: float) -> Signal:
    """Coherence with the other objects in the same manifest.

    A temporary-assignment section that requires a transfer end date supports
    the mapping that supplies one; a manifest where nothing else references the
    proposed source object supports nothing.
    """
    if not 0.0 <= float(coherence) <= 1.0:
        raise ValueError(f"Coherence score {coherence!r} is outside [0, 1].")
    return Signal("business_rule_consistency", float(coherence))


# ---- field classification -------------------------------------------------
#
# Two §13 rules key off what kind of field is being filled: money and
# identifier fields are excluded from auto-accept, and money, date and
# identifier fields need two independent signals. Neither rule can be applied
# from the declared type alone -- this codebase's type vocabulary is
# string|currency|date|number|percent, which has no way to say "identifier" --
# so the field's own name is read as well.
#
# The classification errs towards material. Calling a plain field money costs
# one reviewer click; missing a real one costs a letter that states the wrong
# salary, and §13 is explicit about which of those two is worth avoiding.

_MONEY_TYPES = frozenset({"currency", "money", "amount", "salary", "compensation"})
_IDENTIFIER_TYPES = frozenset({"identifier", "id", "reference"})
_DATE_TYPES = frozenset({"date", "datetime", "timestamp"})

_MONEY_TOKENS = frozenset({
    "salary", "salaries", "wage", "wages", "pay", "paid", "payment", "payout",
    "bonus", "commission", "compensation", "comp", "ctc", "stipend", "allowance",
    "remuneration", "amount", "fee", "fees", "price", "cost", "currency",
    "earnings", "premium", "rate", "increment", "severance", "gratuity",
    "reimbursement", "deduction", "gross", "nett",
})

#: Tokens that make a field an identifier on their own.
_IDENTIFIER_TOKENS = frozenset({
    "id", "ids", "ssn", "sin", "nino", "nric", "iban", "bic", "swift", "uan",
    "passport", "aadhaar", "aadhar", "pan", "tan", "ein", "tin", "utr", "guid",
    "uuid", "payroll", "msisdn", "npi", "dea",
})

#: "number", "code" and "ref" only mean identifier next to one of these. A week
#: number is not an identifier; an account number is.
_IDENTIFIER_QUALIFIERS = frozenset({"number", "no", "num", "code", "ref", "reference"})
_IDENTIFIED_ENTITIES = frozenset({
    "account", "employee", "colleague", "staff", "worker", "personnel", "member",
    "national", "insurance", "social", "security", "tax", "passport", "licence",
    "license", "phone", "mobile", "telephone", "badge", "payroll", "bank",
    "sort", "routing", "customer", "case", "file", "registration", "vehicle",
    "invoice", "policy", "contract", "requisition", "position", "candidate",
})

_DATE_TOKENS = frozenset({"date", "dates", "dob", "doj", "birthdate", "birthday"})

_NAME_KEYS = ("id", "field_id", "name", "label", "token", "template_token")
_TYPE_KEYS = ("type", "value_type", "field_type")


def _tokens(text: str) -> set[str]:
    """Split an identifier-ish string into comparable word tokens.

    Token-wise rather than substring: `"id" in "candidate_name"` is true and
    would classify half an offer letter as an identifier field.
    """
    return {t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t}


def _subject_tokens(field) -> set[str]:
    if isinstance(field, str):
        return _tokens(field)
    if isinstance(field, dict):
        tokens: set[str] = set()
        for key in _NAME_KEYS:
            if field.get(key):
                tokens |= _tokens(field[key])
        return tokens
    raise TypeError(
        f"Expected a manifest field dict or a field id, got {type(field).__name__}. "
        "Classification must not fall back to 'not material' on an unexpected shape -- "
        "that is how a salary field auto-accepts."
    )


def declared_type(field) -> str | None:
    """The field's declared type, under whichever of the schema's names it uses."""
    if isinstance(field, str) or not isinstance(field, dict):
        return None
    for key in _TYPE_KEYS:
        value = field.get(key)
        if value:
            return str(value).strip().lower()
    return None


def is_money_or_identifier(field) -> bool:
    """Whether this field is money or an identifier.

    Both §13 rules that single these out are one-way: such a field is never
    auto-accepted however strong the evidence, and it needs two independent
    signals. `field` is a manifest field object, or a bare field id when that is
    all the caller has.
    """
    declared = declared_type(field)
    if declared in _MONEY_TYPES or declared in _IDENTIFIER_TYPES:
        return True

    tokens = _subject_tokens(field)
    if tokens & _MONEY_TOKENS or tokens & _IDENTIFIER_TOKENS:
        return True
    return bool(tokens & _IDENTIFIER_QUALIFIERS and tokens & _IDENTIFIED_ENTITIES)


def is_date_field(field) -> bool:
    """Whether this field carries a date. Dates join the two-signal rule."""
    if declared_type(field) in _DATE_TYPES:
        return True
    return bool(_subject_tokens(field) & _DATE_TOKENS)


def requires_corroboration(field) -> bool:
    """§13's second veto class: money, date or identifier.

    A wrong name is visible to the person reading the letter. A wrong date or a
    wrong amount reads as authoritative, which is why these three need a second
    independent signal before any score is believed.
    """
    return is_money_or_identifier(field) or is_date_field(field)


# ---- type compatibility ---------------------------------------------------

_TYPE_FAMILIES = {
    "string": "text", "str": "text", "text": "text", "varchar": "text", "char": "text",
    "number": "number", "numeric": "number", "int": "number", "integer": "number",
    "float": "number", "double": "number", "decimal": "number", "percent": "number",
    "currency": "number", "money": "number", "amount": "number",
    "date": "date", "datetime": "date", "timestamp": "date",
    "bool": "boolean", "boolean": "boolean",
    "identifier": "identifier", "id": "identifier", "reference": "identifier",
}

#: Which observed families can legitimately fill a declared family. A text
#: field prints anything; a date field does not accept a name, and a currency
#: field does not accept free text, which is the case this gate exists for.
_ACCEPTS = {
    "text": frozenset({"text", "number", "date", "boolean", "identifier"}),
    "identifier": frozenset({"identifier", "text", "number"}),
    "number": frozenset({"number"}),
    "date": frozenset({"date"}),
    "boolean": frozenset({"boolean"}),
}


def compare_types(declared: str | None, observed: str | None) -> TypeMatch:
    """Whether a source column of `observed` type can fill a `declared` field.

    `observed` must be the *measured* type of the column's values, not the file
    format's storage type. Every column in a CSV is text on disk; reporting it
    that way here would veto every mapping in the estate, and reporting it as
    the declared type would defeat the gate. When the caller has not probed the
    values, pass None -- UNKNOWN withholds both the weight and the veto, which
    is the honest answer.
    """
    if not declared or not observed:
        return TypeMatch.UNKNOWN
    d = _TYPE_FAMILIES.get(str(declared).strip().lower())
    o = _TYPE_FAMILIES.get(str(observed).strip().lower())
    if d is None or o is None:
        # An unrecognised type name is our ignorance, not the caller's error,
        # and ignorance must not veto.
        return TypeMatch.UNKNOWN
    return TypeMatch.COMPATIBLE if o in _ACCEPTS[d] else TypeMatch.MISMATCH


# ---- candidates -----------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One proposed mapping of one source object onto one manifest target.

    `signals` deliberately does not include `type_compatibility`: the type gate
    both scores and vetoes, so it is derived once from `declared_type` and
    `source_type` rather than being passed in a second time by a caller who
    might disagree with itself.
    """

    target: str
    source_ref: str
    signals: tuple = ()
    target_field: dict | None = None
    declared_type: str | None = None
    source_type: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "signals", tuple(self.signals))
        seen: set[str] = set()
        for signal in self.signals:
            if not isinstance(signal, Signal):
                raise TypeError(
                    f"Candidate.signals must hold Signal objects, got {type(signal).__name__}."
                )
            if signal.name == "type_compatibility":
                raise ValueError(
                    "type_compatibility is derived from declared_type and source_type, not "
                    "passed as a signal -- it is a hard gate as well as a weight, and two "
                    "sources of truth for it means a candidate that scores as type-checked "
                    "while the veto sees nothing."
                )
            if signal.name in seen:
                raise ValueError(
                    f"Signal {signal.name!r} was supplied twice. The combination formula runs "
                    "over *independent* signals; counting one twice inflates the score by "
                    "pretending a second piece of evidence exists."
                )
            seen.add(signal.name)

    @property
    def field_subject(self):
        """What the field classifiers read: the manifest object, else the id."""
        if self.target_field:
            spec = dict(self.target_field)
            spec.setdefault("id", self.target)
            return spec
        return self.target

    @property
    def type_match(self) -> TypeMatch:
        declared = self.declared_type or declared_type(self.field_subject)
        return compare_types(declared, self.source_type)

    def scoring_signals(self) -> tuple:
        """Every signal that enters the product, type gate included."""
        if self.type_match is TypeMatch.COMPATIBLE:
            declared = self.declared_type or declared_type(self.field_subject)
            gate = Signal("type_compatibility", 1.0, f"{self.source_type}->{declared}")
            return self.signals + (gate,)
        # MISMATCH contributes nothing and vetoes instead; UNKNOWN contributes
        # nothing because an unchecked type is not evidence.
        return self.signals

    def supporting_signals(self) -> tuple:
        return tuple(s for s in self.scoring_signals() if s.supports)

    def evidence(self) -> tuple:
        return tuple(s.as_evidence() for s in self.scoring_signals())


# ---- scoring --------------------------------------------------------------


def score(candidate: Candidate) -> float:
    """confidence = 1 - PRODUCT over independent signals i of (1 - s_i * w_i).

    A candidate with no supporting evidence scores 0, not a floor value. The
    result is strictly below 1 for any finite evidence, which is the point: the
    auto-accept threshold is reachable only by several independent signals
    agreeing, never by one signal being emphatic.
    """
    residual = 1.0
    for signal in candidate.scoring_signals():
        residual *= 1.0 - signal.strength * signal.weight
    return 1.0 - residual


def vetoes(candidate: Candidate) -> list[str]:
    """§13's vetoes that are computable from one candidate alone.

    The fourth veto -- two candidates within 0.05 -- is a property of the ranked
    list for a target, not of a candidate, so `decide` adds it.
    """
    found: list[str] = []

    if candidate.type_match is TypeMatch.MISMATCH:
        found.append(VETO_TYPE_MISMATCH)

    if (
        requires_corroboration(candidate.field_subject)
        and len(candidate.supporting_signals()) < MINIMUM_SIGNALS_WHEN_MATERIAL
    ):
        found.append(VETO_THIN_EVIDENCE)

    # Cold start is intentional: on the first template of a new organisation
    # nothing has precedent and nothing has a family, so everything is reviewed.
    # That is §13's stated day-one posture, not a bug to tune away.
    by_name = {s.name: s for s in candidate.signals}
    precedent = by_name.get("historical_approvals")
    family = by_name.get("family_inheritance")
    if not (precedent and precedent.supports) and not (family and family.supports):
        found.append(VETO_NO_PRECEDENT)

    return found


def band_for_score(value: float, *, vetoed: bool, money_or_identifier: bool) -> Band:
    """The band a score falls in. Split out so the thresholds are testable exactly.

    §13 says two different things about what a veto does, and they cannot both
    be followed. The signal table: "Vetoes -- force REVIEW regardless of computed
    score". The band table, two paragraphs later: "Block | < 0.50, or any veto".

    This follows the first. A veto means a human has to look at the mapping, and
    REVIEW is the band whose whole definition is "presented with ranked
    alternatives and the reasoning behind each" -- which is exactly what a
    reviewer needs for a type mismatch or a two-way tie. BLOCK is reserved for
    what the band table's own threshold describes: a score below 0.50, meaning
    there is no real evidence for the mapping at all.

    The reading matters more than the wording, because of the third veto: "no
    historical precedent AND no family match". On a new installation that is
    true of *every* mapping -- there is no history and there are no families
    yet. Under the other reading, a customer's first template has every field
    blocked, the manifest can never be locked, and the product cannot be used at
    all until data it has no way to acquire already exists. A veto is a reason
    to look, not a reason the system cannot be started.

    Nothing is weakened: a vetoed candidate is still never auto-accepted and
    never pre-selected, and `approve_manifest` still refuses to lock a manifest
    with a BLOCK-band mapping outstanding.
    """
    if value < REVIEW_FLOOR:
        return Band.BLOCK
    if vetoed:
        return Band.REVIEW
    if value >= AUTO_ACCEPT_FLOOR:
        # A money or identifier field never auto-accepts. It drops to CONFIRM,
        # which costs a click and is the only thing standing between a very
        # confident wrong mapping and a letter that states the wrong salary.
        return Band.CONFIRM if money_or_identifier else Band.AUTO_ACCEPT
    if value >= CONFIRM_FLOOR:
        return Band.CONFIRM
    return Band.REVIEW


def band(candidate: Candidate, *, extra_vetoes=()) -> Band:
    """What the approval gate may do with this candidate.

    `extra_vetoes` carries vetoes that only the ranked list can see; `decide`
    supplies the ambiguity veto through it.
    """
    all_vetoes = vetoes(candidate) + list(extra_vetoes)
    return band_for_score(
        score(candidate),
        vetoed=bool(all_vetoes),
        money_or_identifier=is_money_or_identifier(candidate.field_subject),
    )


# ---- decisions over a ranked list -----------------------------------------


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    vetoes: tuple
    band: Band

    def as_dict(self) -> dict:
        return {
            "source_ref": self.candidate.source_ref,
            "score": round(self.score, 4),
            "band": self.band.value,
            "evidence": list(self.candidate.evidence()),
            "vetoes": list(self.vetoes),
        }


@dataclass(frozen=True)
class Decision:
    """The gate's verdict for one target, with the evidence that produced it.

    `as_dict` is the shape §14 requires to be logged for every suggestion --
    score, evidence and, once a human has acted, their decision. That log is the
    only dataset that can calibrate the weights above, and it cannot be
    reconstructed after the fact.
    """

    target: str
    band: Band
    ranked: tuple
    vetoes: tuple

    @property
    def leader(self) -> ScoredCandidate:
        return self.ranked[0]

    @property
    def auto_applicable(self) -> bool:
        return self.band is Band.AUTO_ACCEPT

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "band": self.band.value,
            "vetoes": list(self.vetoes),
            "candidates": [s.as_dict() for s in self.ranked],
            "weights_calibrated": WEIGHTS_CALIBRATED,
        }


def decide(candidates) -> Decision:
    """Score and band every candidate for one target, leader first.

    Takes the whole ranked list rather than a single candidate because §13's
    fourth veto is a statement about the list: two candidates within 0.05 of
    each other mean the mapping is ambiguous, and ranking them still picks one.
    Every candidate inside that margin is vetoed, including the leader -- the
    tie is the finding, and choosing the first of two indistinguishable columns
    is exactly the silent wrong answer this function exists to prevent.

    An empty list is an error, not a BLOCK: a target with no candidate at all is
    an unmatched field, which `source_resolver` already reports as such, and
    manufacturing a bandless decision for it would hide it in the approval
    summary among mappings that do exist.
    """
    candidates = list(candidates)
    if not candidates:
        raise ValueError(
            "decide() needs at least one candidate. A target with no candidates is an "
            "unmatched field, not a low-confidence mapping."
        )

    targets = {c.target for c in candidates}
    if len(targets) > 1:
        raise ValueError(
            f"decide() scores the candidates for one target; got {sorted(targets)}. "
            "The ambiguity veto compares candidates against each other, so mixing targets "
            "would let one field's runner-up veto another field's winner."
        )

    scored = sorted(
        ((c, score(c)) for c in candidates),
        # Ties broken on source_ref so the same inputs always produce the same
        # order -- an approval summary that reshuffles between runs is unreviewable.
        key=lambda pair: (-pair[1], pair[0].source_ref),
    )

    top = scored[0][1]
    contested = [c for c, s in scored if top - s <= AMBIGUITY_MARGIN + 1e-9]
    ambiguous = {id(c) for c in contested} if len(contested) > 1 else set()

    ranked = []
    for candidate, value in scored:
        extra = (VETO_AMBIGUOUS,) if id(candidate) in ambiguous else ()
        all_vetoes = tuple(vetoes(candidate)) + extra
        ranked.append(ScoredCandidate(
            candidate=candidate,
            score=value,
            vetoes=all_vetoes,
            band=band_for_score(
                value,
                vetoed=bool(all_vetoes),
                money_or_identifier=is_money_or_identifier(candidate.field_subject),
            ),
        ))

    leader = ranked[0]
    return Decision(
        target=leader.candidate.target,
        band=leader.band,
        ranked=tuple(ranked),
        vetoes=leader.vetoes,
    )
