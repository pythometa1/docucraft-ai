import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Counter, DocumentVersion, DraftDocument, GeneratedDocument, GenerationJob,
    Project, SectionOutput, SourceChunk, TemplateLibrary, TemplateLibraryVersion,
    TemplateSection, TemplateVersion, User,
)
from app.security import error, get_current_user
from app.audit.service import log_audit
from app.downloads import GRANT_TTL_SECONDS, issue as issue_download_grant, redeem as redeem_download_grant
from app.metrics import record_qa_overrides
from app.retention import delete_generated_document
from app.ownership import owned_document, owned_document_version, owned_draft, owned_project
from app.generation.legacy_assembly import assemble_from_html
from app.generation.narrative_engine import build_fact_sheet, resolve_token_unit
from app.llm.provider import get_llm_provider
from app.tenancy import llm_policy_for
from app.generation.renderers import DOCX_TEMPLATE_ASSEMBLY, HTML_ASSEMBLY, is_html_editable
from app.expressions.token_parser import fact_sheet_fields, parse_tokens, render_content_html
from app.storage import abs_path

router = APIRouter(tags=["generation"])


class LibraryGenerateRequest(BaseModel):
    project_id: str
    language: str = "en"


@router.post("/template-library/{library_id}/generate", status_code=201)
def generate_from_library(library_id: str, body: LibraryGenerateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Token-path generation (spec §8/§10.3) -- resolves every source/prompt/
    conditional/repeat token in a native template against a project's source
    chunks, with no template-section/mapping graph involved at all."""
    tl = db.get(TemplateLibrary, library_id)
    if not tl or tl.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template library entry not found", 404)
    project = db.get(Project, body.project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    tlv = db.get(TemplateLibraryVersion, tl.current_version_id)

    stamped_html, tokens, _source_fields = parse_tokens(tlv.content_html)
    chunks = db.scalars(select(SourceChunk).where(SourceChunk.project_id == body.project_id)).all()
    fact_sheet = build_fact_sheet(chunks, fact_sheet_fields(tokens))
    llm = get_llm_provider("Token-template generation", policy=llm_policy_for(db, user.org_id))

    resolved = {t.token_id: resolve_token_unit(token=t, chunks=chunks, fact_sheet=fact_sheet, llm=llm) for t in tokens}
    rendered_html = render_content_html(stamped_html, resolved)

    job = GenerationJob(org_id=user.org_id, project_id=body.project_id, template_library_version_id=tlv.id, status="completed", model_profile="claude-sonnet-4.6", languages=[body.language], started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc), created_by=user.id)
    db.add(job)
    db.flush()
    for t in tokens:
        db.add(SectionOutput(job_id=job.id, org_id=job.org_id, token_id=t.token_id, unit_kind="token", language=body.language, status="done", blocks=[{"type": "paragraph", "text": resolved[t.token_id], "citations": []}]))

    display_id = _next_display_id(db, "generated_doc_display_id", 50000)
    out_dir = f"generated/{body.project_id}"
    os.makedirs(str(abs_path(out_dir)), exist_ok=True)
    out_rel = f"{out_dir}/{job.id}.docx"
    assemble_from_html(rendered_html, str(abs_path(out_rel)))

    gen_doc = GeneratedDocument(org_id=user.org_id, project_id=body.project_id, draft_id=None, display_id=display_id, language=body.language)
    db.add(gen_doc)
    db.flush()
    dv = DocumentVersion(document_id=gen_doc.id, org_id=gen_doc.org_id, version_no=1, blob_path=out_rel, html_content=rendered_html, renderer=HTML_ASSEMBLY, change_summary="Initial generation (native template)", status="draft", created_by=user.id)
    db.add(dv)
    db.flush()
    gen_doc.current_version_id = dv.id

    tl.uses += 1
    log_audit(db, user, "Generated document", "generated_document", gen_doc.id, body.project_id, "info", f"from template library '{tl.name}'")
    db.commit()

    return {"job_id": job.id, "document_id": gen_doc.id, "document_version_id": dv.id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = db.get(GenerationJob, job_id)
    if not job or job.org_id != user.org_id:
        raise error("JOB_NOT_FOUND", "Job not found", 404)
    outputs = db.scalars(select(SectionOutput).where(SectionOutput.job_id == job_id)).all()
    return {
        "id": job.id, "status": job.status, "progress": job.progress, "token_usage": job.token_usage,
        "error": job.error,
        "sections": [{"section_id": o.section_id, "status": o.status, "grounding_score": o.grounding_score, "error": o.error} for o in outputs],
    }


def _doc_out(db: Session, gd: GeneratedDocument, project: Project) -> dict:
    filename = f"{project.name}_{project.display_id}_{gd.display_id}_{gd.language}.docx"
    # The dashboard used to print a constant "0.04 MB" and a constant author for
    # every document ever generated. Both are measurable, so measure them: a
    # size that never changes tells the user nothing about whether the fill
    # actually produced a document.
    version = db.get(DocumentVersion, gd.current_version_id) if gd.current_version_id else None
    size_bytes = None
    if version and version.blob_path:
        try:
            size_bytes = os.path.getsize(str(abs_path(version.blob_path)))
        except OSError:
            size_bytes = None  # blob missing on disk -- surfaced as "—", not invented
    author = db.get(User, version.created_by) if version and version.created_by else None
    draft = db.get(DraftDocument, gd.draft_id) if gd.draft_id else None
    return {
        "id": gd.id, "display_id": gd.display_id, "filename": filename, "language": gd.language,
        "status": gd.status, "current_version_id": gd.current_version_id,
        "size_bytes": size_bytes,
        "created_by_name": author.full_name if author else None,
        "draft_name": draft.name if draft else None,
        "created_at": gd.created_at,
    }


@router.get("/projects/{project_id}/documents")
def list_project_documents(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    rows = db.scalars(select(GeneratedDocument).where(GeneratedDocument.project_id == project_id)).all()
    return {"items": [_doc_out(db, gd, project) for gd in rows]}


@router.get("/documents/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    gd = db.get(GeneratedDocument, document_id)
    if not gd or gd.org_id != user.org_id:
        raise error("DOCUMENT_NOT_FOUND", "Document not found", 404)
    project = db.get(Project, gd.project_id)
    return _doc_out(db, gd, project)


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Destroy one generated document, its versions, and the file on disk.

    A hard delete, unlike a project's. `generated_documents` carries no
    `deleted_at`, and adding one would be the wrong fix: §16 requires deletion
    to reach the blob, and a flagged row leaves the rendered DOCX on disk while
    telling the customer it is gone.

    An approved document is refused. Approval is the point at which somebody
    put their name to the contents, and §16's four-eyes rule means the record of
    that signature is not the signer's to erase on their own -- revoke it first,
    which is auditable, and then it can go.
    """
    gd = db.get(GeneratedDocument, document_id)
    if not gd or gd.org_id != user.org_id:
        raise error("DOCUMENT_NOT_FOUND", "Document not found", 404)
    if gd.status == "approved":
        raise error(
            "DOCUMENT_APPROVED",
            "This document has been approved. Revoke the approval before deleting it, so the "
            "withdrawal is recorded against the person who made it.",
            409,
        )

    project_id = gd.project_id
    manifest = delete_generated_document(db, org_id=user.org_id, document_id=document_id)
    log_audit(
        db, user, "Deleted generated document", "generated_document", document_id,
        project_id=project_id, severity="warning",
    )
    db.commit()
    return {"status": "deleted", "blobs_deleted": len(manifest.blobs), "rows_deleted": manifest.counts}


@router.get("/documents/{document_id}/versions")
def list_document_versions(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_document(db, document_id, user)
    rows = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id).order_by(DocumentVersion.version_no.desc())).all()
    return {"items": [{"id": v.id, "version_no": v.version_no, "status": v.status, "change_summary": v.change_summary, "created_at": v.created_at} for v in rows]}


@router.get("/document-versions/{version_id}")
def get_document_version(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dv, _gd = owned_document_version(db, version_id, user)
    # `html_editable` rather than leaving the client to infer it from
    # `html_content`: the editor used to open on "this row has HTML", which is
    # true of template-rendered documents too -- so it offered a rich-text
    # editor over a document that a save would have destroyed. One decision,
    # made in one place, sent to the client.
    return {
        "id": dv.id, "html_content": dv.html_content, "status": dv.status,
        "version_no": dv.version_no, "renderer": dv.renderer,
        "html_editable": is_html_editable(dv.renderer),
    }


class VersionPatch(BaseModel):
    html_content: str


@router.patch("/document-versions/{version_id}")
def save_document_version(version_id: str, body: VersionPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mutates the current version's content in place (spec §12.2) -- matches the
    frontend draft editor's autosave-while-editing model exactly."""
    dv, _gd = owned_document_version(db, version_id, user)

    # A document whose layout came from a Word template must never be rebuilt
    # from HTML: `assemble_from_html` constructs a brand-new document, so saving
    # over one of these replaces a finished letter -- letterhead, tables,
    # headers, hyperlinks and all -- with whatever the editor happened to be
    # showing, at the same blob_path, with no way back.
    #
    # The test is which renderer produced the file, not whether the row happens
    # to carry HTML. The legacy path at `run_generation` stores an HTML *preview*
    # next to a template-filled .docx, so the old "has no html_content" guard
    # never fired on the one path that needed it.
    if dv.blob_path and not is_html_editable(dv.renderer):
        raise error(
            "DOCUMENT_NOT_HTML_EDITABLE",
            "This document was produced by filling a Word template, so its layout does not come from HTML. "
            "Saving here would rebuild it from scratch and lose the template's formatting. "
            "Download it and edit in Word instead.",
            409,
        )

    dv.html_content = body.html_content
    out_rel = dv.blob_path or f"generated/_edited/{dv.id}.docx"
    os.makedirs(str(abs_path(out_rel).parent), exist_ok=True)
    assemble_from_html(body.html_content, str(abs_path(out_rel)))
    dv.blob_path = out_rel
    db.commit()
    return {"status": "saved"}


@router.post("/document-versions/{version_id}:approve")
def approve_document_version(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dv, gd = owned_document_version(db, version_id, user)
    # A QA failure that an approval can step over is not a gate. The document
    # has an unresolved required field, an undecidable condition or leftover
    # scaffolding; the fix is to correct the manifest or the source row and
    # generate again, not to sign this one off.
    if dv.status == "blocked":
        raise error(
            "DOCUMENT_BLOCKED",
            "This document failed its QA checks and cannot be approved. "
            "Review the QA notes on its generation record, fix the manifest or the source data, and generate again.",
            409,
        )
    dv.status = "approved"
    dv.approved_by = user.id
    dv.approved_at = datetime.now(timezone.utc)
    gd.status = "approved"
    # §22 asks whether a human overrode a QA failure. A blocking one cannot be
    # overridden -- the guard above refuses the document outright -- so what is
    # recorded here is the warning-severity findings this person signed under.
    record_qa_overrides(db, document_version_id=version_id, user_id=user.id)
    log_audit(db, user, "Approved draft", "document_version", version_id, gd.project_id, "success")
    db.commit()
    return {"status": "approved"}


@router.post("/document-versions/{version_id}:revoke")
def revoke_document_version(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dv, gd = owned_document_version(db, version_id, user)
    dv.status = "draft"
    dv.approved_by = None
    dv.approved_at = None
    gd.status = "draft"
    db.commit()
    return {"status": "draft"}


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _download_filename(db: Session, gd: GeneratedDocument) -> str:
    project = db.get(Project, gd.project_id)
    return f"{project.name}_{project.display_id}_{gd.display_id}_{gd.language}.docx"


@router.post("/document-versions/{version_id}/download-url", status_code=201)
def create_download_url(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mint a short-lived, single-use link to this document (§16).

    Splitting the grant from the transfer is what makes the download auditable:
    the request that *asks* for a document carries an authenticated user, and
    that is the event worth recording. The transfer itself then needs no bearer
    token, so the URL can be followed by a browser without handing a credential
    to the address bar.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not dv.blob_path:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)

    grant = issue_download_grant(version_id=dv.id, org_id=gd.org_id, user_id=user.id)
    log_audit(db, user, "Requested a document download link", "document_version", dv.id, gd.project_id, "info")
    db.commit()
    return {
        "url": grant.path,
        "expires_in": GRANT_TTL_SECONDS,
        "filename": _download_filename(db, gd),
    }


@router.get("/document-versions/{version_id}/download")
def download_document_version(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The direct, authenticated download.

    Kept because the frontend fetches documents with a bearer token rather than
    by navigating, so removing it would break every download in the app for the
    sake of a redirect. It is now audited like the grant path -- the §16 gap was
    never that this route existed, it was that a fetch left no trace.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not dv.blob_path:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)
    log_audit(db, user, "Downloaded a document", "document_version", dv.id, gd.project_id, "success")
    db.commit()
    return FileResponse(
        str(abs_path(dv.blob_path)),
        filename=_download_filename(db, gd),
        media_type=DOCX_MEDIA_TYPE,
    )


@router.get("/document-versions/{version_id}/citations")
def get_citations(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _dv, gd = owned_document_version(db, version_id, user)
    job = None
    if gd.draft_id:
        job = db.scalar(select(GenerationJob).where(GenerationJob.draft_id == gd.draft_id).order_by(GenerationJob.created_at.desc()))
    if not job:
        return {"items": []}
    outputs = db.scalars(select(SectionOutput).where(SectionOutput.job_id == job.id)).all()
    items = []
    for o in outputs:
        for cid in (o.citations or []):
            chunk = db.get(SourceChunk, cid)
            if chunk:
                items.append({"section_id": o.section_id, "chunk_id": cid, "quote": chunk.text[:200], "heading_path": chunk.heading_path})
    return {"items": items}
