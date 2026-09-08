"""The CSR module, milestone M1: create the project extension, choose the
built-in template, hold the ICH E3 section tree.

Positioning, stated once and rendered in the UI: this is an AI-ASSISTED
DRAFTING TOOL for medical writers. Sections will move Draft -> In Review ->
Approved under a person's hand; nothing exports unapproved. Later milestones
add ingestion (M2), grounded generation (M3), QC (M4) and export (M5); this
router refuses those surfaces rather than stubbing them silently.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.csr.ich_e3 import seed_sections
from app.csr.ingest import DOC_TYPES, file_hash, ingest_in_background, readiness
from app.db import get_db
from app.models import (
    CsrChunk, CsrCitation, CsrDocument, CsrProject, CsrSection, CsrSectionDraft,
    CsrTemplate, Project, Study, User, now,
)
from app.ownership import owned_project
from app.security import error, get_current_user
from app.storage import abs_path, save_bytes

router = APIRouter(tags=["csr"])

BLINDING_VALUES = ("open_label", "single_blind", "double_blind")

#: The portal taxonomy this module serves. A CSR extension on an HR project
#: would put patient-data machinery where nobody expects it.
CSR_FUNCTION = "Clinical"
CSR_DOCUMENT_TYPE = "Clinical Study Report"


# ------------------------------------------------------------------ helpers

def _owned_study(db: Session, study_id: str, user: User) -> Study:
    s = db.get(Study, study_id)
    if not s or s.org_id != user.org_id or s.deleted_at is not None:
        raise error("STUDY_NOT_FOUND", "Study not found", 404)
    return s


def _owned_csr_project(db: Session, csr_project_id: str, user: User) -> CsrProject:
    cp = db.get(CsrProject, csr_project_id)
    if not cp or cp.org_id != user.org_id:
        raise error("CSR_PROJECT_NOT_FOUND", "CSR project not found", 404)
    return cp


def _study_out(s: Study | None) -> dict | None:
    if s is None:
        return None
    return {"id": s.id, "protocol_number": s.protocol_number, "title": s.title,
            "sponsor": s.sponsor, "phase": s.phase, "indication": s.indication,
            "principal_investigator": s.principal_investigator}


def _project_out(db: Session, cp: CsrProject) -> dict:
    project = db.get(Project, cp.project_id)
    study = db.get(Study, cp.study_id) if cp.study_id else None
    template = db.scalar(select(CsrTemplate).where(
        CsrTemplate.csr_project_id == cp.id).order_by(CsrTemplate.created_at.desc()))
    return {
        "id": cp.id, "project_id": cp.project_id,
        "project_name": project.name if project else None,
        "study": _study_out(study),
        "compound_name": cp.compound_name,
        "therapeutic_area": cp.therapeutic_area,
        "blinding": cp.blinding,
        "study_design_summary": cp.study_design_summary,
        "status": cp.status,
        "template": {"source": template.source, "parsed_at": template.parsed_at}
        if template else None,
        "created_at": cp.created_at, "updated_at": cp.updated_at,
    }


def _section_out(s: CsrSection) -> dict:
    return {"id": s.id, "section_number": s.section_number, "title": s.title,
            "sort_order": s.sort_order, "enabled": s.enabled,
            "is_container": s.is_container, "status": s.status,
            "guidance_text": s.guidance_text}


# ------------------------------------------------------------------ projects

class CsrProjectIn(BaseModel):
    #: The portal project this CSR lives in (function Clinical, document type
    #: Clinical Study Report).
    project_id: str
    study_id: str | None = None
    #: A one-off study, saved into the study book -- the CSR always references
    #: a book row, because ten documents quoting ten spellings of one protocol
    #: number is the defect the book exists to prevent.
    study: dict | None = None
    compound_name: str | None = None
    therapeutic_area: str | None = None
    blinding: str | None = None
    study_design_summary: str | None = None


@router.post("/csr/projects", status_code=201)
def create_csr_project(body: CsrProjectIn, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    project = owned_project(db, body.project_id, user)
    if project.function != CSR_FUNCTION or project.document_type != CSR_DOCUMENT_TYPE:
        raise error(
            "CSR_WRONG_PROJECT",
            f"A CSR lives in a {CSR_FUNCTION} project whose document type is "
            f"{CSR_DOCUMENT_TYPE!r}; this project is {project.function}/"
            f"{project.document_type}.", 422)
    existing = db.scalar(select(CsrProject).where(CsrProject.project_id == project.id))
    if existing is not None:
        raise error("CSR_PROJECT_EXISTS",
                    "This project already has a CSR. Open it instead.", 409)

    if body.blinding is not None and body.blinding not in BLINDING_VALUES:
        raise error("CSR_BAD_BLINDING",
                    f"blinding must be one of {', '.join(BLINDING_VALUES)}.", 422)

    if body.study_id:
        study = _owned_study(db, body.study_id, user)
    elif body.study and (str(body.study.get("protocol_number") or "").strip()):
        study = Study(org_id=user.org_id, created_by=user.id,
                      protocol_number=str(body.study["protocol_number"]).strip(),
                      title=body.study.get("title"),
                      sponsor=body.study.get("sponsor"),
                      phase=body.study.get("phase"),
                      indication=body.study.get("indication"),
                      principal_investigator=body.study.get("principal_investigator"))
        db.add(study)
        db.flush()
        log_audit(db, user, "Added a study", "study", study.id, None, "info",
                  study.protocol_number)
    else:
        raise error(
            "CSR_NEEDS_STUDY",
            "A CSR is about a study: pass study_id from the study book, or an inline "
            "study with at least a protocol number.", 422)

    cp = CsrProject(org_id=user.org_id, project_id=project.id, study_id=study.id,
                    compound_name=body.compound_name,
                    therapeutic_area=body.therapeutic_area,
                    blinding=body.blinding,
                    study_design_summary=body.study_design_summary,
                    created_by=user.id)
    db.add(cp)
    db.flush()
    log_audit(db, user, "Created a CSR project", "csr_project", cp.id, project.id,
              "info", study.protocol_number)
    db.commit()
    db.refresh(cp)
    return _project_out(db, cp)


@router.get("/csr/projects")
def list_csr_projects(db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    rows = db.scalars(select(CsrProject).where(
        CsrProject.org_id == user.org_id).order_by(CsrProject.created_at.desc())).all()
    return {"items": [_project_out(db, cp) for cp in rows]}


@router.get("/csr/projects/{csr_project_id}")
def get_csr_project(csr_project_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    cp = _owned_csr_project(db, csr_project_id, user)
    sections = db.scalars(select(CsrSection).where(
        CsrSection.csr_project_id == cp.id).order_by(CsrSection.sort_order)).all()
    return {**_project_out(db, cp),
            "sections": [_section_out(s) for s in sections]}


@router.delete("/csr/projects/{csr_project_id}")
def delete_csr_project(csr_project_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Purge the CSR module's own data. The portal project remains -- it has
    its own deletion flow and its own retention rules.

    M1 purges sections and template rows; each later milestone extends this
    with its tables (documents, chunks, vectors, drafts, exports), keeping the
    promise that deleting a CSR project verifiably removes what it ingested.
    """
    cp = _owned_csr_project(db, csr_project_id, user)
    sections = db.scalars(select(CsrSection).where(
        CsrSection.csr_project_id == cp.id)).all()
    documents = db.scalars(select(CsrDocument).where(
        CsrDocument.csr_project_id == cp.id)).all()

    # Drafts and their citations hang off sections, so they go first: a
    # citation row outliving the draft it annotates is an orphan pointing at
    # evidence that no longer exists.
    draft_count = 0
    for section in sections:
        for draft in db.scalars(select(CsrSectionDraft).where(
                CsrSectionDraft.csr_section_id == section.id)).all():
            for citation in db.scalars(select(CsrCitation).where(
                    CsrCitation.draft_id == draft.id)).all():
                db.delete(citation)
            db.delete(draft)
            draft_count += 1
    db.flush()

    chunk_count = 0
    for chunk in db.scalars(select(CsrChunk).where(
            CsrChunk.csr_project_id == cp.id)).all():
        db.delete(chunk)
        chunk_count += 1
    blobs = [abs_path(d.storage_path) for d in documents]
    for document in documents:
        db.delete(document)
    for section in sections:
        db.delete(section)
    for template in db.scalars(select(CsrTemplate).where(
            CsrTemplate.csr_project_id == cp.id)).all():
        db.delete(template)
    db.delete(cp)
    log_audit(db, user, "Deleted a CSR project", "csr_project", cp.id, cp.project_id,
              "warning",
              f"purged {len(sections)} sections, {len(documents)} sources, "
              f"{chunk_count} chunks, {draft_count} drafts")
    db.commit()

    # The rows are gone; the bytes follow. An undeletable blob is an ops
    # problem to notice in storage, not a reason to fail a delete that the
    # database has already committed.
    purged_files = 0
    for blob in blobs:
        try:
            blob.unlink(missing_ok=True)
            purged_files += 1
        except OSError:
            pass
    return {"deleted": True, "purged_sections": len(sections),
            "purged_documents": len(documents), "purged_chunks": chunk_count,
            "purged_drafts": draft_count, "purged_files": purged_files}


# ------------------------------------------------------------------ template

class TemplateChoice(BaseModel):
    #: builtin_ich_e3 today; "uploaded" arrives in milestone M6.
    source: str = "builtin_ich_e3"


@router.post("/csr/projects/{csr_project_id}/template", status_code=201)
def choose_template(csr_project_id: str, body: TemplateChoice,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    cp = _owned_csr_project(db, csr_project_id, user)
    if body.source == "uploaded":
        raise error(
            "CSR_TEMPLATE_UPLOAD_UNBUILT",
            "Sponsor template upload is not built yet (milestone M6). Use the built-in "
            "ICH E3 structure for now.", 422)
    if body.source != "builtin_ich_e3":
        raise error("CSR_BAD_TEMPLATE_SOURCE",
                    "source must be builtin_ich_e3 or uploaded.", 422)

    existing = db.scalars(select(CsrSection).where(
        CsrSection.csr_project_id == cp.id)).all()
    if any(s.status != "not_started" for s in existing):
        raise error(
            "CSR_TEMPLATE_LOCKED",
            "Sections already carry work; the template cannot be replaced under them.",
            409)
    for section in existing:
        db.delete(section)
    db.flush()

    template = CsrTemplate(org_id=user.org_id, csr_project_id=cp.id,
                           source="builtin_ich_e3")
    db.add(template)
    sections = [CsrSection(org_id=user.org_id, csr_project_id=cp.id, **row)
                for row in seed_sections()]
    db.add_all(sections)
    cp.status = "ready"
    cp.updated_at = now()
    db.flush()
    log_audit(db, user, "Chose the CSR template", "csr_project", cp.id, cp.project_id,
              "info", "builtin_ich_e3")
    db.commit()
    return {"template": {"source": "builtin_ich_e3"},
            "sections": [_section_out(s) for s in sorted(sections, key=lambda s: s.sort_order)]}


@router.get("/csr/projects/{csr_project_id}/sections")
def list_sections(csr_project_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    cp = _owned_csr_project(db, csr_project_id, user)
    rows = db.scalars(select(CsrSection).where(
        CsrSection.csr_project_id == cp.id).order_by(CsrSection.sort_order)).all()
    return {"items": [_section_out(s) for s in rows]}


class SectionPatch(BaseModel):
    enabled: bool


@router.patch("/csr/sections/{csr_section_id}")
def patch_section(csr_section_id: str, body: SectionPatch,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    section = db.get(CsrSection, csr_section_id)
    if not section or section.org_id != user.org_id:
        raise error("CSR_SECTION_NOT_FOUND", "Section not found", 404)
    if section.is_container:
        raise error("CSR_SECTION_IS_CONTAINER",
                    "A container heading has no prose of its own to enable or "
                    "disable; toggle its subsections.", 422)
    section.enabled = body.enabled
    section.updated_at = now()
    log_audit(db, user, "Toggled a CSR section", "csr_section", section.id, None,
              "info", f"{section.section_number} enabled={body.enabled}")
    db.commit()
    return _section_out(section)


# ------------------------------------------------------------------ sources

MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: What each extractor can read. A file outside this list is refused at upload
#: rather than accepted and failed later in a thread nobody is watching.
ALLOWED_SUFFIXES = (".pdf", ".docx", ".rtf", ".xlsx", ".csv", ".txt", ".md")


def _document_out(d: CsrDocument) -> dict:
    return {
        "id": d.id, "doc_type": d.doc_type, "filename": d.original_filename,
        "mime_type": d.mime_type, "size_bytes": d.size_bytes,
        "page_count": d.page_count, "processing_status": d.processing_status,
        "error_message": d.error_message, "chunk_count": d.chunk_count,
        "created_at": d.created_at, "updated_at": d.updated_at,
    }


def _owned_document(db: Session, document_id: str, user: User) -> CsrDocument:
    d = db.get(CsrDocument, document_id)
    if not d or d.org_id != user.org_id:
        raise error("CSR_DOCUMENT_NOT_FOUND", "Source document not found", 404)
    return d


@router.post("/csr/projects/{csr_project_id}/documents", status_code=201)
async def upload_documents(csr_project_id: str,
                           files: list[UploadFile] = File(...),
                           doc_types: list[str] = Form(...),
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Upload one or many tagged source documents.

    The tag is required per file and arrives alongside it: an untagged source
    cannot be mapped to the sections that may cite it, so accepting one would
    only postpone the question to generation time, where it becomes a section
    citing the wrong kind of document.
    """
    cp = _owned_csr_project(db, csr_project_id, user)
    if len(doc_types) != len(files):
        raise error("CSR_TAGS_MISMATCH",
                    f"{len(files)} files arrived with {len(doc_types)} tags; every "
                    "file needs exactly one document type.", 422)

    saved = []
    for upload, doc_type in zip(files, doc_types):
        tag = (doc_type or "").strip().lower()
        if tag not in DOC_TYPES:
            raise error("CSR_BAD_DOC_TYPE",
                        f"Unknown document type {doc_type!r}; one of "
                        f"{', '.join(DOC_TYPES)}.", 422)
        name = upload.filename or "source"
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise error("CSR_UNSUPPORTED_FILE",
                        f"{name}: {suffix or 'files with no extension'} cannot be read. "
                        f"Supported: {', '.join(ALLOWED_SUFFIXES)}.", 422)
        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise error("CSR_FILE_TOO_LARGE",
                        f"{name} is {len(data) // (1024 * 1024)} MB; the limit is "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file.", 413)
        if not data:
            raise error("CSR_EMPTY_FILE", f"{name} is empty.", 422)

        storage_path = save_bytes(data, f"csr/{cp.id}", suffix)
        document = CsrDocument(
            org_id=user.org_id, csr_project_id=cp.id, doc_type=tag,
            original_filename=name, storage_path=storage_path,
            mime_type=upload.content_type, size_bytes=len(data),
            file_hash=file_hash(data), uploaded_by=user.id)
        db.add(document)
        saved.append(document)
    db.flush()
    log_audit(db, user, "Uploaded CSR sources", "csr_project", cp.id, cp.project_id,
              "info", f"{len(saved)} files")
    db.commit()
    for document in saved:
        db.refresh(document)
    return {"items": [_document_out(d) for d in saved]}


@router.get("/csr/projects/{csr_project_id}/documents")
def list_documents(csr_project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    cp = _owned_csr_project(db, csr_project_id, user)
    rows = db.scalars(select(CsrDocument).where(
        CsrDocument.csr_project_id == cp.id).order_by(CsrDocument.created_at)).all()
    return {"items": [_document_out(d) for d in rows], "readiness": readiness(rows)}


class DocTypePatch(BaseModel):
    doc_type: str


@router.patch("/csr/documents/{csr_document_id}")
def retag_document(csr_document_id: str, body: DocTypePatch,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Re-tag a source. The chunks carry the tag too (retrieval filters on it),
    so they are re-indexed rather than left describing the old answer."""
    document = _owned_document(db, csr_document_id, user)
    tag = (body.doc_type or "").strip().lower()
    if tag not in DOC_TYPES:
        raise error("CSR_BAD_DOC_TYPE",
                    f"Unknown document type {body.doc_type!r}.", 422)
    document.doc_type = tag
    document.updated_at = now()
    for chunk in db.scalars(select(CsrChunk).where(
            CsrChunk.document_id == document.id)).all():
        chunk.doc_type = tag
    log_audit(db, user, "Re-tagged a CSR source", "csr_document", document.id, None,
              "info", tag)
    db.commit()
    db.refresh(document)
    return _document_out(document)


@router.delete("/csr/documents/{csr_document_id}")
def delete_document(csr_document_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Remove a source and everything derived from it. Drafts already written
    keep their text -- deleting a source cannot rewrite history -- but its
    chunks stop being retrievable, and QC will flag any citation that pointed
    at them as unresolved rather than pretending the evidence still exists."""
    document = _owned_document(db, csr_document_id, user)
    removed = 0
    for chunk in db.scalars(select(CsrChunk).where(
            CsrChunk.document_id == document.id)).all():
        db.delete(chunk)
        removed += 1
    blob = abs_path(document.storage_path)
    db.delete(document)
    log_audit(db, user, "Deleted a CSR source", "csr_document", document.id, None,
              "warning", f"{document.original_filename} (-{removed} chunks)")
    db.commit()
    try:
        blob.unlink(missing_ok=True)
    except OSError:
        pass  # the row is gone; an undeletable blob is an ops problem, not a 500
    return {"deleted": True, "purged_chunks": removed}


@router.post("/csr/projects/{csr_project_id}/process", status_code=202)
def process_documents(csr_project_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Queue every not-yet-indexed source for extraction, chunking and
    indexing. Real background work: this returns immediately and the client
    polls `processing-status`, one row per file."""
    cp = _owned_csr_project(db, csr_project_id, user)
    pending = db.scalars(select(CsrDocument).where(
        CsrDocument.csr_project_id == cp.id,
        CsrDocument.processing_status.in_(("queued", "failed")))).all()
    if not pending:
        raise error("CSR_NOTHING_TO_PROCESS",
                    "Every uploaded source is already indexed.", 409)
    ids = []
    for document in pending:
        document.processing_status = "queued"
        document.error_message = None
        ids.append(document.id)
    log_audit(db, user, "Started CSR source processing", "csr_project", cp.id,
              cp.project_id, "info", f"{len(ids)} files")
    db.commit()
    ingest_in_background(ids)
    return {"queued": len(ids),
            "poll": f"/api/v1/csr/projects/{cp.id}/processing-status"}


@router.post("/csr/documents/{csr_document_id}/retry", status_code=202)
def retry_document(csr_document_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Retry one failed file. One bad PDF must not mean re-ingesting forty."""
    document = _owned_document(db, csr_document_id, user)
    document.processing_status = "queued"
    document.error_message = None
    db.commit()
    ingest_in_background([document.id])
    return {"queued": 1}


@router.get("/csr/projects/{csr_project_id}/processing-status")
def processing_status(csr_project_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    cp = _owned_csr_project(db, csr_project_id, user)
    rows = db.scalars(select(CsrDocument).where(
        CsrDocument.csr_project_id == cp.id).order_by(CsrDocument.created_at)).all()
    done = sum(1 for d in rows if d.processing_status in ("done", "failed"))
    return {
        "items": [_document_out(d) for d in rows],
        "total": len(rows), "settled": done,
        "in_flight": any(d.processing_status in ("queued", "parsing", "chunking", "indexing")
                         for d in rows),
        "readiness": readiness(rows),
    }


# ------------------------------------------------------------------ drafting

def _draft_out(draft: CsrSectionDraft, citations: list | None = None) -> dict:
    return {
        "id": draft.id, "version": draft.version, "content": draft.content,
        "created_by": draft.created_by, "model": draft.model,
        "generation_params": draft.generation_params,
        "created_at": draft.created_at,
        "citations": [
            {"id": c.id, "marker": c.marker, "document_id": c.document_id,
             "chunk_id": c.chunk_id, "page": c.page, "table_ref": c.table_ref,
             "cited_value": c.cited_value}
            for c in (citations or [])
        ],
    }


def _owned_section(db: Session, section_id: str, user: User) -> CsrSection:
    section = db.get(CsrSection, section_id)
    if not section or section.org_id != user.org_id:
        raise error("CSR_SECTION_NOT_FOUND", "Section not found", 404)
    return section


def _latest_draft(db: Session, section_id: str) -> CsrSectionDraft | None:
    return db.scalar(select(CsrSectionDraft).where(
        CsrSectionDraft.csr_section_id == section_id
    ).order_by(CsrSectionDraft.version.desc()))


def _study_metadata(db: Session, cp: CsrProject) -> dict:
    """What every section's prompt is told about the study. Read from the
    study book at generation time, never copied at project creation -- a
    sponsor corrected in the book must be corrected in the next draft."""
    study = db.get(Study, cp.study_id) if cp.study_id else None
    return {
        "study_id": (study.protocol_number if study else None),
        "protocol_number": (study.protocol_number if study else None),
        "study_title": (study.title if study else None),
        "sponsor": (study.sponsor if study else None),
        "phase": (study.phase if study else None),
        "indication": (study.indication if study else None),
        "principal_investigator": (study.principal_investigator if study else None),
        "compound_name": cp.compound_name,
        "therapeutic_area": cp.therapeutic_area,
        "blinding": cp.blinding,
        "study_design_summary": cp.study_design_summary,
    }


class GenerateRequest(BaseModel):
    #: "shorten this", "emphasise the subgroup analysis" -- appended to the
    #: prompt as an extra rule. Always produces a NEW version.
    instruction: str | None = None


@router.post("/csr/sections/{csr_section_id}/generate", status_code=201)
def generate_section(csr_section_id: str, body: GenerateRequest,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Draft one section from this project's indexed sources.

    Section-wise by construction: this endpoint sees one section's retrieved
    evidence and writes one section. There is no "generate the whole CSR"
    call, because a 200-page document written in one pass is a document no
    reviewer can trace back to its sources.
    """
    from app.csr.drafting import draft_section
    from app.csr.retrieval import format_extracts, retrieve_for_section
    from app.tenancy import llm_policy_for

    section = _owned_section(db, csr_section_id, user)
    if section.is_container:
        raise error("CSR_SECTION_IS_CONTAINER",
                    "A container heading has no prose of its own; generate its "
                    "subsections.", 422)
    if not section.enabled:
        raise error("CSR_SECTION_DISABLED",
                    "This section is excluded from the report. Include it first.", 409)
    cp = _owned_csr_project(db, section.csr_project_id, user)

    indexed = db.scalar(select(func.count()).select_from(CsrChunk).where(
        CsrChunk.csr_project_id == cp.id))
    if not indexed:
        raise error(
            "CSR_NO_SOURCES",
            "No source documents are indexed yet. Upload the protocol, SAP and "
            "statistical outputs, then process them.", 409)

    metadata = _study_metadata(db, cp)
    chunks = retrieve_for_section(
        db, csr_project_id=cp.id, org_id=user.org_id,
        section_number=section.section_number, section_title=section.title,
        guidance_text=section.guidance_text, study_metadata=metadata)
    extracts, source_map = format_extracts(chunks)

    section.status = "generating"
    db.commit()
    try:
        result = draft_section(
            section_number=section.section_number, section_title=section.title,
            guidance=section.guidance_text, study_metadata=metadata,
            chunks=chunks, source_map=source_map, extracts=extracts,
            instruction=body.instruction,
            llm_policy=llm_policy_for(db, user.org_id, project_id=cp.project_id,
                                      user_id=user.id, subject_type="csr_section",
                                      subject_id=section.id))
    except Exception:
        # The section goes back to what it was: a section stuck on "generating"
        # is a section nobody can retry from the UI.
        section.status = "draft" if _latest_draft(db, section.id) else "not_started"
        db.commit()
        raise

    previous = _latest_draft(db, section.id)
    draft = CsrSectionDraft(
        org_id=user.org_id, csr_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=result.content, created_by="ai", model=result.model,
        generation_params={
            "prompt_version": result.prompt_version,
            "instruction": body.instruction,
            "chunk_ids": [c.id for c in chunks],
            "k": len(chunks),
            "source_map": source_map,
        })
    db.add(draft)
    db.flush()
    citations = [
        CsrCitation(org_id=user.org_id, draft_id=draft.id, **citation)
        for citation in result.citations
    ]
    db.add_all(citations)
    section.status = "draft"
    section.updated_at = now()
    log_audit(db, user, "Generated a CSR section", "csr_section", section.id,
              cp.project_id, "info",
              f"{section.section_number} v{draft.version} ({len(chunks)} sources)")
    db.commit()
    db.refresh(draft)
    return {**_draft_out(draft, citations),
            "section": _section_out(section),
            "data_needed": result.data_needed}


@router.get("/csr/sections/{csr_section_id}/draft")
def get_draft(csr_section_id: str, version: int | None = None,
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """The current draft, or a named earlier version. Every version is kept:
    what the model wrote and what the writer changed are both part of the
    record."""
    section = _owned_section(db, csr_section_id, user)
    if version is not None:
        draft = db.scalar(select(CsrSectionDraft).where(
            CsrSectionDraft.csr_section_id == section.id,
            CsrSectionDraft.version == version))
    else:
        draft = _latest_draft(db, section.id)
    versions = db.scalars(select(CsrSectionDraft.version).where(
        CsrSectionDraft.csr_section_id == section.id
    ).order_by(CsrSectionDraft.version)).all()
    if draft is None:
        return {"section": _section_out(section), "draft": None, "versions": list(versions),
                "sources": []}
    citations = db.scalars(select(CsrCitation).where(
        CsrCitation.draft_id == draft.id)).all()
    chunk_ids = [c.chunk_id for c in citations if c.chunk_id]
    chunk_ids += [s.get("chunk_id") for s in (draft.generation_params or {}).get("source_map", [])
                  if s.get("chunk_id")]
    sources = []
    if chunk_ids:
        rows = db.scalars(select(CsrChunk).where(CsrChunk.id.in_(set(chunk_ids)))).all()
        by_id = {c.id: c for c in rows}
        documents = {d.id: d for d in db.scalars(select(CsrDocument).where(
            CsrDocument.csr_project_id == section.csr_project_id)).all()}
        for entry in (draft.generation_params or {}).get("source_map", []):
            chunk = by_id.get(entry.get("chunk_id"))
            if chunk is None:
                continue
            document = documents.get(chunk.document_id)
            sources.append({
                "marker": entry.get("marker"), "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "filename": document.original_filename if document else None,
                "doc_type": chunk.doc_type, "page": chunk.page,
                "table_id": chunk.table_id, "is_table": chunk.is_table,
                "content": chunk.content,
            })
    return {"section": _section_out(section), "draft": _draft_out(draft, citations),
            "versions": list(versions), "sources": sources}


class DraftEdit(BaseModel):
    content: str


@router.put("/csr/sections/{csr_section_id}/draft", status_code=201)
def edit_draft(csr_section_id: str, body: DraftEdit,
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """A human edit, saved as a new version under that person's name.

    Never an overwrite: the model's text and the writer's correction are two
    versions of one section, and which is which is exactly what an auditor of
    an AI-assisted document needs to see.
    """
    section = _owned_section(db, csr_section_id, user)
    previous = _latest_draft(db, section.id)
    draft = CsrSectionDraft(
        org_id=user.org_id, csr_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=body.content, created_by=user.id, model=None,
        generation_params={"edited_from_version": previous.version if previous else None,
                           "source_map": (previous.generation_params or {}).get("source_map", [])
                           if previous else []})
    db.add(draft)
    db.flush()
    # Citations are re-resolved against the same sources the draft was written
    # from, so a writer who deletes a sentence deletes its citation with it.
    if previous is not None:
        from app.csr.drafting import parse_citations

        source_map = (previous.generation_params or {}).get("source_map", [])
        for citation in parse_citations(body.content, source_map):
            db.add(CsrCitation(org_id=user.org_id, draft_id=draft.id, **citation))
    if section.status in ("not_started", "generating"):
        section.status = "draft"
    section.updated_at = now()
    log_audit(db, user, "Edited a CSR section", "csr_section", section.id, None,
              "info", f"{section.section_number} v{draft.version}")
    db.commit()
    db.refresh(draft)
    return _draft_out(draft)


SECTION_STATUSES = ("draft", "in_review", "approved")


class StatusPatch(BaseModel):
    status: str


@router.patch("/csr/sections/{csr_section_id}/status")
def set_section_status(csr_section_id: str, body: StatusPatch,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Move a section along Draft -> In Review -> Approved.

    Approval is the human-in-the-loop gate export depends on, so it needs
    something to approve: a section with no draft cannot be approved into
    existence.
    """
    section = _owned_section(db, csr_section_id, user)
    if body.status not in SECTION_STATUSES:
        raise error("CSR_BAD_STATUS",
                    f"status must be one of {', '.join(SECTION_STATUSES)}.", 422)
    if _latest_draft(db, section.id) is None:
        raise error("CSR_NOTHING_TO_REVIEW",
                    "This section has no draft yet.", 409)
    section.status = body.status
    section.updated_at = now()
    log_audit(db, user, "Set a CSR section status", "csr_section", section.id, None,
              "success" if body.status == "approved" else "info",
              f"{section.section_number} -> {body.status}")
    db.commit()
    return _section_out(section)
