"""The §22 instrumentation: what gets logged, and what the logs add up to.

Two jobs live here, and they are the same job seen from either end.

The first is the calibration log. `app/compiler/confidence.py` implements §13's
scoring function with seven weights and four bands, every one of them declared
in the module itself as a starting value rather than a measurement --
`WEIGHTS_CALIBRATED` is False and is meant to stay False until somebody fits
them against real reviewer decisions. §22 says where that fit's data comes from,
and says it twice: "Log every mapping suggestion with its score, its evidence
and the reviewer's decision. This is the only dataset that can ever calibrate
the weights in §13, and it cannot be reconstructed later." Nothing was logging
it. Every binding session an organisation had run so far -- every accept, every
correction, every rejection -- was a training row that existed for the length of
one HTTP response and was then gone. This module writes them down.

The second is measurement. §18's service level table opens with a warning that
every figure in it "is a design target or an arithmetic estimate. None of it is
a benchmark result on a real customer template estate," and yet those figures
are precisely the ones that end up on a slide. So the operations §18 names are
timed, and `slo_report` reports the target and the measurement in separate
fields with a status that can say `unmeasured`. A row nobody has ever exercised
says so, rather than inheriting the target's number and looking green.

Two rules run through the metric half of this module.

**Unavailable is not zero.** A metric whose input does not exist yet comes back
with `value: None` and a reason. Escaped error rate is the case that matters: an
organisation that has never had a defect reported and an organisation with no
way to report one both produce a numerator of zero, and printing "0.0 wrong
values per 10,000 fields" for the second is a lie that reads as an achievement.

**The doc's own ranking is encoded in the ordering.** §22: "Auto-map rate is the
vanity metric. Escaped error rate is the real one. Every roadmap decision,
threshold change and model swap should be evaluated against escaped errors first
and automation percentage second." So `compute_metrics` returns an ordered list
with escaped error rate at rank 1 and auto-map rate last and flagged, not a dict
whose iteration order is an accident of how it was typed.
"""

from __future__ import annotations

import math
import statistics
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from time import perf_counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compiler import confidence
from app.generation.renderers import OOXML_FILL
from app.models import (
    DocumentVersion, FieldDictionary, GeneratedDocument, GenerationJob,
    ManifestGeneration, OperationTiming, QaFailureLog, SuggestionLog,
    TemplateClusterMember, TemplateFile, TemplateManifest,
)
from app.qa.policy import BLOCKING, REGISTRY, WARNING

# --------------------------------------------------------------- vocabulary

#: What a reviewer did with a suggestion. `pending` is a real state and is
#: excluded from every rate computed here: a suggestion nobody has looked at yet
#: is not a rejection, and counting it as one would make the compiler look worse
#: the faster it works.
PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
EDITED = "edited"
DECISIONS = (PENDING, ACCEPTED, REJECTED, EDITED)
DECIDED = (ACCEPTED, REJECTED, EDITED)

#: Before approval a QA failure is the system working. After approval the same
#: failure is an escaped error, and §22 says which of those two numbers decides
#: whether the product survives contact with legal.
PRE_APPROVAL = "pre_approval"
POST_APPROVAL = "post_approval"

#: The check name for a wrong value that no registered check caught. It is not
#: in `app/qa/policy.py`'s registry precisely because nothing checked for it --
#: that is what makes it escaped.
ESCAPED_WRONG_VALUE = "escaped_wrong_value"

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"

#: How many source values are enough to call a column's type. One value is a
#: coincidence; the whole column is the measurement, but a sample keeps the
#: probe cheap on a 50,000-row workbook.
TYPE_PROBE_ROWS = 200

#: §22 asks for the rate per 10,000 fields, not per document. A letter with
#: forty fields and a letter with four are not comparable units of exposure.
ESCAPED_ERROR_BASIS = 10_000

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 3650

#: Days in a quarter, for §22's "manifest versions per template per quarter".
#: 365.25 / 4 -- a quarter is not 90 days, and the difference shows up as a 1.5%
#: bias in a number people compare quarter on quarter.
QUARTER_DAYS = 91.3125

#: How many timing rows a percentile is computed over. The most recent N rather
#: than all of them: a p95 taken over a year of history keeps reporting last
#: spring's hardware long after it was replaced.
TIMING_SAMPLE_LIMIT = 5000

#: SQL `IN (...)` has a practical size limit on several drivers, so long id
#: lists are asked for in chunks rather than in one statement.
_IN_CHUNK = 400


# ------------------------------------------------------------------ helpers

def _utcnow() -> datetime:
    """Naive UTC, to match what the ORM columns actually hold.

    `DateTime` here is timezone-naive on both SQLite and Postgres, so a value
    read back from the database has no tzinfo. Comparing one of those against an
    aware `datetime.now(timezone.utc)` raises, and it raises at the worst
    possible moment -- inside a metric, on real customer data, months after the
    code was written. Every comparison in this module is naive-to-naive.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _chunks(items: list, size: int = _IN_CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _percentile(values: list, q: float) -> float:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated so the reported p95 is a duration
    something actually took. An interpolated 1.47 s that no render ever took is
    harder to argue with than a real one, which is the wrong way round.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# ------------------------------------------------------- §18 operation timing

SINGLE_DOCX_RENDER = "single_docx_render"
PDF_OVERLAY_RENDER = "pdf_overlay_render"
BATCH_THROUGHPUT = "batch_1000_documents"
TEMPLATE_PARSE = "template_parse"
AGENTIC_COMPILE = "agentic_compile"
FAMILY_MATCH_LOOKUP = "family_match_lookup"


@dataclass(frozen=True)
class SloTarget:
    """One row of §18's service level table, with its own wording preserved.

    `target_text` and `rationale` are quoted rather than paraphrased because the
    point of the endpoint is to put the target next to the measurement; a
    paraphrase invites an argument about what the target was.
    """

    operation: str
    dimension: str
    target_ms: float
    target_text: str
    statistic: str
    rationale: str


#: §18, "Service level objectives". Generation availability and reproducibility
#: are in that table too and are deliberately absent here: neither is a duration,
#: and reporting them alongside timings would imply this endpoint measures them.
SLO_TARGETS: tuple[SloTarget, ...] = (
    SloTarget(
        SINGLE_DOCX_RENDER, "Single DOCX render, p95", 1_500, "< 1.5 s", "p95",
        "Deterministic path with no model call in the loop",
    ),
    SloTarget(
        PDF_OVERLAY_RENDER, "Single PDF overlay render, p95", 2_500, "< 2.5 s", "p95",
        "Page rasterisation and region validation dominate",
    ),
    SloTarget(
        BATCH_THROUGHPUT, "Batch of 1,000 documents", 600_000, "< 10 min wall clock",
        "projected wall clock for 1,000 documents",
        "Roughly 2 documents per second sustained across workers",
    ),
    SloTarget(
        TEMPLATE_PARSE, "Template parse and semantic model, p95", 30_000, "< 30 s", "p95",
        "Asynchronous; the user is not blocked on it",
    ),
    SloTarget(
        AGENTIC_COMPILE, "Agentic compilation of a new template", 300_000, "< 5 min", "p95",
        "Asynchronous; several model round trips plus retrieval",
    ),
    SloTarget(
        FAMILY_MATCH_LOOKUP, "Family match lookup, p95", 400, "< 400 ms", "p95",
        "Fingerprint comparison plus tenant-filtered vector search",
    ),
)

SLO_BY_OPERATION = {t.operation: t for t in SLO_TARGETS}

#: §18's own health warning, carried in the payload so it travels with the
#: numbers instead of being left behind in the document nobody opens.
SLO_CAVEAT = (
    "Every target in §18 is a design target or an arithmetic estimate, not a benchmark "
    "result on a real customer template estate. Compare a target against a measurement "
    "with a sample size, and do not quote either to a customer until the sample is "
    "representative of their templates and hardware."
)


def record_timing(
    db: Session,
    *,
    org_id: str,
    operation: str,
    duration_ms: float,
    unit_count: int = 1,
    outcome: str = OUTCOME_OK,
) -> OperationTiming:
    """Write one measurement. The caller owns the commit.

    Adding rather than committing matters: a timing recorded inside a preview
    that is deliberately rolled back must roll back with it, and a timing taken
    during a batch must land on the same commit as the document it measured, so
    the two can never disagree about whether the work happened.
    """
    if operation not in SLO_BY_OPERATION:
        raise ValueError(
            f"Unknown timed operation {operation!r}. §18's SLO table is the closed set this "
            f"module reports against: {', '.join(sorted(SLO_BY_OPERATION))}. Timing something "
            "outside it means adding the row to SLO_TARGETS with its target, not inventing a "
            "name that the SLO report will never look at."
        )
    if unit_count < 1:
        raise ValueError(f"unit_count must be at least 1, got {unit_count!r}.")
    row = OperationTiming(
        org_id=org_id, operation=operation, duration_ms=float(duration_ms),
        unit_count=int(unit_count), outcome=outcome,
    )
    db.add(row)
    return row


@contextmanager
def timed(db: Session, *, org_id: str, operation: str, unit_count: int = 1):
    """Time the block, record it, and never change what the block does.

    A failed run is still timed, and is then excluded from the percentiles: how
    long a crash took is not how long the operation takes. It is kept rather
    than dropped because a rising error count next to a healthy p95 is a
    different story from a rising p95, and only one of them is a latency
    problem.
    """
    started = perf_counter()
    outcome = OUTCOME_OK
    try:
        yield
    except BaseException:
        outcome = OUTCOME_ERROR
        raise
    finally:
        record_timing(
            db, org_id=org_id, operation=operation,
            duration_ms=(perf_counter() - started) * 1000.0,
            unit_count=unit_count, outcome=outcome,
        )


# -------------------------------------------------- the §13 evidence bridge

#: Date formats a source column is probed against. Deliberately short: the point
#: is to tell a date column from a free-text one, not to parse every calendar
#: convention. Anything not recognised falls through to text, which is the
#: conservative answer -- text filling a declared date field is a §13 type
#: mismatch, and a mismatch means a human looks at it.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%dT%H:%M:%S")


def _is_numeric(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return False
    for symbol in ("$", "£", "€", "¥", "%"):
        text = text.replace(symbol, "")
    try:
        float(text)
    except ValueError:
        return False
    return True


def _is_date(value) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    text = str(value).strip()
    if not text:
        return False
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
        except ValueError:
            continue
        return True
    return False


def observed_column_type(values) -> str | None:
    """The *measured* type of a source column's values, or None if unprobed.

    `confidence.compare_types` is explicit about what it wants here, and about
    what goes wrong if it is given the wrong thing: "Every column in a CSV is
    text on disk; reporting it that way here would veto every mapping in the
    estate, and reporting it as the declared type would defeat the gate." So
    this reads the values, not the file format, and returns None for a column
    with nothing in it -- UNKNOWN withholds both the §13 weight and the §13
    veto, which is the honest answer when nothing was measured.
    """
    seen = [v for v in list(values)[:TYPE_PROBE_ROWS] if v not in (None, "")]
    if not seen:
        return None
    if all(_is_numeric(v) for v in seen):
        return "number"
    if all(_is_date(v) for v in seen):
        return "date"
    return "text"


def column_types(records: list, columns=None) -> dict:
    """`observed_column_type` for every column in a sample of records."""
    names = list(columns) if columns is not None else sorted({k for r in records if isinstance(r, dict) for k in r})
    out: dict[str, str | None] = {}
    for name in names:
        if str(name).startswith("_"):
            continue  # `_row_index` and friends are the ingester's, not the customer's
        out[name] = observed_column_type(
            [r.get(name) for r in records[:TYPE_PROBE_ROWS] if isinstance(r, dict)]
        )
    return out


@dataclass(frozen=True)
class ScoredSuggestion:
    """A resolver suggestion after §13 has had its say."""

    object_id: str
    column: str | None
    method: str
    score: float
    band: str
    vetoes: tuple
    evidence: tuple


#: Which resolver tiers count as a §13 `exact_name_match`.
#:
#: `mergefield` is the column header literally being the MERGEFIELD code;
#: `exact_slug` is the header normalising onto the field id; `dictionary` is a
#: human-approved synonym fold, which §13's own wording ("after normalisation
#: and synonym folding") puts on exactly the same footing.
_NAME_MATCH_METHODS = frozenset({"mergefield", "exact_slug", "dictionary"})

# Two tiers deliberately contribute no signal at all, and both absences are the
# point rather than an omission.
#
# `fuzzy` is a SequenceMatcher ratio over two strings. §13's semantic signal is
# an embedding cosine with a 0.75 floor, and feeding a string ratio in through
# that door would inflate every score in the estate with evidence the record
# never described. A fuzzy match with no precedent therefore scores 0 and lands
# in BLOCK -- which is §13's stated day-one posture, not a bug to tune away.
#
# `llm` is the model's own proposal. §13 excludes a model's stated confidence
# twice over, and `confidence.Signal` refuses a signal named after one, so what
# gets scored is the independent evidence supporting the proposal. Where there
# is none, there is none.


def score_suggestion(
    *,
    object_id: str,
    column: str | None,
    method: str,
    target_field: dict | None = None,
    observed_type: str | None = None,
    approvals: int = 0,
) -> ScoredSuggestion:
    """Turn one resolver suggestion into a §13 score, band, vetoes and evidence.

    `approvals` is how many times *this exact pair* has been confirmed before,
    not how often the field has been bound to anything -- §13's saturating
    precedent signal means something quite different from a usage counter.
    """
    subject = dict(target_field) if target_field else {}
    if subject:
        subject.setdefault("id", object_id)
    field_subject = subject or object_id

    if not column:
        # `confidence.decide` refuses an empty candidate list on purpose: "a
        # target with no candidate at all is an unmatched field, not a
        # low-confidence mapping". That is exactly this row, so it is banded
        # directly rather than by manufacturing a candidate nothing proposed.
        return ScoredSuggestion(
            object_id=object_id, column=None, method=method, score=0.0,
            band=confidence.band_for_score(
                0.0, vetoed=False,
                money_or_identifier=confidence.is_money_or_identifier(field_subject),
            ).value,
            vetoes=(), evidence=(),
        )

    matched = method in _NAME_MATCH_METHODS
    signals = [
        # The detail is attached only when the names actually matched. §13's
        # evidence list is read by a person, and "exact_name_match:fuzzy" on a
        # signal whose strength is zero reads as a name match that happened.
        confidence.exact_name_match(matched, detail=method if matched else ""),
        confidence.historical_approvals(approvals),
    ]
    candidate = confidence.Candidate(
        target=object_id,
        source_ref=column,
        signals=tuple(signals),
        target_field=subject or None,
        declared_type=(subject.get("type") if subject else None),
        source_type=observed_type,
    )
    # One candidate rather than a ranked list, so §13's fourth veto -- a second
    # candidate within 0.05 -- has nothing to fire on. The resolver hands back a
    # single column per target; when it starts returning alternatives, the whole
    # list should come through here and `decide` will find the ties itself.
    decision = confidence.decide([candidate])
    leader = decision.leader
    return ScoredSuggestion(
        object_id=object_id, column=column, method=method,
        score=round(leader.score, 4), band=leader.band.value,
        vetoes=tuple(leader.vetoes), evidence=tuple(candidate.evidence()),
    )


def _precedent_for(db: Session, *, org_id: str, object_id: str, column: str) -> int:
    """How many times a human has already confirmed this exact mapping.

    The calibration log is its own precedent store: an accepted suggestion, or
    one a reviewer edited *to* this column, is a human having said yes to this
    pair. Where the log knows nothing but the org field dictionary already
    carries the alias, that counts for exactly one -- a mapping confirmed before
    this table existed is still a confirmation, and it is capped at one because
    the dictionary counts uses of the field, not approvals of the pair.
    """
    from app.compiler.rule_compiler import _slug

    rows = db.scalars(
        select(SuggestionLog).where(
            SuggestionLog.org_id == org_id,
            SuggestionLog.object_id == object_id,
            SuggestionLog.reviewer_decision.in_([ACCEPTED, EDITED]),
        )
    ).all()
    approvals = sum(1 for r in rows if (r.final_column or r.suggested_column) == column)
    if approvals:
        return approvals

    entry = db.scalar(
        select(FieldDictionary).where(
            FieldDictionary.org_id == org_id, FieldDictionary.canonical_id == object_id
        )
    )
    if entry and _slug(column) in (entry.aliases or []):
        return 1
    return 0


# ------------------------------------------------------- the calibration log

def record_binding_suggestions(
    db: Session,
    *,
    org_id: str,
    manifest,
    plan,
    source_version_id: str | None,
    records: list | None = None,
    columns: list | None = None,
) -> list:
    """Log every suggestion this binding session produced. The caller commits.

    One row per object, pending until a reviewer acts. Re-opening the binding
    screen rewrites the pending rows in place -- the same suggestion shown twice
    is one suggestion -- and never touches a row that already carries a
    decision, because the decision is the datum the whole table exists for.

    Objects nothing matched are logged too, with a null column. Dropping them
    would take the hardest objects out of the denominator of the auto-map rate,
    and §22 already calls that one the vanity metric without any help.
    """
    fields_by_id = {f["id"]: f for f in (manifest.fields or []) if isinstance(f, dict) and f.get("id")}
    types = column_types(records or [], columns)

    existing = {
        row.object_id: row
        for row in db.scalars(
            select(SuggestionLog).where(
                SuggestionLog.org_id == org_id,
                SuggestionLog.manifest_id == manifest.id,
                SuggestionLog.source_version_id == source_version_id,
            )
        ).all()
    }

    written = []
    for suggestion in plan.suggestions:
        object_id = suggestion.field_id
        column = suggestion.column
        row = existing.get(object_id)
        if row is not None and row.reviewer_decision != PENDING:
            # Already decided. Restating the suggestion would overwrite the
            # evidence the reviewer actually saw with the evidence available now.
            continue

        # Prefer the numbers the reviewer was actually shown. Where the
        # resolver has already put the candidate through §13 -- it carries a
        # band -- those are the score, vetoes and evidence that were on the
        # screen, and re-deriving them here would log a number nobody saw and
        # calibrate the weights against a fiction. `score_suggestion` is the
        # fallback for a caller that has not scored its own suggestions.
        if getattr(suggestion, "band", None):
            scored = ScoredSuggestion(
                object_id=object_id,
                column=column,
                method=suggestion.method,
                score=round(float(getattr(suggestion, "score", suggestion.confidence)), 4),
                band=str(suggestion.band),
                vetoes=tuple(getattr(suggestion, "vetoes", ()) or ()),
                evidence=tuple(getattr(suggestion, "evidence", ()) or ()),
            )
        else:
            scored = score_suggestion(
                object_id=object_id,
                column=column,
                method=suggestion.method,
                target_field=fields_by_id.get(object_id),
                observed_type=types.get(column) if column else None,
                approvals=(
                    _precedent_for(db, org_id=org_id, object_id=object_id, column=column)
                    if column else 0
                ),
            )

        if row is None:
            row = SuggestionLog(
                org_id=org_id, manifest_id=manifest.id, source_version_id=source_version_id,
                object_id=object_id,
            )
            db.add(row)
            existing[object_id] = row
        row.suggested_column = column
        row.method = scored.method
        row.score = scored.score
        row.band = scored.band
        row.vetoes = list(scored.vetoes)
        row.evidence = list(scored.evidence)
        # Stamped per row, not inferred from the date: a fit over rows scored
        # under two different weight tables is a fit over nothing.
        row.weights_calibrated = confidence.WEIGHTS_CALIBRATED
        row.reviewer_decision = PENDING
        written.append(row)

    return written


def _decision_for(suggested: str | None, final: str | None) -> str:
    """What the reviewer did, read off the binding they saved.

    Only called for objects the reviewer actually addressed -- see
    `record_binding_decisions`. `accepted` and `rejected` are the labels a fit
    trains on, and `edited` carries the correct answer in `final_column`, which
    is the most informative row in the corpus.
    """
    if suggested and final and suggested == final:
        return ACCEPTED
    if final and suggested != final:
        return EDITED
    return REJECTED


def record_binding_decisions(
    db: Session,
    *,
    org_id: str,
    manifest_id: str,
    source_version_id: str,
    field_bindings: dict,
    decided_by: str,
) -> list:
    """Close out the pending suggestions for one binding with what the human did.

    Called when a binding is saved, because that is the moment the reviewer's
    judgement exists. Waiting for approval would lose every correction made and
    then re-corrected, which is the most informative part of the corpus.
    """
    # Every row for this binding, not only the ones still PENDING.
    #
    # `field_bindings` is a wholesale replace -- `upsert_binding` assigns it
    # rather than merging -- so it is the reviewer's complete answer, and an
    # object missing from it genuinely has no column. That makes absent a
    # rejection, which is a real label worth training on.
    #
    # But the binding screen autosaves. Closing out only PENDING rows would
    # freeze whatever the first autosave happened to catch: a suggestion the
    # reviewer had not reached yet is stamped `rejected`, and the accept they
    # made a second later can never correct it, because the row is no longer
    # pending. The corpus then teaches §13's weights that good suggestions were
    # refused -- in the estates where the reviewer worked slowest. Re-deciding
    # every row on every save is what keeps the log a record of the reviewer's
    # final answer rather than of their typing speed.
    rows = db.scalars(
        select(SuggestionLog).where(
            SuggestionLog.org_id == org_id,
            SuggestionLog.manifest_id == manifest_id,
            SuggestionLog.source_version_id == source_version_id,
        )
    ).all()

    decided_at = _utcnow()
    for row in rows:
        final = field_bindings.get(row.object_id) or None
        row.reviewer_decision = _decision_for(row.suggested_column, final)
        row.final_column = final
        row.decided_by = decided_by
        row.decided_at = decided_at
    return rows


# ------------------------------------------------------------ the QA log

def record_qa_findings(
    db: Session,
    *,
    org_id: str,
    findings,
    manifest_id: str | None = None,
    generation_id: str | None = None,
    document_version_id: str | None = None,
    phase: str = PRE_APPROVAL,
) -> list:
    """Log each QA finding structurally. The caller commits.

    `qa_notes` on the generation record already held these as prose, which meant
    "which check fired" could only be recovered by matching strings against a
    registry, and "how often does this check fire on this template" could not be
    answered at all. §22 asks for the check, the object, and whether a human
    overrode it; a list of sentences answers none of the three.
    """
    if phase not in (PRE_APPROVAL, POST_APPROVAL):
        raise ValueError(f"Unknown QA phase {phase!r}; expected {PRE_APPROVAL} or {POST_APPROVAL}.")

    written = []
    for finding in findings:
        check = finding.get("check") or ESCAPED_WRONG_VALUE
        severity = finding.get("severity") or BLOCKING
        if severity not in (BLOCKING, WARNING):
            raise ValueError(f"QA finding severity must be {BLOCKING} or {WARNING}, got {severity!r}.")
        row = QaFailureLog(
            org_id=org_id, manifest_id=manifest_id, generation_id=generation_id,
            document_version_id=document_version_id, check_name=check, severity=severity,
            object_id=finding.get("object_id"), detail=str(finding.get("detail") or ""),
            phase=phase,
        )
        db.add(row)
        written.append(row)
    return written


def record_qa_overrides(db: Session, *, document_version_id: str, user_id: str) -> list:
    """Mark the findings a human signed under when they approved this document.

    A blocking failure cannot be overridden -- `approve_document_version`
    refuses a blocked document outright -- so what is recorded here is the
    warning-severity findings that were on the document at the moment somebody
    put their name to it. That is the honest reading of §22's "whether a human
    overrode it": nobody switched the check off, they read it and proceeded.
    """
    rows = db.scalars(
        select(QaFailureLog).where(
            QaFailureLog.document_version_id == document_version_id,
            QaFailureLog.severity == WARNING,
            QaFailureLog.overridden.is_(False),
        )
    ).all()
    at = _utcnow()
    for row in rows:
        row.overridden = True
        row.overridden_by = user_id
        row.overridden_at = at
    return rows


def record_escaped_error(
    db: Session,
    *,
    org_id: str,
    document_version_id: str,
    object_id: str,
    detail: str,
    check_name: str = ESCAPED_WRONG_VALUE,
    manifest_id: str | None = None,
) -> QaFailureLog:
    """Record that a wrong value was found in a document that was already approved.

    This is the numerator of the one metric §22 calls "the number that decides
    whether the product survives contact with legal", and it is the only
    numerator that cannot be derived from anything the system already stores: a
    value is wrong because a person who knows the answer says so. Until one
    does, `escaped_error_rate` reports unavailable rather than zero.
    """
    if check_name != ESCAPED_WRONG_VALUE and check_name not in REGISTRY:
        raise ValueError(
            f"Unknown check {check_name!r}. An escaped error names either the registered check "
            f"that should have caught it ({', '.join(sorted(REGISTRY))}) or {ESCAPED_WRONG_VALUE!r} "
            "for a wrong value no check looks for."
        )
    if not str(detail).strip():
        raise ValueError(
            "An escaped error needs a description of what was wrong. It is a compliance record "
            "before it is a metric, and a count with no detail cannot be investigated."
        )
    row = QaFailureLog(
        org_id=org_id, manifest_id=manifest_id, document_version_id=document_version_id,
        check_name=check_name, severity=BLOCKING, object_id=object_id,
        detail=str(detail), phase=POST_APPROVAL,
    )
    db.add(row)
    return row


# ---------------------------------------------------------------- §22 metrics

@dataclass(frozen=True)
class Metric:
    """One §22 metric, or an honest statement of why it cannot be computed.

    A metric is either available with a value or unavailable with a reason.
    Constructing one with both, or with neither, raises: the failure this guards
    against is a metric that quietly returns 0.0 with an explanatory note
    attached that no dashboard renders.
    """

    key: str
    label: str
    definition: str
    direction: str
    unit: str
    rank: int
    value: float | None = None
    sample: dict = field(default_factory=dict)
    unavailable_reason: str | None = None
    note: str | None = None

    def __post_init__(self):
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError(
                f"Metric {self.key!r} must have exactly one of value and unavailable_reason. "
                "A metric with neither is a silent hole; a metric with both lets a dashboard "
                "print the number and drop the caveat, which is how 'no data' becomes 'zero'."
            )

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "key": self.key,
            "label": self.label,
            "definition": self.definition,
            "direction": self.direction,
            "unit": self.unit,
            "available": self.available,
            "value": self.value,
            "unavailable_reason": self.unavailable_reason,
            "sample": self.sample,
            "note": self.note,
        }


#: §22's table, in the order §22's own callout says to read it: "Escaped error
#: rate is the real one... Every roadmap decision, threshold change and model
#: swap should be evaluated against escaped errors first and automation
#: percentage second." The list is ordered, and auto-map rate is last.
_SPEC = {
    "escaped_error_rate": (
        "Escaped error rate",
        "Wrong values reaching an approved output per 10,000 fields",
        "Down, hard target",
        "wrong values per 10,000 fields",
    ),
    "time_to_first_correct_document": (
        "Time to first correct document",
        "Upload of a new template to the first QA-passing generated output",
        "Down",
        "seconds (median)",
    ),
    "reviewer_touches_per_template": (
        "Reviewer touches per template",
        "Manual decisions required to lock a manifest",
        "Down",
        "decisions per template",
    ),
    "qa_block_rate": (
        "QA block rate",
        "Generations blocked by a QA check",
        "Down after ramp",
        "share of generations",
    ),
    "regeneration_rate": (
        "Regeneration rate",
        "Documents regenerated after an issue is found",
        "Down",
        "share of documents",
    ),
    "family_reuse_rate": (
        "Family reuse rate",
        "New templates resolved through an existing family",
        "Up",
        "share of new templates",
    ),
    "manifest_churn": (
        "Manifest churn",
        "Manifest versions per template per quarter",
        "Watch",
        "manifest versions per template per quarter",
    ),
    "zero_llm_generation_share": (
        "Zero-LLM generation share",
        "Documents produced with no model call at all",
        "Up",
        "share of documents",
    ),
    "auto_map_rate": (
        "Auto-map rate",
        "Share of objects auto-accepted with no reviewer edit",
        "Up, slowly",
        "share of decided objects",
    ),
}

METRIC_ORDER = tuple(_SPEC)

#: Carried on the two metrics §22 singles out, so the ranking survives being
#: read out of a JSON payload by someone who has not read §22.
_NOTES = {
    "escaped_error_rate": (
        "§22 ranks this first: the number that decides whether the product survives contact "
        "with legal. Evaluate every threshold change and model swap against it before "
        "looking at automation."
    ),
    "auto_map_rate": (
        "§22: this is the vanity metric. It is reported last on purpose. A rise here that "
        "comes with a rise in escaped error rate is a regression, not progress."
    ),
}


def _metric(key: str, **kwargs) -> Metric:
    label, definition, direction, unit = _SPEC[key]
    return Metric(
        key=key, label=label, definition=definition, direction=direction, unit=unit,
        rank=METRIC_ORDER.index(key) + 1, note=_NOTES.get(key), **kwargs,
    )


def _escaped_error_rate(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    approved = db.scalars(
        select(DocumentVersion).where(
            DocumentVersion.org_id == org_id,
            DocumentVersion.status == "approved",
            DocumentVersion.approved_at.is_not(None),
            DocumentVersion.approved_at >= since,
            DocumentVersion.approved_at < until,
        )
    ).all()
    paths = sorted({dv.blob_path for dv in approved if dv.blob_path})

    generations = []
    for chunk in _chunks(paths):
        generations += db.scalars(
            select(ManifestGeneration).where(
                ManifestGeneration.org_id == org_id, ManifestGeneration.blob_path.in_(chunk)
            )
        ).all()
    fields_written = sum(len(g.field_lineage or []) for g in generations)

    escaped = select(func.count()).select_from(QaFailureLog).where(
        QaFailureLog.org_id == org_id, QaFailureLog.phase == POST_APPROVAL
    )
    escaped_ever = db.scalar(escaped) or 0
    escaped_in_window = db.scalar(
        escaped.where(QaFailureLog.created_at >= since, QaFailureLog.created_at < until)
    ) or 0

    sample = {
        "approved_document_versions": len(approved),
        "fields_in_approved_outputs": fields_written,
        "escaped_errors_in_window": escaped_in_window,
        "escaped_errors_ever": escaped_ever,
        "basis": ESCAPED_ERROR_BASIS,
    }

    if not approved:
        return _metric(
            "escaped_error_rate", sample=sample,
            unavailable_reason=(
                "No document was approved in this window, so there is nothing for a wrong value "
                "to have escaped into. This is not a rate of zero."
            ),
        )
    if fields_written == 0:
        return _metric(
            "escaped_error_rate", sample=sample,
            unavailable_reason=(
                f"{len(approved)} document version(s) were approved in this window but none of them "
                "carries field-level lineage, so the per-10,000-fields denominator cannot be counted. "
                "Only manifest-driven generation records lineage per field."
            ),
        )
    if not escaped_ever:
        return _metric(
            "escaped_error_rate", sample=sample,
            unavailable_reason=(
                "No wrong value has ever been reported against an approved output for this "
                "organisation. Zero here would mean 'nobody has reported one', which is not the "
                "same as 'none escaped', and §22 calls this the number that matters most -- so it "
                "is reported as unmeasured until the reporting path has been used at least once."
            ),
        )
    return _metric(
        "escaped_error_rate",
        value=round(escaped_in_window * ESCAPED_ERROR_BASIS / fields_written, 4),
        sample=sample,
    )


def _time_to_first_correct_document(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    templates = db.scalars(
        select(TemplateFile).where(
            TemplateFile.org_id == org_id,
            TemplateFile.deleted_at.is_(None),
            TemplateFile.created_at >= since,
            TemplateFile.created_at < until,
        )
    ).all()
    if not templates:
        return _metric(
            "time_to_first_correct_document", sample={"templates_uploaded": 0},
            unavailable_reason="No template was uploaded in this window, so no onboarding clock started.",
        )

    manifests = db.scalars(
        select(TemplateManifest).where(
            TemplateManifest.org_id == org_id,
            TemplateManifest.template_file_id.in_([t.id for t in templates]),
        )
    ).all() if templates else []
    by_template: dict[str, list] = {}
    for m in manifests:
        by_template.setdefault(m.template_file_id, []).append(m.id)

    # One grouped query rather than one per template. Bulk onboarding uploads
    # thousands of templates in a window, and a metric that costs a query each
    # is a metric somebody eventually turns off.
    #
    # No upper bound on the generation side: a template uploaded inside the
    # window whose first clean document lands tomorrow still measures that
    # template's onboarding. The clock belongs to the template, not to the
    # reporting window.
    first_pass: dict[str, datetime] = {}
    all_manifest_ids = [m.id for m in manifests]
    for chunk in _chunks(all_manifest_ids):
        for manifest_id, earliest in db.execute(
            select(ManifestGeneration.manifest_id, func.min(ManifestGeneration.created_at))
            .where(
                ManifestGeneration.org_id == org_id,
                ManifestGeneration.manifest_id.in_(chunk),
                ManifestGeneration.qa_passed.is_(True),
            )
            .group_by(ManifestGeneration.manifest_id)
        ).all():
            if earliest is not None:
                first_pass[manifest_id] = earliest

    durations = []
    for template in templates:
        candidates = [
            first_pass[manifest_id]
            for manifest_id in by_template.get(template.id) or []
            if manifest_id in first_pass
        ]
        if candidates:
            durations.append((min(candidates) - template.created_at).total_seconds())

    sample = {
        "templates_uploaded": len(templates),
        "templates_with_a_qa_passing_document": len(durations),
    }
    if not durations:
        return _metric(
            "time_to_first_correct_document", sample=sample,
            unavailable_reason=(
                f"{len(templates)} template(s) were uploaded in this window and none has produced a "
                "QA-passing document yet. The clock is still running, which is not the same as a "
                "time of zero."
            ),
        )
    sample.update({
        "fastest_seconds": round(min(durations), 1),
        "slowest_seconds": round(max(durations), 1),
    })
    return _metric(
        "time_to_first_correct_document",
        value=round(statistics.median(durations), 1), sample=sample,
    )


def _reviewer_touches_per_template(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    rows = db.scalars(
        select(SuggestionLog).where(
            SuggestionLog.org_id == org_id,
            SuggestionLog.reviewer_decision.in_(DECIDED),
            SuggestionLog.decided_at.is_not(None),
            SuggestionLog.decided_at >= since,
            SuggestionLog.decided_at < until,
        )
    ).all()
    # An auto-accepted mapping cost nobody anything. Counting it as a touch would
    # make a better compiler look like a heavier review burden.
    touches = [r for r in rows if r.band != confidence.Band.AUTO_ACCEPT.value]
    sample = {
        "decisions_recorded": len(rows),
        "manual_decisions": len(touches),
        "auto_accepted": len(rows) - len(touches),
    }
    if not rows:
        return _metric(
            "reviewer_touches_per_template", sample=sample,
            unavailable_reason=(
                "No mapping suggestion was decided in this window, so there is no review effort to "
                "divide across templates."
            ),
        )

    manifest_ids = sorted({r.manifest_id for r in touches})
    templates: set[str] = set()
    for chunk in _chunks(manifest_ids):
        for m in db.scalars(
            select(TemplateManifest).where(TemplateManifest.id.in_(chunk))
        ).all():
            templates.add(m.template_file_id or m.id)
    sample["templates_reviewed"] = len(templates)
    if not templates:
        return _metric(
            "reviewer_touches_per_template", sample=sample,
            unavailable_reason=(
                "Every decision in this window was an auto-accept, so no template required a manual "
                "decision. Reported as unmeasured rather than 0.0 because a denominator of zero "
                "templates is not an average of zero touches."
            ),
        )
    return _metric(
        "reviewer_touches_per_template",
        value=round(len(touches) / len(templates), 2), sample=sample,
    )


def _qa_block_rate(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    in_window = (
        ManifestGeneration.org_id == org_id,
        ManifestGeneration.created_at >= since,
        ManifestGeneration.created_at < until,
    )
    generations = db.scalar(
        select(func.count()).select_from(ManifestGeneration).where(*in_window)
    ) or 0
    blocked = db.scalar(
        select(func.count()).select_from(ManifestGeneration)
        .where(*in_window, ManifestGeneration.qa_passed.is_(False))
    ) or 0
    sample = {"generations": generations, "blocked": blocked}
    if not generations:
        return _metric(
            "qa_block_rate", sample=sample,
            unavailable_reason="Nothing was generated in this window, so no QA check ran.",
        )

    fired = (
        QaFailureLog.org_id == org_id,
        QaFailureLog.phase == PRE_APPROVAL,
        QaFailureLog.created_at >= since,
        QaFailureLog.created_at < until,
    )
    by_check = {
        name: count
        for name, count in db.execute(
            select(QaFailureLog.check_name, func.count()).where(*fired).group_by(QaFailureLog.check_name)
        ).all()
    }
    sample["by_check"] = dict(sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0])))
    sample["overridden_by_a_human"] = db.scalar(
        select(func.count()).select_from(QaFailureLog).where(*fired, QaFailureLog.overridden.is_(True))
    ) or 0
    return _metric("qa_block_rate", value=round(blocked / generations, 4), sample=sample)


def _regeneration_rate(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    in_window = (
        GeneratedDocument.org_id == org_id,
        GeneratedDocument.created_at >= since,
        GeneratedDocument.created_at < until,
    )
    documents = db.scalar(
        select(func.count()).select_from(GeneratedDocument).where(*in_window)
    ) or 0
    sample = {"documents": documents}
    if not documents:
        return _metric(
            "regeneration_rate", sample=sample,
            unavailable_reason="No document was produced in this window.",
        )

    # More than one version means the first output was not the one that was
    # issued -- a regeneration or a manual correction. Both mean QA let something
    # through, which is what §22 wants this number to be a proxy for.
    with_extra_versions = (
        select(DocumentVersion.document_id)
        .join(GeneratedDocument, GeneratedDocument.id == DocumentVersion.document_id)
        .where(*in_window)
        .group_by(DocumentVersion.document_id)
        .having(func.count(DocumentVersion.id) > 1)
        .subquery()
    )
    regenerated = db.scalar(select(func.count()).select_from(with_extra_versions)) or 0
    sample["regenerated"] = regenerated
    return _metric("regeneration_rate", value=round(regenerated / documents, 4), sample=sample)


def _family_reuse_rate(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    in_window = (
        TemplateFile.org_id == org_id,
        TemplateFile.deleted_at.is_(None),
        TemplateFile.created_at >= since,
        TemplateFile.created_at < until,
    )
    templates = db.scalar(select(func.count()).select_from(TemplateFile).where(*in_window)) or 0
    sample = {"templates_uploaded": templates}
    if not templates:
        return _metric(
            "family_reuse_rate", sample=sample,
            unavailable_reason="No template was uploaded in this window.",
        )

    matched = (
        select(func.count(func.distinct(TemplateClusterMember.template_file_id)))
        .select_from(TemplateClusterMember)
        .join(TemplateFile, TemplateFile.id == TemplateClusterMember.template_file_id)
        .where(*in_window, TemplateClusterMember.org_id == org_id)
    )
    considered = db.scalar(matched) or 0
    # A member that is not the representative is a template that took its
    # manifest from a family instead of being compiled from scratch.
    reused = db.scalar(matched.where(TemplateClusterMember.is_representative.is_(False))) or 0
    sample.update({
        "templates_put_through_family_matching": considered,
        "templates_resolved_through_a_family": reused,
    })
    if not considered:
        return _metric(
            "family_reuse_rate", sample=sample,
            unavailable_reason=(
                f"None of the {templates} template(s) uploaded in this window went through "
                "family matching at all, so a reuse rate of zero would describe the pipeline rather "
                "than the estate."
            ),
        )
    return _metric("family_reuse_rate", value=round(reused / templates, 4), sample=sample)


def _manifest_churn(db: Session, org_id: str, since: datetime, until: datetime, window_days: float) -> Metric:
    manifests = db.scalars(
        select(TemplateManifest).where(
            TemplateManifest.org_id == org_id,
            TemplateManifest.template_file_id.is_not(None),
            TemplateManifest.created_at >= since,
            TemplateManifest.created_at < until,
        )
    ).all()
    sample = {"manifest_versions": len(manifests), "window_days": window_days}
    if not manifests:
        return _metric(
            "manifest_churn", sample=sample,
            unavailable_reason="No manifest version was compiled in this window.",
        )
    by_template: dict[str, int] = {}
    for m in manifests:
        by_template[m.template_file_id] = by_template.get(m.template_file_id, 0) + 1
    per_template = len(manifests) / len(by_template)
    # §22 asks for versions per template per *quarter*, so a 30-day window is
    # projected rather than reported as though it were three months of history.
    scaled = per_template * (QUARTER_DAYS / window_days)
    sample.update({
        "templates": len(by_template),
        "versions_per_template_in_window": round(per_template, 3),
        "projected_to_days": QUARTER_DAYS,
    })
    return _metric("manifest_churn", value=round(scaled, 2), sample=sample)


def _zero_llm_generation_share(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    in_window = (
        DocumentVersion.org_id == org_id,
        DocumentVersion.created_at >= since,
        DocumentVersion.created_at < until,
    )
    counted = select(func.count()).select_from(DocumentVersion)
    versions = db.scalar(counted.where(*in_window)) or 0
    # A row with no renderer recorded predates the column and cannot be
    # classified either way. It is excluded from both sides and counted
    # separately, rather than being assumed to be the cheap path.
    classified = db.scalar(
        counted.where(*in_window, DocumentVersion.renderer.is_not(None))
    ) or 0
    zero_llm = db.scalar(
        counted.where(*in_window, DocumentVersion.renderer == OOXML_FILL)
    ) or 0
    sample = {
        "document_versions": versions,
        "classified": classified,
        "unclassified_no_renderer_recorded": versions - classified,
        "zero_llm": zero_llm,
    }
    if not classified:
        return _metric(
            "zero_llm_generation_share", sample=sample,
            unavailable_reason=(
                "No document version in this window records which renderer produced it, so whether a "
                "model was in the loop cannot be established."
            ),
        )
    return _metric(
        "zero_llm_generation_share",
        value=round(zero_llm / classified, 4), sample=sample,
    )


def _auto_map_rate(db: Session, org_id: str, since: datetime, until: datetime) -> Metric:
    counted = select(func.count()).select_from(SuggestionLog)
    in_window = (
        SuggestionLog.org_id == org_id,
        SuggestionLog.reviewer_decision.in_(DECIDED),
        SuggestionLog.decided_at.is_not(None),
        SuggestionLog.decided_at >= since,
        SuggestionLog.decided_at < until,
    )
    decided = db.scalar(counted.where(*in_window)) or 0
    auto = db.scalar(
        counted.where(
            *in_window,
            SuggestionLog.band == confidence.Band.AUTO_ACCEPT.value,
            SuggestionLog.reviewer_decision == ACCEPTED,
        )
    ) or 0
    pending = db.scalar(
        counted.where(
            SuggestionLog.org_id == org_id,
            SuggestionLog.reviewer_decision == PENDING,
            SuggestionLog.created_at >= since,
            SuggestionLog.created_at < until,
        )
    ) or 0
    sample = {
        "decided": decided,
        "auto_accepted_and_untouched": auto,
        "still_pending": pending,
        "weights_calibrated": confidence.WEIGHTS_CALIBRATED,
    }
    if not decided:
        return _metric(
            "auto_map_rate", sample=sample,
            unavailable_reason=(
                "No mapping suggestion has been reviewed in this window. A share of zero would say "
                "the compiler automated nothing; the truth is that nobody has decided anything yet."
            ),
        )
    return _metric("auto_map_rate", value=round(auto / decided, 4), sample=sample)


def compute_metrics(db: Session, *, org_id: str, window_days: int = DEFAULT_WINDOW_DAYS) -> list:
    """Every §22 metric for one organisation, ordered by how much it matters.

    Ordered, not keyed: the ranking is §22's, and a dict would leave it to
    whoever renders the payload.
    """
    if window_days < 1 or window_days > MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {MAX_WINDOW_DAYS}, got {window_days!r}.")
    until = _utcnow()
    since = until - timedelta(days=window_days)
    return [
        _escaped_error_rate(db, org_id, since, until),
        _time_to_first_correct_document(db, org_id, since, until),
        _reviewer_touches_per_template(db, org_id, since, until),
        _qa_block_rate(db, org_id, since, until),
        _regeneration_rate(db, org_id, since, until),
        _family_reuse_rate(db, org_id, since, until),
        _manifest_churn(db, org_id, since, until, float(window_days)),
        _zero_llm_generation_share(db, org_id, since, until),
        _auto_map_rate(db, org_id, since, until),
    ]


# --------------------------------------------------------------- §18 report

def _batch_projections(db: Session, org_id: str, since: datetime) -> list:
    """Each finished batch, projected onto §18's "batch of 1,000 documents".

    Jobs already record `started_at`, `finished_at` and `rows_total`, so batch
    throughput needs no separate instrumentation -- and a job's own wall clock is
    a better measurement than one taken inside the process, because it includes
    everything the customer waits for.
    """
    jobs = db.scalars(
        select(GenerationJob).where(
            GenerationJob.org_id == org_id,
            GenerationJob.started_at.is_not(None),
            GenerationJob.finished_at.is_not(None),
            GenerationJob.finished_at >= since,
        )
    ).all()
    projections = []
    for job in jobs:
        rows_total = (job.progress or {}).get("rows_total") or 0
        if rows_total <= 0:
            continue  # a job that never reported a row count cannot be normalised
        elapsed_ms = (job.finished_at - job.started_at).total_seconds() * 1000.0
        projections.append(elapsed_ms / rows_total * 1000.0)
    return projections


def slo_report(db: Session, *, org_id: str, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """§18's table with a measurement column, and an honest `unmeasured` state.

    The endpoint's whole job is to stop a design target being quoted as though it
    were a benchmark, so target and measurement never share a field and a row
    with no samples reports `unmeasured` rather than inheriting the target.
    """
    if window_days < 1 or window_days > MAX_WINDOW_DAYS:
        raise ValueError(f"window_days must be between 1 and {MAX_WINDOW_DAYS}, got {window_days!r}.")
    since = _utcnow() - timedelta(days=window_days)

    rows = []
    for target in SLO_TARGETS:
        if target.operation == BATCH_THROUGHPUT:
            samples = _batch_projections(db, org_id, since)
            errors = 0
        else:
            timings = db.scalars(
                select(OperationTiming)
                .where(
                    OperationTiming.org_id == org_id,
                    OperationTiming.operation == target.operation,
                    OperationTiming.created_at >= since,
                )
                .order_by(OperationTiming.created_at.desc())
                .limit(TIMING_SAMPLE_LIMIT)
            ).all()
            samples = [t.duration_ms for t in timings if t.outcome == OUTCOME_OK]
            errors = sum(1 for t in timings if t.outcome != OUTCOME_OK)

        row = {
            "operation": target.operation,
            "dimension": target.dimension,
            "target": target.target_text,
            "target_ms": target.target_ms,
            "statistic": target.statistic,
            "rationale": target.rationale,
            "measured": None,
            "status": "unmeasured",
            "failed_runs": errors,
        }
        if samples:
            headline = _percentile(samples, 0.95) if target.statistic == "p95" else statistics.median(samples)
            row["measured"] = {
                "samples": len(samples),
                "p50_ms": round(statistics.median(samples), 1),
                "p95_ms": round(_percentile(samples, 0.95), 1),
                "worst_ms": round(max(samples), 1),
                "headline_ms": round(headline, 1),
            }
            row["status"] = "meeting" if headline <= target.target_ms else "breaching"
        else:
            row["reason"] = (
                "No run of this operation has been timed in this window. The target is quoted from "
                "§18, not measured here."
            )
        rows.append(row)

    return {
        "caveat": SLO_CAVEAT,
        "window_days": window_days,
        "operations": rows,
        "measured_operations": sum(1 for r in rows if r["status"] != "unmeasured"),
    }


def calibration_status(db: Session, *, org_id: str) -> dict:
    """How far the §13 weights are from being calibrated, in rows.

    §13's weights stay declared guesses until this corpus is big enough to fit
    them, so the size of the corpus is itself a number worth watching -- and one
    the endpoint can report without anybody running a query by hand.
    """
    mine = SuggestionLog.org_id == org_id
    total = db.scalar(select(func.count()).select_from(SuggestionLog).where(mine)) or 0
    counted_by_decision = dict(
        db.execute(
            select(SuggestionLog.reviewer_decision, func.count())
            .where(mine).group_by(SuggestionLog.reviewer_decision)
        ).all()
    )
    # Every decision key is present even at zero. A missing key reads as "this
    # state does not exist" rather than "nobody has reached it yet".
    by_decision = {d: counted_by_decision.get(d, 0) for d in DECISIONS}
    by_band = dict(
        db.execute(
            select(SuggestionLog.band, func.count()).where(mine).group_by(SuggestionLog.band)
        ).all()
    )
    return {
        "weights_calibrated": confidence.WEIGHTS_CALIBRATED,
        "suggestions_logged": total,
        "by_decision": by_decision,
        "by_band": dict(sorted(by_band.items())),
        "note": (
            "The §13 weights are starting values, not measurements. They stay that way until they "
            "are fitted against these decisions; §22 is explicit that this dataset cannot be "
            "reconstructed after the fact."
        ),
    }
