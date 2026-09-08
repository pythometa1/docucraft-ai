"""Turning an uploaded source file into retrievable chunks, in the background.

One document at a time, in its own session, with its status written at every
step -- so the upload screen can say "parsing" rather than "working…", and so
one unreadable PDF in a bundle of forty leaves the other thirty-nine indexed.
A failed document records WHY on its own row and is retryable by itself; it
never fails its siblings and never fails the project.

Embeddings are optional here. Retrieval scores lexically when a chunk carries
no vector (see `app.csr.retrieval`), because a study team that has uploaded a
protocol should be able to draft from it whether or not an embedding provider
is configured -- an unconfigured key is a smaller problem than a CSR that
cannot be written.
"""

import hashlib
import threading

from sqlalchemy import select

from app.csr.chunking import chunk_extraction
from app.csr.extraction import ExtractorUnavailable, UnsupportedSource, extract
from app.db import SessionLocal
from app.models import CsrChunk, CsrDocument
from app.storage import abs_path

#: Every document this module knows how to be given. `prior_csr` is ingested
#: like any other but marked style-reference at retrieval time.
DOC_TYPES = (
    "protocol", "sap", "tlf", "narrative", "ib", "icf", "crf",
    "randomization", "prior_csr", "other",
)

#: What a CSR cannot be written without (spec §4).
REQUIRED_DOC_TYPES = ("protocol", "sap", "tlf")
RECOMMENDED_DOC_TYPES = ("narrative", "ib", "icf")


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _set_status(db, document, status: str, *, error: str | None = None) -> None:
    document.processing_status = status
    document.error_message = error
    db.commit()


def ingest_document(document_id: str) -> None:
    """Extract, chunk and index one uploaded document.

    Runs in its own session because it runs in its own thread: the request
    that queued it has long since returned, and its session with it.
    """
    db = SessionLocal()
    try:
        document = db.get(CsrDocument, document_id)
        if document is None:
            return
        _set_status(db, document, "parsing")
        try:
            extraction = extract(str(abs_path(document.storage_path)),
                                 mime_type=document.mime_type)
        except (UnsupportedSource, ExtractorUnavailable) as exc:
            _set_status(db, document, "failed", error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a broken file is data, not a bug
            _set_status(db, document, "failed",
                        error=f"The file could not be read: {exc}")
            return

        document.page_count = extraction.page_count
        _set_status(db, document, "chunking")
        try:
            rows = chunk_extraction(extraction, doc_type=document.doc_type)
        except Exception as exc:  # noqa: BLE001
            _set_status(db, document, "failed",
                        error=f"The file could not be split into sources: {exc}")
            return
        if not rows:
            _set_status(db, document, "failed",
                        error="No readable text or tables were found in this file. "
                              "A scanned PDF needs OCR before it can be a source.")
            return

        _set_status(db, document, "indexing")
        # Re-ingesting replaces this document's chunks rather than adding a
        # second copy: two identical sources would both be retrievable and the
        # draft would cite one of them arbitrarily.
        for stale in db.scalars(select(CsrChunk).where(
                CsrChunk.document_id == document.id)).all():
            db.delete(stale)
        db.flush()
        for row in rows:
            db.add(CsrChunk(org_id=document.org_id,
                            csr_project_id=document.csr_project_id,
                            document_id=document.id,
                            doc_type=document.doc_type, **row))
        document.chunk_count = len(rows)
        _set_status(db, document, "done")
    finally:
        db.close()


def ingest_in_background(document_ids: list) -> None:
    """One thread per document, so a slow 300-page protocol does not hold up
    the RTF next to it. The threads are daemons: a shutdown mid-ingest leaves
    the documents in `parsing`, which the UI shows and the retry button fixes.
    """
    for document_id in document_ids:
        thread = threading.Thread(target=ingest_document, args=(document_id,),
                                  daemon=True)
        thread.start()


def readiness(documents) -> dict:
    """What the upload screen needs to say: which required types are present
    and indexed, and what is still missing."""
    done_types = {d.doc_type for d in documents if d.processing_status == "done"}
    present_types = {d.doc_type for d in documents}
    return {
        "required": [
            {"doc_type": t, "uploaded": t in present_types, "indexed": t in done_types}
            for t in REQUIRED_DOC_TYPES
        ],
        "recommended": [
            {"doc_type": t, "uploaded": t in present_types, "indexed": t in done_types}
            for t in RECOMMENDED_DOC_TYPES
        ],
        "missing_required": [t for t in REQUIRED_DOC_TYPES if t not in done_types],
        "ready_to_generate": all(t in done_types for t in REQUIRED_DOC_TYPES),
    }
