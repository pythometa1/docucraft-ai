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

import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.public_errors import public_message
from app.audit.service import log_audit
from app.cmc import registry
from app.cmc.ctd import seed_sections
from app.db import delete_in_order, get_db
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

#: Tables whose rows are `cmc_batch_formula`, not `cmc_results`. Named once
#: here so the approval gate and the QC pass cannot come to disagree about
#: which store a section's table is verified against.
FORMULA_BACKED_TABLES = ("batch_formula", "composition_table")


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
    drafts = db.scalars(select(CmcSectionDraft).where(CmcSectionDraft.cmc_section_id.in_(
        [s.id for s in sections]))).all() if sections else []
    citations = db.scalars(select(CmcCitation).where(CmcCitation.draft_id.in_(
        [d.id for d in drafts]))).all() if drafts else []
    delete_in_order(db, citations, drafts, sections)

    documents = db.scalars(select(CmcDocument).where(
        CmcDocument.cmc_project_id == cp.id)).all()
    blobs = [abs_path(d.storage_path) for d in documents if d.storage_path]
    counts["documents"] = len(documents)

    # The exports too. Their rows were deleted with everything else and their
    # FILES were left on disk -- and an exported dossier is not a lesser copy
    # of this data, it is every specification limit, batch result and
    # stability figure in one document. A customer told the project was purged
    # would have been told something untrue about the most complete artefact
    # in it.
    #
    # Read from `options["files"]` and not only from `storage_path`: an export
    # writes one file per granularity and the column records just the first of
    # them, so a combined-and-eCTD export leaves its .zip behind if only the
    # column is followed.
    seen = {str(b) for b in blobs}
    for export in db.scalars(select(CmcExport).where(
            CmcExport.cmc_project_id == cp.id)).all():
        paths = [export.storage_path] + [
            entry.get("storage_path")
            for entry in (export.options or {}).get("files", [])]
        for path in paths:
            if not path:
                continue
            absolute = abs_path(path)
            if str(absolute) not in seen:
                seen.add(str(absolute))
                blobs.append(absolute)
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
    # `seed_sections` fills table_key from the CTD map; a non-CTD deliverable
    # declares its own beside its tree, so the registry decides rather than
    # the CTD module -- otherwise an APQR's results section would render no
    # table and nobody would see why.
    sections = []
    for row in seed_sections(entry["tree"]):
        row = {**row,
               "table_key": registry.table_key_for(body.doc_type_key, row["section_code"])}
        sections.append(CmcSection(org_id=user.org_id,
                                   cmc_deliverable_id=deliverable.id, **row))
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
    from app.models import CmcBatchFormula, CmcSectionDraft

    deliverable = _owned_deliverable(db, cmc_deliverable_id, user)
    sections = db.scalars(select(CmcSection).where(
        CmcSection.cmc_deliverable_id == deliverable.id)).all()
    if any(s.status != "not_started" for s in sections):
        raise error(
            "CMC_DELIVERABLE_HAS_WORK",
            "Sections of this deliverable already carry drafts. Remove them first, or "
            "keep the deliverable and disable the sections you do not need.", 409)
    drafts = db.scalars(select(CmcSectionDraft).where(CmcSectionDraft.cmc_section_id.in_(
        [s.id for s in sections]))).all() if sections else []
    # A batch formula is verified data, not part of the deliverable's text: it
    # is detached, not deleted, when the deliverable it was filed under goes.
    for formula in db.scalars(select(CmcBatchFormula).where(
            CmcBatchFormula.cmc_deliverable_id == deliverable.id)).all():
        formula.cmc_deliverable_id = None
    delete_in_order(db, drafts, sections, [deliverable])
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


def _total_of(db, statement, column) -> int:
    """How many rows the filter matches, counted in the database.

    Not `len(rows_loaded)`: the grid pages, and a total taken from the page is
    the page size. Not `len(all_rows)` either -- materialising a fifty-batch
    programme to count it is the paging undoing itself.
    """
    return db.scalar(statement.with_only_columns(func.count(column)).order_by(None)) or 0


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
              scope: str | None = None,
              q: str | None = None,
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

    `scope` splits results into "release" and "stability" the way the table
    builders do, so each tab of the grid pages through its own rows instead of
    sharing one page and filtering it in the browser. `q` filters on the
    server for the same reason: a filter that searched only the rows already
    fetched would quietly answer "no matches" for a value that is in the
    dossier.
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
        if (q or "").strip():
            statement = statement.where(CmcBatch.batch_number.ilike(f"%{q.strip()}%"))
        total = _total_of(db, statement, CmcBatch.id)
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
        } for b in rows], "total": total}

    if entity == "specifications":
        statement = select(CmcTest).where(CmcTest.cmc_project_id == cp.id)
        if material_id:
            statement = statement.where(CmcTest.material_id == material_id)
        if (q or "").strip():
            like = f"%{q.strip()}%"
            statement = statement.where(or_(CmcTest.test_name.ilike(like),
                                            CmcTest.method_id.ilike(like),
                                            CmcTest.acceptance_criterion_text.ilike(like)))
        total = _total_of(db, statement, CmcTest.id)
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
        } for t in rows], "total": total}

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
        statement = statement.where(CmcResult.test_id.in_(
            select(CmcTest.id).where(CmcTest.cmc_project_id == cp.id,
                                     CmcTest.material_id == material_id)))
    # The same split the table builders make: a release result has neither a
    # storage condition nor a timepoint, and everything else is on stability.
    # Stated in SQL here so the grid's two tabs page independently.
    if scope == "release":
        statement = statement.where(CmcResult.storage_condition.is_(None),
                                    CmcResult.timepoint_months.is_(None))
    elif scope == "stability":
        statement = statement.where(or_(CmcResult.storage_condition.is_not(None),
                                        CmcResult.timepoint_months.is_not(None)))
    elif scope:
        raise error("CMC_UNKNOWN_SCOPE",
                    f"{scope!r} is not a result scope; one of release, stability.", 422)
    if (q or "").strip():
        like = f"%{q.strip()}%"
        statement = statement.where(or_(
            CmcResult.value_text.ilike(like),
            CmcResult.test_id.in_(select(CmcTest.id).where(
                CmcTest.cmc_project_id == cp.id, CmcTest.test_name.ilike(like))),
            CmcResult.batch_id.in_(select(CmcBatch.id).where(
                CmcBatch.cmc_project_id == cp.id, CmcBatch.batch_number.ilike(like)))))

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
            acceptance_criterion_text=test.acceptance_criterion_text if test else None,
            unit=r.unit)
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
    # Counted in SQL rather than by loading every matching row into memory.
    # `len(all_rows)` meant one full materialisation of the result store on
    # every keystroke of the grid's filter -- on the fifty-batch programmes
    # this endpoint's own docstring describes, that is the paging defeating
    # itself. `count(column)` counts non-nulls, which is exactly what
    # "verified" and "in conflict" mean here.
    counted = statement.with_only_columns(
        func.count(CmcResult.id),
        func.count(CmcResult.verified_by),
        func.count(CmcResult.conflict_with_id),
    ).order_by(None)
    total, verified, conflicts = db.execute(counted).one()
    return {"items": items, "total": total,
            "summary": {"total": total, "verified": verified,
                        "unverified": total - verified, "conflicts": conflicts,
                        "all_verified": total > 0 and verified == total}}


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

    # Every field that moved, not just the value. `before` captured the
    # stability coordinates from the first version of this endpoint and the
    # audit line ignored them, so a correction that moved a result from
    # 25C/60RH at 6 months to 40C/75RH at 3 months -- a different cell of a
    # different table -- was recorded as "verified '0.12 %'", which reads as
    # nothing having happened at all.
    moved = [f"{field} {before[field]!r} -> {getattr(result, field)!r}"
             for field in ("value_text", "storage_condition", "timepoint_months")
             if before[field] != getattr(result, field)]
    log_audit(db, user, "Corrected a quality value", "cmc_result", result.id, None,
              "warning",
              "; ".join(moved) if moved else f"verified {result.value_text!r}")
    db.commit()
    db.refresh(result)

    # The conformance verdict is recomputed and returned. The grid merges this
    # response over the row it just edited, so a response without it left the
    # OLD value's verdict on screen beside the NEW value -- a batch shown as
    # conforming on the strength of a number no longer in the cell.
    from app.cmc.limits import evaluate_row
    from app.models import CmcTest

    test = db.get(CmcTest, result.test_id) if result.test_id else None
    verdict = evaluate_row(
        value_text=result.value_text,
        acceptance_criterion_text=test.acceptance_criterion_text if test else None,
        unit=result.unit)
    return {"id": result.id, "value_text": result.value_text,
            "operator": result.operator, "unit": result.unit,
            "storage_condition": result.storage_condition,
            "timepoint_months": result.timepoint_months,
            "verified_by": result.verified_by, "verified_at": result.verified_at,
            "extraction_confidence": result.extraction_confidence,
            "conformance": verdict.outcome, "conformance_reason": verdict.reason}


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


# ------------------------------------------------------------ tables (M4)

def _owned_section_project(db: Session, section: CmcSection, user: User) -> CmcProject:
    """The dossier a section belongs to, checked as its own object.

    Two hops rather than a join, because the section's org check has already
    happened and this one is about the project the caller is allowed to touch.
    """
    deliverable = _owned_deliverable(db, section.cmc_deliverable_id, user)
    return _owned_cmc_project(db, deliverable.cmc_project_id, user)


@router.get("/cmc/projects/{cmc_project_id}/tables/{table_key}")
def preview_table(cmc_project_id: str, table_key: str,
                  material_id: str | None = None,
                  include_unverified: bool = True,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """What a rendered table would contain, as rows of strings.

    The same builder the export runs, so what the screen shows and what the
    document prints cannot drift: a preview computed a second way is a preview
    that reassures about a document nobody has produced.
    """
    from app.cmc.tables import BUILDERS, TableUnavailable, render_table

    cp = _owned_cmc_project(db, cmc_project_id, user)
    if table_key not in BUILDERS:
        raise error("CMC_UNKNOWN_TABLE",
                    f"{table_key!r} is not a table this module renders; one of "
                    f"{', '.join(sorted(BUILDERS))}.", 422)
    try:
        rendered = render_table(db, cmc_project_id=cp.id, org_id=user.org_id,
                                table_key=table_key, material_id=material_id,
                                include_unverified=include_unverified)
    except TableUnavailable as exc:
        raise error("CMC_TABLE_NO_DATA", str(exc), 409)
    # `groups` is the document's own arrangement -- one grid per table the
    # export will write. The screen renders that rather than `rows`, which is
    # a flattened union for QC: a preview drawn from the union would show a
    # stability summary as one wide grid where the document holds one per
    # batch and condition, which is a preview of a document nobody produced.
    return {"key": rendered.key, "title": rendered.title,
            "columns": rendered.columns, "rows": rendered.rows,
            "groups": rendered.groups,
            "notes": rendered.notes, "unverified": rendered.unverified,
            "missing": rendered.missing}


# ------------------------------------------------------- drafting (M5)

def _project_metadata(db: Session, cp: CmcProject) -> dict:
    """What every section's prompt is told about the product.

    Read at generation time rather than copied at project creation: a dosage
    form corrected in the setup screen must be corrected in the next draft.
    """
    sites = db.scalars(select(CmcSite).where(
        CmcSite.cmc_project_id == cp.id, CmcSite.deleted_at.is_(None))).all()
    return {
        "product_name": cp.product_name,
        "inn_or_ds_name": cp.inn_or_ds_name,
        "dosage_form": cp.dosage_form,
        "strengths": ", ".join(cp.strengths or []),
        "route_of_administration": cp.route_of_administration,
        "submission_type": cp.submission_type,
        "development_phase": cp.development_phase,
        "target_regions": ", ".join(cp.target_regions or []),
        "sites": [{"name": s.name, "address": s.address, "identifier": s.identifier,
                   "activities": s.activities or []} for s in sites],
    }


def _verified_data_summary(db: Session, cp: CmcProject, section: CmcSection) -> dict:
    """The verified figures this section is allowed to mention in prose.

    Only verified ones, and only as strings. Rule 3 of the prompt lets the
    model restate a value it can see here, exactly; handing it unverified
    numbers would make that permission a way for an unchecked figure to reach
    the narrative without ever passing the grid.
    """
    from app.models import CmcResult, CmcTest

    if not section.table_key:
        return {}
    rows = db.scalars(select(CmcResult).where(
        CmcResult.cmc_project_id == cp.id,
        CmcResult.verified_by.is_not(None)).limit(400)).all()
    if not rows:
        return {}
    tests = {t.id: t for t in db.scalars(select(CmcTest).where(
        CmcTest.cmc_project_id == cp.id)).all()}
    summary: dict = {}
    for row in rows:
        test = tests.get(row.test_id)
        if test is None:
            continue
        summary.setdefault(test.test_name, []).append({
            "value": row.value_text,
            "condition": row.storage_condition,
            "timepoint_months": row.timepoint_months,
            "acceptance_criterion": test.acceptance_criterion_text,
        })
    return summary


class CmcGenerateRequest(BaseModel):
    instruction: str | None = None


@router.post("/cmc/sections/{cmc_section_id}/generate", status_code=201)
def generate_section(cmc_section_id: str, body: CmcGenerateRequest,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Draft one section's PROSE from the indexed sources.

    Never its tables. A section that carries data is told to emit
    `[TABLE: key]` and write no numbers of its own, and the marker is resolved
    at export from the verified store -- which is why a value corrected in the
    grid reaches every deliverable without a single section being regenerated.
    """
    from app.cmc.drafting import draft_section
    from app.docgen.ranking import build_query, format_extracts, score_chunks
    from app.models import CmcChunk, CmcSectionDraft, CmcCitation
    from app.tenancy import llm_policy_for

    section = _owned_section(db, cmc_section_id, user)
    if section.is_container:
        raise error("CMC_SECTION_IS_CONTAINER",
                    "A container heading has no prose of its own; generate its subsections.", 422)
    if not section.enabled:
        raise error("CMC_SECTION_DISABLED",
                    "This section is excluded from the dossier. Include it first.", 409)
    if section.applicability != "applicable":
        raise error(
            "CMC_SECTION_NOT_APPLICABLE",
            "This section is marked "
            f"{section.applicability.replace('_', ' ')}; its justification is its content.", 409)

    deliverable = _owned_deliverable(db, section.cmc_deliverable_id, user)
    cp = _owned_cmc_project(db, deliverable.cmc_project_id, user)
    entry = registry.DELIVERABLES.get(deliverable.doc_type_key) or {}

    doc_types = registry.source_types_for(deliverable.doc_type_key, section.section_code)
    statement = select(CmcChunk).where(
        CmcChunk.org_id == user.org_id, CmcChunk.cmc_project_id == cp.id)
    if doc_types:
        statement = statement.where(
            CmcChunk.doc_type.in_([*doc_types, registry.STYLE_REFERENCE_TYPE]))
    candidates = list(db.scalars(statement.order_by(CmcChunk.id)))
    metadata = _project_metadata(db, cp)
    query = build_query(section_number=section.section_code,
                        section_title=section.title,
                        guidance_text=section.guidance_text,
                        study_metadata={k: v for k, v in metadata.items()
                                        if isinstance(v, (str, int, float))})
    scores = score_chunks(query, candidates)
    ranked = sorted(candidates, key=lambda c: (-scores.get(c.id, 0.0), c.id))
    chunks = [c for c in ranked if scores.get(c.id, 0.0) > 0][:16]
    extracts, source_map = format_extracts(
        chunks, style_reference_type=registry.STYLE_REFERENCE_TYPE)

    section.status = "generating"
    db.commit()
    try:
        result = draft_section(
            section_code=section.section_code, section_title=section.title,
            deliverable_name=entry.get("name") or deliverable.doc_type_key,
            structure_basis=entry.get("structure_basis") or "ICH M4Q",
            guidance=_guidance_with_table(section),
            project_metadata=metadata,
            verified_data=_verified_data_summary(db, cp, section),
            chunks=chunks, source_map=source_map, extracts=extracts,
            wanted_doc_types=doc_types, instruction=body.instruction,
            llm_policy=llm_policy_for(db, user.org_id, project_id=cp.project_id,
                                      user_id=user.id, subject_type="cmc_section",
                                      subject_id=section.id))
    except Exception:
        section.status = "draft" if _latest_cmc_draft(db, section.id) else "not_started"
        db.commit()
        raise

    previous = _latest_cmc_draft(db, section.id)
    draft = CmcSectionDraft(
        org_id=user.org_id, cmc_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=result.content, created_by="ai", model=result.model,
        generation_params={"prompt_version": result.prompt_version,
                           "instruction": body.instruction,
                           "chunk_ids": [c.id for c in chunks],
                           "source_map": source_map})
    db.add(draft)
    db.flush()
    for citation in result.citations:
        db.add(CmcCitation(org_id=user.org_id, draft_id=draft.id, **citation))
    section.status = "draft"
    section.updated_at = now()
    log_audit(db, user, "Generated a CMC section", "cmc_section", section.id,
              cp.project_id, "info",
              f"{section.section_code} v{draft.version} ({len(chunks)} sources)")
    db.commit()
    db.refresh(draft)
    return {**_draft_out(draft), "section": _section_out(section),
            "data_needed": result.data_needed,
            "table_markers": _table_markers(result.content)}


def _guidance_with_table(section) -> str:
    """The section's guidance, with its table named exactly.

    Rule 2 tells the model to emit `[TABLE: <table_key>]` and never write the
    numbers itself, but nothing told it WHICH key -- so it invented a
    plausible one from the document type it had been reading ("spec_dp"), the
    marker resolved to no builder, and the dossier would have carried the
    marker text where its specification table belongs. The valid key is known
    here from the section itself, so it is stated rather than guessed at.
    """
    guidance = section.guidance_text or ""
    if section.table_key:
        return (f"{guidance}\n\nThis section carries a data table. Emit the single line "
                f"[TABLE: {section.table_key}] where it belongs and write none of its "
                f"numbers yourself. {section.table_key} is the ONLY table key that exists "
                f"for this section; any other key renders as literal text in the dossier.")
    return (f"{guidance}\n\nThis section carries no data table, so emit no [TABLE: ...] "
            "marker.") if guidance else guidance


def _table_markers(content: str) -> list:
    from app.cmc.drafting import table_markers

    return table_markers(content)


def _latest_cmc_draft(db: Session, section_id: str):
    from app.models import CmcSectionDraft

    return db.scalar(select(CmcSectionDraft).where(
        CmcSectionDraft.cmc_section_id == section_id
    ).order_by(CmcSectionDraft.version.desc()))


def _draft_out(draft) -> dict:
    return {"id": draft.id, "version": draft.version, "content": draft.content,
            # The model stays on the row, not on the wire.
            "created_by": draft.created_by,
            "generation_params": draft.generation_params,
            "created_at": draft.created_at}


@router.get("/cmc/sections/{cmc_section_id}/draft")
def get_cmc_draft(cmc_section_id: str, version: int | None = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    from app.models import CmcChunk, CmcCitation, CmcDocument, CmcSectionDraft

    section = _owned_section(db, cmc_section_id, user)
    if version is not None:
        draft = db.scalar(select(CmcSectionDraft).where(
            CmcSectionDraft.cmc_section_id == section.id,
            CmcSectionDraft.version == version))
    else:
        draft = _latest_cmc_draft(db, section.id)
    versions = list(db.scalars(select(CmcSectionDraft.version).where(
        CmcSectionDraft.cmc_section_id == section.id
    ).order_by(CmcSectionDraft.version)).all())
    if draft is None:
        return {"section": _section_out(section), "draft": None,
                "versions": versions, "sources": [], "table_markers": []}

    citations = db.scalars(select(CmcCitation).where(
        CmcCitation.draft_id == draft.id)).all()
    source_map = (draft.generation_params or {}).get("source_map", [])
    chunk_ids = [entry.get("chunk_id") for entry in source_map if entry.get("chunk_id")]
    sources = []
    if chunk_ids:
        chunks = {c.id: c for c in db.scalars(select(CmcChunk).where(
            CmcChunk.id.in_(set(chunk_ids)))).all()}
        documents = {d.id: d.original_filename for d in db.scalars(select(CmcDocument).where(
            CmcDocument.cmc_project_id == _owned_section_project(db, section, user).id)).all()}
        for entry in source_map:
            chunk = chunks.get(entry.get("chunk_id"))
            if chunk is None:
                continue
            sources.append({
                "marker": entry.get("marker"), "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "filename": documents.get(chunk.document_id),
                "doc_type": chunk.doc_type, "page": chunk.page,
                "table_id": chunk.table_id, "is_table": chunk.is_table,
                "content": chunk.content,
            })
    return {"section": _section_out(section), "draft": _draft_out(draft),
            "versions": versions, "sources": sources,
            "citations": [{"id": c.id, "marker": c.marker, "chunk_id": c.chunk_id,
                           "page": c.page, "table_ref": c.table_ref,
                           "cited_value": c.cited_value} for c in citations],
            "table_markers": _table_markers(draft.content)}


class CmcDraftEdit(BaseModel):
    content: str


@router.put("/cmc/sections/{cmc_section_id}/draft", status_code=201)
def edit_cmc_draft(cmc_section_id: str, body: CmcDraftEdit,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """A human edit, saved as a new version under that person's name."""
    from app.docgen.markers import parse_citations
    from app.models import CmcCitation, CmcSectionDraft

    section = _owned_section(db, cmc_section_id, user)
    previous = _latest_cmc_draft(db, section.id)
    source_map = (previous.generation_params or {}).get("source_map", []) if previous else []
    draft = CmcSectionDraft(
        org_id=user.org_id, cmc_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=body.content, created_by=user.id, model=None,
        generation_params={"edited_from_version": previous.version if previous else None,
                           "source_map": source_map})
    db.add(draft)
    db.flush()
    for citation in parse_citations(body.content, source_map):
        db.add(CmcCitation(org_id=user.org_id, draft_id=draft.id, **citation))
    # Any new version demotes the section. The rule used to be "demote only
    # from not_started or generating", which left an APPROVED section approved
    # while export assembled the newest draft -- so the text that shipped was
    # not the text anybody approved, and the status said otherwise.
    was = section.status
    section.status = "draft"
    section.updated_at = now()
    log_audit(db, user, "Edited a CMC section", "cmc_section", section.id, None,
              "warning" if was == "approved" else "info",
              f"{section.section_code} v{draft.version}"
              + (f" (was {was.replace('_', ' ')}; approval withdrawn)"
                 if was in ("in_review", "approved") else ""))
    db.commit()
    db.refresh(draft)
    return _draft_out(draft)


CMC_SECTION_STATUSES = ("draft", "in_review", "approved")


class CmcStatusPatch(BaseModel):
    status: str


@router.patch("/cmc/sections/{cmc_section_id}/status")
def set_cmc_section_status(cmc_section_id: str, body: CmcStatusPatch,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Move a section along Draft -> In Review -> Approved.

    Approving a section whose tables draw on unverified values is refused: the
    approval is what the export gate trusts, and trusting it means it cannot
    have been given over numbers nobody checked.
    """
    from app.models import CmcResult

    section = _owned_section(db, cmc_section_id, user)
    if body.status not in CMC_SECTION_STATUSES:
        raise error("CMC_BAD_STATUS",
                    f"status must be one of {', '.join(CMC_SECTION_STATUSES)}.", 422)
    if _latest_cmc_draft(db, section.id) is None and section.applicability == "applicable":
        raise error("CMC_NOTHING_TO_REVIEW", "This section has no draft yet.", 409)

    if body.status == "approved" and section.table_key:
        cp = _owned_section_project(db, section, user)
        # Which rows the section's table is actually built from. The gate
        # counted `cmc_results` for every table key, so a section whose table
        # is the batch formula or the composition table -- both built entirely
        # from `cmc_batch_formula` -- could be approved with every quantity in
        # it unverified, because the rows it prints were never the rows being
        # counted.
        if section.table_key in FORMULA_BACKED_TABLES:
            from app.models import CmcBatchFormula

            unverified = db.scalar(select(func.count()).select_from(CmcBatchFormula).where(
                CmcBatchFormula.cmc_project_id == cp.id,
                CmcBatchFormula.verified_by.is_(None)))
            noun = "formula line"
        else:
            unverified = db.scalar(select(func.count()).select_from(CmcResult).where(
                CmcResult.cmc_project_id == cp.id, CmcResult.verified_by.is_(None)))
            noun = "value"
        if unverified:
            raise error(
                "CMC_UNVERIFIED_DATA",
                f"{unverified} {noun}{'s' if unverified != 1 else ''} feeding this "
                "section's tables have not been verified. Check them in Data review "
                "first.", 409)

    section.status = body.status
    section.updated_at = now()
    log_audit(db, user, "Set a CMC section status", "cmc_section", section.id, None,
              "success" if body.status == "approved" else "info",
              f"{section.section_code} -> {body.status}")
    db.commit()
    return _section_out(section)


# ------------------------------------------------------------ QC (M6)

@router.get("/cmc/projects/{cmc_project_id}/qc")
def run_project_qc(cmc_project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Every check, grouped by severity.

    Run on demand rather than continuously: these queries read the whole
    structured store, and a dashboard that recomputed them on every keystroke
    would make the grid unusable on the fifty-batch programmes this module
    exists for.
    """
    from app.cmc.qc import run_qc

    cp = _owned_cmc_project(db, cmc_project_id, user)
    findings = run_qc(db, cmc_project_id=cp.id, org_id=user.org_id)
    grouped: dict = {"blocker": [], "warning": [], "info": []}
    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding.as_dict())
    return {"findings": [f.as_dict() for f in findings],
            "blockers": grouped["blocker"], "warnings": grouped["warning"],
            "info": grouped["info"],
            "exportable": not grouped["blocker"]}


# ------------------------------------------------------------ export (M7)

class ExportRequest(BaseModel):
    #: Which deliverable to write. Omitted means every one in the dossier.
    deliverable_id: str | None = None
    #: ectd_leaves | combined | both
    granularity: str = "combined"
    #: inline | stripped | appendix -- what happens to [S#] citation markers.
    #: Stripped by default: a citation marker points at a retrieval chunk the
    #: recipient has never seen. "inline" is an internal review copy, chosen.
    citations: str = "stripped"
    draft_watermark: bool = False
    #: Export unapproved sections anyway. Audited, never silent.
    override_approval: bool = False


GRANULARITIES = ("ectd_leaves", "combined", "both")
CITATION_MODES = ("inline", "stripped", "appendix")


@router.post("/cmc/projects/{cmc_project_id}/export", status_code=201)
def export_dossier(cmc_project_id: str, body: ExportRequest,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Write the approved sections, resolving every table from verified data.

    The tables are rendered HERE, not when the section was drafted, which is
    the property the whole Flow A / Flow B split buys: a value corrected in
    the grid appears in every deliverable that quotes it without one section
    being regenerated.

    An eCTD backbone is deliberately not produced. Leaf files and a manifest
    are, with conventional names; assembling and validating a submission
    belongs to a publishing tool, and a half-built backbone would look
    submittable without being so.
    """
    import os
    import re as _re
    import zipfile

    from app.cmc import export as export_mod
    from app.cmc.tables import TableError, render_table
    from app.generation.filenames import safe_filename
    from app.generation.reproducibility import write_fixed
    from app.models import CmcExport, CmcSectionDraft
    from app.storage import abs_path, save_bytes

    cp = _owned_cmc_project(db, cmc_project_id, user)
    if body.granularity not in GRANULARITIES:
        raise error("CMC_BAD_GRANULARITY",
                    f"granularity must be one of {', '.join(GRANULARITIES)}.", 422)
    if body.citations not in CITATION_MODES:
        raise error("CMC_BAD_CITATION_MODE",
                    f"citations must be one of {', '.join(CITATION_MODES)}.", 422)

    deliverables = db.scalars(select(CmcDeliverable).where(
        CmcDeliverable.cmc_project_id == cp.id)).all()
    if body.deliverable_id:
        deliverables = [d for d in deliverables if d.id == body.deliverable_id]
    if not deliverables:
        raise error("CMC_NOTHING_TO_EXPORT",
                    "This dossier has no deliverable to write.", 409)

    written: list = []
    plans: list = []
    all_blockers: list = []
    citation_blockers: list = []
    base = f"cmc-export/{cp.id}"

    # QC is the gate, and it was not wired to the gate. `run_qc` was reachable
    # only from its own endpoint, so everything it blocks on -- a batch out of
    # specification, unverified data feeding an enabled section, an unanswered
    # [DATA NEEDED], a batch formula that does not reconcile -- was invisible
    # here. The screen's Export button was gated on QC and this endpoint was
    # gated on section approval: two gates, each guarding what the other one
    # checked, and a dossier could pass both while failing either.
    #
    # Run once for the project rather than per deliverable: the findings are
    # about the shared data store, and the queries read all of it.
    from app.cmc.qc import BLOCKER as _QC_BLOCKER
    from app.cmc.qc import run_qc as _run_qc

    qc_blockers = [{"code": f.code, "section_code": f.section_code,
                    "message": f.message}
                   for f in _run_qc(db, cmc_project_id=cp.id, org_id=user.org_id)
                   if f.severity == _QC_BLOCKER]
    all_blockers.extend(qc_blockers)

    def _apply_citation_mode(section_render):
        """Citation markers out, and NOT the newline in front of them.

        The pattern led with `\\s*`, which consumes line breaks. A citation
        sitting at the start of its own line therefore pulled the following
        text up onto the preceding one -- and where that preceding line was a
        `[TABLE: key]` marker, the marker stopped being alone on its line,
        stopped matching `TABLE_MARKER_RE`, and was printed into the dossier
        as literal text.
        """
        if body.citations == "inline":
            return section_render
        # Anything from "[S<digits>" to the next "]" on the same line, not
        # `[S\d+(?:,...)?]`: the older pattern missed the multi-source form
        # "[S5; S1, p.2]", which then shipped. Never across a newline or
        # another "[": an unclosed "[S7" must not swallow the `[TABLE: key]`
        # line after it.
        stripped = _re.sub(r"[ \t]*\[S\d+[^\]\[\n]*\]", "",
                           section_render.content or "")
        section_render.content = stripped
        return section_render

    for deliverable in deliverables:
        sections = db.scalars(select(CmcSection).where(
            CmcSection.cmc_deliverable_id == deliverable.id,
            CmcSection.enabled.is_(True),
            CmcSection.is_container.is_(False)
        ).order_by(CmcSection.sort_order)).all()

        rows = []
        for section in sections:
            draft = _latest_cmc_draft(db, section.id)
            content = draft.content if draft else ""
            justification_only = False
            if section.applicability != "applicable" and not content.strip():
                content = section.applicability_justification or ""
                justification_only = True
            rows.append((section.section_code, section.title, section.status,
                         section.applicability, content,
                         [section.table_key] if section.table_key else [],
                         justification_only))

        # Always planned as though approval were required, whatever the
        # override says. An override that recorded no blockers left a
        # CmcExport row asserting there had been nothing to override -- the
        # audit trail agreeing with the person who bypassed it.
        plan = export_mod.plan_export(rows, require_approved=True)
        # Deliberately NOT short-circuited on `plan.blockers`. Stopping at the
        # first kind of blocker means a reviewer fixes the approvals, exports
        # again, and only then learns that a table cannot be rendered -- one
        # round trip per class of problem. The tables are resolved either way
        # and the whole list is reported at once.
        blockers = list(plan.blockers)

        # Citation mode is applied BEFORE the markers are resolved, because
        # stripping citations rewrites the very text the markers are found in.
        # Resolving first and building second meant the document was assembled
        # from a string nobody had looked for tables in.
        rendered_sections = [_apply_citation_mode(s) for s in plan.sections]

        # Resolve every [TABLE: key] against the store, once per deliverable.
        tables_by_section: dict = {}
        unresolved: list = []
        withheld: list = []
        for section_render in rendered_sections:
            keys = export_mod.TABLE_MARKER_RE.findall(section_render.content or "")
            resolved: dict = {}
            for key in keys:
                try:
                    rendered = render_table(db, cmc_project_id=cp.id, org_id=user.org_id,
                                            table_key=key,
                                            deliverable_id=deliverable.id,
                                            include_unverified=False)
                    resolved[key] = rendered.blocks
                    # `include_unverified=False` turns an unverified value
                    # into a hole and records why. Throwing that report away is
                    # how unverified numbers became "-" cells in a shipped
                    # dossier with nothing anywhere saying one was withheld.
                    #
                    # The two kinds of gap are not the same thing. A value the
                    # store simply does not have is a hole a reader can see and
                    # QC already names; a value withheld because nobody
                    # verified it is a governance gate, and it blocks.
                    for gap in rendered.missing:
                        where = {"section_code": section_render.section_code,
                                 "table_key": key}
                        if "has not been verified" in gap:
                            withheld.append(dict(where, reason=gap))
                        else:
                            plan.warnings.append(dict(
                                where, code="TABLE_INCOMPLETE",
                                message=f"[TABLE: {key}]: {gap}"))
                except TableError as exc:
                    # Every way a builder can decline -- no data, an unknown
                    # key a model invented, a grid that came out ragged --
                    # blocks the export rather than raising. A 500 here would
                    # tell somebody the server broke when what actually
                    # happened is that the dossier is not ready.
                    unresolved.append({"section_code": section_render.section_code,
                                       "table_key": key, "reason": str(exc)})
                except Exception as exc:  # noqa: BLE001 - a builder bug is not a 500 either
                    unresolved.append({"section_code": section_render.section_code,
                                       "table_key": key,
                                       "reason": public_message(
                                           exc, "the table could not be built")})
            tables_by_section[section_render.section_code] = resolved
        blockers.extend([
            {"code": "TABLE_UNRESOLVED", "section_code": u["section_code"],
             "message": f"[TABLE: {u['table_key']}] could not be rendered: {u['reason']}"}
            for u in unresolved])
        blockers.extend([
            {"code": "TABLE_UNVERIFIED_VALUE", "section_code": w["section_code"],
             "message": (f"[TABLE: {w['table_key']}] would print a gap where an "
                         f"unverified value belongs: {w['reason']}")}
            for w in withheld])

        plans.append({"deliverable_id": deliverable.id,
                      "doc_type_key": deliverable.doc_type_key,
                      "blockers": blockers, "warnings": plan.warnings,
                      "leaves": plan.leaves})
        all_blockers.extend(blockers)
        # A QC blocker stops every deliverable, so nothing is written to disk
        # for an export that is about to be refused.
        if (blockers or qc_blockers) and not body.override_approval:
            continue

        entry = registry.DELIVERABLES.get(deliverable.doc_type_key) or {}
        title = f"{cp.product_name} -- {entry.get('name') or deliverable.doc_type_key}"
        # Named after the deliverable a reader knows ("CTD Module 3.2.P - Drug
        # Product"), never the internal key it is registered under.
        human_name = safe_filename(entry.get("name") or "Dossier", fallback="Dossier")
        subtitle = "CONFIDENTIAL" + (" -- DRAFT" if body.draft_watermark else "")

        if body.granularity in ("combined", "both"):
            document = export_mod.build_document(
                rendered_sections, tables_by_section, title=title, subtitle=subtitle)
            relative = save_bytes(b"", base, ".docx")
            export_mod.write_docx(document, str(abs_path(relative)))
            leftover = [] if body.citations == "inline" else \
                export_mod.citation_markers(str(abs_path(relative)))
            if leftover:
                # The strip ran and something that reads as a citation is still
                # in the file. Refused, not shipped: the recipient cannot
                # resolve a marker, and one surviving says the strip missed a form.
                abs_path(relative).unlink(missing_ok=True)
                citation_blockers.append({
                    "code": "CITATION_MARKERS_REMAIN", "section_code": None,
                    "message": (f"{len(leftover)} citation marker(s) survived stripping in "
                                f"{human_name} (for example {leftover[0]!r}).")})
                continue
            written.append({"kind": "combined",
                            "deliverable_id": deliverable.id,
                            "filename": f"{human_name}.docx",
                            "storage_path": relative})

        if body.granularity in ("ectd_leaves", "both"):
            by_leaf: dict = {}
            for section_render in rendered_sections:
                by_leaf.setdefault(export_mod.leaf_for(section_render.section_code),
                                   []).append(section_render)
            leaf_files = []
            for leaf, leaf_sections in sorted(by_leaf.items()):
                document = export_mod.build_document(
                    leaf_sections, tables_by_section,
                    title=f"{title} -- {leaf}", subtitle=subtitle)
                relative = save_bytes(b"", base, ".docx")
                export_mod.write_docx(document, str(abs_path(relative)))
                leftover = [] if body.citations == "inline" else \
                    export_mod.citation_markers(str(abs_path(relative)))
                if leftover:
                    citation_blockers.append({
                        "code": "CITATION_MARKERS_REMAIN", "section_code": None,
                        "message": (f"{len(leftover)} citation marker(s) survived stripping in "
                                    f"{human_name}, {leaf} (for example {leftover[0]!r}).")})
                leaf_files.append((export_mod.ectd_path("", leaf), relative))
            # One zip, so the folder tree survives a download.
            bundle = save_bytes(b"", base, ".zip")
            with zipfile.ZipFile(str(abs_path(bundle)), "w", zipfile.ZIP_DEFLATED) as archive:
                # Fixed entry headers: no server mtimes or Unix permissions.
                for arcname, relative in leaf_files:
                    write_fixed(archive, arcname, path=str(abs_path(relative)))
                write_fixed(archive, "manifest.txt", data=export_mod.manifest_lines(plan.leaves))
                write_fixed(
                    archive, "README.txt", data=
                    "Leaf files and a manifest, named by convention. This is NOT an eCTD "
                    "backbone: assembling and validating a submission is a publishing tool's "
                    "job, and a partial backbone would look submittable without being so.\n")
            for _arc, relative in leaf_files:
                try:
                    abs_path(relative).unlink(missing_ok=True)
                except OSError:
                    pass
            written.append({"kind": "ectd_leaves",
                            "deliverable_id": deliverable.id,
                            "filename": f"{human_name} (eCTD leaves).zip",
                            "storage_path": bundle})

    if citation_blockers:
        # Not overridable: approval can be overridden, a marker the recipient
        # cannot resolve is a defect in the file.
        for item in written:
            abs_path(item["storage_path"]).unlink(missing_ok=True)
        raise error(
            "CMC_EXPORT_BLOCKED",
            "Citation markers survived stripping, so the export was refused. Edit the "
            "sections that contain them and export again.",
            409, {"blockers": citation_blockers + all_blockers[:49], "plans": plans})
    if all_blockers and not body.override_approval:
        raise error(
            "CMC_EXPORT_BLOCKED",
            f"{len(all_blockers)} thing(s) stand in the way of an export. Approve the "
            "sections and resolve the findings, or export with an explicit override.",
            409, {"blockers": all_blockers[:50], "plans": plans})

    record = CmcExport(
        org_id=user.org_id, cmc_project_id=cp.id,
        granularity=body.granularity,
        options={"citations": body.citations, "draft_watermark": body.draft_watermark,
                 "override_approval": body.override_approval,
                 "files": written, "blockers": all_blockers},
        storage_path=written[0]["storage_path"] if written else None,
        created_by=user.id)
    db.add(record)
    db.flush()
    log_audit(db, user, "Exported the dossier", "cmc_export", record.id, cp.project_id,
              "warning" if body.override_approval else "success",
              f"{body.granularity}, {len(written)} file(s)"
              + (" (approval overridden)" if body.override_approval else ""))
    db.commit()
    db.refresh(record)
    return {"id": record.id, "granularity": record.granularity,
            "files": written, "plans": plans,
            "overridden": body.override_approval, "created_at": record.created_at}


@router.get("/cmc/projects/{cmc_project_id}/exports")
def list_exports(cmc_project_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    from app.models import CmcExport

    cp = _owned_cmc_project(db, cmc_project_id, user)
    rows = db.scalars(select(CmcExport).where(
        CmcExport.cmc_project_id == cp.id).order_by(CmcExport.created_at.desc())).all()
    return {"items": [{"id": e.id, "granularity": e.granularity,
                       "files": (e.options or {}).get("files", []),
                       "overridden": (e.options or {}).get("override_approval", False),
                       "created_at": e.created_at} for e in rows]}


@router.get("/cmc/exports/{cmc_export_id}/download")
def download_export(cmc_export_id: str, index: int = Query(0, ge=0),
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """One file out of an export, by its position in `options["files"]`.

    An export of granularity "both" writes a combined .docx AND an eCTD .zip,
    and `CmcExport.storage_path` records only the first of them. The screen
    listed every file and offered one download, so the second was named in the
    UI and reachable by nothing -- a dossier a reviewer could see and not open.
    """
    from fastapi.responses import FileResponse

    from app.generation.filenames import content_disposition
    from app.models import CmcExport
    from app.storage import abs_path

    record = db.get(CmcExport, cmc_export_id)
    if not record or record.org_id != user.org_id:
        raise error("CMC_EXPORT_NOT_FOUND", "Export not found", 404)

    files = (record.options or {}).get("files", [])
    if index and index >= len(files):
        raise error("CMC_EXPORT_NO_SUCH_FILE",
                    f"This export produced {len(files)} file(s); there is no file "
                    f"{index + 1}.", 404)
    entry = files[index] if index < len(files) else None
    # `storage_path` remains the fallback for the first file, so exports
    # written before `options["files"]` existed still download.
    relative = (entry or {}).get("storage_path") or (
        record.storage_path if index == 0 else None)
    if not relative:
        raise error("CMC_EXPORT_EMPTY", "This export produced no file.", 409)
    path = abs_path(relative)
    if not path.exists():
        raise error("CMC_EXPORT_MISSING",
                    "The exported file is no longer in storage.", 410)
    name = (entry or {}).get("filename") or os.path.basename(str(path))
    return FileResponse(str(path), headers={"Content-Disposition": content_disposition(name)})
