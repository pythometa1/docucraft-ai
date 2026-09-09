"""Turning an uploaded safety source into rows, and stopping.

The stopping is the design. §2's fourth principle says patient identifiers are
masked *before* content is indexed, embedded, or sent to any model, and §6
makes de-identification a blocking pipeline stage rather than a warning. M3 is
where that stage is built.

So this milestone's pipeline is deliberately short:

    queued -> parsing -> awaiting_deid

and it does not chunk, does not embed, and does not call a model. Nothing here
writes a `pv_chunks` row. A pipeline that indexed narratives now and masked
them at M3 would mean every case ingested in between had its patient names
embedded into a vector store, where deleting them later is a different and much
harder problem than never putting them there. `AWAITING_DEID` is a real gate
with nothing behind it yet, which is the honest state of the module.

Free text goes to `pv_case_originals` -- the access-controlled store that
nothing reads -- and NOT to `pv_case_narratives.raw_text_redacted`, whose name
says what belongs in it. The masked working copy does not exist until something
has masked it.

What this milestone does do is the structured half: an E2B export or a line
listing becomes `pv_cases`, `pv_case_events` and `pv_case_drugs`, with every
absent field recorded rather than defaulted. Nothing arrives confirmed:
`confirmed_by` is null on every row, so nothing counts anywhere until a person
says so in the M4 review grid.
"""

import hashlib
import threading

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    PvCase, PvCaseDrug, PvCaseEvent, PvCaseOriginal, PvDocument, PvProduct,
)
from app.safety import e2b, line_listing, registry
from app.storage import abs_path

#: The pipeline's states. `awaiting_deid` is where M2 ends and M3 begins.
QUEUED = "queued"
PARSING = "parsing"
AWAITING_DEID = "awaiting_deid"
DONE = "done"
FAILED = "failed"

#: Input types that carry case-level data and are therefore parsed rather than
#: chunked.
CASE_INPUTS = ("e2b_r3_xml", "line_listing", "cioms_form", "case_narrative_doc")

#: Input types whose free text must be masked before anything reads it. In
#: practice that is all of them: §6 lists site and investigator names alongside
#: patient ones, and those appear in study reports and authority
#: correspondence as readily as in a narrative.
NEEDS_DEID = CASE_INPUTS + ("document",)


class IngestFailed(Exception):
    """This source could not be read. Recorded on its own row."""


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set_status(db, document, status: str, *, error: str | None = None) -> None:
    document.processing_status = status
    document.error_message = error
    db.commit()


def _product_names(db, pv_product_id: str) -> list[str]:
    """What to call this product when deciding whether a drug in a case is it.

    An ICSR names a drug; it does not know whose portfolio the drug is in, so
    the match is made here against the names the product records.
    """
    product = db.get(PvProduct, pv_product_id)
    if product is None:
        return []
    return [n for n in (product.product_name, product.inn) if n]


# ------------------------------------------------------------------- storing

def _store_case(db, document, parsed: e2b.ParsedCase) -> PvCase:
    """One parsed case, written as it was read.

    Existing cases are UPDATED rather than duplicated when the file carries a
    newer version of one already held: a follow-up is the same case, and two
    rows for it would be counted twice in every tabulation. What is not done is
    merging two cases that merely look alike -- that is duplicate detection,
    it is M4, and it is never automatic.
    """
    existing = None
    if parsed.worldwide_case_id:
        existing = db.scalar(select(PvCase).where(
            PvCase.pv_product_id == document.pv_product_id,
            PvCase.worldwide_case_id == parsed.worldwide_case_id))

    if existing is not None:
        incoming = parsed.case_version or 0
        held = existing.case_version or 0
        if incoming and incoming < held:
            # An older version of a case already held. Ignored rather than
            # written: rolling a case backwards would undo a follow-up.
            return existing
        case = existing
    else:
        case = PvCase(org_id=document.org_id, pv_product_id=document.pv_product_id)
        db.add(case)

    for name in ("worldwide_case_id", "case_version", "report_source", "study_id",
                 "country_of_occurrence", "primary_reporter_qualification",
                 "initial_receipt_date", "latest_receipt_date", "case_outcome",
                 "patient_age", "patient_age_group", "patient_sex"):
        value = getattr(parsed, name)
        if value is not None:
            setattr(case, name, value)
    if parsed.local_case_ids:
        case.local_case_ids = parsed.local_case_ids
    # The three-way flags: only written when the file said something. Leaving
    # the column at its default is the difference between "not stated" and a
    # determination of "not serious".
    for name in ("is_medically_confirmed", "is_serious", "is_pregnancy_case"):
        value = getattr(parsed, name)
        if value is not None:
            setattr(case, name, value)
    if parsed.seriousness_criteria:
        case.seriousness_criteria = parsed.seriousness_criteria
    case.source_document_id = document.id
    case.imported_from = document.input_type
    case.deidentification_status = "pending"
    db.flush()

    # Events and drugs are replaced wholesale for this case: a follow-up
    # restates the whole case, and merging term-by-term would leave an event
    # the follow-up removed still counted.
    if existing is not None:
        for row in db.scalars(select(PvCaseEvent).where(
                PvCaseEvent.case_id == case.id)).all():
            db.delete(row)
        for row in db.scalars(select(PvCaseDrug).where(
                PvCaseDrug.case_id == case.id)).all():
            db.delete(row)
        db.flush()

    for event in parsed.events:
        db.add(PvCaseEvent(
            org_id=document.org_id, pv_product_id=document.pv_product_id,
            case_id=case.id, verbatim_term=event.verbatim_term,
            meddra_llt=event.meddra_llt, meddra_pt=event.meddra_pt,
            meddra_soc=event.meddra_soc, meddra_version=event.meddra_version,
            onset_date=event.onset_date, outcome=event.outcome,
            is_serious=bool(event.is_serious) if event.is_serious is not None else False,
            # A term with no MedDRA code is surfaced rather than dropped: an
            # uncoded event is invisible to every tabulation, and the grid is
            # where somebody codes it.
            coding_required=not bool(event.meddra_pt),
            suggested_by_system_json={}))
    for drug in parsed.drugs:
        db.add(PvCaseDrug(
            org_id=document.org_id, pv_product_id=document.pv_product_id,
            case_id=case.id, drug_name=drug.drug_name,
            is_company_product=drug.is_company_product, role=drug.role,
            dose=drug.dose, dose_unit=drug.dose_unit, frequency=drug.frequency,
            route=drug.route, indication=drug.indication,
            start_date=drug.start_date, end_date=drug.end_date,
            action_taken=drug.action_taken, dechallenge=drug.dechallenge,
            rechallenge=drug.rechallenge))

    if parsed.narrative:
        _store_original(db, document, case_id=case.id, kind="narrative",
                        content=parsed.narrative)
    db.flush()
    return case


def _store_original(db, document, *, case_id, kind: str, content: str) -> None:
    """Un-masked text, into the store that nothing reads.

    Not into `pv_case_narratives`: that table's column is called
    `raw_text_redacted` because redacted is what belongs in it, and there is
    nothing yet that redacts. Writing un-masked text there would make the
    column's name a lie for every caller that trusts it.
    """
    existing = db.scalar(select(PvCaseOriginal).where(
        PvCaseOriginal.case_id == case_id,
        PvCaseOriginal.kind == kind,
        PvCaseOriginal.source_document_id == document.id))
    if existing is not None:
        existing.content = content
        return
    db.add(PvCaseOriginal(
        org_id=document.org_id, pv_product_id=document.pv_product_id,
        case_id=case_id, kind=kind, content=content,
        source_document_id=document.id))


# ------------------------------------------------------------------ the parse

def _parse_e2b(db, document, data: bytes) -> int:
    cases = e2b.parse_icsr(
        data, product_names=_product_names(db, document.pv_product_id))
    for parsed in cases:
        _store_case(db, document, parsed)
    return len(cases)


def _parse_line_listing(db, document, path, mapping: dict) -> int:
    from app.docgen.extraction import extract

    if not mapping:
        raise IngestFailed(
            "a line listing needs a column mapping before it can be read; open the "
            "mapping step and say which column holds the case identifier")
    problems = line_listing.validate_mapping(mapping)
    if problems:
        raise IngestFailed("; ".join(problems))

    extraction = extract(str(path), mime_type=document.mime_type,
                         source_name=document.original_filename)
    tables = [t for t in extraction.tables if len(t.rows) >= 2]
    if not tables:
        raise IngestFailed("no table with a header row and at least one data row")
    table = max(tables, key=lambda t: len(t.rows))
    headers, rows = table.rows[0], table.rows[1:]

    result = line_listing.read_rows(
        headers, rows, mapping,
        product_names=_product_names(db, document.pv_product_id))
    for parsed in result.cases:
        _store_case(db, document, parsed)
    if result.skipped:
        # A refused row is reported on the document, not swallowed. Half a
        # listing loaded in silence is the failure mode this reports around.
        lines = [f"row {number}: {reason}"
                 for number, reason in sorted(result.skipped.items())[:10]]
        more = len(result.skipped) - len(lines)
        document.error_message = (
            f"{len(result.skipped)} of {result.rows_read} row(s) were not read. "
            + " · ".join(lines) + (f" · and {more} more" if more > 0 else ""))
    return len(result.cases)


def _parse_text_source(db, document, path) -> int:
    """A CIOMS form or a narrative document: its text, kept un-masked, in the
    store that nothing reads until M3 has masked it."""
    from app.docgen.extraction import extract

    extraction = extract(str(path), mime_type=document.mime_type,
                         source_name=document.original_filename)
    text = "\n\n".join(page.text for page in extraction.pages if page.text).strip()
    if not text:
        raise IngestFailed("no readable text")
    case = _store_case(db, document, e2b.ParsedCase(
        worldwide_case_id=None,
        unmapped={"worldwide_case_id":
                  "this source is free text; the case identifier has to be entered"}))
    _store_original(db, document, case_id=case.id, kind="document_text",
                    content=text)
    document.page_count = extraction.page_count
    return 1


def _parse_document(db, document, path) -> int:
    """A supporting document -- an RSI, a previous report, a study report.

    Its text is read so that the page count and the failure are known now
    rather than at M6, and then it stops: chunking and embedding belong after
    de-identification, and §6 puts investigator and site names in exactly these
    files.
    """
    from app.docgen.extraction import extract

    extraction = extract(str(path), mime_type=document.mime_type,
                         source_name=document.original_filename)
    document.page_count = extraction.page_count
    # `.strip()`, because a page of whitespace is not text. Without it a file
    # that extracted to nothing but blank lines was recorded as read, and the
    # first anybody would know is a section at M6 citing an empty source.
    if (not any((page.text or "").strip() for page in extraction.pages)
            and not extraction.tables):
        raise IngestFailed("no readable text or tables")
    return 0


# --------------------------------------------------------------- the pipeline

def ingest_document(document_id: str, *, mapping: dict | None = None) -> None:
    """Parse one uploaded source and leave it at the de-identification gate.

    Its own session because its own thread. A failure is recorded on the
    document's own row and never touches its siblings: forty line listings
    where one is malformed is thirty-nine loaded listings and one that says
    why.
    """
    from app.docgen.extraction import ExtractorUnavailable, UnsupportedSource

    db = SessionLocal()
    try:
        document = db.get(PvDocument, document_id)
        if document is None:
            return
        _set_status(db, document, PARSING)
        path = abs_path(document.blob_path)
        try:
            if document.input_type == "e2b_r3_xml":
                count = _parse_e2b(db, document, path.read_bytes())
            elif document.input_type == "line_listing":
                count = _parse_line_listing(db, document, path, mapping or {})
            elif document.input_type in ("cioms_form", "case_narrative_doc"):
                count = _parse_text_source(db, document, path)
            else:
                count = _parse_document(db, document, path)
        except (IngestFailed, e2b.E2bUnreadable, UnsupportedSource,
                ExtractorUnavailable) as exc:
            _set_status(db, document, FAILED, error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a parser bug is this file's failure
            _set_status(db, document, FAILED,
                        error=f"the source could not be read: {exc}")
            return

        document.case_count = count
        # The gate. Nothing was chunked and nothing was embedded, because
        # nothing has been masked. M3 is what moves a document past here.
        document.processing_status = (
            AWAITING_DEID if document.input_type in NEEDS_DEID else DONE)
        db.commit()
    finally:
        db.close()


def ingest_in_background(jobs: list) -> None:
    """One worker per batch, processing sources in sequence.

    Sequential like the CMC module's and for the same reason: two sources can
    describe the same case -- an ICSR and the line listing that summarises it --
    and deciding whether the incoming version is newer means reading what the
    other one already wrote. Run in parallel, both threads see no existing case,
    both insert, and the store holds the same case twice.
    """
    items = [(job["document_id"], job.get("mapping")) for job in jobs]

    def run() -> None:
        for document_id, mapping in items:
            try:
                ingest_document(document_id, mapping=mapping)
            except Exception:  # noqa: BLE001 - one bad file never stops the rest
                continue

    threading.Thread(target=run, daemon=True).start()


# ----------------------------------------------------------------- readiness

def readiness(documents, doc_type_keys) -> dict:
    """The upload checklist for the report types this product is producing.

    Computed from what was chosen, not fixed: a product doing only literature
    monitoring is not told it is missing a trial registry.
    """
    requirements = registry.requirements(doc_type_keys)
    present = {d.doc_type for d in documents
               if d.processing_status not in (FAILED,)}
    return {
        "required": [{"doc_type": key, "label": registry.DOC_TYPES.get(key, key),
                      "present": key in present}
                     for key in requirements["required"]],
        "recommended": [{"doc_type": key, "label": registry.DOC_TYPES.get(key, key),
                         "present": key in present}
                        for key in requirements["recommended"]],
        "missing_required": [key for key in requirements["required"]
                             if key not in present],
    }
