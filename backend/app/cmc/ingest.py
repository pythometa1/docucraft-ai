"""Turning an uploaded CMC source into chunks and, where it holds numbers,
into rows of the structured store.

Two passes over the same file, and the difference between them is the whole
module. The first is the shared one every document module runs: text and
tables become retrievable chunks a section can cite. The second is CMC's own:
a certificate of analysis, a specification, a stability table or a batch
record is read for its VALUES, which become `cmc_results`, `cmc_tests` and
`cmc_batches` rows -- data a document prints rather than evidence a model
reads.

Nothing the second pass produces is trusted. Every value arrives with an
extraction confidence and no `verified_by`, and the grid is where a person
turns one into the other. A parser that placed a value in the wrong cell is a
defect a reviewer can catch; a parser that was believed without being checked
is a defect that ships.
"""

import hashlib
import threading

from sqlalchemy import select

from app.public_errors import public_message
from app.cmc import registry
from app.cmc.extraction_structured import extract_structured
from app.db import SessionLocal
from app.docgen.chunking import chunk_extraction
from app.docgen.extraction import UnsupportedSource, extract
from app.models import CmcChunk, CmcDocument
from app.storage import abs_path

#: This module's chunking policy, bound explicitly rather than defaulted --
#: see `tests/test_docgen_bindings.py` for why a silent default is the
#: dangerous kind. A specification's pages are hard boundaries for the same
#: reason a TLF's are: page 4's limits belong to page 4's test list.
PAGE_LOCAL_TYPES = ("coa", "spec_ds", "spec_dp", "spec_excipient",
                    "stability_data", "bmr")

#: A CMC source calls its tables by many names. "Table" is still the right
#: header word: it is what makes a chunk findable by id, and a specification
#: numbered 3.2.P.5.1 is referred to as a table wherever it is quoted.
TABLE_LABEL = "Table"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set_status(db, document, status: str, *, error: str | None = None) -> None:
    document.processing_status = status
    document.error_message = error
    db.commit()


def ingest_document(document_id: str) -> None:
    """Extract, chunk, index and -- for a data-bearing source -- read the
    numbers out of one uploaded document.

    Its own session because its own thread. A failure is recorded on the
    document's own row and never touches its siblings: forty certificates of
    analysis where one is a scan should index thirty-nine and say so about the
    fortieth.
    """
    db = SessionLocal()
    try:
        document = db.get(CmcDocument, document_id)
        if document is None:
            return
        _set_status(db, document, "parsing")
        try:
            extraction = extract(str(abs_path(document.storage_path)),
                                 mime_type=document.mime_type,
                                 source_name=document.original_filename)
        except Exception as exc:  # noqa: BLE001 - a broken file is data, not a bug
            # UnsupportedSource is written for the uploader; ExtractorUnavailable
            # names a package and an install command, which is for the operator.
            _set_status(db, document, "failed", error=public_message(
                exc, "The file could not be read.", user_facing=(UnsupportedSource,)))
            return

        document.page_count = extraction.page_count
        _set_status(db, document, "chunking")
        try:
            rows = chunk_extraction(extraction, doc_type=document.doc_type,
                                    page_local_types=PAGE_LOCAL_TYPES,
                                    table_label=TABLE_LABEL)
        except Exception as exc:  # noqa: BLE001
            _set_status(db, document, "failed", error=public_message(
                exc, "The file could not be split into sources."))
            return
        if not rows:
            _set_status(db, document, "failed",
                        error="No readable text or tables were found in this file. "
                              "A scanned PDF needs OCR before it can be a source.")
            return

        _set_status(db, document, "indexing")
        for stale in db.scalars(select(CmcChunk).where(
                CmcChunk.document_id == document.id)).all():
            db.delete(stale)
        db.flush()
        for row in rows:
            db.add(CmcChunk(org_id=document.org_id,
                            cmc_project_id=document.cmc_project_id,
                            document_id=document.id,
                            doc_type=document.doc_type,
                            material_id=document.material_id, **row))
        document.chunk_count = len(rows)

        # -- the pass that makes this module different --
        if document.doc_type in registry.STRUCTURED_TYPES:
            _set_status(db, document, "extracting")
            try:
                document.value_count = extract_structured(db, document, extraction)
            except Exception as exc:  # noqa: BLE001 - the chunks are already good
                # The document stays usable as prose evidence. Saying the
                # numbers did not come out is better than failing a file whose
                # text a section can still cite.
                _set_status(db, document, "done", error=public_message(
                    exc, "Indexed, but no structured values could be read."))
                return
        _set_status(db, document, "done")
    finally:
        db.close()


def ingest_in_background(document_ids: list) -> None:
    """One worker per batch of documents, processing them in sequence.

    Sequential on purpose, unlike the clinical module's thread-per-document.
    Two CMC sources can describe the SAME cell -- two certificates for one
    batch, a specification and a certificate stating one limit -- and
    detecting that they disagree means reading what the other one already
    wrote. Run in parallel, both threads see an empty cell, both insert, and
    the conflict the grid exists to surface never appears.

    Failure isolation is unaffected: each document is still handled inside
    `ingest_document`, which records its own error and returns rather than
    raising into the loop.
    """
    ids = list(document_ids)

    def run() -> None:
        for document_id in ids:
            try:
                ingest_document(document_id)
            except Exception:  # noqa: BLE001 - one bad file never stops the rest
                continue

    threading.Thread(target=run, daemon=True).start()


def readiness(documents, deliverable_keys) -> dict:
    """What the upload screen says is still missing.

    Computed from the deliverables this project actually selected, not from a
    fixed list: a 3.2.P needs a batch record and a 3.2.S does not, and a
    checklist that asked for both would teach people to ignore it.
    """
    required, recommended = registry.requirements(deliverable_keys)
    done = {d.doc_type for d in documents if d.processing_status == "done"}
    present = {d.doc_type for d in documents}
    return {
        "required": [{"doc_type": t, "uploaded": t in present, "indexed": t in done}
                     for t in required],
        "recommended": [{"doc_type": t, "uploaded": t in present, "indexed": t in done}
                        for t in recommended],
        "missing_required": [t for t in required if t not in done],
        "ready_to_generate": bool(required) and all(t in done for t in required),
    }
