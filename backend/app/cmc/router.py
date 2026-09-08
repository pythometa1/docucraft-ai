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

from fastapi import APIRouter, Depends
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
