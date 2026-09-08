"""The CSR module, milestone M1: create the project extension, choose the
built-in template, hold the ICH E3 section tree.

Positioning, stated once and rendered in the UI: this is an AI-ASSISTED
DRAFTING TOOL for medical writers. Sections will move Draft -> In Review ->
Approved under a person's hand; nothing exports unapproved. Later milestones
add ingestion (M2), grounded generation (M3), QC (M4) and export (M5); this
router refuses those surfaces rather than stubbing them silently.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.csr.ich_e3 import seed_sections
from app.db import get_db
from app.models import CsrProject, CsrSection, CsrTemplate, Project, Study, User, now
from app.ownership import owned_project
from app.security import error, get_current_user

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
    section_count = 0
    for section in db.scalars(select(CsrSection).where(
            CsrSection.csr_project_id == cp.id)).all():
        db.delete(section)
        section_count += 1
    for template in db.scalars(select(CsrTemplate).where(
            CsrTemplate.csr_project_id == cp.id)).all():
        db.delete(template)
    db.delete(cp)
    log_audit(db, user, "Deleted a CSR project", "csr_project", cp.id, cp.project_id,
              "warning", f"purged {section_count} sections")
    db.commit()
    return {"deleted": True, "purged_sections": section_count}


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
