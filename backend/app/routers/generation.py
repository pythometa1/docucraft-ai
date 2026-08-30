import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import APPROVE_DOCUMENT, require
from app.db import get_db
from app.models import (
    Counter, DocumentReview, DocumentVersion, DraftDocument, GeneratedDocument, GenerationJob,
    Project, ReviewTask, SectionOutput, SourceChunk, TemplateLibrary, TemplateLibraryVersion,
    TemplateSection, TemplateVersion, User,
)
from app.security import error, get_current_user
from app.audit.service import log_audit
from app.downloads import GRANT_TTL_SECONDS, issue as issue_download_grant, redeem as redeem_download_grant
from app.metrics import record_qa_overrides
from app.retention import delete_generated_document
from app.ownership import owned_document, owned_document_version, owned_draft, owned_project
from app.generation.document_status import BLOCKED, refresh_status
from app.generation.legacy_assembly import assemble_from_html
from app.generation.text_edit import EditRejected, apply_edits, read_document, structural_diff
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
    llm = get_llm_provider(
        "Token-template generation",
        policy=llm_policy_for(db, user.org_id, project_id=body.project_id, user_id=user.id,
                              subject_type="template_library", subject_id=library_id))

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
    # Whether somebody has already objected, as a fact rather than an inference.
    # The documents list used to read it off `status == "changes_requested"`, but
    # `derive_status` is worst-first and returns `blocked` before it ever looks at
    # the reviews -- so a letter that both failed QA and had an open review looked
    # like it had none, offered "Request changes", and answered 409.
    open_review = db.scalars(
        select(DocumentReview.id).where(
            DocumentReview.document_version_id == gd.current_version_id,
            DocumentReview.state == "open",
        )
    ).first() if gd.current_version_id else None
    return {
        "id": gd.id, "display_id": gd.display_id, "filename": filename, "language": gd.language,
        "status": gd.status, "status_reason": version.status_reason if version else None,
        "current_version_id": gd.current_version_id,
        "open_review_id": open_review,
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
    open_review = db.scalars(
        select(DocumentReview)
        .where(DocumentReview.document_version_id == dv.id, DocumentReview.state == "open")
    ).first()
    return {
        "id": dv.id, "html_content": dv.html_content, "status": dv.status,
        # Why it is in that state. A chip reading "changes requested" with no
        # words attached sends the reader looking for the review; this is the
        # words.
        "status_reason": dv.status_reason,
        "version_no": dv.version_no, "renderer": dv.renderer,
        "html_editable": is_html_editable(dv.renderer),
        "document_id": dv.document_id,
        "approved_by": dv.approved_by, "approved_at": dv.approved_at,
        "open_review_id": open_review.id if open_review else None,
    }


# ---------------------------------------------------------------- text editing
#
# The narrow editor. A letter produced by filling a Word template cannot be
# rebuilt from HTML without losing everything HTML has no word for -- section
# breaks, headers, numbering, cell borders -- so `save_document_version` refuses
# it. That refusal used to mean "no editing at all". These three endpoints are
# what editing means instead: read the runs, ask a model to rewrite one, write
# the words back into that same run and touch nothing else.


@router.get("/document-versions/{version_id}/text")
def get_version_text(version_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The letter as editable runs, addressed the way everything else addresses it.

    `(paragraph_index, span_index)` is the coordinate the compiler, the fill
    engine and the QA gates already speak, so an edit, a manifest slot and a QA
    finding all point at the same run with no translation to get wrong.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not dv.blob_path:
        raise error("VERSION_HAS_NO_FILE", "This version has no document to edit.", 409)
    path = str(abs_path(dv.blob_path))
    if not os.path.exists(path):
        raise error("VERSION_FILE_MISSING", "This version's file is no longer on disk.", 410)

    paragraphs = read_document(path)
    return {
        "version_id": dv.id,
        "document_id": gd.id,
        "version_no": dv.version_no,
        "status": dv.status,
        "renderer": dv.renderer,
        # Text editing is available for every renderer, because it never
        # rebuilds the file. `html_editable` remains a separate, narrower claim
        # about whether the whole document may be replaced from HTML.
        "text_editable": True,
        "html_editable": is_html_editable(dv.renderer),
        "paragraphs": [p.as_dict() for p in paragraphs],
    }


class EditSuggestion(BaseModel):
    selection: str
    instruction: str
    paragraph_index: int | None = None


SUGGEST_SYSTEM = (
    "You are helping someone edit one passage of a finished business letter.\n\n"
    "Return ONLY the replacement text for the passage you are given. No markup, no markdown, no "
    "quotation marks around it, no commentary, no explanation of what you changed -- the value you "
    "return is written straight into the document exactly as you write it.\n\n"
    "Keep the register and the conventions of the surrounding letter. Do not invent facts: if the "
    "instruction asks for a name, a date, an amount or a term that the passage and its context do "
    "not contain, leave that part of the wording as it is and say what is missing in `note`. A "
    "letter that reads well and states something untrue is the worst outcome here."
)

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "replacement": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["replacement", "note"],
    "additionalProperties": False,
}


@router.post("/document-versions/{version_id}/suggest-edit")
def suggest_edit(version_id: str, body: EditSuggestion, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ask a model to rewrite one selected passage. Returns text, never a saved change.

    The model is given the passage, the instruction and the paragraphs around it,
    and constrained to return replacement text only. It cannot return markup, so
    it cannot introduce structure; it cannot write to the document, so a person
    is always the one who accepts.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not (body.selection or "").strip():
        raise error("NO_SELECTION", "Select some text to rewrite.", 422)
    if not (body.instruction or "").strip():
        raise error("NO_INSTRUCTION", "Say what you would like changed.", 422)

    path = str(abs_path(dv.blob_path)) if dv.blob_path else None
    context = ""
    if path and os.path.exists(path) and body.paragraph_index is not None:
        paragraphs = read_document(path)
        lo, hi = max(0, body.paragraph_index - 3), body.paragraph_index + 4
        context = "\n".join(p.text for p in paragraphs[lo:hi] if p.text.strip())

    provider = get_llm_provider(
        "Suggesting a document edit",
        policy=llm_policy_for(db, user.org_id, project_id=gd.project_id, user_id=user.id,
                              subject_type="document_version", subject_id=dv.id))
    result = provider.structured(
        system=SUGGEST_SYSTEM,
        prompt=(
            f"SURROUNDING TEXT:\n{context or '(not available)'}\n\n"
            f"PASSAGE TO REWRITE:\n{body.selection}\n\n"
            f"WHAT TO CHANGE:\n{body.instruction}"
        ),
        schema=SUGGEST_SCHEMA,
        purpose="generate",
    )
    if result.data is None:
        raise error("SUGGESTION_UNAVAILABLE", f"The model could not answer: {result.error}", 502)
    return {
        "replacement": result.data.get("replacement", ""),
        "note": result.data.get("note", ""),
        "model": result.model,
    }


class TextEdit(BaseModel):
    paragraph_index: int
    span_index: int
    text: str


class TextEditRequest(BaseModel):
    edits: list[TextEdit]
    change_summary: str | None = None


@router.post("/document-versions/{version_id}/text", status_code=201)
def apply_version_text(version_id: str, body: TextEditRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Write edited text back, as a new version, touching only the runs named.

    A new `DocumentVersion` rather than an overwrite: an approved letter that
    changes under the same id is not auditable, and this product's whole claim
    is that a finished document can be traced to what produced it.

    The structural check before the commit is not ceremony. This module says a
    text edit changes text and nothing else; verifying it costs one canonical
    comparison, and the alternative is trusting a guarantee nobody measured.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not dv.blob_path:
        raise error("VERSION_HAS_NO_FILE", "This version has no document to edit.", 409)
    source = str(abs_path(dv.blob_path))
    if not os.path.exists(source):
        raise error("VERSION_FILE_MISSING", "This version's file is no longer on disk.", 410)
    if dv.status in ("approved", "final"):
        raise error(
            "VERSION_APPROVED",
            "This version is approved. Editing it would change a document somebody signed off. "
            "Create a new version first.",
            409,
        )

    next_no = (db.scalar(
        select(DocumentVersion.version_no)
        .where(DocumentVersion.document_id == gd.id)
        .order_by(DocumentVersion.version_no.desc())
    ) or 0) + 1
    out_rel = f"generated/{gd.project_id}/{gd.id}_v{next_no}.docx"
    os.makedirs(str(abs_path(out_rel).parent), exist_ok=True)

    try:
        outcome = apply_edits(source, str(abs_path(out_rel)), [e.model_dump() for e in body.edits])
    except EditRejected as exc:
        raise error("EDIT_REJECTED", str(exc), 422) from exc

    moved = structural_diff(source, str(abs_path(out_rel)))
    if moved:
        os.remove(str(abs_path(out_rel)))
        raise error(
            "EDIT_CHANGED_LAYOUT",
            "This edit moved parts of the document it must not touch "
            f"({', '.join(moved)}). The change was discarded.",
            500,
        )

    version = DocumentVersion(
        document_id=gd.id, org_id=gd.org_id, version_no=next_no, blob_path=out_rel,
        renderer=dv.renderer,
        # The QA verdict carries forward. It came from the fill engine, which is
        # the only thing that read the finished file, and rewording a sentence
        # does not re-run it -- so a new version that started at "draft" was
        # *asserting* the document had been fixed rather than establishing it.
        #
        # That assertion was a way past both approval gates. Editing needs no
        # capability at all while `:approve` needs APPROVE_DOCUMENT, so any member
        # of the org could take a QA-blocked letter, change one word, and have it
        # signed. `changes_requested` is the one status an edit really does answer,
        # and `derive_status` drops that on its own by looking at the new version's
        # (empty) review list.
        status=BLOCKED if dv.status == BLOCKED else "draft",
        status_reason=dv.status_reason if dv.status == BLOCKED else None,
        change_summary=body.change_summary or outcome["summary"], created_by=user.id,
    )
    db.add(version)
    db.flush()
    gd.current_version_id = version.id

    # Open questions follow the document, not the words. A `ReviewTask` asks what
    # a *manifest unit* should resolve to -- "what is the pro-rata bonus?" -- and
    # rewriting the sentence around the figure does not answer it. Leaving them
    # pointing at the superseded version made them invisible to `derive_status`,
    # which counts per version, so the second gate opened too.
    db.query(ReviewTask).filter(
        ReviewTask.document_version_id == dv.id,
        ReviewTask.status == "open",
    ).update({"document_version_id": version.id}, synchronize_session=False)

    refresh_status(db, version=version, document=gd)
    log_audit(db, user, "Edited document text", "document_version", version.id, gd.project_id,
              "success", outcome["summary"][:200])
    db.commit()
    db.refresh(version)
    return {
        "version_id": version.id, "version_no": version.version_no,
        "change_summary": version.change_summary,
        "applied": outcome["applied"], "unchanged": outcome["unchanged"],
    }


class VersionPatch(BaseModel):
    html_content: str


@router.patch("/document-versions/{version_id}")
def save_document_version(version_id: str, body: VersionPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mutates the current version's content in place (spec §12.2) -- matches the
    frontend draft editor's autosave-while-editing model exactly."""
    dv, gd = owned_document_version(db, version_id, user)

    # The guard its sibling `apply_version_text` has, and this one did not.
    # Saving here overwrites the blob *in place*, so an approved letter's text
    # could be changed under its own signature -- `status`, `approved_by` and
    # `approved_at` all left untouched, by a caller who does not hold
    # APPROVE_DOCUMENT, with nothing in the audit trail. The other editor mints a
    # new version precisely so that cannot happen; this one had no equivalent.
    if dv.status in ("approved", "final"):
        raise error(
            "VERSION_APPROVED",
            "This version is approved. Editing it would change a document somebody signed off. "
            "Withdraw the approval first.",
            409,
        )

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
def approve_document_version(version_id: str, db: Session = Depends(get_db),
                             user: User = Depends(require(APPROVE_DOCUMENT))):
    """Sign the document off.

    Gated on `APPROVE_DOCUMENT`, which until now was declared in `authz.py` and
    enforced by nothing: any authenticated member of the org could approve any
    document in it. That is a live behaviour change -- a `generator` who could
    approve yesterday gets a 403 today -- and it is the intended one, because a
    capability nothing checks is a comment.

    The status is recomputed rather than read, so that an objection raised while
    somebody had the page open cannot be signed straight past.
    """
    dv, gd = owned_document_version(db, version_id, user)
    # Recompute before deciding. `dv.status` is what was true when the row was
    # last written; a review opened since then has not touched it if nothing
    # called refresh, and an approval that steps over a live objection is the
    # whole failure this feature exists to prevent.
    if dv.status not in ("approved", "final"):
        refresh_status(db, version=dv, document=gd)

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
    if dv.status == "changes_requested":
        raise error(
            "CHANGES_REQUESTED",
            "Somebody has asked for changes to this document: "
            f"{dv.status_reason or 'see the open review'}. "
            "Close the review before approving it.",
            409,
        )
    if dv.status == "pending_review":
        raise error(
            "DOCUMENT_PENDING_REVIEW",
            "The engine could not work out every value in this document and is still waiting for "
            "an answer. Approving it would record a human decision that nobody made. "
            "Resolve the open questions on the Review screen first.",
            409,
        )
    dv.status = "approved"
    dv.status_reason = None
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


class RevokeRequest(BaseModel):
    reason: str


@router.post("/document-versions/{version_id}:revoke")
def revoke_document_version(version_id: str, body: RevokeRequest,
                            db: Session = Depends(get_db),
                            user: User = Depends(require(APPROVE_DOCUMENT))):
    """Withdraw a signature that has already been given.

    Three things were wrong with this and each of them is the same shape --
    writing a fact instead of establishing one.

    It took no reason, so the record said an approval had been withdrawn and not
    why, which is the one thing anybody reads it for. It wrote no audit row,
    while granting the approval wrote one, so signing was traceable and
    unsigning was not. And it hardcoded `"draft"`, which is a *claim* that
    nothing else is wrong with the document -- a QA-blocked letter or one with an
    open review came back from a revoke looking clean, and `:approve` would then
    wave it through, because the only thing it refused was the string this had
    just overwritten.

    `refresh_status` replaces the hardcoded string: the document goes back to
    whatever is actually true of it.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not body.reason.strip():
        raise error(
            "REASON_REQUIRED",
            "Say why the approval is being withdrawn. It is what the next person to look at this "
            "document will read.",
            400,
        )
    if dv.status not in ("approved", "final"):
        raise error(
            "NOT_APPROVED",
            "This document is not approved, so there is no approval to withdraw.",
            409,
        )

    dv.approved_by = None
    dv.approved_at = None
    dv.status = "draft"  # cleared so refresh_status is willing to look; see SIGNED there
    status = refresh_status(db, version=dv, document=gd)
    log_audit(db, user, "Withdrew document approval", "document_version", version_id,
              gd.project_id, "warning", body.reason.strip()[:200])
    db.commit()
    return {"status": status, "status_reason": dv.status_reason}


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


PDF_MEDIA_TYPE = "application/pdf"

#: PDF conversion is one LibreOffice subprocess per document, so a large archive
#: belongs on a job with a worker rather than in a request that holds a connection.
MAX_BULK_DOCUMENTS = 100


def _download_filename(db: Session, gd: GeneratedDocument, ext: str = "docx") -> str:
    project = db.get(Project, gd.project_id)
    return f"{project.name}_{project.display_id}_{gd.display_id}_{gd.language}.{ext}"


def _as_pdf(docx_path: str, language: str) -> str:
    """Convert a generated .docx to PDF, or say why it cannot be done.

    LibreOffice is the converter, and it is an optional dependency of this
    deployment rather than a guaranteed one -- so its absence is a 503 naming the
    missing package, not a 500. The .docx is always available regardless, which
    is what the message says.
    """
    from app.generation.pdf_renderer import PreviewUnavailable, render_pdf

    try:
        result = render_pdf(docx_path, os.path.dirname(docx_path), language=language)
    except PreviewUnavailable as exc:
        raise error("PDF_UNAVAILABLE", str(exc), 503) from exc
    except Exception as exc:  # noqa: BLE001 - a converter failure is not a bug in the letter
        raise error(
            "PDF_CONVERSION_FAILED",
            f"The document could not be converted to PDF: {exc}. The .docx is unaffected.",
            502,
        ) from exc
    return result.pdf_path


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
def download_document_version(version_id: str, format: str = "docx", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The direct, authenticated download.

    Kept because the frontend fetches documents with a bearer token rather than
    by navigating, so removing it would break every download in the app for the
    sake of a redirect. It is now audited like the grant path -- the §16 gap was
    never that this route existed, it was that a fetch left no trace.
    """
    dv, gd = owned_document_version(db, version_id, user)
    if not dv.blob_path:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)
    if format not in ("docx", "pdf"):
        raise error("UNSUPPORTED_FORMAT", f"{format!r} is not a format this endpoint serves. Use docx or pdf.", 422)

    source = str(abs_path(dv.blob_path))
    if format == "pdf":
        path, media, ext = _as_pdf(source, gd.language or "en"), PDF_MEDIA_TYPE, "pdf"
    else:
        path, media, ext = source, DOCX_MEDIA_TYPE, "docx"

    log_audit(db, user, f"Downloaded a document ({ext})", "document_version", dv.id, gd.project_id, "success")
    db.commit()
    return FileResponse(path, filename=_download_filename(db, gd, ext), media_type=media)


class BulkDownload(BaseModel):
    document_ids: list[str]
    format: str = "docx"


@router.post("/documents:download")
def download_documents(body: BulkDownload, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Several documents as one zip, in the format asked for.

    Ownership is checked per document rather than once for the list: a caller
    can put any id in a JSON array, and a bulk endpoint that trusts the array is
    how one tenant reads another's letters.

    A document that cannot be converted does not fail the archive. It becomes a
    line in `_FAILED.txt` inside the zip, because a reviewer downloading two
    hundred letters needs the other hundred and ninety-nine plus a list of what
    is missing -- not a 502 and nothing.
    """
    import io
    import zipfile

    if not body.document_ids:
        raise error("NO_DOCUMENTS", "Select at least one document to download.", 422)
    if body.format not in ("docx", "pdf"):
        raise error("UNSUPPORTED_FORMAT", f"{body.format!r} is not a format this endpoint serves. Use docx or pdf.", 422)
    if len(body.document_ids) > MAX_BULK_DOCUMENTS:
        raise error(
            "TOO_MANY_DOCUMENTS",
            f"{len(body.document_ids)} documents were requested; this endpoint serves at most "
            f"{MAX_BULK_DOCUMENTS} at a time. PDF conversion runs one subprocess per document, so "
            f"a larger archive belongs on a batch job rather than a request.",
            422,
        )

    buffer = io.BytesIO()
    failed: list[str] = []
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        seen: set[str] = set()
        for document_id in body.document_ids:
            gd = owned_document(db, document_id, user)
            dv = db.get(DocumentVersion, gd.current_version_id) if gd.current_version_id else None
            if dv is None or not dv.blob_path:
                failed.append(f"{document_id}: no saved version to download")
                continue
            source = str(abs_path(dv.blob_path))
            if not os.path.exists(source):
                failed.append(f"{document_id}: the file is no longer on disk")
                continue
            try:
                path = _as_pdf(source, gd.language or "en") if body.format == "pdf" else source
            except Exception as exc:  # noqa: BLE001 - recorded in the archive, not raised
                detail = getattr(exc, "detail", None) or str(exc)
                if isinstance(detail, dict):
                    detail = detail.get("error", {}).get("message", str(exc))
                failed.append(f"{_download_filename(db, gd, body.format)}: {detail}")
                continue

            name = _download_filename(db, gd, body.format)
            # Two documents in one project can share a display name; a zip with
            # duplicate entries silently keeps one of them.
            if name in seen:
                stem, _dot, ext = name.rpartition(".")
                name = f"{stem}_{gd.display_id}.{ext}"
            seen.add(name)
            archive.write(path, arcname=name)
            written += 1

        if failed:
            archive.writestr(
                "_FAILED.txt",
                "These documents are not in this archive:\n\n" + "\n".join(failed) + "\n",
            )

    if not written:
        raise error(
            "NOTHING_TO_DOWNLOAD",
            "None of the selected documents could be prepared: " + "; ".join(failed[:3]),
            422,
        )

    log_audit(db, user, f"Downloaded {written} document(s) as {body.format}", "generated_document",
              body.document_ids[0], None, "success" if not failed else "warning",
              f"{written} written, {len(failed)} failed")
    db.commit()
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="documents_{body.format}.zip"'},
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
