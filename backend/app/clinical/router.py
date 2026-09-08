"""The clinical service: a study book, a numbered document registry, and one
endpoint that turns a manifest plus typed values into a stored, numbered
clinical document.

The generation itself is `generation.single.generate_one` -- the same core the
manifest and invoice endpoints use -- wrapped in what makes a clinical document
a clinical document: the study identity comes from the study book and is
snapshotted as reported, the number comes from a per-document-type org-scoped
sequence (CSR-0001 and PA-0001 never share a counter), and any derived totals
over the repeating table are computed server-side in Decimal
(`app.clinical.derivations`). Everything happens inside the one transaction
that also stores the document, so a failed fill rolls the number allocation
back instead of burning CSR-0042 on a document that never existed.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.authz import APPROVE_DOCUMENT, has_capability, require
from app.clinical.derivations import derive_row_totals
from app.clinical.service import DOC_TYPES
from app.db import get_db
from app.generation.single import FillFailed, _next_display_id, generate_one
from app.metrics import record_qa_overrides
from app.models import (
    ClinicalDocument, Project, Study, TemplateFile, TemplateManifest,
    TemplateVersion, User, now,
)
from app.numbering import allocate
from app.ownership import owned_manifest, owned_project
from app.security import error, get_current_user

router = APIRouter(tags=["clinical"])

#: The function every lazily-created workspace project carries. The document
#: type varies per request (a CSR and a consent form do not share a project),
#: unlike the invoice service's single Invoice workspace.
WORKSPACE_FUNCTION = "Clinical"


# ------------------------------------------------------------------ studies

class StudyIn(BaseModel):
    protocol_number: str
    title: str | None = None
    sponsor: str | None = None
    phase: str | None = None
    indication: str | None = None
    principal_investigator: str | None = None
    status: str | None = None
    notes: str | None = None


def _study_out(s: Study) -> dict:
    return {
        "id": s.id, "protocol_number": s.protocol_number, "title": s.title,
        "sponsor": s.sponsor, "phase": s.phase, "indication": s.indication,
        "principal_investigator": s.principal_investigator,
        "status": s.status, "notes": s.notes,
        "created_at": s.created_at, "updated_at": s.updated_at,
    }


def _owned_study(db: Session, study_id: str, user: User) -> Study:
    s = db.get(Study, study_id)
    if not s or s.org_id != user.org_id or s.deleted_at is not None:
        raise error("STUDY_NOT_FOUND", "Study not found", 404)
    return s


@router.get("/studies")
def list_studies(q: str | None = None, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    stmt = select(Study).where(
        Study.org_id == user.org_id, Study.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Study.protocol_number.ilike(f"%{q}%")
                          | Study.title.ilike(f"%{q}%"))
    rows = db.scalars(stmt.order_by(Study.protocol_number)).all()
    return {"items": [_study_out(s) for s in rows]}


@router.post("/studies", status_code=201)
def create_study(body: StudyIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    if not body.protocol_number.strip():
        raise error("STUDY_NEEDS_PROTOCOL", "A study needs a protocol number.", 422)
    s = Study(org_id=user.org_id, created_by=user.id,
              **{**body.model_dump(exclude_none=False),
                 "status": body.status or "active"})
    db.add(s)
    db.flush()
    log_audit(db, user, "Added a study", "study", s.id, None, "info",
              s.protocol_number)
    db.commit()
    db.refresh(s)
    return _study_out(s)


@router.get("/studies/{study_id}")
def get_study(study_id: str, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    return _study_out(_owned_study(db, study_id, user))


class StudyPatch(BaseModel):
    protocol_number: str | None = None
    title: str | None = None
    sponsor: str | None = None
    phase: str | None = None
    indication: str | None = None
    principal_investigator: str | None = None
    status: str | None = None
    notes: str | None = None


@router.patch("/studies/{study_id}")
def update_study(study_id: str, body: StudyPatch,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = _owned_study(db, study_id, user)
    changed = body.model_dump(exclude_unset=True)
    if "protocol_number" in changed and not (changed["protocol_number"] or "").strip():
        raise error("STUDY_NEEDS_PROTOCOL", "A study needs a protocol number.", 422)
    for key, value in changed.items():
        setattr(s, key, value)
    s.updated_at = now()
    log_audit(db, user, "Updated a study", "study", s.id, None, "info",
              s.protocol_number)
    db.commit()
    db.refresh(s)
    return _study_out(s)


@router.delete("/studies/{study_id}")
def delete_study(study_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Soft delete. Documents keep their snapshot, so history is undisturbed."""
    s = _owned_study(db, study_id, user)
    s.deleted_at = now()
    log_audit(db, user, "Removed a study", "study", s.id, None, "info",
              s.protocol_number)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------- workspace

def _workspace_project(db: Session, user: User, doc_type_label: str) -> Project:
    """The org's project for this clinical document type, created on first use.

    Lazily, so an organisation that never writes consent forms never carries a
    project for them -- and found by function/document_type rather than by
    name, so renaming the project does not orphan it. One project per document
    type, unlike the invoice service's single workspace: a study report and a
    consent form do not belong in one pile.
    """
    project = db.scalar(select(Project).where(
        Project.org_id == user.org_id,
        Project.function == WORKSPACE_FUNCTION,
        Project.document_type == doc_type_label,
        Project.deleted_at.is_(None),
    ).order_by(Project.created_at))
    if project is not None:
        return project
    project = Project(
        org_id=user.org_id, display_id=_next_display_id(db, "project_display_id", 51000),
        name=f"{doc_type_label}s",
        description=f"{doc_type_label} documents generated by the clinical service.",
        region="Global", function=WORKSPACE_FUNCTION,
        document_type=doc_type_label, language="English",
        status="active", created_by=user.id)
    db.add(project)
    db.flush()
    log_audit(db, user, "Created a clinical workspace", "project", project.id,
              project.id, "info", project.name)
    return project


# ------------------------------------------------------------- the registry

def _document_out(doc: ClinicalDocument) -> dict:
    return {
        "id": doc.id, "number": doc.number, "status": doc.status,
        "project_id": doc.project_id, "study_id": doc.study_id,
        "study": doc.study_snapshot,
        "document_type": doc.document_type, "title": doc.title,
        "version_label": doc.version_label,
        "manifest_id": doc.manifest_id, "document_id": doc.document_id,
        "document_version_id": doc.document_version_id,
        "document_date": doc.document_date,
        "qa_passed": doc.qa_passed,
        "created_at": doc.created_at,
    }


def _owned_clinical_document(db: Session, document_id: str, user: User) -> ClinicalDocument:
    doc = db.get(ClinicalDocument, document_id)
    if not doc or doc.org_id != user.org_id or doc.deleted_at is not None:
        raise error("CLINICAL_DOCUMENT_NOT_FOUND", "Clinical document not found", 404)
    return doc


@router.get("/clinical-documents")
def list_clinical_documents(study_id: str | None = None,
                            project_id: str | None = None,
                            document_type: str | None = None,
                            status_: str | None = Query(None, alias="status"),
                            limit: int = Query(200, ge=1, le=500),
                            db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    stmt = select(ClinicalDocument).where(
        ClinicalDocument.org_id == user.org_id, ClinicalDocument.deleted_at.is_(None))
    if study_id:
        stmt = stmt.where(ClinicalDocument.study_id == study_id)
    if project_id:
        stmt = stmt.where(ClinicalDocument.project_id == project_id)
    if document_type:
        stmt = stmt.where(ClinicalDocument.document_type == document_type)
    if status_:
        stmt = stmt.where(ClinicalDocument.status == status_)
    rows = db.scalars(stmt.order_by(ClinicalDocument.created_at.desc()).limit(limit)).all()
    return {"items": [_document_out(d) for d in rows]}


@router.get("/clinical-documents/{clinical_document_id}")
def get_clinical_document(clinical_document_id: str, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    doc = _owned_clinical_document(db, clinical_document_id, user)
    return {**_document_out(doc), "source_record": doc.source_record}


@router.post("/clinical-documents/{clinical_document_id}:void")
def void_clinical_document(clinical_document_id: str, db: Session = Depends(get_db),
                           user: User = Depends(require(APPROVE_DOCUMENT))):
    """Mark a clinical document void. The row and its number remain -- a
    numbering with silent gaps is what voiding exists to avoid.

    Gated on APPROVE_DOCUMENT, exactly like voiding an invoice: it is the
    sign-off decision run backwards, and a role that cannot approve a study
    document has no business cancelling one."""
    doc = _owned_clinical_document(db, clinical_document_id, user)
    if doc.status == "void":
        raise error("CLINICAL_DOCUMENT_ALREADY_VOID",
                    "This document is already void.", 409)
    doc.status = "void"
    doc.updated_at = now()
    log_audit(db, user, "Voided a clinical document", "clinical_document", doc.id,
              doc.project_id, "warning", doc.number)
    db.commit()
    return _document_out(doc)


class ClinicalGenerateRequest(BaseModel):
    manifest_id: str
    #: A request key of `app.clinical.service.DOC_TYPES`: csr,
    #: protocol_amendment, icf, or investigator_brochure.
    document_type: str
    project_id: str | None = None
    study_id: str | None = None
    #: A one-off study, when there is no book entry:
    #: {protocol_number, title, sponsor, phase, indication, principal_investigator}.
    study: dict | None = None
    #: The repeating table's records (disposition rows, visits, amendment
    #: items -- whatever the template's own TABLE_ROW iterates over). Empty is
    #: fine when the template has no repeating table, or an optional one.
    rows: list[dict] = []
    #: Scalar template fields beyond the ones this endpoint computes --
    #: narrative summaries, descriptions, anything the template asks for.
    fields: dict = {}
    title: str | None = None
    document_date: str | None = None
    version_label: str | None = None
    language: str = "en"
    locale: str | None = None


def _parse_date(value: str | None, field_name: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise error("BAD_DATE", f"{field_name} is not an ISO date: {value!r}", 422)


@router.post("/clinical-documents:generate", status_code=201)
def generate_clinical_document(body: ClinicalGenerateRequest,
                               db: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    m = owned_manifest(db, body.manifest_id, user)
    tv = db.get(TemplateVersion, m.template_version_id)
    tf = db.get(TemplateFile, m.template_file_id) if m.template_file_id else None

    # The same readiness gates as the manifest endpoint, with the same codes --
    # a study document is a real, stored, downloadable document, so a rule
    # enforced there and not here would be a rule with a door next to it.
    if m.status == "failed":
        raise error(
            "MANIFEST_NOT_READ",
            "The compiler could not produce a usable reading of this template, so there is "
            "nothing to fill from. Publish or re-read the template first.", 409)
    if m.status in ("superseded", "deprecated"):
        raise error(
            "MANIFEST_RETIRED",
            "This reading of the template has been replaced by a newer one. Use the current "
            "manifest for this template.", 409)
    if tf is not None and tf.legally_binding and m.status != "approved":
        raise error(
            "MANIFEST_NOT_APPROVED",
            "This template is marked legally binding, so it needs sign-off before it can "
            "generate.", 409)

    label = DOC_TYPES.get((body.document_type or "").strip().lower())
    if label is None:
        raise error(
            "CLINICAL_BAD_DOCUMENT_TYPE",
            f"Unknown clinical document type {body.document_type!r}; one of "
            f"{', '.join(DOC_TYPES)}.", 422)
    numbering_key = (body.document_type or "").strip().lower()

    if body.project_id:
        project = owned_project(db, body.project_id, user)
    elif tf is not None and tf.project_id:
        # `owned_project` and not a raw get: it refuses another org's project
        # AND a soft-deleted one, and new documents must not be written into a
        # project the organisation deleted.
        project = owned_project(db, tf.project_id, user)
    else:
        project = _workspace_project(db, user, label)

    # -- which study this document is about --
    study_row = None
    if body.study_id:
        study_row = _owned_study(db, body.study_id, user)
        snapshot = {
            "protocol_number": study_row.protocol_number, "title": study_row.title,
            "sponsor": study_row.sponsor, "phase": study_row.phase,
            "indication": study_row.indication,
            "principal_investigator": study_row.principal_investigator,
        }
    elif body.study:
        snapshot = {k: body.study.get(k)
                    for k in ("protocol_number", "title", "sponsor", "phase",
                              "indication", "principal_investigator")}
    else:
        snapshot = {}

    # -- the content gates, in the invoice's order: what the document is made
    # of first, then who it is about --
    spec = next((b for b in (m.blocks or ())
                 if str((b or {}).get("object_type") or "").upper() == "TABLE_ROW"), None)
    rows = [r for r in body.rows or []
            if isinstance(r, dict) and any(
                v is not None and str(v).strip() for v in r.values())]
    if spec is not None and spec.get("required") and not rows:
        raise error("CLINICAL_NEEDS_ROWS",
                    "This template's table repeats per record and is required -- "
                    "add at least one row.", 422)
    if not (snapshot.get("protocol_number") or "").strip():
        raise error(
            "CLINICAL_NEEDS_STUDY",
            "A clinical document is about a study: pass study_id from the study book, or "
            "an inline study with at least a protocol number.", 422)

    # -- what the server owns, the caller cannot supply. Without this, an
    # incomplete column's honestly-absent total would let a caller-typed
    # fields["total_subjects_enrolled"] print as if the server had derived it,
    # and fields[<collection name>] would smuggle rows past the filter above. --
    reserved_keys: set = set()
    collection_key = None
    if spec is not None:
        collection_key = str(spec.get("iterate_over") or "").strip()
        if not collection_key:
            raise error(
                "CLINICAL_BAD_TEMPLATE",
                "This template's repeating table names no collection to iterate over. "
                "Fix the template and publish it again.", 422)
        reserved_keys.add(collection_key)
        reserved_keys.add("row_count")
        for column in spec.get("columns") or ():
            source_key = str((column or {}).get("source_key") or "").strip()
            if source_key:
                reserved_keys.add(f"total_{source_key}")

    totals = derive_row_totals(rows, (spec or {}).get("columns") or [])
    doc_date = _parse_date(body.document_date, "document_date") or now()

    # -- the number, allocated in this transaction so a failed fill returns it --
    number = allocate(db, user.org_id, numbering_key)

    # -- the fill input. Caller's fields first; what this endpoint computes
    # wins over anything the caller typed, because the snapshot and the Decimal
    # totals are the authoritative record. Only *values* win, never absences:
    # the computed dict is filtered for None BEFORE the merge, so a book study
    # with no recorded sponsor cannot erase a sponsor_name the caller supplied
    # in `fields` -- the same defect the invoice endpoint already met once. --
    date_iso = body.document_date or doc_date.date().isoformat()
    computed = {
        "document_number": number,
        "document_date": date_iso,
        "report_date": date_iso,
        "version_label": body.version_label,
        "document_title": body.title,
        "study_title": snapshot.get("title"),
        "protocol_number": snapshot.get("protocol_number"),
        "sponsor_name": snapshot.get("sponsor"),
        "investigator_name": snapshot.get("principal_investigator"),
        "principal_investigator": snapshot.get("principal_investigator"),
        "phase": snapshot.get("phase"),
        "indication": snapshot.get("indication"),
        **totals,
    }
    if collection_key is not None and rows:
        # Under the manifest's own collection name (disposition_rows, visits,
        # amendment_items, or whatever a model-authored template chose) --
        # never a name hardcoded here. A template whose collection name is
        # also a computed scalar would have the rows list clobber a number or
        # a date, so the clash is refused loudly rather than resolved quietly.
        if collection_key in computed:
            raise error(
                "CLINICAL_BAD_TEMPLATE",
                f"This template's repeating table is named {collection_key!r}, which is "
                "also a value this endpoint computes. Rename the collection in the "
                "template.", 422)
        computed[collection_key] = rows
    source_record = {
        **{k: v for k, v in body.fields.items()
           if v is not None and k not in reserved_keys},
        **{k: v for k, v in computed.items() if v is not None},
    }

    try:
        outcome = generate_one(
            db, user, manifest=m, template_version=tv, project=project,
            source_record=source_record, language=body.language, locale=body.locale,
            change_summary=f"{label} {number}",
        )
    except FillFailed as exc:
        raise error("FILL_FAILED", f"Could not generate the document: {exc}", 422)

    # A document the person just generated from values they typed, that passed
    # every QA gate, is approved in the same act -- same shape and same
    # refusals as the invoice endpoint: a role without APPROVE_DOCUMENT waits
    # for sign-off, a legally-binding template waits for a second person, and
    # a QA-blocked document stays blocked and undownloadable.
    approval_note = None
    approved = False
    if outcome.fill.qa_passed:
        if not has_capability(user, APPROVE_DOCUMENT):
            approval_note = (
                f"Your role ({user.role_key}) cannot approve documents, so this document is "
                "waiting for sign-off before it can be downloaded.")
        elif tf is not None and tf.legally_binding:
            approval_note = (
                "This template is marked legally binding, so the document needs a second "
                "person's sign-off before it can be downloaded.")
        else:
            outcome.version.status = "approved"
            outcome.version.approved_by = user.id
            outcome.version.approved_at = now()
            outcome.document.status = "approved"
            approved = True
            record_qa_overrides(db, document_version_id=outcome.version.id, user_id=user.id)
            log_audit(db, user, "Approved draft", "document_version", outcome.version.id,
                      project.id, "success")

    doc = ClinicalDocument(
        org_id=user.org_id, project_id=project.id, number=number,
        study_id=study_row.id if study_row else None,
        study_snapshot=snapshot, document_type=numbering_key,
        title=body.title or f"{label} {number}",
        version_label=body.version_label,
        manifest_id=m.id, document_id=outcome.document.id,
        document_version_id=outcome.version.id,
        document_date=doc_date,
        status="final" if approved else "draft",
        qa_passed=outcome.fill.qa_passed, source_record=source_record,
        created_by=user.id)
    db.add(doc)
    db.flush()
    log_audit(db, user, "Generated a clinical document", "clinical_document", doc.id,
              project.id, "info" if outcome.fill.qa_passed else "warning", number)
    db.commit()
    db.refresh(doc)

    return {
        **_document_out(doc),
        "filename": outcome.filename,
        "qa_notes": outcome.fill.qa_notes,
        "approval_note": approval_note,
        "locale": outcome.locale, "locale_source": outcome.locale_source,
    }
