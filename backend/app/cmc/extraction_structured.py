"""Reading the numbers out of a certificate of analysis, a specification, a
stability table or a batch record.

Deterministic first, by decision: the table's own grid decides which value
belongs to which test, and every value is COPIED by code rather than restated
by anything. A model may later be asked which column is which -- that is a
question about layout -- but no model is ever asked what a value is. The
difference matters because the failure modes are not comparable: a
mis-mapped column is a value in the wrong row, which a reviewer sees at once
in the grid; a restated value is a plausible number in the right row, which
nobody sees at all.

What the parser cannot place, it says so about. A header it cannot classify
produces no rows rather than a guess, and the document reports how many
values it yielded so a source that read as prose is visibly that rather than
silently empty.

Confidence is evidence, not opinion:
  1.00  a person typed it (set by the router, never here)
  0.95  a labelled column in a table whose headers were all recognised
  0.70  a column matched by position or by a loose header match
  below that the grid demands attention before the value can print
"""

import re
from datetime import date, datetime

from sqlalchemy import select

from app.cmc.values import limits_of, parse_criterion, parse_value
from app.models import (
    CmcBatch, CmcMaterial, CmcResult, CmcSpecification, CmcTest,
)

CONFIDENCE_LABELLED = 0.95
CONFIDENCE_LOOSE = 0.70

#: Header words that name the row's test rather than a value.
_TEST_HEADERS = ("test", "parameter", "attribute", "analysis", "quality attribute",
                 "test / parameter", "tests", "determination")
#: Header words that name the acceptance criterion.
_CRITERION_HEADERS = ("acceptance criteria", "acceptance criterion", "specification",
                      "limit", "limits", "spec", "requirement", "requirements",
                      "acceptance")
#: Header words that name the analytical procedure.
_METHOD_HEADERS = ("method", "method id", "procedure", "analytical procedure",
                   "test method", "method reference", "sop")
#: Header words that name a unit column.
_UNIT_HEADERS = ("unit", "units")
#: Header words that name a result column on a single-batch certificate.
_RESULT_HEADERS = ("result", "results", "observed", "observed value", "value",
                   "found", "reported", "analysis result")

#: "Batch B-2026-001", "Lot 12345", "B-001". Used to recognise a column header
#: that names a batch on a multi-batch table.
_BATCH_RE = re.compile(
    r"\b(?:batch|lot|b\.?no\.?|batch\s*no\.?|lot\s*no\.?)\b[\s:.#-]*([A-Za-z0-9][\w./-]*)",
    re.IGNORECASE)
#: A bare batch-looking token, for a column headed only "B-2026-001".
_BARE_BATCH_RE = re.compile(r"^[A-Za-z]{0,3}[-/]?\d{2,}[\w./-]*$")

#: "25C/60RH", "25 °C / 60 % RH", "40C/75RH", "Long term", "Accelerated".
#:
#: The lookarounds are load-bearing. Without the trailing `(?![A-Za-z])`, the
#: "01 C" inside "...Batch B-2026-001 Certificate of Analysis" matches, and
#: every release result on that certificate is filed under a storage condition
#: that does not exist -- a phantom column in the stability pivot and a
#: release value nobody can find. Without the leading one, the "26" of a batch
#: number can start a temperature.
_CONDITION_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})\s*°?\s*C(?![A-Za-z0-9])"
    r"(?:\s*[/±]?\s*(\d{1,2})\s*%?\s*RH)?", re.IGNORECASE)
_NAMED_CONDITIONS = {
    "long term": "long_term", "long-term": "long_term", "accelerated": "accelerated",
    "intermediate": "intermediate", "stress": "stress",
}

#: "0 months", "3M", "T=6", "Initial". Two forms: a number followed by a
#: month word, or the T= convention where the unit is implied.
_TIMEPOINT_RE = re.compile(
    r"(?:\bt\s*=\s*(\d+(?:\.\d+)?)\b"
    r"|(?:^|\b)(\d+(?:\.\d+)?)\s*(?:m|mo|month|months)\b)", re.IGNORECASE)
_INITIAL_WORDS = ("initial", "t0", "t=0", "release", "0m", "start")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower().rstrip(":")


def _classify(header: str) -> str | None:
    """What a column header names, or None when the parser cannot tell."""
    text = _norm(header)
    if not text:
        return None
    if text in _TEST_HEADERS or any(text.startswith(h) for h in _TEST_HEADERS):
        return "test"
    if text in _CRITERION_HEADERS or any(h in text for h in _CRITERION_HEADERS):
        return "criterion"
    if text in _METHOD_HEADERS or any(text.startswith(h) for h in _METHOD_HEADERS):
        return "method"
    if text in _UNIT_HEADERS:
        return "unit"
    if text in _RESULT_HEADERS or any(text.startswith(h) for h in _RESULT_HEADERS):
        return "result"
    return None


def batch_from_header(header: str) -> str | None:
    """The batch a column header names, if it names one."""
    text = str(header or "").strip()
    if not text:
        return None
    match = _BATCH_RE.search(text)
    if match:
        return match.group(1)
    # A storage condition and a timepoint both look like bare batch tokens
    # ("25C/60RH", "6M"). Read as a batch, a stability column would file every
    # timepoint under a batch nobody manufactured, so they are ruled out
    # before the loose pattern is allowed to claim anything.
    if condition_from_text(text) or timepoint_from_text(text) is not None:
        return None
    return text if _BARE_BATCH_RE.match(text) else None


def condition_from_text(text: str) -> str | None:
    """A storage condition in its canonical short form, or None.

    Canonical because the same condition is written five ways across a
    stability bundle and a pivot that treated them as five conditions would
    show a matrix full of holes. The ORIGINAL is not needed here: unlike a
    value, a condition is a key rather than a reported quantity.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = _norm(raw)
    for word, canonical in _NAMED_CONDITIONS.items():
        if word in lowered:
            return canonical
    match = _CONDITION_RE.search(raw)
    if not match:
        return None
    temperature, humidity = match.group(1), match.group(2)
    return f"{int(temperature)}C/{int(humidity)}RH" if humidity else f"{int(temperature)}C"


def timepoint_from_text(text: str):
    """A timepoint in months, or None. "Initial" is month zero, which is a
    real timepoint and not a missing one."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if _norm(raw) in _INITIAL_WORDS:
        return 0.0
    match = _TIMEPOINT_RE.search(raw)
    if match is None:
        return None
    return float(match.group(1) if match.group(1) is not None else match.group(2))


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%b %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _material_for(db, document) -> CmcMaterial:
    """The material this document is about.

    Named by the uploader where they said so. Where they did not, a
    placeholder is created from the document's type rather than the values
    being attached to nothing -- a result with no material cannot be compared
    with the specification that governs it, and the grid is where somebody
    corrects the attribution.
    """
    if document.material_id:
        material = db.get(CmcMaterial, document.material_id)
        if material is not None and material.org_id == document.org_id:
            return material
    kind = {"spec_ds": "drug_substance", "spec_dp": "drug_product",
            "spec_excipient": "excipient"}.get(document.doc_type, "drug_product")
    existing = db.scalar(select(CmcMaterial).where(
        CmcMaterial.cmc_project_id == document.cmc_project_id,
        CmcMaterial.kind == kind, CmcMaterial.deleted_at.is_(None)))
    if existing is not None:
        return existing
    material = CmcMaterial(
        org_id=document.org_id, cmc_project_id=document.cmc_project_id,
        kind=kind, name={"drug_substance": "Drug substance",
                         "drug_product": "Drug product",
                         "excipient": "Excipient"}.get(kind, "Material"))
    db.add(material)
    db.flush()
    return material


def _test_for(db, document, material, *, name: str, criterion: str | None,
              method: str | None, unit: str | None, spec_version_id: str | None,
              order: int) -> CmcTest | None:
    """The specification row a result belongs to, created on first sight.

    Matched on the normalised test name, so a certificate calling it "Assay
    (HPLC)" and a specification calling it "Assay" do not become two tests
    whose results never meet. Where a criterion is supplied it is recorded,
    including its machine-readable bounds -- but an unreadable criterion is
    stored as text with no bounds rather than as an invented limit.
    """
    clean = re.sub(r"\s+", " ", str(name or "").strip())
    if not clean:
        return None
    normalised = _norm(clean)
    for candidate in db.scalars(select(CmcTest).where(
            CmcTest.cmc_project_id == document.cmc_project_id,
            CmcTest.material_id == material.id)).all():
        if _norm(candidate.test_name) == normalised or \
                _norm(candidate.test_name).startswith(normalised) or \
                normalised.startswith(_norm(candidate.test_name)):
            test = candidate
            if criterion and not test.acceptance_criterion_text:
                lower, upper, operator = limits_of(parse_criterion(criterion))
                test.acceptance_criterion_text = str(criterion).strip()
                test.limit_lower, test.limit_upper, test.limit_operator = lower, upper, operator
                test.spec_version_id = test.spec_version_id or spec_version_id
            if method and not test.method_id:
                test.method_id = method
            if unit and not test.unit:
                test.unit = unit
            return test

    lower = upper = operator = None
    if criterion:
        lower, upper, operator = limits_of(parse_criterion(criterion))
    test = CmcTest(
        org_id=document.org_id, cmc_project_id=document.cmc_project_id,
        material_id=material.id, spec_version_id=spec_version_id,
        test_name=clean, method_id=method, unit=unit,
        acceptance_criterion_text=(str(criterion).strip() if criterion else None),
        limit_lower=lower, limit_upper=upper, limit_operator=operator,
        sort_order=order, source_document_id=document.id)
    db.add(test)
    db.flush()
    return test


def _batch_for(db, document, material, number: str) -> CmcBatch:
    clean = str(number or "").strip()
    existing = db.scalar(select(CmcBatch).where(
        CmcBatch.cmc_project_id == document.cmc_project_id,
        CmcBatch.material_id == material.id,
        CmcBatch.batch_number == clean))
    if existing is not None:
        return existing
    batch = CmcBatch(org_id=document.org_id, cmc_project_id=document.cmc_project_id,
                     material_id=material.id, batch_number=clean,
                     source_document_id=document.id)
    db.add(batch)
    db.flush()
    return batch


def _record_result(db, document, *, batch, test, value_text, confidence,
                   condition=None, timepoint=None, page=None, table_ref=None) -> int:
    """One value, stored as reported. Returns 1 if a row was written.

    A cell that already holds a value for this (batch, test, condition,
    timepoint) is a conflict rather than an update: two sources disagreeing
    about one result is a question for a person, and whichever the parser kept
    would be a number nobody chose.
    """
    value = parse_value(value_text, unit_hint=test.unit)
    if not value.text:
        return 0
    existing = db.scalar(select(CmcResult).where(
        CmcResult.cmc_project_id == document.cmc_project_id,
        CmcResult.batch_id == batch.id, CmcResult.test_id == test.id,
        CmcResult.storage_condition.is_(None) if condition is None
        else CmcResult.storage_condition == condition,
        CmcResult.timepoint_months.is_(None) if timepoint is None
        else CmcResult.timepoint_months == timepoint))
    if existing is not None:
        if _norm(existing.value_text) == _norm(value.text):
            return 0
        row = CmcResult(
            org_id=document.org_id, cmc_project_id=document.cmc_project_id,
            batch_id=batch.id, test_id=test.id, storage_condition=condition,
            timepoint_months=timepoint, source_document_id=document.id,
            page=page, table_ref=table_ref, extraction_confidence=confidence,
            conflict_with_id=existing.id, **value.as_row())
        db.add(row)
        db.flush()
        existing.conflict_with_id = row.id
        return 1

    db.add(CmcResult(
        org_id=document.org_id, cmc_project_id=document.cmc_project_id,
        batch_id=batch.id, test_id=test.id, storage_condition=condition,
        timepoint_months=timepoint, source_document_id=document.id,
        page=page, table_ref=table_ref, extraction_confidence=confidence,
        **value.as_row()))
    return 1


def _header_row(rows) -> tuple:
    """`(index, classifications)` for the row that labels the columns.

    The first row whose cells classify into anything recognised. Not assumed
    to be row zero: a certificate of analysis often opens with a title row
    spanning the table.
    """
    for index, row in enumerate(rows[:4]):
        classes = [_classify(cell) for cell in row]
        if any(c == "test" for c in classes) and any(
                c in ("criterion", "result") for c in classes):
            return index, classes
        if any(c == "test" for c in classes) and any(
                batch_from_header(cell) for cell in row):
            return index, classes
    return -1, []


def extract_structured(db, document, extraction) -> int:
    """Read every table of one document into the structured store.

    Returns how many values were stored -- zero is a real answer, and the
    document records it so a source that yielded nothing is visibly that.
    """
    material = _material_for(db, document)
    spec_version_id = None
    if document.doc_type in ("spec_ds", "spec_dp", "spec_excipient"):
        specification = CmcSpecification(
            org_id=document.org_id, cmc_project_id=document.cmc_project_id,
            material_id=material.id, source_document_id=document.id)
        db.add(specification)
        db.flush()
        spec_version_id = specification.id

    stored = 0
    for table in getattr(extraction, "tables", None) or ():
        stored += _read_table(db, document, material, table,
                              spec_version_id=spec_version_id)
    return stored


def _read_table(db, document, material, table, *, spec_version_id) -> int:
    rows = [[str(cell or "").strip() for cell in (row or ())]
            for row in (getattr(table, "rows", None) or ())]
    rows = [row for row in rows if any(row)]
    if len(rows) < 2:
        return 0

    header_index, classes = _header_row(rows)
    if header_index < 0:
        return 0
    header = rows[header_index]
    body = rows[header_index + 1:]
    page = getattr(table, "page", None)
    table_ref = getattr(table, "table_id", None)

    def column_of(kind):
        for index, klass in enumerate(classes):
            if klass == kind:
                return index
        return None

    test_column = column_of("test")
    criterion_column = column_of("criterion")
    method_column = column_of("method")
    unit_column = column_of("unit")
    if test_column is None:
        return 0

    # Which columns carry values, and what each one means. A column headed by
    # a batch number is that batch; one headed by a condition and a timepoint
    # is a stability cell; a bare "Result" column is the single batch this
    # certificate is about.
    value_columns = []
    for index, cell in enumerate(header):
        if index in (test_column, criterion_column, method_column, unit_column):
            continue
        batch_number = batch_from_header(cell)
        condition = condition_from_text(cell)
        timepoint = timepoint_from_text(cell)
        if batch_number or condition or timepoint is not None:
            value_columns.append((index, batch_number, condition, timepoint,
                                  CONFIDENCE_LABELLED))
        elif classes[index] == "result":
            value_columns.append((index, None, None, None, CONFIDENCE_LABELLED))
        elif not _norm(cell):
            continue
        else:
            # A header the parser could not classify. Its values are still
            # read, at a confidence that puts every one of them in front of a
            # person before it can print.
            value_columns.append((index, None, None, None, CONFIDENCE_LOOSE))
    # No value columns is not nothing: a specification table has only tests,
    # criteria and methods, and it is the source the whole test list comes
    # from. The row loop below still runs and still records the tests; it
    # simply writes no results, which is correct -- a specification states
    # what a batch must be, never what one was.

    # A table-level batch, condition or timepoint. Read from the caption AND
    # from whatever rows sit above the header, because that is where a
    # certificate of analysis states its batch: the file is titled "Certificate
    # of Analysis - Batch B-2026-001" on its first line and the column headers
    # come after. Looking only at the caption attributed every value on such a
    # file to no batch at all, which stored nothing.
    preamble = " ".join(cell for row in rows[:header_index] for cell in row if cell)
    caption = " ".join(filter(None, [table_ref or "",
                                     getattr(table, "title", "") or "", preamble]))
    caption_batch = batch_from_header(caption)
    # A storage condition is read from the caption of a STABILITY source only.
    # A certificate of analysis reports release results, which have no storage
    # condition by definition, so any temperature-looking text on one is prose
    # about the product rather than a condition its results were held at.
    caption_condition = (condition_from_text(caption)
                         if document.doc_type in ("stability_data", "stability_protocol")
                         else None)

    default_batch = None
    if caption_batch:
        default_batch = _batch_for(db, document, material, caption_batch)

    stored = 0
    for order, row in enumerate(body):
        if len(row) <= test_column:
            continue
        test_name = row[test_column]
        if not test_name.strip():
            continue
        criterion = row[criterion_column] if (
            criterion_column is not None and len(row) > criterion_column) else None
        method = row[method_column] if (
            method_column is not None and len(row) > method_column) else None
        unit = row[unit_column] if (
            unit_column is not None and len(row) > unit_column) else None
        test = _test_for(db, document, material, name=test_name, criterion=criterion,
                         method=method, unit=unit, spec_version_id=spec_version_id,
                         order=order)
        if test is None:
            continue

        for index, batch_number, condition, timepoint, confidence in value_columns:
            if len(row) <= index:
                continue
            cell = row[index]
            if not cell.strip():
                continue
            batch = (_batch_for(db, document, material, batch_number)
                     if batch_number else default_batch)
            if batch is None:
                # A specification has no batch: its rows define tests and
                # limits, and a value column on one is a nominal figure rather
                # than a measured result.
                continue
            stored += _record_result(
                db, document, batch=batch, test=test, value_text=cell,
                confidence=confidence,
                condition=condition or caption_condition,
                timepoint=timepoint, page=page, table_ref=table_ref)
    db.flush()
    return stored
