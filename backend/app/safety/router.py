"""The Safety / Pharmacovigilance module's HTTP surface, M1.

What M1 answers: a product exists, its Reference Safety Information has
versions and one of them is pinned, and a reporting interval can be set up with
its three dates and told -- before anybody commits to it -- how many cases it
would actually contain.

Everything downstream (ingestion, de-identification, coding, tabulations,
drafting, QC, export) arrives in later milestones. What is here is deliberately
the part those depend on: the dates, the pin, and the roles.

Two conventions worth stating because they differ from the other modules.

`app.safety.scope` is the ONLY place a date becomes a filter. The
`scope-preview` endpoint and the report's own preview both go through it, so
the number shown on the setup screen and the number the report is built from
cannot be two different numbers.

`require_pv_role` guards the acts that carry regulatory weight, and it is a
different dimension from `app.authz`. Pinning the reference safety information
is the first thing it guards, because the pinned version is what "expected"
means for every event in the report that follows.
"""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.db import get_db
from app.models import (
    Project, PvApprovalStatus, PvDueDate, PvMember, PvProduct, PvReportInstance,
    PvRsiListedTerm, PvRsiVersion, PvSection, PvSectionDraft, User, now,
)
from app.ownership import owned_project
from app.safety import ingest as ingest_mod
from app.safety import registry, roles, scope as scope_mod, trees
from app.security import error, get_current_user
from app.storage import abs_path, save_bytes

router = APIRouter(tags=["safety"])

PV_FUNCTION = "Safety"

#: Every report instance status, weakest first.
REPORT_STATUSES = ("setup", "in_progress", "in_review", "approved")
SECTION_STATUSES = ("draft", "in_review", "approved")
APPROVAL_STATUSES = ("approved", "withdrawn", "not_approved", "pending")


# ------------------------------------------------------------------ helpers

def _owned_product(db: Session, pv_product_id: str, user: User) -> PvProduct:
    product = db.get(PvProduct, pv_product_id)
    if not product or product.org_id != user.org_id:
        raise error("PV_PRODUCT_NOT_FOUND", "Safety product not found", 404)
    return product


def _owned_report(db: Session, report_id: str, user: User) -> PvReportInstance:
    report = db.get(PvReportInstance, report_id)
    if not report or report.org_id != user.org_id:
        raise error("PV_REPORT_NOT_FOUND", "Report instance not found", 404)
    return report


def _owned_rsi(db: Session, rsi_version_id: str, user: User) -> PvRsiVersion:
    version = db.get(PvRsiVersion, rsi_version_id)
    if not version or version.org_id != user.org_id:
        raise error("PV_RSI_NOT_FOUND", "RSI version not found", 404)
    return version


def _product_out(db: Session, product: PvProduct) -> dict:
    reports = db.scalars(select(PvReportInstance).where(
        PvReportInstance.pv_product_id == product.id
    ).order_by(PvReportInstance.period_end.desc())).all()
    versions = db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == product.id
    ).order_by(PvRsiVersion.effective_date.desc().nullslast())).all()
    return {
        "id": product.id, "project_id": product.project_id,
        "product_name": product.product_name, "inn": product.inn,
        "mah_name": product.mah_name, "atc_code": product.atc_code,
        "ibd": product.ibd, "dibd": product.dibd,
        "formulations": product.formulations or [],
        "routes": product.routes or [],
        "approved_indications": product.approved_indications or [],
        "development_indications": product.development_indications or [],
        "regions": product.regions or [], "status": product.status,
        "reports": [_report_out(r) for r in reports],
        "rsi_versions": [_rsi_out(v) for v in versions],
        "created_at": product.created_at, "updated_at": product.updated_at,
    }


def _report_out(report: PvReportInstance, *, section_count: int | None = None) -> dict:
    entry = registry.DELIVERABLES.get(report.doc_type_key) or {}
    out = {
        "id": report.id, "pv_product_id": report.pv_product_id,
        "doc_type_key": report.doc_type_key,
        "doc_type_name": entry.get("name") or report.doc_type_key,
        "structure_basis": entry.get("structure_basis"),
        "cumulative_anchor": entry.get("cumulative_anchor"),
        "sequence_number": report.sequence_number,
        "period_start": report.period_start, "period_end": report.period_end,
        "data_lock_point": report.data_lock_point,
        "rsi_version_id": report.rsi_version_id,
        "meddra_version": report.meddra_version,
        "baseline_report_id": report.baseline_report_id,
        "regions": report.regions or [], "status": report.status,
        "qppv_signoff_by": report.qppv_signoff_by,
        "qppv_signoff_at": report.qppv_signoff_at,
        "created_at": report.created_at, "updated_at": report.updated_at,
    }
    if section_count is not None:
        out["section_count"] = section_count
    return out


def _rsi_out(version: PvRsiVersion, *, term_count: int | None = None) -> dict:
    out = {
        "id": version.id, "rsi_type": version.rsi_type,
        "version_label": version.version_label,
        "effective_date": version.effective_date,
        "source_document_id": version.source_document_id,
        "superseded_by": version.superseded_by,
        "is_current": version.is_current,
        "created_at": version.created_at,
    }
    if term_count is not None:
        out["listed_term_count"] = term_count
    return out


def _section_out(section: PvSection) -> dict:
    return {
        "id": section.id, "section_code": section.section_code,
        "title": section.title, "sort_order": section.sort_order,
        "level": section.level, "is_container": section.is_container,
        "enabled": section.enabled, "guidance_text": section.guidance_text,
        "table_key": section.table_key, "source_types": section.source_types or [],
        "status": section.status, "delta_status": section.delta_status,
        "baseline_section_id": section.baseline_section_id,
    }


# ------------------------------------------------------------------ products

class ProductIn(BaseModel):
    project_id: str
    product_name: str
    inn: str | None = None
    mah_name: str | None = None
    atc_code: str | None = None
    ibd: date | None = None
    dibd: date | None = None
    formulations: list = []
    routes: list = []
    approved_indications: list = []
    development_indications: list = []
    regions: list = []


class ProductPatch(BaseModel):
    product_name: str | None = None
    inn: str | None = None
    mah_name: str | None = None
    atc_code: str | None = None
    ibd: date | None = None
    dibd: date | None = None
    formulations: list | None = None
    routes: list | None = None
    approved_indications: list | None = None
    development_indications: list | None = None
    regions: list | None = None
    status: str | None = None


def _validate_regions(regions) -> None:
    unknown = [r for r in (regions or []) if r not in registry.REGIONS]
    if unknown:
        raise error("PV_BAD_REGION",
                    f"unknown region(s) {', '.join(unknown)}; one of "
                    f"{', '.join(registry.REGIONS)}.", 422)


@router.post("/pv/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    project = owned_project(db, body.project_id, user)
    if project.function != PV_FUNCTION:
        raise error("PV_WRONG_PROJECT",
                    f"A safety product lives in a {PV_FUNCTION} project; this one is "
                    f"{project.function}.", 422)
    if db.scalar(select(PvProduct).where(PvProduct.project_id == project.id)) is not None:
        raise error("PV_PRODUCT_EXISTS",
                    "This project already has a safety product. Open it instead.", 409)
    _validate_regions(body.regions)

    product = PvProduct(
        org_id=user.org_id, project_id=project.id, product_name=body.product_name,
        inn=body.inn, mah_name=body.mah_name, atc_code=body.atc_code,
        ibd=body.ibd, dibd=body.dibd, formulations=body.formulations,
        routes=body.routes, approved_indications=body.approved_indications,
        development_indications=body.development_indications, regions=body.regions,
        created_by=user.id)
    db.add(product)
    db.flush()
    # The creator becomes a WRITER, and only a writer. Nobody acquires the
    # authority to confirm a causality assessment by being the first person to
    # press a button; a qualified person is granted, explicitly, by somebody.
    roles.grant(db, pv_product_id=product.id, org_id=user.org_id, user_id=user.id,
                pv_role=roles.WRITER, granted_by=user.id)
    log_audit(db, user, "Created a safety product", "pv_product", product.id,
              project.id, "info", product.product_name)
    db.commit()
    db.refresh(product)
    return _product_out(db, product)


@router.get("/pv/products")
def list_products(db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    rows = db.scalars(select(PvProduct).where(
        PvProduct.org_id == user.org_id).order_by(PvProduct.created_at.desc())).all()
    return {"items": [_product_out(db, p) for p in rows]}


@router.get("/pv/products/{pv_product_id}")
def get_product(pv_product_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    out = _product_out(db, product)
    out["my_role"] = roles.role_of(db, product.id, user)
    return out


@router.patch("/pv/products/{pv_product_id}")
def update_product(pv_product_id: str, body: ProductPatch,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    changes = body.model_dump(exclude_unset=True)
    if "regions" in changes:
        _validate_regions(changes["regions"])
    # The birth dates are where cumulative counting starts. Moving one silently
    # rewrites every cumulative figure in every report of this product, so the
    # change is recorded as a change rather than as an edit.
    anchors_before = {"ibd": product.ibd, "dibd": product.dibd}
    for key, value in changes.items():
        setattr(product, key, value)
    product.updated_at = now()
    moved = [f"{k} {anchors_before[k]} -> {getattr(product, k)}"
             for k in ("ibd", "dibd")
             if k in changes and anchors_before[k] != getattr(product, k)]
    log_audit(db, user, "Updated a safety product", "pv_product", product.id,
              product.project_id, "warning" if moved else "info",
              "; ".join(moved) if moved else ", ".join(sorted(changes)))
    db.commit()
    db.refresh(product)
    return _product_out(db, product)


@router.delete("/pv/products/{pv_product_id}")
def delete_product(pv_product_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Purge this module's data for the product.

    Children before parents, and the list is hand-written on purpose: a purge
    that silently missed a table would leave case-level safety data in a
    database somebody was told was empty. Extended by every milestone that adds
    a table -- and `tests/test_safety_purge.py` walks the metadata to check
    that none was forgotten.
    """
    from app.models import (
        PvCase, PvCaseDrug, PvCaseEvent, PvCaseLab, PvCaseNarrative, PvCaseOriginal,
        PvChunk, PvCitation, PvDeidItem, PvDocument, PvDuplicateCandidate, PvExport,
        PvExposure, PvLiteratureRef, PvMappingProfile, PvSafetyAction,
        PvSafetyConcern, PvSignal, PvStudy,
    )
    from app.storage import abs_path

    product = _owned_product(db, pv_product_id, user)
    counts: dict = {}

    reports = db.scalars(select(PvReportInstance).where(
        PvReportInstance.pv_product_id == product.id)).all()
    report_ids = [r.id for r in reports]
    sections = db.scalars(select(PvSection).where(
        PvSection.report_instance_id.in_(report_ids))).all() if report_ids else []
    for section in sections:
        for draft in db.scalars(select(PvSectionDraft).where(
                PvSectionDraft.pv_section_id == section.id)).all():
            for citation in db.scalars(select(PvCitation).where(
                    PvCitation.draft_id == draft.id)).all():
                db.delete(citation)
            db.delete(draft)
        db.delete(section)
    counts["pv_sections"] = len(sections)
    db.flush()

    # Blobs are collected before the rows that name them are deleted.
    blobs = []
    for model in (PvDocument, PvExport):
        for row in db.scalars(select(model).where(
                model.pv_product_id == product.id)).all():
            if row.blob_path:
                blobs.append(abs_path(row.blob_path))

    # `PvMappingProfile` is last of the product-scoped models and is the one
    # that needs saying: its `pv_product_id` is nullable, because a column
    # mapping for one safety system can be reused across products. A profile
    # bound to THIS product goes with it; an org-wide one is not this
    # product's to delete.
    for model in (PvCaseLab, PvCaseNarrative, PvCaseOriginal, PvCaseDrug,
                  PvCaseEvent, PvDuplicateCandidate, PvDeidItem, PvCase,
                  PvChunk, PvDocument, PvExposure, PvExport, PvStudy, PvSignal,
                  PvSafetyConcern, PvSafetyAction, PvLiteratureRef,
                  PvApprovalStatus, PvMappingProfile):
        rows = db.scalars(select(model).where(
            model.pv_product_id == product.id)).all()
        counts[model.__tablename__] = len(rows)
        for row in rows:
            db.delete(row)
        db.flush()

    # Due dates hang off the report, RSI terms off the version: both are
    # deleted before the parent that owns them.
    if report_ids:
        for due in db.scalars(select(PvDueDate).where(
                PvDueDate.report_instance_id.in_(report_ids))).all():
            db.delete(due)
    versions = db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == product.id)).all()
    if versions:
        for term in db.scalars(select(PvRsiListedTerm).where(
                PvRsiListedTerm.rsi_version_id.in_([v.id for v in versions]))).all():
            db.delete(term)
    db.flush()
    for version in versions:
        db.delete(version)
    counts["pv_rsi_versions"] = len(versions)
    for report in reports:
        db.delete(report)
    counts["pv_report_instances"] = len(reports)
    for member in db.scalars(select(PvMember).where(
            PvMember.pv_product_id == product.id)).all():
        db.delete(member)
    db.flush()
    db.delete(product)
    log_audit(db, user, "Deleted a safety product", "pv_product", product.id,
              product.project_id, "warning",
              f"purged {counts.get('pv_cases', 0)} cases, "
              f"{counts.get('pv_case_events', 0)} events, "
              f"{counts['pv_sections']} sections")
    db.commit()

    purged_files = 0
    for blob in blobs:
        try:
            blob.unlink(missing_ok=True)
            purged_files += 1
        except OSError:
            pass
    return {"deleted": True, "purged": counts, "purged_files": purged_files}


# --------------------------------------------------------------------- roles

class MemberIn(BaseModel):
    user_id: str
    pv_role: str


@router.get("/pv/products/{pv_product_id}/members")
def list_members(pv_product_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvMember).where(
        PvMember.pv_product_id == product.id)).all()
    users = {u.id: u for u in db.scalars(select(User).where(
        User.org_id == user.org_id)).all()}
    return {
        "items": [{
            "id": m.id, "user_id": m.user_id,
            "user_name": getattr(users.get(m.user_id), "full_name", None),
            "user_email": getattr(users.get(m.user_id), "email", None),
            "pv_role": m.pv_role, "granted_by": m.granted_by,
            "created_at": m.created_at,
        } for m in rows],
        "roles": [{"key": key, "label": roles.ROLE_LABELS[key]}
                  for key in registry.PV_ROLES],
        "my_role": roles.role_of(db, product.id, user),
    }


@router.post("/pv/products/{pv_product_id}/members", status_code=201)
def add_member(pv_product_id: str, body: MemberIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Grant a role.

    Granting the qualified-person role needs either an existing qualified
    person on this product, or `MANAGE_USERS` at the organisation. The second
    is not a back door -- it is the bootstrap. Requiring only the first would
    deadlock every new product: the role could never be given to anyone,
    because nobody would hold it yet.

    `MANAGE_USERS` is the right capability to carry it. Appointing the person
    who takes regulatory responsibility for a product's safety IS an
    organisational act, and that capability already means "may change what this
    organisation is". Note what this does NOT do: holding it does not make
    anybody a qualified person. It lets them name one, explicitly, in an
    entry that records who named whom.
    """
    from app.authz import MANAGE_USERS, has_capability

    product = _owned_product(db, pv_product_id, user)
    if body.pv_role == roles.QUALIFIED_PERSON:
        if not (has_capability(user, MANAGE_USERS)
                or roles.has_pv_role(db, product.id, user, roles.QUALIFIED_PERSON)):
            raise error(
                "PV_ROLE_REQUIRED",
                "Naming a qualified person requires either an existing qualified "
                "person on this product or the ability to manage users in this "
                "organisation.", 403,
                {"required_role": roles.QUALIFIED_PERSON,
                 "actual_role": roles.role_of(db, product.id, user),
                 "or_capability": MANAGE_USERS})
    else:
        roles.require_pv_role(db, product.id, user, roles.REVIEWER,
                              action="Granting a role")
    target = db.get(User, body.user_id)
    if not target or target.org_id != user.org_id:
        raise error("PV_USER_NOT_FOUND", "User not found in this organisation", 404)
    member = roles.grant(db, pv_product_id=product.id, org_id=user.org_id,
                         user_id=body.user_id, pv_role=body.pv_role,
                         granted_by=user.id)
    log_audit(db, user, "Granted a pharmacovigilance role", "pv_product", product.id,
              product.project_id,
              "warning" if body.pv_role == roles.QUALIFIED_PERSON else "info",
              f"{target.email} -> {body.pv_role}")
    db.commit()
    db.refresh(member)
    return {"id": member.id, "user_id": member.user_id, "pv_role": member.pv_role}


# ----------------------------------------------------------------------- RSI

class RsiVersionIn(BaseModel):
    rsi_type: str
    version_label: str
    effective_date: date | None = None
    source_document_id: str | None = None


class ListedTermIn(BaseModel):
    meddra_pt: str
    meddra_soc: str | None = None
    condition_text: str | None = None


@router.get("/pv/products/{pv_product_id}/rsi-versions")
def list_rsi_versions(pv_product_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == product.id
    ).order_by(PvRsiVersion.effective_date.desc().nullslast())).all()
    counts = dict(db.execute(
        select(PvRsiListedTerm.rsi_version_id, func.count(PvRsiListedTerm.id))
        .where(PvRsiListedTerm.rsi_version_id.in_([r.id for r in rows] or [""]))
        .group_by(PvRsiListedTerm.rsi_version_id)).all())
    return {"items": [_rsi_out(v, term_count=counts.get(v.id, 0)) for v in rows],
            "rsi_types": list(registry.RSI_TYPES)}


@router.post("/pv/products/{pv_product_id}/rsi-versions", status_code=201)
def create_rsi_version(pv_product_id: str, body: RsiVersionIn,
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    if body.rsi_type not in registry.RSI_TYPES:
        raise error("PV_BAD_RSI_TYPE",
                    f"rsi_type must be one of {', '.join(registry.RSI_TYPES)}.", 422)
    existing = db.scalar(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == product.id,
        PvRsiVersion.rsi_type == body.rsi_type,
        PvRsiVersion.version_label == body.version_label))
    if existing is not None:
        raise error("PV_RSI_EXISTS",
                    f"{body.rsi_type} {body.version_label} already exists for this "
                    "product.", 409)
    version = PvRsiVersion(
        org_id=user.org_id, pv_product_id=product.id, rsi_type=body.rsi_type,
        version_label=body.version_label, effective_date=body.effective_date,
        source_document_id=body.source_document_id, created_by=user.id)
    db.add(version)
    db.flush()
    log_audit(db, user, "Added an RSI version", "pv_rsi_version", version.id,
              product.project_id, "info",
              f"{body.rsi_type} {body.version_label}")
    db.commit()
    db.refresh(version)
    return _rsi_out(version, term_count=0)


@router.get("/pv/rsi-versions/{rsi_version_id}/listed-terms")
def list_listed_terms(rsi_version_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user),
                      q: str | None = None,
                      limit: int = Query(200, ge=1, le=2000),
                      offset: int = Query(0, ge=0)):
    version = _owned_rsi(db, rsi_version_id, user)
    statement = select(PvRsiListedTerm).where(
        PvRsiListedTerm.rsi_version_id == version.id)
    if (q or "").strip():
        statement = statement.where(PvRsiListedTerm.meddra_pt.ilike(f"%{q.strip()}%"))
    total = db.scalar(statement.with_only_columns(
        func.count(PvRsiListedTerm.id)).order_by(None)) or 0
    rows = db.scalars(statement.order_by(PvRsiListedTerm.meddra_pt)
                      .limit(limit).offset(offset)).all()
    return {"items": [{"id": t.id, "meddra_pt": t.meddra_pt,
                       "meddra_soc": t.meddra_soc,
                       "condition_text": t.condition_text} for t in rows],
            "total": total, "rsi_version": _rsi_out(version)}


@router.post("/pv/rsi-versions/{rsi_version_id}/listed-terms", status_code=201)
def add_listed_terms(rsi_version_id: str, body: list[ListedTermIn],
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """The terms this RSI version lists. This is what "expected" means, so
    adding to it is a qualified-person act."""
    version = _owned_rsi(db, rsi_version_id, user)
    roles.require_pv_role(db, version.pv_product_id, user, roles.QUALIFIED_PERSON,
                          action="Changing what the reference safety information lists")
    added = 0
    for entry in body:
        db.add(PvRsiListedTerm(
            org_id=user.org_id, rsi_version_id=version.id,
            meddra_pt=entry.meddra_pt, meddra_soc=entry.meddra_soc,
            condition_text=entry.condition_text))
        added += 1
    log_audit(db, user, "Added listed terms to an RSI version", "pv_rsi_version",
              version.id, None, "warning",
              f"{added} term(s) on {version.rsi_type} {version.version_label}")
    db.commit()
    return {"added": added}


@router.post("/pv/rsi-versions/{rsi_version_id}/pin")
def pin_rsi_version(rsi_version_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Make this the version new reports default to.

    A qualified-person act, and the first one this module has. The pinned
    version is what every subsequent expectedness determination is made
    against: the same event is listed under one CCDS and unlisted under the
    next, so changing the pin changes what "expected" means for everything that
    follows it.

    Reports already pinned to another version are NOT moved. An expectedness
    confirmed against version 3.1 was a determination about version 3.1, and
    re-pointing it at 3.2 would be recording a judgment nobody made.
    """
    version = _owned_rsi(db, rsi_version_id, user)
    roles.require_pv_role(db, version.pv_product_id, user, roles.QUALIFIED_PERSON,
                          action="Pinning the reference safety information")
    previous = db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == version.pv_product_id,
        PvRsiVersion.rsi_type == version.rsi_type,
        PvRsiVersion.is_current.is_(True))).all()
    for old in previous:
        if old.id == version.id:
            continue
        old.is_current = False
        old.superseded_by = version.id
    version.is_current = True
    version.updated_at = now()

    affected = db.scalar(select(func.count(PvReportInstance.id)).where(
        PvReportInstance.pv_product_id == version.pv_product_id,
        PvReportInstance.rsi_version_id.in_([o.id for o in previous] or [""]),
        PvReportInstance.status != "approved")) or 0
    log_audit(db, user, "Pinned an RSI version", "pv_rsi_version", version.id, None,
              "warning",
              f"{version.rsi_type} {version.version_label} is now current"
              + (f"; {affected} open report(s) still pinned to the previous version"
                 if affected else ""))
    db.commit()
    db.refresh(version)
    return {
        "pinned": _rsi_out(version),
        "superseded": [o.id for o in previous if o.id != version.id],
        # Stated rather than acted on: §2's sixth principle says an RSI change
        # mid-cycle flags affected determinations for re-review, and flagging
        # is a person's decision about each report, not a sweep.
        "open_reports_on_previous_version": affected,
    }


# ------------------------------------------------------------ report instances

class ReportIn(BaseModel):
    doc_type_key: str
    period_start: date
    period_end: date
    data_lock_point: date
    sequence_number: int | None = None
    rsi_version_id: str | None = None
    meddra_version: str | None = None
    baseline_report_id: str | None = None
    regions: list = []


class ScopePreviewIn(BaseModel):
    """The proposed dates, before an instance exists to hold them."""
    doc_type_key: str
    period_start: date
    period_end: date
    data_lock_point: date
    baseline_report_id: str | None = None


class ReportPatch(BaseModel):
    sequence_number: int | None = None
    rsi_version_id: str | None = None
    meddra_version: str | None = None
    regions: list | None = None
    status: str | None = None


def _validate_dates(*, period_start: date, period_end: date,
                    data_lock_point: date) -> None:
    if period_end < period_start:
        raise error("PV_BAD_PERIOD",
                    "The reporting period ends before it starts.", 422)
    if data_lock_point < period_end:
        # A lock before the period ends would exclude part of the very interval
        # the report is about, and every figure would be short by the tail.
        raise error("PV_DLP_BEFORE_PERIOD_END",
                    "The data lock point falls before the end of the reporting "
                    "period, so the report could not count its own final weeks. "
                    "Set the lock on or after the period end.", 422)


def _scope_from(product: PvProduct, *, doc_type_key: str, period_start: date,
                period_end: date, data_lock_point: date) -> scope_mod.Scope:
    anchor = registry.cumulative_anchor(doc_type_key)
    return scope_mod.Scope(
        pv_product_id=product.id, org_id=product.org_id,
        period_start=period_start, period_end=period_end,
        data_lock_point=data_lock_point,
        cumulative_from=getattr(product, anchor, None), anchor=anchor)


@router.get("/pv/report-types")
def list_report_types():
    """The registry, for the report-type picker."""
    return {"items": registry.catalogue(),
            "doc_types": registry.DOC_TYPES,
            "input_types": registry.INPUT_TYPES,
            "regions": list(registry.REGIONS)}


@router.post("/pv/products/{pv_product_id}/scope-preview")
def preview_scope_before_creating(pv_product_id: str, body: ScopePreviewIn,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """"Cases in interval: N · cumulative: M · new since last report: K",
    computed for dates nobody has committed to yet.

    Screen S2 shows this before the instance is created, because the three
    dates are the hardest thing to change afterwards -- every figure in the
    report is a function of them -- and a preview after the fact is a preview
    nobody can act on.
    """
    product = _owned_product(db, pv_product_id, user)
    if body.doc_type_key not in registry.DELIVERABLES:
        raise error("PV_UNKNOWN_REPORT_TYPE",
                    f"{body.doc_type_key!r} is not a report type; one of "
                    f"{', '.join(sorted(registry.DELIVERABLES))}.", 422)
    _validate_dates(period_start=body.period_start, period_end=body.period_end,
                    data_lock_point=body.data_lock_point)
    baseline = None
    if body.baseline_report_id:
        baseline = _owned_report(db, body.baseline_report_id, user)
    scope = _scope_from(product, doc_type_key=body.doc_type_key,
                        period_start=body.period_start, period_end=body.period_end,
                        data_lock_point=body.data_lock_point)
    return scope_mod.preview(db, scope, baseline=baseline)


@router.get("/pv/reports/{report_instance_id}/preview-scope")
def preview_scope(report_instance_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    report = _owned_report(db, report_instance_id, user)
    product = _owned_product(db, report.pv_product_id, user)
    baseline = (db.get(PvReportInstance, report.baseline_report_id)
                if report.baseline_report_id else None)
    return scope_mod.preview(db, scope_mod.scope_for(product, report),
                             baseline=baseline)


def _seed_sections(db, report: PvReportInstance, *, baseline=None) -> list[PvSection]:
    """The report's section tree, and whatever the baseline can carry into it.

    A section whose text the previous approved report already holds starts as
    `carried_forward` with that text as version 1: a periodic report is written
    against its predecessor, and starting every section blank would throw away
    the document the writer's own organisation approved last cycle.

    A section that prints a computed table is NOT carried forward. Its table is
    rendered from this interval's data at export time, so its narrative is
    about numbers that have already changed -- `changed` is what the badge must
    say, or a writer keeps last interval's sentences over this interval's
    table.
    """
    seeded = trees.seed_sections(report.doc_type_key)
    baseline_sections = {}
    if baseline is not None:
        baseline_sections = {
            s.section_code: s for s in db.scalars(select(PvSection).where(
                PvSection.report_instance_id == baseline.id)).all()}

    created: list[PvSection] = []
    for spec in seeded:
        source = baseline_sections.get(spec["section_code"])
        delta = "fresh"
        carried_text = None
        if source is not None:
            if spec["table_key"]:
                delta = "changed"
            else:
                latest = db.scalar(select(PvSectionDraft).where(
                    PvSectionDraft.pv_section_id == source.id
                ).order_by(PvSectionDraft.version.desc()))
                if latest is not None and (latest.content or "").strip():
                    delta = "carried_forward"
                    carried_text = latest.content
                else:
                    delta = "changed"
        section = PvSection(
            org_id=report.org_id, report_instance_id=report.id,
            section_code=spec["section_code"], title=spec["title"],
            sort_order=spec["sort_order"], level=spec["level"],
            is_container=spec["is_container"], guidance_text=spec["guidance_text"],
            table_key=spec["table_key"], source_types=spec["source_types"],
            delta_status=delta,
            baseline_section_id=source.id if source is not None else None)
        db.add(section)
        db.flush()
        if carried_text is not None:
            db.add(PvSectionDraft(
                org_id=report.org_id, pv_section_id=section.id, version=1,
                content=carried_text, origin="carried_forward",
                generation_params={"carried_from_section_id": source.id,
                                   "carried_from_report_id": baseline.id},
                created_by=report.created_by))
            section.status = "draft"
        created.append(section)
    return created


@router.post("/pv/products/{pv_product_id}/reports", status_code=201)
def create_report(pv_product_id: str, body: ReportIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Creating a report instance")
    if body.doc_type_key not in registry.DELIVERABLES:
        raise error("PV_UNKNOWN_REPORT_TYPE",
                    f"{body.doc_type_key!r} is not a report type; one of "
                    f"{', '.join(sorted(registry.DELIVERABLES))}.", 422)
    _validate_dates(period_start=body.period_start, period_end=body.period_end,
                    data_lock_point=body.data_lock_point)
    _validate_regions(body.regions)

    baseline = None
    if body.baseline_report_id:
        baseline = _owned_report(db, body.baseline_report_id, user)
        if baseline.pv_product_id != product.id:
            raise error("PV_BASELINE_WRONG_PRODUCT",
                        "The baseline report belongs to a different product.", 422)
        if baseline.doc_type_key != body.doc_type_key:
            raise error("PV_BASELINE_WRONG_TYPE",
                        f"The baseline is a {baseline.doc_type_key} and this is a "
                        f"{body.doc_type_key}; sections would not line up.", 422)
        if baseline.period_end > body.period_start:
            raise error("PV_BASELINE_OVERLAPS",
                        "The baseline report's period ends after this one starts, so "
                        "the two intervals overlap and cases would be counted twice.",
                        422)

    if body.rsi_version_id:
        version = _owned_rsi(db, body.rsi_version_id, user)
        if version.pv_product_id != product.id:
            raise error("PV_RSI_WRONG_PRODUCT",
                        "That RSI version belongs to a different product.", 422)

    report = PvReportInstance(
        org_id=user.org_id, pv_product_id=product.id,
        doc_type_key=body.doc_type_key, sequence_number=body.sequence_number,
        period_start=body.period_start, period_end=body.period_end,
        data_lock_point=body.data_lock_point, rsi_version_id=body.rsi_version_id,
        meddra_version=body.meddra_version,
        baseline_report_id=baseline.id if baseline else None,
        regions=body.regions, created_by=user.id)
    db.add(report)
    db.flush()
    sections = _seed_sections(db, report, baseline=baseline)
    carried = sum(1 for s in sections if s.delta_status == "carried_forward")
    log_audit(db, user, "Created a report instance", "pv_report_instance", report.id,
              product.project_id, "info",
              f"{body.doc_type_key} {body.period_start}..{body.period_end}, "
              f"DLP {body.data_lock_point}, {len(sections)} sections"
              + (f", {carried} carried forward" if carried else ""))
    db.commit()
    db.refresh(report)
    out = _report_out(report, section_count=len(sections))
    out["sections"] = [_section_out(s) for s in sorted(sections,
                                                       key=lambda s: s.sort_order)]
    out["carried_forward"] = carried
    return out


@router.get("/pv/products/{pv_product_id}/reports")
def list_reports(pv_product_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvReportInstance).where(
        PvReportInstance.pv_product_id == product.id
    ).order_by(PvReportInstance.period_end.desc())).all()
    return {"items": [_report_out(r) for r in rows]}


@router.get("/pv/reports/{report_instance_id}")
def get_report(report_instance_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    report = _owned_report(db, report_instance_id, user)
    count = db.scalar(select(func.count(PvSection.id)).where(
        PvSection.report_instance_id == report.id)) or 0
    out = _report_out(report, section_count=count)
    out["my_role"] = roles.role_of(db, report.pv_product_id, user)
    return out


@router.get("/pv/reports/{report_instance_id}/sections")
def list_sections(report_instance_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    report = _owned_report(db, report_instance_id, user)
    rows = db.scalars(select(PvSection).where(
        PvSection.report_instance_id == report.id
    ).order_by(PvSection.sort_order)).all()
    return {"items": [_section_out(s) for s in rows]}


@router.patch("/pv/reports/{report_instance_id}")
def update_report(report_instance_id: str, body: ReportPatch,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    report = _owned_report(db, report_instance_id, user)
    roles.require_pv_role(db, report.pv_product_id, user, roles.WRITER,
                          action="Changing a report instance")
    changes = body.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] not in REPORT_STATUSES:
        raise error("PV_BAD_STATUS",
                    f"status must be one of {', '.join(REPORT_STATUSES)}.", 422)
    if changes.get("status") == "approved":
        raise error("PV_SIGNOFF_REQUIRED",
                    "A report is approved by sign-off, not by setting its status.", 409)
    if "regions" in changes:
        _validate_regions(changes["regions"])
    if changes.get("rsi_version_id"):
        version = _owned_rsi(db, changes["rsi_version_id"], user)
        if version.pv_product_id != report.pv_product_id:
            raise error("PV_RSI_WRONG_PRODUCT",
                        "That RSI version belongs to a different product.", 422)
    for key, value in changes.items():
        setattr(report, key, value)
    report.updated_at = now()
    log_audit(db, user, "Updated a report instance", "pv_report_instance", report.id,
              None, "info", ", ".join(sorted(changes)))
    db.commit()
    db.refresh(report)
    return _report_out(report)


@router.delete("/pv/reports/{report_instance_id}")
def delete_report(report_instance_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Remove one interval's report. The case store is untouched: it belongs to
    the product, and the next report is built from it."""
    from app.models import PvExposure

    report = _owned_report(db, report_instance_id, user)
    roles.require_pv_role(db, report.pv_product_id, user, roles.WRITER,
                          action="Deleting a report instance")
    if report.status == "approved":
        raise error("PV_REPORT_APPROVED",
                    "An approved report cannot be deleted.", 409)
    dependents = db.scalars(select(PvReportInstance).where(
        PvReportInstance.baseline_report_id == report.id)).all()
    if dependents:
        raise error("PV_REPORT_IS_BASELINE",
                    f"{len(dependents)} later report(s) carry text forward from this "
                    "one. Delete those first, or they would lose their baseline.", 409)

    sections = db.scalars(select(PvSection).where(
        PvSection.report_instance_id == report.id)).all()
    from app.models import PvCitation

    for section in sections:
        for draft in db.scalars(select(PvSectionDraft).where(
                PvSectionDraft.pv_section_id == section.id)).all():
            for citation in db.scalars(select(PvCitation).where(
                    PvCitation.draft_id == draft.id)).all():
                db.delete(citation)
            db.delete(draft)
        db.delete(section)
    for due in db.scalars(select(PvDueDate).where(
            PvDueDate.report_instance_id == report.id)).all():
        db.delete(due)
    for exposure in db.scalars(select(PvExposure).where(
            PvExposure.report_instance_id == report.id)).all():
        db.delete(exposure)
    db.flush()
    db.delete(report)
    log_audit(db, user, "Deleted a report instance", "pv_report_instance", report.id,
              None, "warning", f"{report.doc_type_key} {report.period_start}"
              f"..{report.period_end}, {len(sections)} sections")
    db.commit()
    return {"deleted": True, "sections": len(sections)}


# ---------------------------------------------------------------- due dates

class DueDateIn(BaseModel):
    region: str
    submission_due_date: date | None = None
    basis_note: str | None = None


@router.post("/pv/reports/{report_instance_id}/due-dates", status_code=201)
def add_due_date(report_instance_id: str, body: DueDateIn,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    report = _owned_report(db, report_instance_id, user)
    _validate_regions([body.region])
    due = PvDueDate(org_id=user.org_id, report_instance_id=report.id,
                    region=body.region,
                    submission_due_date=body.submission_due_date,
                    basis_note=body.basis_note, is_informational=True)
    db.add(due)
    db.flush()
    log_audit(db, user, "Recorded an informational due date", "pv_report_instance",
              report.id, None, "info",
              f"{body.region} {body.submission_due_date}")
    db.commit()
    return {"id": due.id, "region": due.region,
            "submission_due_date": due.submission_due_date,
            "basis_note": due.basis_note, "is_informational": True}


@router.get("/pv/products/{pv_product_id}/calendar")
def reporting_calendar(pv_product_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """This product's reporting intervals by data lock point and due date.

    `disclaimer` travels with the data rather than living in the screen, so a
    caller cannot render the dates without it. §2's third principle: this is
    not the reporting clock, and the sponsor's PV system of record governs the
    obligation.
    """
    product = _owned_product(db, pv_product_id, user)
    reports = db.scalars(select(PvReportInstance).where(
        PvReportInstance.pv_product_id == product.id
    ).order_by(PvReportInstance.data_lock_point)).all()
    dues = {}
    for due in db.scalars(select(PvDueDate).where(
            PvDueDate.report_instance_id.in_([r.id for r in reports] or [""]))).all():
        dues.setdefault(due.report_instance_id, []).append({
            "id": due.id, "region": due.region,
            "submission_due_date": due.submission_due_date,
            "basis_note": due.basis_note, "is_informational": True})
    return {
        "items": [dict(_report_out(r), due_dates=dues.get(r.id, []))
                  for r in reports],
        "disclaimer": (
            "Informational only. These dates are derived from the reporting "
            "intervals recorded here and do not establish a submission "
            "obligation. Your pharmacovigilance system of record governs "
            "reporting obligations and their timing."),
    }


# --------------------------------------------------------- approval statuses

class ApprovalStatusIn(BaseModel):
    country: str
    approval_date: date | None = None
    indication: str | None = None
    formulation: str | None = None
    status: str = "approved"


@router.get("/pv/products/{pv_product_id}/approval-statuses")
def list_approval_statuses(pv_product_id: str, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvApprovalStatus).where(
        PvApprovalStatus.pv_product_id == product.id
    ).order_by(PvApprovalStatus.country)).all()
    return {"items": [{
        "id": a.id, "country": a.country, "approval_date": a.approval_date,
        "indication": a.indication, "formulation": a.formulation,
        "status": a.status, "source_document_id": a.source_document_id,
    } for a in rows], "statuses": list(APPROVAL_STATUSES)}


@router.post("/pv/products/{pv_product_id}/approval-statuses", status_code=201)
def add_approval_status(pv_product_id: str, body: ApprovalStatusIn,
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    product = _owned_product(db, pv_product_id, user)
    if body.status not in APPROVAL_STATUSES:
        raise error("PV_BAD_APPROVAL_STATUS",
                    f"status must be one of {', '.join(APPROVAL_STATUSES)}.", 422)
    existing = db.scalar(select(PvApprovalStatus).where(
        PvApprovalStatus.pv_product_id == product.id,
        PvApprovalStatus.country == body.country,
        PvApprovalStatus.formulation == body.formulation))
    if existing is not None:
        raise error("PV_APPROVAL_EXISTS",
                    f"{body.country} is already recorded for this formulation.", 409)
    row = PvApprovalStatus(
        org_id=user.org_id, pv_product_id=product.id, country=body.country,
        approval_date=body.approval_date, indication=body.indication,
        formulation=body.formulation, status=body.status)
    db.add(row)
    db.flush()
    log_audit(db, user, "Recorded a marketing approval status", "pv_product",
              product.id, product.project_id, "info",
              f"{body.country}: {body.status}")
    db.commit()
    return {"id": row.id, "country": row.country, "status": row.status,
            "approval_date": row.approval_date, "indication": row.indication,
            "formulation": row.formulation}


# ================================================================= M2: sources

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = (".xml", ".csv", ".xlsx", ".pdf", ".docx", ".rtf", ".txt", ".md")

#: Which suffixes each input type can actually be. An E2B export is XML; a
#: line listing is a spreadsheet. Refusing the mismatch at upload is cheaper
#: than a parser failure twenty files later, and the message can say what was
#: expected rather than what went wrong.
INPUT_SUFFIXES = {
    "e2b_r3_xml": (".xml",),
    "line_listing": (".csv", ".xlsx"),
    "cioms_form": (".pdf", ".docx", ".txt", ".md"),
    "case_narrative_doc": (".pdf", ".docx", ".rtf", ".txt", ".md"),
    "document": ALLOWED_SUFFIXES,
}


def _document_out(d) -> dict:
    return {"id": d.id, "doc_type": d.doc_type, "input_type": d.input_type,
            "report_instance_id": d.report_instance_id,
            "filename": d.original_filename, "mime_type": d.mime_type,
            "size_bytes": d.size_bytes, "page_count": d.page_count,
            "processing_status": d.processing_status,
            "error_message": d.error_message, "chunk_count": d.chunk_count,
            "case_count": d.case_count,
            "created_at": d.created_at, "updated_at": d.updated_at}


def _owned_document(db: Session, document_id: str, user: User):
    from app.models import PvDocument

    document = db.get(PvDocument, document_id)
    if not document or document.org_id != user.org_id:
        raise error("PV_DOCUMENT_NOT_FOUND", "Source not found", 404)
    return document


def _report_type_keys(db: Session, pv_product_id: str) -> list:
    return [r.doc_type_key for r in db.scalars(select(PvReportInstance).where(
        PvReportInstance.pv_product_id == pv_product_id)).all()]


@router.post("/pv/products/{pv_product_id}/documents", status_code=201)
async def upload_sources(pv_product_id: str,
                         files: list[UploadFile] = File(...),
                         doc_types: list[str] = Form(...),
                         input_types: list[str] | None = Form(None),
                         report_instance_id: str | None = Form(None),
                         db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Upload tagged sources.

    Two tags per file. The document type decides which sections may cite it;
    the input type decides whether it is parsed into cases or read as a
    document, and those are different pipelines -- an E2B export read as a
    document would become prose nobody can count, and a study report read as an
    ICSR would fail on every field.
    """
    from app.models import PvDocument
    from app.safety.ingest import file_hash

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Uploading safety sources")
    if len(doc_types) != len(files):
        raise error("PV_TAGS_MISMATCH",
                    f"{len(files)} file(s) arrived with {len(doc_types)} document "
                    "type(s); every file needs exactly one.", 422)
    inputs = list(input_types or [])
    if inputs and len(inputs) != len(files):
        raise error("PV_TAGS_MISMATCH",
                    "input_types, when given, needs one entry per file.", 422)
    if report_instance_id:
        report = _owned_report(db, report_instance_id, user)
        if report.pv_product_id != product.id:
            raise error("PV_REPORT_WRONG_PRODUCT",
                        "That report instance belongs to a different product.", 422)

    saved = []
    for index, (upload, doc_type) in enumerate(zip(files, doc_types)):
        tag = (doc_type or "").strip().lower()
        if tag not in registry.DOC_TYPES:
            raise error("PV_BAD_DOC_TYPE",
                        f"Unknown document type {doc_type!r}; one of "
                        f"{', '.join(sorted(registry.DOC_TYPES))}.", 422)
        input_type = ((inputs[index] if inputs else "") or "document").strip().lower()
        if input_type not in registry.INPUT_TYPES:
            raise error("PV_BAD_INPUT_TYPE",
                        f"Unknown input type {input_type!r}; one of "
                        f"{', '.join(sorted(registry.INPUT_TYPES))}.", 422)

        name = upload.filename or "source"
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise error("PV_UNSUPPORTED_FILE",
                        f"{name}: {suffix or 'files with no extension'} cannot be "
                        f"read. Supported: {', '.join(ALLOWED_SUFFIXES)}.", 422)
        expected = INPUT_SUFFIXES.get(input_type, ALLOWED_SUFFIXES)
        if suffix not in expected:
            raise error("PV_INPUT_TYPE_MISMATCH",
                        f"{name} is a {suffix} file, and a "
                        f"{registry.INPUT_TYPES[input_type]} is expected to be "
                        f"{' or '.join(expected)}.", 422)

        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise error("PV_FILE_TOO_LARGE",
                        f"{name} is {len(data) // (1024 * 1024)} MB; the limit is "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file.", 413)
        if not data:
            raise error("PV_EMPTY_FILE", f"{name} is empty.", 422)

        document = PvDocument(
            org_id=user.org_id, pv_product_id=product.id,
            report_instance_id=report_instance_id or None,
            doc_type=tag, input_type=input_type, original_filename=name,
            blob_path=save_bytes(data, f"pv/{product.id}", suffix),
            mime_type=upload.content_type, size_bytes=len(data),
            file_hash=file_hash(data), uploaded_by=user.id)
        db.add(document)
        saved.append(document)
    db.flush()
    log_audit(db, user, "Uploaded safety sources", "pv_product", product.id,
              product.project_id, "info", f"{len(saved)} file(s)")
    db.commit()
    for document in saved:
        db.refresh(document)
    return {"items": [_document_out(d) for d in saved]}


@router.get("/pv/products/{pv_product_id}/documents")
def list_sources(pv_product_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    from app.models import PvDocument
    from app.safety.ingest import readiness

    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvDocument).where(
        PvDocument.pv_product_id == product.id).order_by(PvDocument.created_at)).all()
    return {"items": [_document_out(d) for d in rows],
            "readiness": readiness(rows, _report_type_keys(db, product.id)),
            "doc_types": registry.DOC_TYPES,
            "input_types": registry.INPUT_TYPES}


class SourcePatch(BaseModel):
    doc_type: str | None = None
    input_type: str | None = None
    report_instance_id: str | None = None


@router.patch("/pv/documents/{pv_document_id}")
def retag_source(pv_document_id: str, body: SourcePatch,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    document = _owned_document(db, pv_document_id, user)
    roles.require_pv_role(db, document.pv_product_id, user, roles.WRITER,
                          action="Retagging a source")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("doc_type") and changes["doc_type"] not in registry.DOC_TYPES:
        raise error("PV_BAD_DOC_TYPE",
                    f"Unknown document type {changes['doc_type']!r}.", 422)
    if changes.get("input_type") and changes["input_type"] not in registry.INPUT_TYPES:
        raise error("PV_BAD_INPUT_TYPE",
                    f"Unknown input type {changes['input_type']!r}.", 422)
    for key, value in changes.items():
        setattr(document, key, value)
    # Retagging changes which pipeline the file goes through, so whatever the
    # last one produced is no longer what this file says. It goes back to the
    # queue rather than keeping a result from a reading nobody wants now.
    document.processing_status = ingest_mod.QUEUED
    document.error_message = None
    document.updated_at = now()
    log_audit(db, user, "Retagged a safety source", "pv_product",
              document.pv_product_id, None, "info",
              f"{document.original_filename}: " + ", ".join(sorted(changes)))
    db.commit()
    db.refresh(document)
    return _document_out(document)


@router.delete("/pv/documents/{pv_document_id}")
def delete_source(pv_document_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Remove a source and everything read out of it.

    Cases first, because a case whose source is gone is a case nobody can trace
    to a document -- and an untraceable case in a safety report is worse than
    no case, since it will be counted.
    """
    from app.models import (
        PvCase, PvCaseDrug, PvCaseEvent, PvCaseNarrative, PvCaseOriginal, PvChunk,
    )

    document = _owned_document(db, pv_document_id, user)
    roles.require_pv_role(db, document.pv_product_id, user, roles.WRITER,
                          action="Deleting a source")
    cases = db.scalars(select(PvCase).where(
        PvCase.source_document_id == document.id)).all()
    case_ids = [c.id for c in cases]
    counts = {"cases": len(cases)}
    if case_ids:
        for model in (PvCaseEvent, PvCaseDrug, PvCaseNarrative, PvCaseOriginal):
            rows = db.scalars(select(model).where(model.case_id.in_(case_ids))).all()
            counts[model.__tablename__] = len(rows)
            for row in rows:
                db.delete(row)
        db.flush()
        for case in cases:
            db.delete(case)
    for original in db.scalars(select(PvCaseOriginal).where(
            PvCaseOriginal.source_document_id == document.id)).all():
        db.delete(original)
    for chunk in db.scalars(select(PvChunk).where(
            PvChunk.document_id == document.id)).all():
        db.delete(chunk)
    blob = abs_path(document.blob_path) if document.blob_path else None
    db.delete(document)
    log_audit(db, user, "Deleted a safety source", "pv_product",
              document.pv_product_id, None, "warning",
              f"{document.original_filename}: {counts['cases']} case(s) removed with it")
    db.commit()
    if blob is not None:
        try:
            blob.unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": True, "purged": counts}


class ProcessRequest(BaseModel):
    """Column mappings for the line listings in this batch, by document id."""
    mappings: dict = {}


@router.post("/pv/products/{pv_product_id}/process", status_code=202)
def process_sources(pv_product_id: str, body: ProcessRequest,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Read every queued source.

    A line listing without a mapping is refused here rather than failing in the
    worker, so the answer arrives while somebody is still looking at the screen
    that would fix it.
    """
    from app.models import PvDocument

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Processing safety sources")
    pending = db.scalars(select(PvDocument).where(
        PvDocument.pv_product_id == product.id,
        PvDocument.processing_status.in_(
            (ingest_mod.QUEUED, ingest_mod.FAILED)))).all()
    if not pending:
        return {"queued": 0, "documents": []}

    unmapped = [d.original_filename for d in pending
                if d.input_type == "line_listing" and not body.mappings.get(d.id)]
    if unmapped:
        raise error("PV_MAPPING_REQUIRED",
                    f"{', '.join(unmapped)} need a column mapping before they can be "
                    "read. Map the columns, or save a profile and reuse it.", 422)

    jobs = []
    for document in pending:
        document.processing_status = ingest_mod.QUEUED
        document.error_message = None
        jobs.append({"document_id": document.id,
                     "mapping": body.mappings.get(document.id)})
    log_audit(db, user, "Processed safety sources", "pv_product", product.id,
              product.project_id, "info", f"{len(jobs)} file(s)")
    db.commit()
    ingest_mod.ingest_in_background(jobs)
    return {"queued": len(jobs), "documents": [j["document_id"] for j in jobs]}


@router.post("/pv/documents/{pv_document_id}/retry", status_code=202)
def retry_source(pv_document_id: str, body: ProcessRequest,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    document = _owned_document(db, pv_document_id, user)
    roles.require_pv_role(db, document.pv_product_id, user, roles.WRITER,
                          action="Retrying a source")
    mapping = body.mappings.get(document.id) or body.mappings.get("mapping")
    if document.input_type == "line_listing" and not mapping:
        raise error("PV_MAPPING_REQUIRED",
                    f"{document.original_filename} needs a column mapping.", 422)
    document.processing_status = ingest_mod.QUEUED
    document.error_message = None
    db.commit()
    ingest_mod.ingest_in_background(
        [{"document_id": document.id, "mapping": mapping}])
    return {"queued": 1}


@router.get("/pv/products/{pv_product_id}/processing-status")
def processing_status(pv_product_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Per-file state, and whether anything is still moving.

    `deid_gate` is the part worth reading. A source that parsed successfully
    stops at `awaiting_deid`, and nothing downstream may use it until the
    de-identification pass has run -- which is M3. Reporting that as "done"
    would tell somebody their sources were ready when what is ready is half a
    pipeline.
    """
    from app.models import PvCase, PvDocument

    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvDocument).where(
        PvDocument.pv_product_id == product.id).order_by(PvDocument.created_at)).all()
    in_flight = [d for d in rows if d.processing_status in
                 (ingest_mod.QUEUED, ingest_mod.PARSING)]
    waiting = [d for d in rows if d.processing_status == ingest_mod.AWAITING_DEID]
    cases = db.scalar(select(func.count(PvCase.id)).where(
        PvCase.pv_product_id == product.id)) or 0
    unmasked = db.scalar(select(func.count(PvCase.id)).where(
        PvCase.pv_product_id == product.id,
        PvCase.deidentification_status == "pending")) or 0
    return {
        "items": [_document_out(d) for d in rows],
        "in_flight": bool(in_flight),
        "cases": cases,
        "deid_gate": {
            "documents_waiting": len(waiting),
            "cases_pending": unmasked,
            "cleared": len(waiting) == 0 and unmasked == 0,
            "note": (
                "Sources are parsed and their free text is held in the "
                "access-controlled store. De-identification, and everything "
                "downstream of it, is not built yet: nothing has been indexed, "
                "embedded, or sent to a model."),
        },
    }


# ------------------------------------------------------------ column mapping

@router.get("/pv/documents/{pv_document_id}/columns")
def read_columns(pv_document_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """The headers of a line listing, and a first guess at what each one is.

    A guess, and returned as one: `confidence` is on every suggestion and the
    screen asks for confirmation. A column mapped wrongly puts one field's
    values under another field's name, and everything downstream is then right
    about the wrong thing.
    """
    from app.docgen.extraction import extract
    from app.safety.line_listing import DATE_ORDER_KEY, FIELDS, suggest

    document = _owned_document(db, pv_document_id, user)
    if document.input_type != "line_listing":
        raise error("PV_NOT_A_LINE_LISTING",
                    "Column mapping applies to a line listing; this source is a "
                    f"{registry.INPUT_TYPES.get(document.input_type, document.input_type)}.",
                    422)
    try:
        extraction = extract(str(abs_path(document.blob_path)),
                            mime_type=document.mime_type,
                            source_name=document.original_filename)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is not a 500
        raise error("PV_SOURCE_UNREADABLE",
                    f"{document.original_filename} could not be read: {exc}", 422)
    tables = [t for t in extraction.tables if len(t.rows) >= 2]
    if not tables:
        raise error("PV_NO_TABLE",
                    f"{document.original_filename} holds no table with a header row "
                    "and at least one data row.", 422)
    table = max(tables, key=lambda t: len(t.rows))
    headers = table.rows[0]
    sample = table.rows[1:6]
    return {
        "headers": headers,
        "sample_rows": sample,
        "row_count": len(table.rows) - 1,
        "suggestions": [{"column": s.column, "field": s.field,
                         "confidence": s.confidence} for s in suggest(headers)],
        "fields": FIELDS,
        "date_order_key": DATE_ORDER_KEY,
    }


class MappingProfileIn(BaseModel):
    name: str
    source_system: str | None = None
    column_map: dict = {}
    #: Bind the profile to this product, or leave it available org-wide.
    scoped_to_product: bool = True


@router.get("/pv/products/{pv_product_id}/mapping-profiles")
def list_mapping_profiles(pv_product_id: str, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """This product's saved mappings, and the organisation's shared ones."""
    from app.models import PvMappingProfile

    product = _owned_product(db, pv_product_id, user)
    rows = db.scalars(select(PvMappingProfile).where(
        PvMappingProfile.org_id == user.org_id,
        or_(PvMappingProfile.pv_product_id == product.id,
            PvMappingProfile.pv_product_id.is_(None))
    ).order_by(PvMappingProfile.name)).all()
    return {"items": [{
        "id": p.id, "name": p.name, "source_system": p.source_system,
        "column_map": p.column_map or {},
        "shared": p.pv_product_id is None,
        "created_at": p.created_at,
    } for p in rows]}


@router.post("/pv/products/{pv_product_id}/mapping-profiles", status_code=201)
def create_mapping_profile(pv_product_id: str, body: MappingProfileIn,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Save a mapping so the next cycle is one click.

    Validated before it is stored, not when it is next used: a profile saved
    broken is a profile that fails a quarter later, on somebody else's shift.
    """
    from app.models import PvMappingProfile
    from app.safety.line_listing import validate_mapping

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Saving a column mapping")
    if not body.name.strip():
        raise error("PV_PROFILE_NEEDS_NAME", "A mapping profile needs a name.", 422)
    problems = validate_mapping(body.column_map)
    if problems:
        raise error("PV_BAD_MAPPING", "; ".join(problems), 422,
                    {"problems": problems})
    profile = PvMappingProfile(
        org_id=user.org_id,
        pv_product_id=product.id if body.scoped_to_product else None,
        name=body.name.strip(), source_system=body.source_system,
        column_map=body.column_map, created_by=user.id)
    db.add(profile)
    db.flush()
    log_audit(db, user, "Saved a column mapping profile", "pv_product", product.id,
              product.project_id, "info", profile.name)
    db.commit()
    db.refresh(profile)
    return {"id": profile.id, "name": profile.name,
            "source_system": profile.source_system,
            "column_map": profile.column_map, "shared": profile.pv_product_id is None}


# ------------------------------------------------------------------- cases

@router.get("/pv/products/{pv_product_id}/cases")
def list_cases(pv_product_id: str,
               report_instance_id: str | None = None,
               q: str | None = None,
               limit: int = Query(100, ge=1, le=1000),
               offset: int = Query(0, ge=0),
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """The case store, paged, with each case's position in time.

    `scope` is the badge, and it comes from `app.safety.scope.scope_of_case` --
    the same three questions the SQL asks, in the same order, so a row shown as
    counted and a row actually counted cannot come apart.
    """
    from app.models import PvCase, PvCaseEvent

    product = _owned_product(db, pv_product_id, user)
    statement = select(PvCase).where(PvCase.pv_product_id == product.id)
    if (q or "").strip():
        like = f"%{q.strip()}%"
        statement = statement.where(or_(
            PvCase.worldwide_case_id.ilike(like),
            PvCase.country_of_occurrence.ilike(like),
            PvCase.id.in_(select(PvCaseEvent.case_id).where(
                PvCaseEvent.pv_product_id == product.id,
                or_(PvCaseEvent.verbatim_term.ilike(like),
                    PvCaseEvent.meddra_pt.ilike(like))))))
    total = db.scalar(statement.with_only_columns(
        func.count(PvCase.id)).order_by(None)) or 0
    rows = db.scalars(statement.order_by(PvCase.created_at.desc())
                      .limit(limit).offset(offset)).all()

    scope = None
    if report_instance_id:
        report = _owned_report(db, report_instance_id, user)
        if report.pv_product_id != product.id:
            raise error("PV_REPORT_WRONG_PRODUCT",
                        "That report instance belongs to a different product.", 422)
        scope = scope_mod.scope_for(product, report)

    events = {}
    for row in db.scalars(select(PvCaseEvent).where(
            PvCaseEvent.case_id.in_([c.id for c in rows] or [""]))).all():
        events.setdefault(row.case_id, []).append(row)

    return {
        "items": [{
            "id": case.id, "worldwide_case_id": case.worldwide_case_id,
            "local_case_ids": case.local_case_ids or [],
            "case_version": case.case_version,
            "report_source": case.report_source,
            "country_of_occurrence": case.country_of_occurrence,
            "initial_receipt_date": case.initial_receipt_date,
            "latest_receipt_date": case.latest_receipt_date,
            "is_serious": case.is_serious,
            "seriousness_criteria": case.seriousness_criteria or [],
            "patient_age": case.patient_age, "patient_sex": case.patient_sex,
            "deidentification_status": case.deidentification_status,
            "confirmed_by": case.confirmed_by,
            "source_document_id": case.source_document_id,
            "imported_from": case.imported_from,
            "event_count": len(events.get(case.id, [])),
            "coding_required": sum(1 for e in events.get(case.id, [])
                                   if e.coding_required),
            "scope": scope_mod.scope_of_case(scope, case) if scope else None,
        } for case in rows],
        "total": total,
    }
