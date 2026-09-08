"""The checks that decide whether a dossier can be exported.

Findings are data, not prose. Every one carries a code that the export gate
and the Issues panel branch on, a severity, and a `detail` dict holding the
ids a reviewer needs to open the offending row. A check that could only
produce a sentence would be a check the gate cannot enforce and the UI cannot
link.

Three rules shape all of them.

* Nothing is repaired and nothing is filled in. A quantity this module cannot
  read is REPORTED as unreadable rather than dropped out of a total; a
  conformance claim it could not make is reported as unmade. The failure this
  file exists to prevent is a dossier that exports clean because a check
  quietly supplied the number it was missing.
* "Cannot tell" is neither a pass nor a failure. `app.cmc.limits` answers
  UNKNOWN for a criterion it could not reduce to bounds, and UNKNOWN becomes
  CONFORMANCE_UNCHECKED -- a finding of its own -- rather than being folded
  into OUT_OF_SPECIFICATION or discarded. Either fold states something about a
  batch that nobody actually determined.
* No reported value is re-parsed on its way into a message. Where a message
  quotes a result it quotes `value_text`, the string the source wrote. The
  Decimals here exist to compare and to project; none of them is ever printed
  as the value, because printing a parsed number is how "0.050" becomes "0.05"
  and a three-figure method claim silently becomes a two-figure one.

Every query scopes on org_id AND cmc_project_id (sections through their
deliverable, which is what carries the project). A QC pass that read one row
from a neighbouring tenant would put another sponsor's batch number in this
sponsor's findings list.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from functools import cached_property

from sqlalchemy import select

from app.cmc.limits import FAIL, UNKNOWN, compare
from app.cmc.values import limits_of, parse_criterion, parse_value
# The acronym grammar is the same grammar wherever a draft is scanned, and a
# second copy of it would drift from this one the first time a stoplist word
# was added to only one of them.
from app.csr.qc import collect_abbreviations
from app.docgen.markers import parse_data_needed
from app.models import (
    CmcBatch, CmcBatchFormula, CmcChunk, CmcDeliverable, CmcMaterial,
    CmcResult, CmcSection, CmcSectionDraft, CmcSite, CmcTest,
)

BLOCKER = "blocker"
WARNING = "warning"
INFO = "info"

#: Blockers first. The order a reviewer works the list in is the order that
#: decides whether they get to the export, so it is decided here rather than
#: by whichever screen happens to render it.
_SEVERITY_ORDER = {BLOCKER: 0, WARNING: 1, INFO: 2}

#: Finding codes. Constants rather than literals at the call sites, because
#: the export gate and the Issues panel both branch on them and a typo'd code
#: silently demotes a blocking finding into one nobody renders.
UNVERIFIED_DATA = "UNVERIFIED_DATA"
OUT_OF_SPECIFICATION = "OUT_OF_SPECIFICATION"
SPEC_INCONSISTENT = "SPEC_INCONSISTENT"
METHOD_UNTRACEABLE = "METHOD_UNTRACEABLE"
BATCH_FORMULA_ARITHMETIC = "BATCH_FORMULA_ARITHMETIC"
DATA_NEEDED = "DATA_NEEDED"
TABLE_UNRESOLVED = "TABLE_UNRESOLVED"
STABILITY_INCOMPLETE = "STABILITY_INCOMPLETE"
OUT_OF_TREND = "OUT_OF_TREND"
CONFORMANCE_UNCHECKED = "CONFORMANCE_UNCHECKED"
UNIT_DRIFT = "UNIT_DRIFT"
NAME_INCONSISTENT = "NAME_INCONSISTENT"
COMPENDIAL_CLAIM = "COMPENDIAL_CLAIM"
ABBREVIATIONS = "ABBREVIATIONS"

#: How far a %w/w column may total from 100 before it is arithmetic nobody can
#: defend. Half a percent absorbs the rounding a real batch formula does on
#: each line; a percent and a half is a missing component.
PERCENT_TOLERANCE = Decimal("0.5")

#: How far one line's units-per-batch may sit from the rest, relatively. A
#: batch formula rounds its per-unit quantities, so demanding exact agreement
#: would flag every formula ever typed; a tenth of a percent is far tighter
#: than any real rounding and far looser than a transposed digit.
_FACTOR_TOLERANCE = Decimal("0.001")

#: Where a method id has to be findable. A procedure lives in an SOP and its
#: evidence in a validation report; a method id mentioned nowhere in either is
#: a test the dossier cannot show anybody how to perform.
_METHOD_DOC_TYPES = ("method_sop", "method_val_report")

#: The rows each table builder actually prints. An unverified stability value
#: reported against a batch-analyses table is a finding a reviewer cannot act
#: on -- they open the section, see only release results, and learn to
#: distrust the check. A key this map does not know prints everything, because
#: under-reporting an unverified number is the failure the check exists to
#: prevent.
_RELEASE = "release_results"
_STABILITY = "stability_results"
_FORMULA = "formula_rows"
_ALL_ROWS = (_RELEASE, _STABILITY, _FORMULA)

_TABLE_ROWS = {
    # A specification table prints limits, and a limit carries no signature to
    # check -- it is the specification, not a measurement of a batch.
    "spec_table": (),
    "site_list": (),
    "batch_analyses": (_RELEASE,),
    "impurity_table": (_RELEASE,),
    "stability_summary": (_STABILITY,),
    "stability_matrix": (_STABILITY,),
    "batch_formula": (_FORMULA,),
    "composition_table": (_FORMULA,),
}

#: `[TABLE: key]` anywhere in a draft, not only alone on its line. The export
#: resolves only the line-anchored form, so an inline marker is a marker that
#: reaches the document as literal text -- which is a defect worth naming
#: rather than a marker worth ignoring.
_TABLE_MARKER_RE = re.compile(r"\[TABLE:\s*([A-Za-z0-9_]+)\s*\]", re.IGNORECASE)

#: A pharmacopoeia named together with a monograph or chapter reference.
#:
#: The monograph half is required. Without it an in-house method id such as
#: "EP-001" reads as a European Pharmacopoeia claim, and a warnings list
#: padded with those is one a reviewer stops reading -- which loses the real
#: claims it exists to surface. An edition number ("USP 41") is deliberately
#: not a monograph: it dates the book rather than citing a test in it.
_PHARMACOPOEIA = r"(?:USP\s*-?\s*NF|USP|Ph\.?\s*Eur\.?|JP|BP|EP|NF)"
_MONOGRAPH = (r"(?:<[^>]{1,16}>"
              r"|\d+(?:\.\d+)+"
              r"|(?:monograph|chapter)(?:\s*<?\s*[\w.\-]{1,16}>?)?)")
_COMPENDIAL_RE = re.compile(rf"\b{_PHARMACOPOEIA}\s*[:,.\-]?\s*{_MONOGRAPH}",
                            re.IGNORECASE)


@dataclass
class CmcFinding:
    """One thing standing between this dossier and an export.

    `detail` carries the evidence and the ids, so the UI can link straight to
    the result row or the section rather than making a reviewer hunt for it.
    Keep everything in it JSON-safe: this crosses the wire, and a Decimal in
    there is a 500 rather than a finding.
    """

    code: str
    severity: str
    message: str
    section_code: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "section_code": self.section_code,
            "detail": dict(self.detail),
        }


# ------------------------------------------------------------------ the store


@dataclass
class _Store:
    """Everything the checks read, fetched once.

    A check per query would run the same six SELECTs for every section of a
    dossier that has two hundred of them, and the grids this module is sized
    for (50 batches x 30 tests x 8 timepoints x 3 conditions) are exactly
    where that stops being free.
    """

    sections: list
    drafts: dict
    materials: list
    tests: list
    batches: list
    results: list
    formula_rows: list
    sites: list
    method_text: str

    # Cached, not recomputed. A grid this module is sized for holds tens of
    # thousands of results, and a lookup dict rebuilt inside the loop over them
    # turns every check that names a batch into quadratic work.
    @cached_property
    def test_by_id(self) -> dict:
        return {test.id: test for test in self.tests}

    @cached_property
    def batch_by_id(self) -> dict:
        return {batch.id: batch for batch in self.batches}

    @cached_property
    def material_by_id(self) -> dict:
        return {material.id: material for material in self.materials}

    @cached_property
    def data_sections(self) -> list:
        """Enabled, applicable sections that print stored rows.

        A section marked not applicable or covered by a DMF prints nothing, so
        an unverified value cannot reach a reader through it -- blocking the
        export on one would be a gate on a document that does not exist.
        """
        return [section for section in self.sections
                if section.enabled and section.applicability == "applicable"
                and section.table_key]

    @cached_property
    def enabled_sections(self) -> list:
        return [section for section in self.sections if section.enabled]


def _load(db, *, cmc_project_id: str, org_id: str) -> _Store:
    deliverable_ids = list(db.scalars(select(CmcDeliverable.id).where(
        CmcDeliverable.org_id == org_id,
        CmcDeliverable.cmc_project_id == cmc_project_id,
    )).all())

    sections: list = []
    drafts: dict = {}
    if deliverable_ids:
        sections = list(db.scalars(select(CmcSection).where(
            CmcSection.org_id == org_id,
            CmcSection.cmc_deliverable_id.in_(deliverable_ids),
        ).order_by(CmcSection.sort_order)).all())
        section_ids = [section.id for section in sections]
        if section_ids:
            # Ascending version, last write wins: the latest draft is the one
            # a reviewer is looking at and the one the export will assemble.
            for draft in db.scalars(select(CmcSectionDraft).where(
                CmcSectionDraft.org_id == org_id,
                CmcSectionDraft.cmc_section_id.in_(section_ids),
            ).order_by(CmcSectionDraft.version)).all():
                drafts[draft.cmc_section_id] = draft

    def _rows(model):
        return list(db.scalars(select(model).where(
            model.org_id == org_id,
            model.cmc_project_id == cmc_project_id,
        )).all())

    method_text = "\n".join(db.scalars(select(CmcChunk.content).where(
        CmcChunk.org_id == org_id,
        CmcChunk.cmc_project_id == cmc_project_id,
        CmcChunk.doc_type.in_(_METHOD_DOC_TYPES),
    )).all())

    return _Store(
        sections=sections,
        drafts=drafts,
        materials=_rows(CmcMaterial),
        tests=_rows(CmcTest),
        batches=_rows(CmcBatch),
        results=_rows(CmcResult),
        formula_rows=_rows(CmcBatchFormula),
        sites=_rows(CmcSite),
        method_text=method_text,
    )


# ----------------------------------------------------------------- the grammar


def _number(raw) -> Decimal | None:
    """The Decimal a stored string asserts, through the module's one parser.

    Never `Decimal(raw)` directly: `app.cmc.values` is where the thousands
    separators, the operator prefixes and the refusal to guess at a decimal
    comma live, and a second reading of the same string is a second set of
    answers.
    """
    return parse_value(raw).number if raw is not None and str(raw).strip() else None


def _fold(text) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _fold_unit(text) -> str:
    """A unit compared without its typography. "mg / mL" and "mg/mL" are one
    unit written two ways, and reporting that as drift teaches reviewers to
    ignore the check that also catches mg against g."""
    return "".join(str(text or "").split()).lower()


def _fold_name(text) -> str:
    """A batch number or site name reduced to what identifies it. "B-2026-001"
    and "b 2026 001" are the same batch typed twice."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _mentions(haystack: str, token: str) -> bool:
    """Whether a method id actually appears, as itself.

    Bounded on both sides, because a plain substring test finds "GC-1" inside
    "GC-10" and reports a method as traceable to a procedure that describes a
    different one.
    """
    if not token:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
    return re.search(pattern, haystack or "", re.IGNORECASE) is not None


def _months(value) -> str:
    """A timepoint as a person writes it: 6 rather than 6.0."""
    if value is None:
        return "none"
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:
        return str(value)


def _ratio(value: Decimal) -> str:
    """A derived ratio, shortened for a message.

    This is the only rounding in the module and it is safe: a units-per-batch
    factor is computed here from two stored strings and is never a reported
    value, never stored, and never printed into a document.
    """
    try:
        return format(round(value, 6).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _named(items, limit: int = 6) -> str:
    """A handful of names in a message and a count for the rest.

    A message listing ninety test names is one a reviewer scrolls past; the
    whole list is in `detail`, which is where the UI reads it from anyway.
    """
    items = list(items)
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", and {len(items) - limit} more"


def _bounds(test) -> tuple:
    """`(lower, upper)` as Decimals for a test, or Nones.

    The stored bounds first, because they are what a person confirmed in the
    grid. Falling back to re-reading the criterion text covers a test written
    before the bounds column was filled -- and when neither yields a bound,
    the answer is (None, None) and the caller says it could not check, rather
    than inventing one.
    """
    lower, upper = _number(test.limit_lower), _number(test.limit_upper)
    if lower is None and upper is None:
        low, high, _operator = limits_of(parse_criterion(test.acceptance_criterion_text or ""))
        lower, upper = _number(low), _number(high)
    return lower, upper


def _cell(store: _Store, result) -> tuple:
    """`(batch number, condition phrase, detail dict)` for one result cell.

    Every conformance finding has to name the batch, the test, the condition
    and the timepoint, because those four are what identifies the cell a
    reviewer must open. They are built in one place so no finding can name
    three of them.
    """
    batch = store.batch_by_id.get(result.batch_id)
    batch_number = batch.batch_number if batch else "an unrecorded batch"
    test = store.test_by_id.get(result.test_id)
    if result.storage_condition:
        phrase = (f"{result.storage_condition} at {_months(result.timepoint_months)} months"
                  if result.timepoint_months is not None
                  else f"{result.storage_condition}, timepoint not recorded")
    else:
        phrase = "release testing (no storage condition, no timepoint)"
    detail = {
        "result_id": result.id,
        "batch_id": result.batch_id,
        "batch_number": batch.batch_number if batch else None,
        "test_id": result.test_id,
        "test_name": test.test_name if test else None,
        "storage_condition": result.storage_condition,
        "timepoint_months": result.timepoint_months,
        "value_text": result.value_text,
    }
    return batch_number, phrase, detail


# ------------------------------------------------------------------- blockers


def _unverified_data(store: _Store) -> list:
    """Values nobody has signed for, feeding a section that prints them."""
    sections = store.data_sections
    if not sections:
        return []

    tests = store.test_by_id
    buckets: dict = {kind: [] for kind in _ALL_ROWS}
    for result in store.results:
        if result.verified_by:
            continue
        test = tests.get(result.test_id)
        kind = _STABILITY if result.storage_condition else _RELEASE
        buckets[kind].append(((test.test_name if test else "an unrecorded test"),
                              kind, result.id))
    for row in store.formula_rows:
        if row.verified_by:
            continue
        buckets[_FORMULA].append((row.component_name or "an unnamed component",
                                  _FORMULA, row.id))

    findings = []
    for section in sections:
        kinds = _TABLE_ROWS.get(section.table_key, _ALL_ROWS)
        items = [item for kind in kinds for item in buckets[kind]]
        if not items:
            continue
        names = sorted({name for name, _kind, _row_id in items})
        findings.append(CmcFinding(
            code=UNVERIFIED_DATA,
            severity=BLOCKER,
            section_code=section.section_code,
            message=(f"{section.section_code} {section.title} prints {len(items)} value"
                     f"{'' if len(items) == 1 else 's'} nobody has verified: {_named(names)}."),
            detail={
                "table_key": section.table_key,
                "section_id": section.id,
                "count": len(items),
                "names": names,
                "result_ids": [row_id for _n, kind, row_id in items if kind != _FORMULA],
                "batch_formula_ids": [row_id for _n, kind, row_id in items
                                      if kind == _FORMULA],
            },
        ))
    return findings


def _conformance(store: _Store) -> tuple:
    """`(out-of-specification blockers, unchecked warnings)`.

    Split at the source rather than filtered later, so there is no arrangement
    of this code in which an UNKNOWN verdict can reach the OOS list. A result
    the system could not compare must read as neither a pass nor a failure.
    """
    tests = store.test_by_id
    out_of_spec, unchecked = [], []
    for result in store.results:
        batch_number, phrase, detail = _cell(store, result)
        test = tests.get(result.test_id)
        if test is None:
            unchecked.append(CmcFinding(
                code=CONFORMANCE_UNCHECKED,
                severity=WARNING,
                message=(f"A result of {result.value_text!r} on {batch_number} at {phrase} "
                         "belongs to no test in the store, so it was compared with nothing."),
                detail=dict(detail, reason="the test row this result points at is missing"),
            ))
            continue

        verdict = compare(result.value_text, test.acceptance_criterion_text or "")
        detail = dict(detail,
                      acceptance_criterion_text=test.acceptance_criterion_text,
                      reason=verdict.reason)
        if verdict.outcome == FAIL:
            out_of_spec.append(CmcFinding(
                code=OUT_OF_SPECIFICATION,
                severity=BLOCKER,
                message=(f"{test.test_name} is out of specification on batch {batch_number}, "
                         f"{phrase}: {verdict.reason}."),
                detail=detail,
            ))
        elif verdict.outcome == UNKNOWN:
            unchecked.append(CmcFinding(
                code=CONFORMANCE_UNCHECKED,
                severity=WARNING,
                message=(f"{test.test_name} on batch {batch_number}, {phrase} was not "
                         f"checked against its acceptance criterion: {verdict.reason}."),
                detail=detail,
            ))
    return out_of_spec, unchecked


def _limit_display(test) -> str:
    lower, upper = test.limit_lower, test.limit_upper
    operator = test.limit_operator
    if lower is None and upper is None:
        return f"no bounds ({test.acceptance_criterion_text or 'no criterion'})"
    if operator == "between" or (lower is not None and upper is not None):
        return f"{lower} to {upper}"
    if upper is not None:
        return f"{operator or 'at most'} {upper}"
    return f"{operator or 'at least'} {lower}"


def _spec_inconsistent(store: _Store) -> list:
    """One test name specified two ways for the same material.

    Scoped to the material on purpose: an assay of the drug substance and an
    assay of the drug product are different tests with different limits and
    saying so would be noise. Two rows for the SAME material that disagree are
    a specification the dossier cannot state.
    """
    groups: dict = defaultdict(list)
    for test in store.tests:
        groups[(test.material_id, _fold(test.test_name))].append(test)

    materials = store.material_by_id
    findings = []
    for (material_id, _key), tests in groups.items():
        if len(tests) < 2:
            continue
        attributes = {
            "limits": sorted({_limit_display(test) for test in tests}),
            "unit": sorted({(test.unit or "(not set)") for test in tests}),
            "method id": sorted({(test.method_id or "(not set)") for test in tests}),
        }
        differences = {label: values for label, values in attributes.items()
                       if len(values) > 1}
        if not differences:
            continue
        material = materials.get(material_id)
        material_name = material.name if material else "an unrecorded material"
        clauses = "; ".join(f"{label} " + " vs ".join(repr(value) for value in values)
                            for label, values in differences.items())
        findings.append(CmcFinding(
            code=SPEC_INCONSISTENT,
            severity=BLOCKER,
            message=(f"{tests[0].test_name} is specified {len(tests)} ways for "
                     f"{material_name}: {clauses}."),
            detail={
                "material_id": material_id,
                "material_name": material_name,
                "test_name": tests[0].test_name,
                "test_ids": [test.id for test in tests],
                "differences": differences,
            },
        ))
    return findings


def _method_untraceable(store: _Store) -> list:
    """An in-house method id no procedure or validation report mentions.

    Compendial methods are exempt because their procedure is the monograph,
    which this system does not hold -- they are covered instead by
    COMPENDIAL_CLAIM, where a person confirms the monograph rather than the
    system pretending to have read it.
    """
    by_method: dict = defaultdict(list)
    for test in store.tests:
        method_id = (test.method_id or "").strip()
        if not method_id or _fold(test.method_type) == "compendial":
            continue
        by_method[method_id].append(test)

    findings = []
    for method_id in sorted(by_method):
        if _mentions(store.method_text, method_id):
            continue
        tests = by_method[method_id]
        names = sorted({test.test_name for test in tests})
        findings.append(CmcFinding(
            code=METHOD_UNTRACEABLE,
            severity=BLOCKER,
            message=(f"Method {method_id} is used by {_named(names)} and appears in no "
                     "method SOP or validation report, and is not declared compendial."),
            detail={
                "method_id": method_id,
                "test_ids": [test.id for test in tests],
                "test_names": names,
                "searched_doc_types": list(_METHOD_DOC_TYPES),
            },
        ))
    return findings


def _formula_group(rows: list, deliverable_id, tolerance: Decimal) -> list:
    findings = []
    base = {"cmc_deliverable_id": deliverable_id}

    # An unreadable quantity is reported, never skipped. Dropping it would
    # leave a total that looks arithmetically sound while missing a component.
    for row in rows:
        for column, raw in (("%w/w", row.percent_ww),
                            ("quantity per unit", row.quantity_per_unit),
                            ("quantity per batch", row.quantity_per_batch)):
            text = str(raw or "").strip()
            if not text or _number(text) is not None:
                continue
            findings.append(CmcFinding(
                code=BATCH_FORMULA_ARITHMETIC,
                severity=BLOCKER,
                message=(f"The batch formula line for {row.component_name} states its "
                         f"{column} as {text!r}, which is not a number this module can read, "
                         "so the formula arithmetic cannot be checked."),
                detail=dict(base, batch_formula_id=row.id,
                            component_name=row.component_name,
                            column=column, value=text, reason="unparseable"),
            ))

    stated = [(row, _number(row.percent_ww)) for row in rows
              if str(row.percent_ww or "").strip()]
    readable = [(row, number) for row, number in stated if number is not None]
    if readable:
        total = sum((number for _row, number in readable), Decimal(0))
        silent = len(rows) - len(stated)
        if abs(total - Decimal(100)) > tolerance:
            note = (f" {silent} of {len(rows)} lines state no %w/w."
                    if silent else "")
            findings.append(CmcFinding(
                code=BATCH_FORMULA_ARITHMETIC,
                severity=BLOCKER,
                message=(f"The batch formula %w/w totals {format(total, 'f')}, not 100 "
                         f"(tolerance {format(tolerance, 'f')}).{note}"),
                detail=dict(base, total=format(total, "f"),
                            tolerance=format(tolerance, "f"),
                            lines_without_percent=silent,
                            components=[{"batch_formula_id": row.id,
                                         "component_name": row.component_name,
                                         "percent_ww": row.percent_ww}
                                        for row, _number in readable]),
            ))

    # Per-unit times units-per-batch has to be per-batch, and the units per
    # batch has to be the same number on every line -- that factor IS the
    # batch size, and a formula stating two of them describes two batches.
    factors = []
    for row in rows:
        per_unit, per_batch = _number(row.quantity_per_unit), _number(row.quantity_per_batch)
        if per_unit is None or per_batch is None or per_unit == 0:
            continue
        try:
            factors.append((row, per_batch / per_unit))
        except (DivisionByZero, InvalidOperation):
            continue
    if len(factors) >= 2:
        def _close(one: Decimal, other: Decimal) -> bool:
            return abs(one - other) <= _FACTOR_TOLERANCE * abs(other)

        agreed, agreement = factors[0][1], -1
        for _row, candidate in factors:
            count = sum(1 for _other_row, other in factors if _close(other, candidate))
            if count > agreement:
                agreed, agreement = candidate, count
        outliers = [(row, factor) for row, factor in factors if not _close(factor, agreed)]
        if outliers:
            findings.append(CmcFinding(
                code=BATCH_FORMULA_ARITHMETIC,
                severity=BLOCKER,
                message=(f"The batch formula does not reconcile: {agreement} of "
                         f"{len(factors)} lines give {_ratio(agreed)} units per batch, but "
                         + "; ".join(f"{row.component_name} gives {_ratio(factor)}"
                                     for row, factor in outliers) + "."),
                detail=dict(base, units_per_batch=_ratio(agreed),
                            lines_agreeing=agreement, lines_compared=len(factors),
                            outliers=[{"batch_formula_id": row.id,
                                       "component_name": row.component_name,
                                       "quantity_per_unit": row.quantity_per_unit,
                                       "quantity_per_batch": row.quantity_per_batch,
                                       "units_per_batch": _ratio(factor)}
                                      for row, factor in outliers]),
            ))
    return findings


def _batch_formula(store: _Store, tolerance: Decimal) -> list:
    """The arithmetic a batch formula has to satisfy, per formula.

    Grouped by deliverable because one project may hold a formula for each of
    several products, and summing two of them together produces a total of
    200 % and a finding about a formula nobody wrote.
    """
    groups: dict = defaultdict(list)
    for row in store.formula_rows:
        groups[row.cmc_deliverable_id].append(row)
    findings = []
    for deliverable_id in sorted(groups, key=lambda value: value or ""):
        rows = sorted(groups[deliverable_id],
                      key=lambda row: (row.sort_order, row.component_name or ""))
        findings.extend(_formula_group(rows, deliverable_id, tolerance))
    return findings


def _data_needed(store: _Store) -> list:
    """Every gap the drafts declare out loud.

    A `[DATA NEEDED: ...]` marker is the generation engine reporting that the
    sources did not contain something the section needs. It blocks the export
    because a document that ships with one prints the bracket to a regulator.
    """
    findings = []
    for section in store.enabled_sections:
        draft = store.drafts.get(section.id)
        if draft is None:
            continue
        seen: set = set()
        for payload in parse_data_needed(draft.content or ""):
            text = payload.strip() or "unspecified"
            if text.lower() in seen:
                continue
            seen.add(text.lower())
            findings.append(CmcFinding(
                code=DATA_NEEDED,
                severity=BLOCKER,
                section_code=section.section_code,
                message=(f"{section.section_code} {section.title} needs data the sources do "
                         f"not contain: {text}"),
                detail={"section_id": section.id, "draft_id": draft.id,
                        "version": draft.version, "payload": text},
            ))
    return findings


def _tables(db, store: _Store, *, cmc_project_id: str, org_id: str) -> list:
    """`[TABLE: key]` markers that would reach the document unrendered."""
    anchored: dict = {}
    loose: dict = {}
    for section in store.enabled_sections:
        draft = store.drafts.get(section.id)
        if draft is None:
            continue
        content = draft.content or ""
        for match in _TABLE_MARKER_RE.finditer(content):
            key = match.group(1)
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.end())
            line_end = len(content) if line_end < 0 else line_end
            alone = not (content[line_start:match.start()].strip()
                         or content[match.end():line_end].strip())
            (anchored if alone else loose).setdefault(
                (section.id, key), (section, key, draft))
    markers = list(anchored.values())
    if not markers and not loose:
        return []

    def _unresolved(section, key, draft, reason: str) -> CmcFinding:
        return CmcFinding(
            code=TABLE_UNRESOLVED,
            severity=BLOCKER,
            section_code=section.section_code,
            message=f"[TABLE: {key}] in {section.section_code} cannot be rendered: {reason}.",
            detail={"section_id": section.id, "draft_id": draft.id,
                    "table_key": key, "reason": reason},
        )

    # A marker sharing its line with prose is never resolved: the splitter in
    # `app.cmc.export` matches the line-anchored form only, so this one is left
    # in the run of text and printed to a regulator as the literal characters
    # "[TABLE: key]". The key may name a perfectly good builder; the defect is
    # the position, so it is reported whether or not the table would render.
    findings = [_unresolved(section, key, draft,
                            "the marker shares its line with other text, so the export "
                            "leaves it in the document as literal characters rather than "
                            "resolving it")
                for section, key, draft in loose.values()]

    # Imported here rather than at module scope: the table builders read this
    # module's finding codes, and importing them at the top makes the two files
    # a cycle that fails at interpreter start rather than at call time.
    try:
        from app.cmc.tables import BUILDERS, TableUnavailable, render_table
    except ImportError as exc:
        # No builders at all is not "every table is fine". Reporting each
        # marker as unrenderable is the honest answer, and it blocks the
        # export rather than passing a document with holes in it.
        findings.extend(_unresolved(section, key, draft,
                                    f"the table builders could not be loaded ({exc})")
                        for section, key, draft in markers)
        return findings

    for section, key, draft in markers:
        if key not in BUILDERS:
            findings.append(_unresolved(
                section, key, draft,
                f"{key!r} names no table this module renders; one of "
                f"{', '.join(sorted(BUILDERS))}"))
            continue
        try:
            render_table(db, cmc_project_id=cmc_project_id, org_id=org_id,
                         table_key=key, deliverable_id=section.cmc_deliverable_id,
                         include_unverified=False)
        except (TableUnavailable, KeyError) as exc:
            # KeyError as well, because that is what the export gate catches:
            # a builder tripping over a gap in the store must produce the same
            # blocker here as it does there. Letting it escape would take all
            # fourteen checks down with it, and a reviewer would be looking at
            # an error page instead of the list of what to fix.
            findings.append(_unresolved(section, key, draft, str(exc)))
    return findings


# ------------------------------------------------------------------- warnings


def _stability_incomplete(store: _Store) -> list:
    """Holes in the stability matrix, measured against the study itself.

    The reference is what the OTHER batches at the same condition have, not a
    protocol this module does not hold: a pull that every batch but one has at
    9 months is a missing pull, and a timepoint nobody has scheduled is not a
    hole to invent.
    """
    tests = store.test_by_id
    batches = store.batch_by_id
    condition_timepoints: dict = defaultdict(set)
    condition_tests: dict = defaultdict(set)
    cell_timepoints: dict = defaultdict(set)
    cell_tests: dict = defaultdict(set)

    for result in store.results:
        if not result.storage_condition or result.timepoint_months is None:
            continue
        condition = result.storage_condition
        cell = (result.batch_id, condition)
        condition_timepoints[condition].add(result.timepoint_months)
        condition_tests[condition].add(result.test_id)
        cell_timepoints[cell].add(result.timepoint_months)
        cell_tests[cell].add(result.test_id)

    findings = []
    for batch_id, condition in sorted(cell_timepoints,
                                      key=lambda cell: (cell[1], cell[0] or "")):
        cell = (batch_id, condition)
        missing_timepoints = sorted(condition_timepoints[condition] - cell_timepoints[cell])
        missing_tests = sorted(
            (tests[test_id].test_name if test_id in tests else "an unrecorded test")
            for test_id in condition_tests[condition] - cell_tests[cell])
        if not missing_timepoints and not missing_tests:
            continue
        batch = batches.get(batch_id)
        batch_number = batch.batch_number if batch else "an unrecorded batch"
        clauses = []
        if missing_timepoints:
            clauses.append("no result at "
                           + _named([f"{_months(point)} months" for point in missing_timepoints]))
        if missing_tests:
            clauses.append("no " + _named(missing_tests))
        findings.append(CmcFinding(
            code=STABILITY_INCOMPLETE,
            severity=WARNING,
            message=(f"Batch {batch_number} at {condition} has "
                     + " and ".join(clauses)
                     + ", which other batches at that condition have."),
            detail={"batch_id": batch_id, "batch_number": batch.batch_number if batch else None,
                    "storage_condition": condition,
                    "missing_timepoints": missing_timepoints,
                    "missing_tests": missing_tests},
        ))
    return findings


def _out_of_trend(store: _Store) -> list:
    """A stability series heading for its limit, flagged and nothing more.

    The projection is a straight line through the first and last timepoint,
    named as such in the message and the detail. It is not an ICH Q1E analysis
    and this module does not pretend it is one: the finding says a
    statistician decides, because a shelf-life conclusion drawn from three
    points by a QC pass is exactly the statistical claim the module is
    forbidden to make.
    """
    tests = store.test_by_id
    batches = store.batch_by_id
    series: dict = defaultdict(list)
    for result in store.results:
        if not result.storage_condition or result.timepoint_months is None:
            continue
        number = _number(result.value_text)
        if number is None:
            continue
        series[(result.batch_id, result.storage_condition, result.test_id)].append(
            (Decimal(str(result.timepoint_months)), number, result))

    findings = []
    for (batch_id, condition, test_id) in sorted(
            series, key=lambda key: (key[1], key[0] or "", key[2] or "")):
        points = sorted(series[(batch_id, condition, test_id)], key=lambda point: point[0])
        if len(points) < 3:
            continue
        values = [value for _time, value, _row in points]
        rising = all(later > earlier for earlier, later in zip(values, values[1:]))
        falling = all(later < earlier for earlier, later in zip(values, values[1:]))
        if not (rising or falling):
            continue
        test = tests.get(test_id)
        if test is None:
            continue
        lower, upper = _bounds(test)
        limit = upper if rising else lower
        if limit is None:
            continue

        first_time, first_value, first_row = points[0]
        last_time, last_value, last_row = points[-1]
        if last_time <= first_time or last_time <= 0:
            continue
        # Already past the limit is out of specification, which OUT_OF_SPECIFICATION
        # has already said; saying it again as a trend would double-count it.
        if (rising and last_value >= limit) or (falling and last_value <= limit):
            continue
        slope = (last_value - first_value) / (last_time - first_time)
        if slope == 0:
            continue
        crossing = last_time + (limit - last_value) / slope
        # Projected no further than one study-length beyond the last pull. A
        # line through two points says nothing about a limit forty months out,
        # and a warning that a batch might drift some time next decade is one
        # that buries the series actually heading out of specification.
        if crossing <= last_time or crossing > last_time * 2:
            continue

        batch = batches.get(batch_id)
        batch_number = batch.batch_number if batch else "an unrecorded batch"
        edge = "upper" if rising else "lower"
        findings.append(CmcFinding(
            code=OUT_OF_TREND,
            severity=WARNING,
            message=(f"{test.test_name} on batch {batch_number} at {condition} moves toward "
                     f"its {edge} limit of {format(limit, 'f')} at every timepoint "
                     f"({first_row.value_text} at {_months(first_time)} months to "
                     f"{last_row.value_text} at {_months(last_time)} months). A straight line "
                     f"through those two points reaches the limit near "
                     f"{_ratio(crossing)} months. Flagged only -- whether this is a real "
                     "trend, and what it means for shelf life, is a statistician's decision "
                     "and not this system's."),
            detail={
                "batch_id": batch_id,
                "batch_number": batch.batch_number if batch else None,
                "storage_condition": condition,
                "test_id": test_id,
                "test_name": test.test_name,
                "direction": "rising" if rising else "falling",
                "limit": format(limit, "f"),
                "limit_edge": edge,
                "method": "straight line through the first and last timepoint",
                "projected_crossing_months": _ratio(crossing),
                "points": [{"timepoint_months": float(time), "value_text": row.value_text,
                            "result_id": row.id}
                           for time, _value, row in points],
            },
        ))
    return findings


def _unit_drift(store: _Store) -> list:
    """A result reported in a unit its specification does not use.

    Only where both are stated. A result carrying no unit asserts nothing to
    drift from, and a test carrying none has no reference to drift against --
    both are gaps in the store rather than disagreements between two claims.
    """
    tests = store.test_by_id
    findings = []
    for result in store.results:
        test = tests.get(result.test_id)
        if test is None:
            continue
        result_unit, test_unit = (result.unit or "").strip(), (test.unit or "").strip()
        if not result_unit or not test_unit:
            continue
        if _fold_unit(result_unit) == _fold_unit(test_unit):
            continue
        batch_number, phrase, detail = _cell(store, result)
        findings.append(CmcFinding(
            code=UNIT_DRIFT,
            severity=WARNING,
            message=(f"{test.test_name} on batch {batch_number}, {phrase} is reported in "
                     f"{result_unit!r} where the specification states {test_unit!r}."),
            detail=dict(detail, result_unit=result_unit, test_unit=test_unit),
        ))
    return findings


def _name_inconsistent(store: _Store) -> list:
    """Two spellings of one batch number or one site name.

    Only spellings that differ by case, spacing or punctuation, because those
    are the ones that are certainly the same thing typed twice. "B-001" and
    "B-002" are two batches and no amount of similarity makes them one.
    """
    findings = []
    for label, key, rows in (("Batch number", "batch_number", store.batches),
                             ("Site name", "name", store.sites)):
        groups: dict = defaultdict(list)
        for row in rows:
            raw = getattr(row, key) or ""
            folded = _fold_name(raw)
            if folded:
                groups[folded].append((row.id, raw))
        for folded in sorted(groups):
            members = groups[folded]
            spellings = sorted({raw for _row_id, raw in members})
            if len(spellings) < 2:
                continue
            findings.append(CmcFinding(
                code=NAME_INCONSISTENT,
                severity=WARNING,
                message=(f"{label}s {', '.join(repr(spelling) for spelling in spellings)} "
                         "differ only in case, spacing or punctuation and are almost "
                         "certainly the same one."),
                detail={"kind": key, "spellings": spellings,
                        "row_ids": [row_id for row_id, _raw in members]},
            ))
    return findings


def _compendial_claim(store: _Store) -> list:
    """Every claim on a pharmacopoeia, put in front of a person.

    This system holds no monograph text. It has not opened USP <711> and
    cannot tell whether a product complies with it, so a criterion or a method
    that rests on one is reported as a claim to verify -- never as a claim
    checked. A dossier that implied otherwise would be asserting conformance
    to a document nobody read.
    """
    findings = []
    for test in store.tests:
        claims = []
        for field_name, text in (("acceptance_criterion_text", test.acceptance_criterion_text),
                                 ("method_id", test.method_id)):
            match = _COMPENDIAL_RE.search(str(text or ""))
            if match:
                claims.append({"field": field_name, "text": " ".join(match.group(0).split())})
        if _fold(test.method_type) == "compendial":
            claims.append({"field": "method_type", "text": "declared a compendial method"})
        if not claims:
            continue
        findings.append(CmcFinding(
            code=COMPENDIAL_CLAIM,
            severity=WARNING,
            message=(f"{test.test_name} rests on a compendial claim "
                     f"({_named([claim['text'] for claim in claims])}). This system holds no "
                     "pharmacopoeia text and has checked nothing against that monograph; a "
                     "person must verify it."),
            detail={"test_id": test.id, "test_name": test.test_name,
                    "material_id": test.material_id, "claims": claims},
        ))

    for material in store.materials:
        match = _COMPENDIAL_RE.search(str(material.compendial_ref or ""))
        if not match:
            continue
        claim = " ".join(match.group(0).split())
        findings.append(CmcFinding(
            code=COMPENDIAL_CLAIM,
            severity=WARNING,
            message=(f"{material.name} is declared to meet {claim}. This system holds no "
                     "pharmacopoeia text and has checked nothing against that monograph; a "
                     "person must verify it."),
            detail={"material_id": material.id, "material_name": material.name,
                    "claims": [{"field": "compendial_ref", "text": claim}]},
        ))
    return findings


# ----------------------------------------------------------------------- info


def _abbreviations(store: _Store) -> list:
    texts = [draft.content or "" for draft in
             (store.drafts.get(section.id) for section in store.enabled_sections)
             if draft is not None]
    collected = collect_abbreviations(texts)
    if not collected:
        return []
    undefined = [entry["abbreviation"] for entry in collected if not entry["expansion"]]
    return [CmcFinding(
        code=ABBREVIATIONS,
        severity=INFO,
        message=(f"{len(collected)} abbreviations are used across the dossier; "
                 f"{len(undefined)} "
                 f"{'is' if len(undefined) == 1 else 'are'} never spelled out."),
        detail={"abbreviations": collected, "undefined": undefined},
    )]


# ------------------------------------------------------------------- the pass


def run_qc(db, *, cmc_project_id: str, org_id: str,
           percent_tolerance: Decimal = PERCENT_TOLERANCE) -> list:
    """Every finding standing between this dossier and an export, blockers first.

    The order is decided here and not by the screen that renders it: the list
    a reviewer works down is the list that decides whether they reach the
    export, and two surfaces sorting it differently is two different gates.
    """
    store = _load(db, cmc_project_id=cmc_project_id, org_id=org_id)
    out_of_spec, unchecked = _conformance(store)

    findings: list = []
    findings.extend(_unverified_data(store))
    findings.extend(out_of_spec)
    findings.extend(_spec_inconsistent(store))
    findings.extend(_method_untraceable(store))
    findings.extend(_batch_formula(store, percent_tolerance))
    findings.extend(_data_needed(store))
    findings.extend(_tables(db, store, cmc_project_id=cmc_project_id, org_id=org_id))
    findings.extend(_stability_incomplete(store))
    findings.extend(_out_of_trend(store))
    findings.extend(unchecked)
    findings.extend(_unit_drift(store))
    findings.extend(_name_inconsistent(store))
    findings.extend(_compendial_claim(store))
    findings.extend(_abbreviations(store))

    # A stable sort: severity decides, and within a severity the checks keep
    # the order they ran in, which is document order for anything anchored to
    # a section.
    return sorted(findings, key=lambda finding: _SEVERITY_ORDER.get(finding.severity, 9))
