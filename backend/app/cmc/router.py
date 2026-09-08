"""The Quality/CMC module: a product's dossier work, its deliverables, and
the sections each one carries.

An AI-ASSISTED DRAFTING TOOL for CMC and regulatory writers. Sections move
Draft -> In Review -> Approved under a person's hand and nothing exports
unapproved; the numbers in a specification or a stability table are rendered
from verified data rather than written by a model at all.

M1 is the front door: the product, the sites that make and test it, which
deliverables are being written, and the section tree of each. Sources (M2),
the structured data store and its review grid (M3), the table renderer (M4)
and prose generation (M5) build on these rows.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.cmc import registry
from app.cmc.ctd import seed_sections
from app.db import get_db
from app.models import (
    CmcDeliverable, CmcProject, CmcSection, CmcSite, Project, User, now,
)
from app.ownership import owned_project
from app.security import error, get_current_user
from app.storage import abs_path, save_bytes

router = APIRouter(tags=["cmc"])

#: The portal taxonomy this module serves. A CMC dossier hung off a Finance
#: project would put trade-secret machinery where nobody expects it.
CMC_FUNCTION = "Quality-CMC"

SUBMISSION_TYPES = ("IND", "IMPD", "NDA", "ANDA", "MAA", "variation", "other")
REGIONS = ("FDA", "EMA", "CDSCO", "PMDA", "HC", "other")
SITE_ACTIVITIES = ("ds_manufacture", "dp_manufacture", "packaging", "testing", "release")
APPLICABILITY = ("applicable", "not_applicable", "referenced_dmf")


# ------------------------------------------------------------------ helpers

def _owned_cmc_project(db: Session, cmc_project_id: str, user: User) -> CmcProject:
    cp = db.get(CmcProject, cmc_project_id)
    if not cp or cp.org_id != user.org_id:
        raise error("CMC_PROJECT_NOT_FOUND", "CMC project not found", 404)
    return cp


def _owned_site(db: Session, site_id: str, user: User) -> CmcSite:
    site = db.get(CmcSite, site_id)
    if not site or site.org_id != user.org_id or site.deleted_at is not None:
        raise error("CMC_SITE_NOT_FOUND", "Site not found", 404)
    return site


def _owned_deliverable(db: Session, deliverable_id: str, user: User) -> CmcDeliverable:
    entry = db.get(CmcDeliverable, deliverable_id)
    if not entry or entry.org_id != user.org_id:
        raise error("CMC_DELIVERABLE_NOT_FOUND", "Deliverable not found", 404)
    return entry


def _owned_section(db: Session, section_id: str, user: User) -> CmcSection:
    section = db.get(CmcSection, section_id)
    if not section or section.org_id != user.org_id:
        raise error("CMC_SECTION_NOT_FOUND", "Section not found", 404)
    return section


def _site_out(s: CmcSite) -> dict:
    return {"id": s.id, "name": s.name, "address": s.address,
            "identifier": s.identifier, "activities": s.activities or [],
            "gmp_evidence_document_id": s.gmp_evidence_document_id,
            "created_at": s.created_at}


def _section_out(s: CmcSection) -> dict:
    return {"id": s.id, "section_code": s.section_code, "title": s.title,
            "sort_order": s.sort_order, "enabled": s.enabled,
            "is_container": s.is_container, "applicability": s.applicability,
            "applicability_justification": s.applicability_justification,
            "guidance_text": s.guidance_text, "table_key": s.table_key,
            "status": s.status}


def _deliverable_out(d: CmcDeliverable, *, section_count: int | None = None) -> dict:
    entry = registry.DELIVERABLES.get(d.doc_type_key) or {}
    return {"id": d.id, "doc_type_key": d.doc_type_key,
            "name": entry.get("name") or d.doc_type_key,
            "structure_basis": entry.get("structure_basis"),
            "template_source": d.template_source, "status": d.status,
            "section_count": section_count, "created_at": d.created_at}


def _project_out(db: Session, cp: CmcProject) -> dict:
    project = db.get(Project, cp.project_id)
    deliverables = db.scalars(select(CmcDeliverable).where(
        CmcDeliverable.cmc_project_id == cp.id).order_by(CmcDeliverable.created_at)).all()
    sites = db.scalars(select(CmcSite).where(
        CmcSite.cmc_project_id == cp.id,
        CmcSite.deleted_at.is_(None)).order_by(CmcSite.name)).all()
    keys = [d.doc_type_key for d in deliverables]
    required, recommended = registry.requirements(keys)
    return {
        "id": cp.id, "project_id": cp.project_id,
        "project_name": project.name if project else None,
        "product_name": cp.product_name, "inn_or_ds_name": cp.inn_or_ds_name,
        "dosage_form": cp.dosage_form, "strengths": cp.strengths or [],
        "route_of_administration": cp.route_of_administration,
        "submission_type": cp.submission_type,
        "target_regions": cp.target_regions or [],
        "development_phase": cp.development_phase,
        "baseline_version": cp.baseline_version, "status": cp.status,
        "sites": [_site_out(s) for s in sites],
        "deliverables": [_deliverable_out(d) for d in deliverables],
        "required_doc_types": list(required),
        "recommended_doc_types": list(recommended),
        "created_at": cp.created_at, "updated_at": cp.updated_at,
    }


# ------------------------------------------------------------------ projects

class CmcProjectIn(BaseModel):
    project_id: str
    product_name: str
    inn_or_ds_name: str | None = None
    dosage_form: str | None = None
    strengths: list = []
    route_of_administration: str | None = None
    submission_type: str | None = None
    target_regions: list = []
    development_phase: str | None = None


def _validate_project_fields(body) -> None:
    if body.submission_type and body.submission_type not in SUBMISSION_TYPES:
        raise error("CMC_BAD_SUBMISSION_TYPE",
                    f"submission_type must be one of {', '.join(SUBMISSION_TYPES)}.", 422)
    unknown = [r for r in (body.target_regions or []) if r not in REGIONS]
    if unknown:
        raise error("CMC_BAD_REGION",
                    f"unknown target region(s) {', '.join(map(str, unknown))}; one of "
                    f"{', '.join(REGIONS)}.", 422)


@router.post("/cmc/projects", status_code=201)
def create_cmc_project(body: CmcProjectIn, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    project = owned_project(db, body.project_id, user)
    if project.function != CMC_FUNCTION:
        raise error(
            "CMC_WRONG_PROJECT",
            f"A quality dossier lives in a {CMC_FUNCTION} project; this one is "
            f"{project.function}.", 422)
    if db.scalar(select(CmcProject).where(CmcProject.project_id == project.id)) is not None:
        raise error("CMC_PROJECT_EXISTS",
                    "This project already has a quality dossier. Open it instead.", 409)
    if not body.product_name.strip():
        raise error("CMC_NEEDS_PRODUCT", "A quality dossier needs a product name.", 422)
    _validate_project_fields(body)

    cp = CmcProject(
        org_id=user.org_id, project_id=project.id,
        product_name=body.product_name.strip(),
        inn_or_ds_name=body.inn_or_ds_name, dosage_form=body.dosage_form,
        strengths=[str(s) for s in (body.strengths or [])],
        route_of_administration=body.route_of_administration,
        submission_type=body.submission_type,
        target_regions=list(body.target_regions or []),
        development_phase=body.development_phase, created_by=user.id)
    db.add(cp)
    db.flush()
    log_audit(db, user, "Created a CMC project", "cmc_project", cp.id, project.id,
              "info", cp.product_name)
    db.commit()
    db.refresh(cp)
    return _project_out(db, cp)


@router.get("/cmc/projects")
def list_cmc_projects(db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    rows = db.scalars(select(CmcProject).where(
        CmcProject.org_id == user.org_id).order_by(CmcProject.created_at.desc())).all()
    return {"items": [_project_out(db, cp) for cp in rows]}


@router.get("/cmc/projects/{cmc_project_id}")
def get_cmc_project(cmc_project_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    return _project_out(db, _owned_cmc_project(db, cmc_project_id, user))


class CmcProjectPatch(BaseModel):
    product_name: str | None = None
    inn_or_ds_name: str | None = None
    dosage_form: str | None = None
    strengths: list | None = None
    route_of_administration: str | None = None
    submission_type: str | None = None
    target_regions: list | None = None
    development_phase: str | None = None


@router.patch("/cmc/projects/{cmc_project_id}")
def update_cmc_project(cmc_project_id: str, body: CmcProjectPatch,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    cp = _owned_cmc_project(db, cmc_project_id, user)
    changed = body.model_dump(exclude_unset=True)
    if "product_name" in changed and not (changed["product_name"] or "").strip():
        raise error("CMC_NEEDS_PRODUCT", "A quality dossier needs a product name.", 422)
    _validate_project_fields(body)
    for key, value in changed.items():
        setattr(cp, key, value)
    cp.updated_at = now()
    log_audit(db, user, "Updated a CMC project", "cmc_project", cp.id, cp.project_id,
              "info", cp.product_name)
    db.commit()
    db.refresh(cp)
    return _project_out(db, cp)


@router.delete("/cmc/projects/{cmc_project_id}")
def delete_cmc_project(cmc_project_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Purge this module's data for the project. Extended by each milestone
    that adds a table -- the promise is that deleting a dossier removes what
    it ingested, and a purge that forgot a table would leave a customer's
    trade secrets in a database they were told was empty.
    """
    from app.models import (
        CmcBatch, CmcBatchFormula, CmcChange, CmcChunk, CmcCitation, CmcDocument,
        CmcExport, CmcMaterial, CmcResult, CmcSectionDraft, CmcSpecification, CmcTest,
    )
    from app.models import CmcSite as _CmcSite
    from app.storage import abs_path

    cp = _owned_cmc_project(db, cmc_project_id, user)
    deliverables = db.scalars(select(CmcDeliverable).where(
        CmcDeliverable.cmc_project_id == cp.id)).all()
    sections = db.scalars(select(CmcSection).where(CmcSection.cmc_deliverable_id.in_(
        [d.id for d in deliverables]))).all() if deliverables else []

    counts = {"sections": len(sections), "deliverables": len(deliverables)}
    for section in sections:
        for draft in db.scalars(select(CmcSectionDraft).where(
                CmcSectionDraft.cmc_section_id == section.id)).all():
            for citation in db.scalars(select(CmcCitation).where(
                    CmcCitation.draft_id == draft.id)).all():
                db.delete(citation)
            db.delete(draft)
        db.delete(section)
    db.flush()

    documents = db.scalars(select(CmcDocument).where(
        CmcDocument.cmc_project_id == cp.id)).all()
    blobs = [abs_path(d.storage_path) for d in documents]
    counts["documents"] = len(documents)
    # Children before parents: results reference batches and tests, tests
    # reference specifications, all of them reference materials.
    # Sites come after batches: a batch names the site that made it, and a
    # site deleted first would strand that reference. Deliverables come last
    # because the batch formula points at them.
    for model in (CmcResult, CmcBatchFormula, CmcBatch, CmcTest, CmcSpecification,
                  CmcMaterial, CmcChunk, CmcDocument, CmcChange, CmcExport,
                  _CmcSite, CmcDeliverable):
        rows = db.scalars(select(model).where(model.cmc_project_id == cp.id)).all()
        counts[model.__tablename__] = len(rows)
        for row in rows:
            db.delete(row)
        db.flush()
    db.delete(cp)
    log_audit(db, user, "Deleted a CMC project", "cmc_project", cp.id, cp.project_id,
              "warning",
              f"purged {counts['documents']} sources, "
              f"{counts.get('cmc_results', 0)} values, {counts['sections']} sections")
    db.commit()

    purged_files = 0
    for blob in blobs:
        try:
            blob.unlink(missing_ok=True)
            purged_files += 1
        except OSError:
            pass
    return {"deleted": True, "purged": counts, "purged_files": purged_files}


# ------------------------------------------------------------------ sites

class SiteIn(BaseModel):
    name: str
    address: str | None = None
    identifier: str | None = None
    activities: list = []
    gmp_evidence_document_id: str | None = None


def _validate_activities(activities) -> None:
    unknown = [a for a in (activities or []) if a not in SITE_ACTIVITIES]
    if unknown:
        raise error("CMC_BAD_ACTIVITY",
                    f"unknown site activity {', '.join(map(str, unknown))}; one of "
                    f"{', '.join(SITE_ACTIVITIES)}.", 422)


@router.post("/cmc/projects/{cmc_project_id}/sites", status_code=201)
def create_site(cmc_project_id: str, body: SiteIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    cp = _owned_cmc_project(db, cmc_project_id, user)
    if not body.name.strip():
        raise error("CMC_SITE_NEEDS_NAME", "A site needs a name.", 422)
    _validate_activities(body.activities)
    site = CmcSite(org_id=user.org_id, cmc_project_id=cp.id,
                   **{**body.model_dump(), "name": body.name.strip()})
    db.add(site)
    db.flush()
    log_audit(db, user, "Added a manufacturing site", "cmc_site", site.id,
              cp.project_id, "info", site.name)
    db.commit()
    db.refresh(site)
    return _site_out(site)


@router.get("/cmc/projects/{cmc_project_id}/sites")
def list_sites(cmc_project_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    cp = _owned_cmc_project(db, cmc_project_id, user)
    rows = db.scalars(select(CmcSite).where(
        CmcSite.cmc_project_id == cp.id,
        CmcSite.deleted_at.is_(None)).order_by(CmcSite.name)).all()
    return {"items": [_site_out(s) for s in rows]}


class SitePatch(BaseModel):
    name: str | None = None
    address: str | None = None
    identifier: str | None = None
    activities: list | None = None
    gmp_evidence_document_id: str | None = None


@router.patch("/cmc/sites/{cmc_site_id}")
def update_site(cmc_site_id: str, body: SitePatch, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    site = _owned_site(db, cmc_site_id, user)
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed and not (changed["name"] or "").strip():
        raise error("CMC_SITE_NEEDS_NAME", "A site needs a name.", 422)
    if "activities" in changed:
        _validate_activities(changed["activities"])
    for key, value in changed.items():
        setattr(site, key, value)
    site.updated_at = now()
    log_audit(db, user, "Updated a manufacturing site", "cmc_site", site.id, None,
              "info", site.name)
    db.commit()
    db.refresh(site)
    return _site_out(site)


@router.delete("/cmc/sites/{cmc_site_id}")
def delete_site(cmc_site_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Soft delete. Batches already attributed to the site keep pointing at it,
    because a batch was made where it was made."""
    site = _owned_site(db, cmc_site_id, user)
    site.deleted_at = now()
    log_audit(db, user, "Removed a manufacturing site", "cmc_site", site.id, None,
              "info", site.name)
    db.commit()
    return {"deleted": True}


# ------------------------------------------------------------ deliverables

class DeliverableIn(BaseModel):
    doc_type_key: str
    template_source: str = "builtin"


@router.get("/cmc/deliverable-types")
def list_deliverable_types():
    """What can be written, and what each one needs. Read by the picker, so
    the unbuilt ones are listed with the milestone that will build them rather
    than hidden -- a picker that silently omits half the registry teaches
    people the product does less than it will."""
    out = []
    for key in registry.DELIVERABLE_ORDER:
        entry = registry.DELIVERABLES[key]
        out.append({
            "key": key, "name": entry["name"],
            "structure_basis": entry["structure_basis"],
            "section_count": len(entry["tree"]),
            "required": list(entry["required"]),
            "recommended": list(entry["recommended"]),
            "unbuilt": entry.get("unbuilt"),
        })
    return {"items": out}


@router.post("/cmc/projects/{cmc_project_id}/deliverables", status_code=201)
def add_deliverable(cmc_project_id: str, body: DeliverableIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    cp = _owned_cmc_project(db, cmc_project_id, user)
    try:
        entry = registry.deliverable(body.doc_type_key)
    except registry.UnknownDeliverable as exc:
        raise error("CMC_UNKNOWN_DELIVERABLE", str(exc), 422)
    if entry.get("unbuilt"):
        raise error(
            "CMC_DELIVERABLE_UNBUILT",
            f"{entry['name']} is not built yet (milestone {entry['unbuilt']}). The CTD "
            "3.2.S, 3.2.P and 3.2.A/R structures are available now.", 422)
    if body.template_source == "uploaded":
        raise error("CMC_TEMPLATE_UPLOAD_UNBUILT",
                    "Sponsor template upload is not built yet (milestone M8). Use the "
                    "built-in structure for now.", 422)
    if body.template_source != "builtin":
        raise error("CMC_BAD_TEMPLATE_SOURCE",
                    "template_source must be builtin or uploaded.", 422)
    if db.scalar(select(CmcDeliverable).where(
            CmcDeliverable.cmc_project_id == cp.id,
            CmcDeliverable.doc_type_key == body.doc_type_key)) is not None:
        raise error("CMC_DELIVERABLE_EXISTS",
                    f"{entry['name']} is already part of this dossier.", 409)

    deliverable = CmcDeliverable(org_id=user.org_id, cmc_project_id=cp.id,
                                 doc_type_key=body.doc_type_key,
                                 template_source="builtin")
    db.add(deliverable)
    db.flush()
    sections = [CmcSection(org_id=user.org_id, cmc_deliverable_id=deliverable.id, **row)
                for row in seed_sections(entry["tree"])]
    db.add_all(sections)
    cp.status = "ready"
    cp.updated_at = now()
    log_audit(db, user, "Added a CMC deliverable", "cmc_deliverable", deliverable.id,
              cp.project_id, "info", entry["name"])
    db.commit()
    return {**_deliverable_out(deliverable, section_count=len(sections)),
            "sections": [_section_out(s) for s in sorted(sections, key=lambda s: s.sort_order)]}


@router.get("/cmc/deliverables/{cmc_deliverable_id}/sections")
def list_deliverable_sections(cmc_deliverable_id: str, db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    deliverable = _owned_deliverable(db, cmc_deliverable_id, user)
    rows = db.scalars(select(CmcSection).where(
        CmcSection.cmc_deliverable_id == deliverable.id
    ).order_by(CmcSection.sort_order)).all()
    return {"deliverable": _deliverable_out(deliverable, section_count=len(rows)),
            "items": [_section_out(s) for s in rows]}


@router.delete("/cmc/deliverables/{cmc_deliverable_id}")
def remove_deliverable(cmc_deliverable_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Remove a deliverable and its sections. Refused once any section carries
    work: dropping a deliverable with approved sections in it would discard a
    reviewed document silently."""
    from app.models import CmcSectionDraft

    deliverable = _owned_deliverable(db, cmc_deliverable_id, user)
    sections = db.scalars(select(CmcSection).where(
        CmcSection.cmc_deliverable_id == deliverable.id)).all()
    if any(s.status != "not_started" for s in sections):
        raise error(
            "CMC_DELIVERABLE_HAS_WORK",
            "Sections of this deliverable already carry drafts. Remove them first, or "
            "keep the deliverable and disable the sections you do not need.", 409)
    for section in sections:
        for draft in db.scalars(select(CmcSectionDraft).where(
                CmcSectionDraft.cmc_section_id == section.id)).all():
            db.delete(draft)
        db.delete(section)
    db.delete(deliverable)
    log_audit(db, user, "Removed a CMC deliverable", "cmc_deliverable",
              deliverable.id, None, "warning", deliverable.doc_type_key)
    db.commit()
    return {"deleted": True, "purged_sections": len(sections)}


# ------------------------------------------------------------------ sections

class SectionPatch(BaseModel):
    enabled: bool | None = None
    applicability: str | None = None
    applicability_justification: str | None = None


@router.patch("/cmc/sections/{cmc_section_id}")
def patch_section(cmc_section_id: str, body: SectionPatch,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Include or exclude a section, and say why it does not apply.

    A section marked not applicable needs its justification: "not applicable"
    with no reason is the answer a reviewer sends back, so the gate is here
    rather than in a QC report somebody reads later.
    """
    section = _owned_section(db, cmc_section_id, user)
    changed = body.model_dump(exclude_unset=True)
    if section.is_container and "enabled" in changed:
        raise error("CMC_SECTION_IS_CONTAINER",
                    "A container heading has no content of its own to include or "
                    "exclude; toggle its subsections.", 422)
    if "applicability" in changed and changed["applicability"] not in APPLICABILITY:
        raise error("CMC_BAD_APPLICABILITY",
                    f"applicability must be one of {', '.join(APPLICABILITY)}.", 422)

    applicability = changed.get("applicability", section.applicability)
    justification = changed.get("applicability_justification",
                                section.applicability_justification)
    if applicability in ("not_applicable", "referenced_dmf") and not (justification or "").strip():
        raise error(
            "CMC_NEEDS_JUSTIFICATION",
            "A section that is not applicable, or covered by a referenced DMF, needs a "
            "justification -- it is the first thing an assessor asks for.", 422)

    for key, value in changed.items():
        setattr(section, key, value)
    section.updated_at = now()
    log_audit(db, user, "Updated a CMC section", "cmc_section", section.id, None,
              "info", f"{section.section_code} {section.applicability}")
    db.commit()
    db.refresh(section)
    return _section_out(section)


# ------------------------------------------------------------------ sources

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = (".pdf", ".docx", ".rtf", ".xlsx", ".csv", ".txt", ".md")


def _document_out(d) -> dict:
    return {"id": d.id, "doc_type": d.doc_type, "material_id": d.material_id,
            "filename": d.original_filename, "mime_type": d.mime_type,
            "size_bytes": d.size_bytes, "page_count": d.page_count,
            "processing_status": d.processing_status,
            "error_message": d.error_message, "chunk_count": d.chunk_count,
            "value_count": d.value_count,
            "created_at": d.created_at, "updated_at": d.updated_at}


def _owned_document(db: Session, document_id: str, user: User):
    from app.models import CmcDocument

    d = db.get(CmcDocument, document_id)
    if not d or d.org_id != user.org_id:
        raise error("CMC_DOCUMENT_NOT_FOUND", "Source document not found", 404)
    return d


def _deliverable_keys(db: Session, cmc_project_id: str) -> list:
    return [d.doc_type_key for d in db.scalars(select(CmcDeliverable).where(
        CmcDeliverable.cmc_project_id == cmc_project_id)).all()]


@router.post("/cmc/projects/{cmc_project_id}/documents", status_code=201)
async def upload_documents(cmc_project_id: str,
                           files: list[UploadFile] = File(...),
                           doc_types: list[str] = Form(...),
                           material_ids: list[str] | None = Form(None),
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Upload tagged source documents.

    Two tags per file, not one: the document type decides which sections may
    cite it, and the material decides which substance or product its numbers
    belong to. A certificate of analysis filed against the wrong material is a
    limit applied to the wrong molecule, which is why the material can be
    stated at upload rather than guessed at extraction.
    """
    from app.cmc.ingest import file_hash
    from app.models import CmcDocument, CmcMaterial

    cp = _owned_cmc_project(db, cmc_project_id, user)
    if len(doc_types) != len(files):
        raise error("CMC_TAGS_MISMATCH",
                    f"{len(files)} files arrived with {len(doc_types)} tags; every file "
                    "needs exactly one document type.", 422)
    materials = list(material_ids or [])
    if materials and len(materials) != len(files):
        raise error("CMC_TAGS_MISMATCH",
                    "material_ids, when given, needs one entry per file.", 422)

    saved = []
    for index, (upload, doc_type) in enumerate(zip(files, doc_types)):
        tag = (doc_type or "").strip().lower()
        if tag not in registry.DOC_TYPES:
            raise error("CMC_BAD_DOC_TYPE",
                        f"Unknown document type {doc_type!r}; one of "
                        f"{', '.join(registry.DOC_TYPES)}.", 422)
        material_id = (materials[index] or "").strip() if materials else ""
        if material_id:
            material = db.get(CmcMaterial, material_id)
            if not material or material.org_id != user.org_id \
                    or material.cmc_project_id != cp.id:
                raise error("CMC_MATERIAL_NOT_FOUND",
                            f"No material {material_id!r} in this dossier.", 404)
        name = upload.filename or "source"
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise error("CMC_UNSUPPORTED_FILE",
                        f"{name}: {suffix or 'files with no extension'} cannot be read. "
                        f"Supported: {', '.join(ALLOWED_SUFFIXES)}.", 422)
        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise error("CMC_FILE_TOO_LARGE",
                        f"{name} is {len(data) // (1024 * 1024)} MB; the limit is "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file.", 413)
        if not data:
            raise error("CMC_EMPTY_FILE", f"{name} is empty.", 422)

        document = CmcDocument(
            org_id=user.org_id, cmc_project_id=cp.id, doc_type=tag,
            material_id=material_id or None, original_filename=name,
            storage_path=save_bytes(data, f"cmc/{cp.id}", suffix),
            mime_type=upload.content_type, size_bytes=len(data),
            file_hash=file_hash(data), uploaded_by=user.id)
        db.add(document)
        saved.append(document)
    db.flush()
    log_audit(db, user, "Uploaded CMC sources", "cmc_project", cp.id, cp.project_id,
              "info", f"{len(saved)} files")
    db.commit()
    for document in saved:
        db.refresh(document)
    return {"items": [_document_out(d) for d in saved]}


@router.get("/cmc/projects/{cmc_project_id}/documents")
def list_documents(cmc_project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    from app.cmc.ingest import readiness
    from app.models import CmcDocument

    cp = _owned_cmc_project(db, cmc_project_id, user)
    rows = db.scalars(select(CmcDocument).where(
        CmcDocument.cmc_project_id == cp.id).order_by(CmcDocument.created_at)).all()
    return {"items": [_document_out(d) for d in rows],
            "readiness": readiness(rows, _deliverable_keys(db, cp.id))}


class DocumentPatch(BaseModel):
    doc_type: str | None = None
    material_id: str | None = None


@router.patch("/cmc/documents/{cmc_document_id}")
def retag_document(cmc_document_id: str, body: DocumentPatch,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Re-tag a source. The chunks carry both tags too, so they are updated
    rather than left describing the old answer."""
    from app.models import CmcChunk

    document = _owned_document(db, cmc_document_id, user)
    changed = body.model_dump(exclude_unset=True)
    if "doc_type" in changed:
        tag = (changed["doc_type"] or "").strip().lower()
        if tag not in registry.DOC_TYPES:
            raise error("CMC_BAD_DOC_TYPE", f"Unknown document type {tag!r}.", 422)
        document.doc_type = tag
    if "material_id" in changed:
        document.material_id = (changed["material_id"] or None)
    for chunk in db.scalars(select(CmcChunk).where(
            CmcChunk.document_id == document.id)).all():
        chunk.doc_type = document.doc_type
        chunk.material_id = document.material_id
    document.updated_at = now()
    log_audit(db, user, "Re-tagged a CMC source", "cmc_document", document.id, None,
              "info", document.doc_type)
    db.commit()
    db.refresh(document)
    return _document_out(document)


@router.delete("/cmc/documents/{cmc_document_id}")
def delete_document(cmc_document_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Remove a source, its chunks, and the values read out of it.

    Values go with it deliberately: a stability result whose certificate has
    been withdrawn is a number with no evidence behind it, and leaving it in
    the store would let it print in a table citing a document nobody holds.
    A value a person has already verified survives -- somebody accepted it,
    and their acceptance is its own evidence -- but it loses its source link
    and QC reports it as unsourced.
    """
    from app.models import CmcChunk, CmcResult

    document = _owned_document(db, cmc_document_id, user)
    removed_chunks = 0
    for chunk in db.scalars(select(CmcChunk).where(
            CmcChunk.document_id == document.id)).all():
        db.delete(chunk)
        removed_chunks += 1
    removed_values = kept_values = 0
    for result in db.scalars(select(CmcResult).where(
            CmcResult.source_document_id == document.id)).all():
        if result.verified_by:
            result.source_document_id = None
            kept_values += 1
        else:
            db.delete(result)
            removed_values += 1
    blob = abs_path(document.storage_path)
    db.delete(document)
    log_audit(db, user, "Deleted a CMC source", "cmc_document", document.id, None,
              "warning",
              f"{document.original_filename} (-{removed_chunks} chunks, "
              f"-{removed_values} values, {kept_values} verified kept)")
    db.commit()
    try:
        blob.unlink(missing_ok=True)
    except OSError:
        pass
    return {"deleted": True, "purged_chunks": removed_chunks,
            "purged_values": removed_values, "kept_verified_values": kept_values}


@router.post("/cmc/projects/{cmc_project_id}/process", status_code=202)
def process_documents(cmc_project_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    from app.cmc.ingest import ingest_in_background
    from app.models import CmcDocument

    cp = _owned_cmc_project(db, cmc_project_id, user)
    pending = db.scalars(select(CmcDocument).where(
        CmcDocument.cmc_project_id == cp.id,
        CmcDocument.processing_status.in_(("queued", "failed")))).all()
    if not pending:
        raise error("CMC_NOTHING_TO_PROCESS",
                    "Every uploaded source is already indexed.", 409)
    ids = []
    for document in pending:
        document.processing_status = "queued"
        document.error_message = None
        ids.append(document.id)
    log_audit(db, user, "Started CMC source processing", "cmc_project", cp.id,
              cp.project_id, "info", f"{len(ids)} files")
    db.commit()
    ingest_in_background(ids)
    return {"queued": len(ids),
            "poll": f"/api/v1/cmc/projects/{cp.id}/processing-status"}


@router.post("/cmc/documents/{cmc_document_id}/retry", status_code=202)
def retry_document(cmc_document_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    from app.cmc.ingest import ingest_in_background

    document = _owned_document(db, cmc_document_id, user)
    document.processing_status = "queued"
    document.error_message = None
    db.commit()
    ingest_in_background([document.id])
    return {"queued": 1}


@router.get("/cmc/projects/{cmc_project_id}/processing-status")
def processing_status(cmc_project_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    from app.cmc.ingest import readiness
    from app.models import CmcDocument

    cp = _owned_cmc_project(db, cmc_project_id, user)
    rows = db.scalars(select(CmcDocument).where(
        CmcDocument.cmc_project_id == cp.id).order_by(CmcDocument.created_at)).all()
    in_flight = any(d.processing_status in
                    ("queued", "parsing", "chunking", "extracting", "indexing")
                    for d in rows)
    return {"items": [_document_out(d) for d in rows], "total": len(rows),
            "settled": sum(1 for d in rows
                           if d.processing_status in ("done", "failed")),
            "in_flight": in_flight,
            "readiness": readiness(rows, _deliverable_keys(db, cp.id))}


# ------------------------------------------------------- the structured store

class MaterialIn(BaseModel):
    kind: str
    name: str
    grade: str | None = None
    compendial_ref: str | None = None
    supplier: str | None = None
    dmf_reference: str | None = None


MATERIAL_KINDS = ("drug_substance", "drug_product", "excipient", "intermediate",
                  "packaging_component")


def _material_out(m) -> dict:
    return {"id": m.id, "kind": m.kind, "name": m.name, "grade": m.grade,
            "compendial_ref": m.compendial_ref, "supplier": m.supplier,
            "dmf_reference": m.dmf_reference}


@router.post("/cmc/projects/{cmc_project_id}/materials", status_code=201)
def create_material(cmc_project_id: str, body: MaterialIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    from app.models import CmcMaterial

    cp = _owned_cmc_project(db, cmc_project_id, user)
    if body.kind not in MATERIAL_KINDS:
        raise error("CMC_BAD_MATERIAL_KIND",
                    f"kind must be one of {', '.join(MATERIAL_KINDS)}.", 422)
    if not body.name.strip():
        raise error("CMC_MATERIAL_NEEDS_NAME", "A material needs a name.", 422)
    material = CmcMaterial(org_id=user.org_id, cmc_project_id=cp.id,
                           **{**body.model_dump(), "name": body.name.strip()})
    db.add(material)
    db.flush()
    log_audit(db, user, "Added a material", "cmc_material", material.id,
              cp.project_id, "info", material.name)
    db.commit()
    db.refresh(material)
    return _material_out(material)


@router.get("/cmc/projects/{cmc_project_id}/materials")
def list_materials(cmc_project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    from app.models import CmcMaterial

    cp = _owned_cmc_project(db, cmc_project_id, user)
    rows = db.scalars(select(CmcMaterial).where(
        CmcMaterial.cmc_project_id == cp.id,
        CmcMaterial.deleted_at.is_(None)).order_by(CmcMaterial.kind, CmcMaterial.name)).all()
    return {"items": [_material_out(m) for m in rows]}


def _verification_summary(results) -> dict:
    total = len(results)
    verified = sum(1 for r in results if r.verified_by)
    conflicts = sum(1 for r in results if r.conflict_with_id)
    return {"total": total, "verified": verified,
            "unverified": total - verified, "conflicts": conflicts,
            "all_verified": total > 0 and verified == total}


@router.get("/cmc/projects/{cmc_project_id}/data/{entity}")
def read_data(cmc_project_id: str, entity: str,
              material_id: str | None = None,
              limit: int = Query(500, ge=1, le=5000),
              offset: int = Query(0, ge=0),
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """The Data Review grid's contents, one entity at a time.

    Paged from the start: a stability programme is fifty batches by thirty
    tests by eight timepoints by three conditions, and a grid that fetched all
    of it at once would be a grid nobody can scroll.

    Every result carries its conformance verdict, computed here rather than in
    the browser, so the grid and the QC report can never disagree about
    whether a batch met its specification.
    """
    from app.cmc.limits import evaluate_row
    from app.models import (
        CmcBatch, CmcBatchFormula, CmcMaterial, CmcResult, CmcSite, CmcTest,
    )

    cp = _owned_cmc_project(db, cmc_project_id, user)
    entities = ("batches", "specifications", "results", "batch-formula", "conflicts")
    if entity not in entities:
        raise error("CMC_UNKNOWN_ENTITY",
                    f"{entity!r} is not a data entity; one of {', '.join(entities)}.", 422)

    if entity == "batches":
        statement = select(CmcBatch).where(CmcBatch.cmc_project_id == cp.id)
        if material_id:
            statement = statement.where(CmcBatch.material_id == material_id)
        rows = db.scalars(statement.order_by(CmcBatch.batch_number)
                          .limit(limit).offset(offset)).all()
        sites = {s.id: s.name for s in db.scalars(select(CmcSite).where(
            CmcSite.cmc_project_id == cp.id)).all()}
        return {"items": [{
            "id": b.id, "material_id": b.material_id, "batch_number": b.batch_number,
            "batch_size": b.batch_size, "batch_size_unit": b.batch_size_unit,
            "manufacture_date": b.manufacture_date, "purpose": b.purpose,
            "scale": b.scale, "site_id": b.site_id,
            "site_name": sites.get(b.site_id), "source_document_id": b.source_document_id,
        } for b in rows]}

    if entity == "specifications":
        statement = select(CmcTest).where(CmcTest.cmc_project_id == cp.id)
        if material_id:
            statement = statement.where(CmcTest.material_id == material_id)
        rows = db.scalars(statement.order_by(CmcTest.material_id, CmcTest.sort_order)
                          .limit(limit).offset(offset)).all()
        return {"items": [{
            "id": t.id, "material_id": t.material_id, "test_name": t.test_name,
            "method_id": t.method_id, "method_type": t.method_type, "unit": t.unit,
            "acceptance_criterion_text": t.acceptance_criterion_text,
            "limit_lower": t.limit_lower, "limit_upper": t.limit_upper,
            "limit_operator": t.limit_operator, "stage": t.stage,
            "spec_version_id": t.spec_version_id,
            "source_document_id": t.source_document_id,
        } for t in rows]}

    if entity == "batch-formula":
        rows = db.scalars(select(CmcBatchFormula).where(
            CmcBatchFormula.cmc_project_id == cp.id
        ).order_by(CmcBatchFormula.sort_order).limit(limit).offset(offset)).all()
        return {"items": [{
            "id": f.id, "component_name": f.component_name, "function": f.function,
            "quantity_per_unit": f.quantity_per_unit, "unit": f.unit,
            "percent_ww": f.percent_ww, "quantity_per_batch": f.quantity_per_batch,
            "reference_to_standard": f.reference_to_standard,
            "extraction_confidence": f.extraction_confidence,
            "verified_by": f.verified_by, "source_document_id": f.source_document_id,
        } for f in rows], "summary": _verification_summary(rows)}

    statement = select(CmcResult).where(CmcResult.cmc_project_id == cp.id)
    if entity == "conflicts":
        statement = statement.where(CmcResult.conflict_with_id.is_not(None))
    if material_id:
        statement = statement.join(CmcTest, CmcResult.test_id == CmcTest.id).where(
            CmcTest.material_id == material_id)
    all_rows = db.scalars(statement).all()
    rows = db.scalars(statement.order_by(CmcResult.created_at)
                      .limit(limit).offset(offset)).all()

    tests = {t.id: t for t in db.scalars(select(CmcTest).where(
        CmcTest.cmc_project_id == cp.id)).all()}
    batches = {b.id: b for b in db.scalars(select(CmcBatch).where(
        CmcBatch.cmc_project_id == cp.id)).all()}
    materials = {m.id: m.name for m in db.scalars(select(CmcMaterial).where(
        CmcMaterial.cmc_project_id == cp.id)).all()}

    items = []
    for r in rows:
        test = tests.get(r.test_id)
        batch = batches.get(r.batch_id)
        verdict = evaluate_row(
            value_text=r.value_text,
            acceptance_criterion_text=test.acceptance_criterion_text if test else None)
        items.append({
            "id": r.id, "batch_id": r.batch_id,
            "batch_number": batch.batch_number if batch else None,
            "material_id": test.material_id if test else None,
            "material_name": materials.get(test.material_id) if test else None,
            "test_id": r.test_id, "test_name": test.test_name if test else None,
            "acceptance_criterion_text": test.acceptance_criterion_text if test else None,
            "storage_condition": r.storage_condition,
            "timepoint_months": r.timepoint_months, "orientation": r.orientation,
            # The reported string, which is what a document prints.
            "value_text": r.value_text,
            "operator": r.operator, "unit": r.unit,
            "extraction_confidence": r.extraction_confidence,
            "verified_by": r.verified_by, "verified_at": r.verified_at,
            "conflict_with_id": r.conflict_with_id,
            "source_document_id": r.source_document_id,
            "page": r.page, "table_ref": r.table_ref,
            "conformance": verdict.outcome, "conformance_reason": verdict.reason,
        })
    return {"items": items, "total": len(all_rows),
            "summary": _verification_summary(all_rows)}


class ResultPatch(BaseModel):
    #: The value as it should read. Stored verbatim, exactly as an extracted
    #: one is: a person retyping 0.050 means 0.050.
    value_text: str | None = None
    storage_condition: str | None = None
    timepoint_months: float | None = None
    orientation: str | None = None
    #: Correcting a value verifies it in the same act, because somebody just
    #: read the source and typed what it says.
    verify: bool = True


@router.patch("/cmc/results/{cmc_result_id}")
def correct_result(cmc_result_id: str, body: ResultPatch,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Correct a value, recording what it was and who changed it.

    The audit entry carries the original and the correction because that pair
    is the record a data-integrity review asks for; a log saying only that a
    value was edited answers none of the questions it exists to answer.
    """
    from app.cmc.values import parse_value
    from app.models import CmcResult

    result = db.get(CmcResult, cmc_result_id)
    if not result or result.org_id != user.org_id:
        raise error("CMC_RESULT_NOT_FOUND", "Result not found", 404)

    changes = body.model_dump(exclude_unset=True)
    before = {"value_text": result.value_text,
              "storage_condition": result.storage_condition,
              "timepoint_months": result.timepoint_months}
    if "value_text" in changes:
        text = (changes["value_text"] or "").strip()
        if not text:
            raise error("CMC_RESULT_NEEDS_VALUE",
                        "A result needs a value. Delete the row instead of blanking it.", 422)
        parsed = parse_value(text, unit_hint=result.unit)
        for key, value in parsed.as_row().items():
            setattr(result, key, value)
        # A corrected value is a value somebody read off the source.
        result.extraction_confidence = 1.0
    for key in ("storage_condition", "timepoint_months", "orientation"):
        if key in changes:
            setattr(result, key, changes[key])
    if body.verify:
        result.verified_by = user.id
        result.verified_at = now()
    result.updated_at = now()

    log_audit(db, user, "Corrected a quality value", "cmc_result", result.id, None,
              "warning",
              f"{before['value_text']!r} -> {result.value_text!r}"
              if before["value_text"] != result.value_text
              else f"verified {result.value_text!r}")
    db.commit()
    db.refresh(result)
    return {"id": result.id, "value_text": result.value_text,
            "operator": result.operator, "unit": result.unit,
            "storage_condition": result.storage_condition,
            "timepoint_months": result.timepoint_months,
            "verified_by": result.verified_by, "verified_at": result.verified_at,
            "extraction_confidence": result.extraction_confidence}


class VerifyRequest(BaseModel):
    result_ids: list = []
    #: Verify every unverified result of one test, or of the whole project.
    test_id: str | None = None
    all_unverified: bool = False


@router.post("/cmc/projects/{cmc_project_id}/results:verify")
def verify_results(cmc_project_id: str, body: VerifyRequest,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Accept values as read. One, a column, or everything outstanding.

    A conflicted value cannot be bulk-verified: two sources disagree about it,
    and sweeping that up with a column of uncontested numbers is how the wrong
    one gets accepted. It has to be resolved on its own.
    """
    from app.models import CmcResult

    cp = _owned_cmc_project(db, cmc_project_id, user)
    statement = select(CmcResult).where(CmcResult.cmc_project_id == cp.id,
                                        CmcResult.verified_by.is_(None))
    if body.result_ids:
        statement = statement.where(CmcResult.id.in_(list(body.result_ids)))
    elif body.test_id:
        statement = statement.where(CmcResult.test_id == body.test_id)
    elif not body.all_unverified:
        raise error("CMC_NOTHING_TO_VERIFY",
                    "Name the results to verify, a test_id, or set all_unverified.", 422)

    rows = db.scalars(statement).all()
    verified = 0
    skipped_conflicts = 0
    for result in rows:
        if result.conflict_with_id:
            skipped_conflicts += 1
            continue
        result.verified_by = user.id
        result.verified_at = now()
        result.updated_at = now()
        verified += 1
    log_audit(db, user, "Verified quality values", "cmc_project", cp.id, cp.project_id,
              "info", f"{verified} values")
    db.commit()
    return {"verified": verified, "skipped_conflicts": skipped_conflicts}


class ResolveRequest(BaseModel):
    #: The result to keep. The other side of the conflict is discarded.
    keep_result_id: str


@router.post("/cmc/results/{cmc_result_id}:resolve")
def resolve_conflict(cmc_result_id: str, body: ResolveRequest,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Settle two sources disagreeing about one cell.

    A person chooses; the system never does. The discarded value is deleted
    rather than kept as a shadow row, because a store holding both would let a
    later query pick the one nobody chose -- and the audit entry records what
    was rejected, which is where that history belongs.
    """
    from app.models import CmcResult

    result = db.get(CmcResult, cmc_result_id)
    if not result or result.org_id != user.org_id:
        raise error("CMC_RESULT_NOT_FOUND", "Result not found", 404)
    if not result.conflict_with_id:
        raise error("CMC_NO_CONFLICT", "This value is not in conflict.", 409)
    other = db.get(CmcResult, result.conflict_with_id)
    if other is None or other.org_id != user.org_id:
        raise error("CMC_NO_CONFLICT", "The conflicting value no longer exists.", 409)

    pair = {result.id: result, other.id: other}
    if body.keep_result_id not in pair:
        raise error("CMC_BAD_RESOLUTION",
                    "keep_result_id must name one of the two conflicting values.", 422)
    keep = pair[body.keep_result_id]
    discard = other if keep is result else result

    discarded_text = discard.value_text
    db.delete(discard)
    keep.conflict_with_id = None
    keep.verified_by = user.id
    keep.verified_at = now()
    keep.updated_at = now()
    log_audit(db, user, "Resolved a value conflict", "cmc_result", keep.id, None,
              "warning", f"kept {keep.value_text!r}, discarded {discarded_text!r}")
    db.commit()
    db.refresh(keep)
    return {"id": keep.id, "value_text": keep.value_text,
            "verified_by": keep.verified_by, "discarded": discarded_text}
