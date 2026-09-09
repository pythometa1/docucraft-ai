"""Turning an uploaded safety source into rows, and masking them before
anything else can see them.

    queued -> parsing -> deidentifying -> [awaiting_deid] -> indexing -> done

The bracketed state is a gate, not a step. §2's fourth principle puts masking
before indexing, embedding or any model call, and §6 makes it blocking: a
detection the machine cannot settle stops the source where it is until a person
answers it. Nothing writes a `pv_chunks` row until that queue is empty, so an
identifier cannot reach the vector store while somebody is still deciding
whether it is one -- and an identifier removed from a store it never entered is
a problem that does not exist.

Free text is kept twice, on purpose. `pv_case_originals` holds what arrived:
access-controlled, read by nothing downstream, never exported.
`pv_case_narratives.raw_text_redacted` holds the masked working copy, which is
the only thing retrieval, drafting and export ever see. The column is named for
what belongs in it.

The structured half arrives unconfirmed. `confirmed_by` is null on every row,
so nothing counts anywhere until a person says so in the review grid.
"""

import hashlib
import threading

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    PvCase, PvCaseDrug, PvCaseEvent, PvCaseNarrative, PvCaseOriginal, PvChunk,
    PvDeidItem, PvDocument, PvProduct,
)
from app.safety import deident, e2b, line_listing, registry
from app.storage import abs_path

#: The pipeline's states. `awaiting_deid` is a gate: a source sits there while
#: a person answers the detections the machine would not settle.
QUEUED = "queued"
PARSING = "parsing"
DEIDENTIFYING = "deidentifying"
AWAITING_DEID = "awaiting_deid"
INDEXING = "indexing"
DONE = "done"
FAILED = "failed"

#: The states the worker passes through. Named once so a caller cannot decide
#: a source has settled while it is halfway through masking.
IN_FLIGHT = (QUEUED, PARSING, DEIDENTIFYING, INDEXING)

#: Input types that carry case-level data and are therefore parsed rather than
#: chunked.
CASE_INPUTS = ("e2b_r3_xml", "line_listing", "cioms_form", "case_narrative_doc")

#: This module's chunking policy, bound explicitly rather than defaulted. The
#: shared chunker's defaults are the CLINICAL ones, and a caller that leaves
#: them alone silently gets another module's page semantics -- which is how a
#: citation comes to point at the wrong page while the draft reads perfectly
#: (see `tests/test_docgen_bindings.py`).
#:
#: Empty on purpose: nothing here is page-local. A masked narrative is one
#: piece of text with a synthetic page number, and treating its pages as hard
#: boundaries would split a single case's story for no reason.
PAGE_LOCAL_TYPES: tuple = ()

#: A safety source calls its tables tables.
TABLE_LABEL = "Table"

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
        db.commit()
        deidentify_document(db, document)
    finally:
        db.close()


# ------------------------------------------------------- de-identification

#: How the document's own text is stored so the masking pass has something to
#: work on for a supporting document, which has no case.
DOCUMENT_KIND = "document_text"


def _known_for(db, case) -> dict:
    """The identifying values this case already carries.

    A name the structured data gave us is the surest detection there is: no
    pattern is as reliable as knowing the string in advance.
    """
    known = deident.known_values(case)
    reporter = (case.primary_reporter_qualification or "").strip()
    if reporter and len(reporter) > 3 and " " in reporter:
        known.setdefault(deident.REPORTER_NAME, []).append(reporter)
    return known


def _queue_item(db, document, *, case_id, detection) -> None:
    """One unsettled detection, put in front of a person.

    Deduplicated on the text within a product: a narrative naming the same
    person six times is one question, not six, and a queue that asks the same
    thing repeatedly is a queue people clear without reading.
    """
    existing = db.scalar(select(PvDeidItem).where(
        PvDeidItem.pv_product_id == document.pv_product_id,
        PvDeidItem.detected_text == detection.text,
        PvDeidItem.status == "pending"))
    if existing is not None:
        return
    db.add(PvDeidItem(
        org_id=document.org_id, pv_product_id=document.pv_product_id,
        case_id=case_id, document_id=document.id,
        identifier_type=detection.identifier_type,
        detected_text=detection.text,
        context_snippet=detection.basis,
        proposed_mask=detection.identifier_type,
        status="pending"))


def _accepted_answers(db, pv_product_id: str) -> dict:
    """What people have already decided, so a re-run does not ask again.

    `None` means "not an identifier" and is remembered exactly as firmly as a
    mask: a reviewer who has said that "Severe Headache" is a diagnosis should
    not be asked about it on every later source.
    """
    answers: dict = {}
    for item in db.scalars(select(PvDeidItem).where(
            PvDeidItem.pv_product_id == pv_product_id,
            PvDeidItem.status.in_(
                ("masked", "not_an_identifier", "overridden")))).all():
        # `overridden` is an answer too, and it means "leave it". A qualified
        # person waved it through with a reason on the record. Treating it as
        # unanswered would re-queue it on the next pass and the override would
        # release nothing -- a gate that cannot be opened even by the person
        # authorised to open it.
        answers[item.detected_text] = (
            item.identifier_type if item.status == "masked" else None)
    return answers


def deidentify_document(db, document) -> None:
    """Mask everything this source produced, then index it -- or stop.

    The order is the guarantee. Masking runs over the original text, the
    result goes to the working copy, and chunking only happens when nothing is
    left in the queue for this source. A document with an unanswered detection
    ends at `awaiting_deid` and has no chunks at all, rather than chunks that
    are mostly masked.
    """
    document.processing_status = DEIDENTIFYING
    # Whatever the parse had to say about this file is kept. The line-listing
    # reader records "2 of 3 rows were not read" here, and this stage
    # overwriting it would take away the only place that refusal was reported.
    parse_note = document.error_message
    db.commit()
    salt = document.pv_product_id
    answers = _accepted_answers(db, document.pv_product_id)

    originals = db.scalars(select(PvCaseOriginal).where(
        PvCaseOriginal.source_document_id == document.id)).all()
    queued_here = 0
    for original in originals:
        if not (original.content or "").strip():
            continue
        case = db.get(PvCase, original.case_id) if original.case_id else None
        known = _known_for(db, case) if case is not None else {}
        result = deident.mask(original.content, known=known, salt=salt,
                              accept=answers)
        for detection in result.queued:
            _queue_item(db, document, case_id=original.case_id,
                        detection=detection)
        queued_here += len(result.queued)

        if case is not None:
            narrative = db.scalar(select(PvCaseNarrative).where(
                PvCaseNarrative.case_id == case.id,
                PvCaseNarrative.version == 1))
            if narrative is None:
                narrative = PvCaseNarrative(
                    org_id=document.org_id, pv_product_id=document.pv_product_id,
                    case_id=case.id, version=1)
                db.add(narrative)
            narrative.raw_text_redacted = result.masked_text
            # The case is only clear when nothing about it is still open.
            case.deidentification_status = (
                "pending" if result.queued else "clear")
    db.flush()

    # Counted from what the masking pass found in THIS document's text, not
    # from the queue rows it created. The queue is deduplicated per product --
    # one question for a name appearing in six files -- so a second source
    # naming the same person creates no row of its own, and a count by
    # `document_id` would read zero and index it with the name still in it.
    outstanding = queued_here
    gate_note = (
        f"{outstanding} detection(s) need a person before this source can be "
        "indexed." if outstanding else None)
    document.error_message = " · ".join(
        note for note in (parse_note, gate_note) if note) or None
    if outstanding:
        document.processing_status = AWAITING_DEID
        db.commit()
        return
    _index_document(db, document)


def _index_document(db, document) -> None:
    """Chunk and index, from the masked copy only.

    Reached only with an empty queue for this source, which is what makes the
    gate a gate. The text handed to the chunker is `raw_text_redacted` for a
    case and the masked document text otherwise -- `pv_case_originals` is not
    read here and must never be.
    """
    from app.docgen.chunking import chunk_extraction
    from app.docgen.extraction import Extraction, ExtractedPage

    document.processing_status = INDEXING
    db.commit()

    for chunk in db.scalars(select(PvChunk).where(
            PvChunk.document_id == document.id)).all():
        db.delete(chunk)
    db.flush()

    texts: list = []
    for narrative in db.scalars(select(PvCaseNarrative).where(
            PvCaseNarrative.pv_product_id == document.pv_product_id)).all():
        case = db.get(PvCase, narrative.case_id)
        if case is None or case.source_document_id != document.id:
            continue
        if (narrative.raw_text_redacted or "").strip():
            texts.append(narrative.raw_text_redacted)

    if not texts and document.input_type == "document":
        masked = _masked_document_text(db, document)
        if masked:
            texts.append(masked)

    written = 0
    for page_number, text in enumerate(texts, start=1):
        extraction = Extraction(
            pages=[ExtractedPage(page=page_number, text=text)], tables=[],
            page_count=len(texts))
        for chunk in chunk_extraction(extraction, doc_type=document.doc_type,
                                      page_local_types=PAGE_LOCAL_TYPES,
                                      table_label=TABLE_LABEL):
            db.add(PvChunk(
                org_id=document.org_id, pv_product_id=document.pv_product_id,
                document_id=document.id,
                report_instance_id=document.report_instance_id,
                doc_type=document.doc_type, page=chunk.get("page"),
                section_hint=chunk.get("section_hint"),
                is_table=bool(chunk.get("is_table")),
                table_id=chunk.get("table_id"),
                content=chunk["content"],
                token_count=chunk.get("token_count", 0)))
            written += 1
    document.chunk_count = written
    document.processing_status = DONE
    db.commit()


def _masked_document_text(db, document) -> str | None:
    """A supporting document's text, masked, for indexing.

    Held on a `pv_case_originals` row with no case: a previous report or an
    investigator's brochure has no case behind it but has exactly the site and
    investigator names §6 lists.
    """
    original = db.scalar(select(PvCaseOriginal).where(
        PvCaseOriginal.source_document_id == document.id,
        PvCaseOriginal.kind == DOCUMENT_KIND))
    if original is None or not (original.content or "").strip():
        return None
    answers = _accepted_answers(db, document.pv_product_id)
    result = deident.mask(original.content, salt=document.pv_product_id,
                          accept=answers)
    return result.masked_text


def resume_after_review(db, pv_product_id: str) -> list:
    """Re-run every source that was waiting, now that the queue has moved.

    Every source, not just the one an item came from: a person who decides
    that "Jane Smith" is a name has decided it for the whole product, and the
    other four sources naming her are waiting on the same answer.
    """
    documents = db.scalars(select(PvDocument).where(
        PvDocument.pv_product_id == pv_product_id,
        PvDocument.processing_status == AWAITING_DEID)).all()
    moved = []
    for document in documents:
        deidentify_document(db, document)
        if document.processing_status == DONE:
            moved.append(document.id)
    return moved


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
