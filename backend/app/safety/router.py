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
from app.safety import deident
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
        "figures_at_signoff": report.figures_at_signoff,
        "created_at": report.created_at, "updated_at": report.updated_at,
    }
    if section_count is not None:
        out["section_count"] = section_count
    return out


def _withdraw_signoff(db, report: PvReportInstance, user: User, why: str) -> bool:
    """Undo a qualified person's sign-off because what they signed has changed.

    Called from every path that changes a signed report's text or its terms.
    A sign-off that survived an edit would be a signature on a document the
    signatory never read. The frozen figures go with it: they described the
    report that was signed.
    """
    if not report.qppv_signoff_by:
        return False
    signed_by = report.qppv_signoff_by
    report.qppv_signoff_by = None
    report.qppv_signoff_at = None
    report.figures_at_signoff = None
    report.status = "in_review"
    report.updated_at = now()
    log_audit(db, user, "Withdrew a qualified-person sign-off", "pv_report_instance",
              report.id, None, "warning", f"signed by {signed_by}; {why}")
    return True


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
    for row in db.scalars(select(PvDocument).where(
            PvDocument.pv_product_id == product.id)).all():
        if row.blob_path:
            blobs.append(abs_path(row.blob_path))
    # An export is several files -- the report, a tracked-changes copy, a PDF
    # -- and `blob_path` names only the first. CMC's purge read only that
    # column and left the rest on disk after the product was "deleted".
    for row in db.scalars(select(PvExport).where(
            PvExport.pv_product_id == product.id)).all():
        blobs.extend(_export_paths(row))

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

    One rule for every role: you may grant up to the role you hold yourself,
    or any role if you hold `MANAGE_USERS` in the organisation. So a reviewer
    can bring in a writer, a qualified person can name another, and nobody can
    hand out an authority they do not have.

    `MANAGE_USERS` is the bootstrap rather than a back door. Without it every
    new product would deadlock -- its creator is a writer, and nobody would yet
    hold the role needed to grant anything above that. Appointing the people who
    carry regulatory responsibility for a product IS an organisational act, and
    that capability already means "may change what this organisation is". It
    does not make its holder anything on the product: it lets them name
    somebody, in an entry recording who named whom.
    """
    from app.authz import MANAGE_USERS, has_capability

    product = _owned_product(db, pv_product_id, user)
    if body.pv_role not in registry.PV_ROLES:
        raise error("PV_BAD_ROLE",
                    f"pv_role must be one of {', '.join(registry.PV_ROLES)}.", 422)
    if not (has_capability(user, MANAGE_USERS)
            or roles.has_pv_role(db, product.id, user, body.pv_role)):
        raise error(
            "PV_ROLE_REQUIRED",
            f"Granting the {body.pv_role.replace('_', ' ')} role requires holding it "
            "on this product, or the ability to manage users in this organisation.",
            403,
            {"required_role": body.pv_role,
             "actual_role": roles.role_of(db, product.id, user),
             "or_capability": MANAGE_USERS})
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
    if changes:
        _withdraw_signoff(db, report, user,
                          "the report's " + ", ".join(sorted(changes)) + " changed")
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
    from app.models import PvExport

    export_files = []
    for record in db.scalars(select(PvExport).where(
            PvExport.report_instance_id == report.id)).all():
        export_files.extend(_export_paths(record))
        db.delete(record)
    db.flush()
    db.delete(report)
    log_audit(db, user, "Deleted a report instance", "pv_report_instance", report.id,
              None, "warning", f"{report.doc_type_key} {report.period_start}"
              f"..{report.period_end}, {len(sections)} sections")
    db.commit()
    for path in export_files:
        path.unlink(missing_ok=True)
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
    # Every state the worker moves through, not just the first two. Masking
    # and indexing are stages, and a screen told "nothing is in flight" while a
    # document is mid-masking stops polling and shows a half-finished pipeline
    # as a finished one.
    in_flight = [d for d in rows if d.processing_status in ingest_mod.IN_FLIGHT]
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


# ================================================== M3: de-identification queue

def _deid_out(item) -> dict:
    return {
        "id": item.id, "identifier_type": item.identifier_type,
        "detected_text": item.detected_text,
        "context_snippet": item.context_snippet,
        "proposed_mask": item.proposed_mask, "status": item.status,
        "case_id": item.case_id, "document_id": item.document_id,
        "resolved_by": item.resolved_by, "resolved_at": item.resolved_at,
        "created_at": item.created_at,
    }


@router.get("/pv/products/{pv_product_id}/deid-queue")
def deid_queue(pv_product_id: str, status: str = "pending",
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """What the de-identification pass could not settle on its own.

    A gate rather than a report: while anything here is pending, the sources it
    came from are not indexed, and nothing downstream may run. Each item says
    what was found and why it was uncertain, so the answer is a judgment about
    one string rather than about the whole file.
    """
    from app.models import PvDeidItem, PvDocument

    product = _owned_product(db, pv_product_id, user)
    statement = select(PvDeidItem).where(PvDeidItem.pv_product_id == product.id)
    if status != "all":
        statement = statement.where(PvDeidItem.status == status)
    items = db.scalars(statement.order_by(PvDeidItem.created_at)).all()
    waiting = db.scalar(select(func.count(PvDocument.id)).where(
        PvDocument.pv_product_id == product.id,
        PvDocument.processing_status == ingest_mod.AWAITING_DEID)) or 0
    pending = db.scalar(select(func.count(PvDeidItem.id)).where(
        PvDeidItem.pv_product_id == product.id,
        PvDeidItem.status == "pending")) or 0
    return {
        "items": [_deid_out(i) for i in items],
        "pending": pending,
        "documents_waiting": waiting,
        "identifier_types": list(deident.IDENTIFIER_TYPES),
        "cleared": pending == 0 and waiting == 0,
    }


class DeidResolution(BaseModel):
    #: mask | not_an_identifier
    action: str
    #: Which kind, when masking. Defaults to what the detector proposed.
    identifier_type: str | None = None


@router.post("/pv/deid-items/{deid_item_id}/resolve")
def resolve_deid_item(deid_item_id: str, body: DeidResolution,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Answer one detection, and re-run everything that was waiting on it.

    Every waiting source, not only the one the item came from: deciding that a
    string is a person's name decides it for the whole product, and the other
    sources naming them are blocked on the same answer.
    """
    from app.models import PvDeidItem

    item = db.get(PvDeidItem, deid_item_id)
    if not item or item.org_id != user.org_id:
        raise error("PV_DEID_ITEM_NOT_FOUND", "Detection not found", 404)
    if body.action not in ("mask", "not_an_identifier"):
        raise error("PV_BAD_DEID_ACTION",
                    "action must be 'mask' or 'not_an_identifier'.", 422)
    if body.action == "mask":
        chosen = body.identifier_type or item.proposed_mask or deident.OTHER
        if chosen not in deident.IDENTIFIER_TYPES:
            raise error("PV_BAD_IDENTIFIER_TYPE",
                        f"identifier_type must be one of "
                        f"{', '.join(deident.IDENTIFIER_TYPES)}.", 422)
        item.identifier_type = chosen
        item.status = "masked"
    else:
        item.status = "not_an_identifier"
    item.resolved_by = user.id
    item.resolved_at = now()
    log_audit(db, user, "Resolved a de-identification detection", "pv_product",
              item.pv_product_id, None,
              "info" if body.action == "mask" else "warning",
              f"{item.detected_text!r} -> {item.status}")
    db.commit()

    moved = ingest_mod.resume_after_review(db, item.pv_product_id)
    return {"resolved": item.status, "documents_indexed": len(moved)}


class DeidOverride(BaseModel):
    reason: str


@router.post("/pv/products/{pv_product_id}/deid-queue:override")
def override_deid_queue(pv_product_id: str, body: DeidOverride,
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Clear the gate without answering it, one time, with a reason.

    A qualified-person act and audited as a warning, because it is the one way
    text nobody has checked can reach an index. §7's S4 allows it and requires
    exactly this: the role, and an entry saying who did it and why.
    """
    from app.models import PvDeidItem

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.QUALIFIED_PERSON,
                          action="Overriding the de-identification gate")
    if not body.reason.strip():
        raise error("PV_OVERRIDE_NEEDS_REASON",
                    "An override of the de-identification gate has to say why.", 422)
    items = db.scalars(select(PvDeidItem).where(
        PvDeidItem.pv_product_id == product.id,
        PvDeidItem.status == "pending")).all()
    for item in items:
        item.status = "overridden"
        item.resolved_by = user.id
        item.resolved_at = now()
    log_audit(db, user, "Overrode the de-identification gate", "pv_product",
              product.id, product.project_id, "warning",
              f"{len(items)} detection(s) left unmasked: {body.reason.strip()}")
    db.commit()
    moved = ingest_mod.resume_after_review(db, product.id)
    return {"overridden": len(items), "documents_indexed": len(moved),
            "reason": body.reason.strip()}


@router.post("/pv/products/{pv_product_id}/leakage-scan")
def leakage_scan(pv_product_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """§11's first blocker: look for identifiers in text that should be clean.

    Runs over the masked narratives and every indexed chunk -- the things that
    reach a model and a document. Only confident detections count: a scan that
    fired on every capitalised pair would flag "Preferred Term" in every file
    and teach people to ignore it.
    """
    from app.models import PvCaseNarrative, PvChunk

    product = _owned_product(db, pv_product_id, user)
    findings = []
    for narrative in db.scalars(select(PvCaseNarrative).where(
            PvCaseNarrative.pv_product_id == product.id)).all():
        for hit in deident.scan(narrative.raw_text_redacted or ""):
            findings.append({"where": "narrative", "case_id": narrative.case_id,
                             "identifier_type": hit.identifier_type,
                             "text": hit.text, "basis": hit.basis})
    for chunk in db.scalars(select(PvChunk).where(
            PvChunk.pv_product_id == product.id)).all():
        for hit in deident.scan(chunk.content or ""):
            findings.append({"where": "chunk", "document_id": chunk.document_id,
                             "identifier_type": hit.identifier_type,
                             "text": hit.text, "basis": hit.basis})
    return {"findings": findings, "clean": not findings}


# ============================== M4: coding, expectedness, duplicates, the grid

def _event_out(event, *, case=None) -> dict:
    return {
        "id": event.id, "case_id": event.case_id,
        "verbatim_term": event.verbatim_term,
        "meddra_llt": event.meddra_llt, "meddra_pt": event.meddra_pt,
        "meddra_hlt": event.meddra_hlt, "meddra_hlgt": event.meddra_hlgt,
        "meddra_soc": event.meddra_soc, "meddra_version": event.meddra_version,
        "coding_required": event.coding_required,
        "is_serious": event.is_serious,
        "seriousness_criteria": event.seriousness_criteria or [],
        "expectedness": event.expectedness,
        "expectedness_rsi_version_id": event.expectedness_rsi_version_id,
        "causality_reporter": event.causality_reporter,
        "causality_company": event.causality_company,
        "onset_date": event.onset_date, "outcome": event.outcome,
        "is_aesi": event.is_aesi,
        "suggested": event.suggested_by_system_json or {},
        "confirmed_by": event.confirmed_by, "confirmed_at": event.confirmed_at,
        "worldwide_case_id": getattr(case, "worldwide_case_id", None),
    }


@router.post("/pv/products/{pv_product_id}/code", status_code=202)
def code_events(pv_product_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Code every uncoded event against the licensed MedDRA dictionary.

    A deployment with no licence codes nothing and says so per event. That is
    the honest outcome: a guessed preferred term puts an event under the wrong
    System Organ Class, inside a total a regulator compares against the last
    report, and nothing in the output looks wrong -- while an uncoded event
    sits in a queue with a number beside it.
    """
    from app.models import PvCaseEvent
    from app.safety import meddra

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Coding events")
    dictionary = meddra.dictionary_for(db, user.org_id, None)
    events = db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.pv_product_id == product.id,
        PvCaseEvent.meddra_pt.is_(None))).all()

    coded = 0
    reasons: dict = {}
    for event in events:
        result = meddra.code_term(event.verbatim_term, dictionary)
        if result.coded:
            event.meddra_llt = result.llt
            event.meddra_pt = result.pt
            event.meddra_hlt = result.hlt
            event.meddra_hlgt = result.hlgt
            event.meddra_soc = result.soc
            event.meddra_version = result.version
            event.coding_required = False
            coded += 1
        else:
            event.coding_required = True
            reasons[result.reason] = reasons.get(result.reason, 0) + 1
    log_audit(db, user, "Coded safety events", "pv_product", product.id,
              product.project_id, "info",
              f"{coded} of {len(events)} coded"
              + (f" (MedDRA {dictionary.version})" if dictionary.loaded else
                 "; no dictionary is licensed in this deployment"))
    db.commit()
    return {"coded": coded, "still_uncoded": len(events) - coded,
            "dictionary_loaded": dictionary.loaded,
            "meddra_version": dictionary.version,
            "reasons": [{"reason": r, "events": n} for r, n in reasons.items()]}


@router.post("/pv/reports/{report_instance_id}/suggest-expectedness", status_code=202)
def suggest_expectedness(report_instance_id: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Propose listedness for every event, against this report's pinned RSI.

    Written to `suggested_by_system_json` and never to the confirmed column.
    The suggestion carries its basis, because the person confirming is
    accountable for the determination and cannot be accountable for reasoning
    they cannot see.
    """
    from app.models import PvCaseEvent
    from app.safety import expectedness as exp

    report = _owned_report(db, report_instance_id, user)
    product = _owned_product(db, report.pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Computing expectedness suggestions")
    rsi = db.get(PvRsiVersion, report.rsi_version_id) if report.rsi_version_id else None
    terms = exp.listed_terms(db, rsi.id) if rsi is not None else {}

    events = db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.pv_product_id == product.id)).all()
    counts = {"listed": 0, "unlisted": 0, "needs_person": 0}
    for event in events:
        suggestion = exp.suggest(event, terms=terms, rsi_version=rsi)
        merged = dict(event.suggested_by_system_json or {})
        merged.update(suggestion.as_json())
        event.suggested_by_system_json = merged
        key = suggestion.value or "needs_person"
        counts[key] = counts.get(key, 0) + 1
    log_audit(db, user, "Computed expectedness suggestions", "pv_report_instance",
              report.id, None, "info",
              f"{len(events)} event(s) against "
              + (f"{rsi.rsi_type} {rsi.version_label}" if rsi else "no pinned RSI"))
    db.commit()
    return {"events": len(events), "counts": counts,
            "rsi_version_id": rsi.id if rsi else None,
            "note": ("These are suggestions. Nothing counts anywhere until a "
                     "qualified person confirms it.")}


class EventConfirmation(BaseModel):
    """What a qualified person is deciding. Anything omitted is left alone."""
    expectedness: str | None = None
    is_serious: bool | None = None
    seriousness_criteria: list | None = None
    causality_reporter: str | None = None
    causality_company: str | None = None
    is_aesi: bool | None = None
    meddra_pt: str | None = None
    meddra_soc: str | None = None
    meddra_version: str | None = None


@router.patch("/pv/case-events/{case_event_id}/confirm")
def confirm_event(case_event_id: str, body: EventConfirmation,
                  report_instance_id: str | None = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Record a determination. Qualified persons only.

    The RSI version is stamped from the report at the moment of confirming, so
    the determination says which version it was made against. §2's sixth
    principle: changing the pin later leaves this one attached to the version
    it was actually about.
    """
    from app.models import PvCaseEvent
    from app.safety.expectedness import LISTED, NOT_ASSESSED, UNLISTED

    event = db.get(PvCaseEvent, case_event_id)
    if not event or event.org_id != user.org_id:
        raise error("PV_EVENT_NOT_FOUND", "Event not found", 404)
    roles.require_pv_role(
        db, event.pv_product_id, user, roles.QUALIFIED_PERSON,
        action="Confirming seriousness, expectedness or causality")

    changes = body.model_dump(exclude_unset=True)
    if "expectedness" in changes and changes["expectedness"] not in (
            LISTED, UNLISTED, NOT_ASSESSED):
        raise error("PV_BAD_EXPECTEDNESS",
                    f"expectedness must be one of {LISTED}, {UNLISTED}, "
                    f"{NOT_ASSESSED}.", 422)

    if changes.get("expectedness") in (LISTED, UNLISTED):
        if report_instance_id:
            report = _owned_report(db, report_instance_id, user)
            if report.pv_product_id != event.pv_product_id:
                raise error("PV_REPORT_WRONG_PRODUCT",
                            "That report belongs to a different product.", 422)
            if not report.rsi_version_id:
                raise error("PV_NO_RSI_PINNED",
                            "This report pins no reference safety information, so "
                            "there is nothing for the event to be expected "
                            "against. Pin a version first.", 409)
            event.expectedness_rsi_version_id = report.rsi_version_id
        elif not event.expectedness_rsi_version_id:
            raise error("PV_RSI_CONTEXT_REQUIRED",
                        "An expectedness determination has to say which reference "
                        "safety information version it was made against. Confirm it "
                        "from a report instance.", 422)

    before = {k: getattr(event, k) for k in changes}
    for key, value in changes.items():
        setattr(event, key, value)
    event.confirmed_by = user.id
    event.confirmed_at = now()
    event.updated_at = now()
    moved = [f"{k}: {before[k]!r} -> {getattr(event, k)!r}" for k in changes
             if before[k] != getattr(event, k)]
    log_audit(db, user, "Confirmed a safety determination", "pv_case_event",
              event.id, None, "warning",
              "; ".join(moved) if moved else "re-confirmed unchanged")
    db.commit()
    db.refresh(event)
    return _event_out(event)


class BulkConfirmation(BaseModel):
    """Confirm every event sharing one preferred term.

    Scoped to a term rather than to a list of ids on purpose: expectedness is a
    property of a TERM against an RSI version, so confirming it for "Headache"
    across forty cases is one determination applied consistently, while
    confirming forty ids is forty chances to be inconsistent.
    """
    meddra_pt: str
    expectedness: str
    report_instance_id: str | None = None


@router.post("/pv/products/{pv_product_id}/case-events:bulk-confirm")
def bulk_confirm(pv_product_id: str, body: BulkConfirmation,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    from app.models import PvCaseEvent
    from app.safety.expectedness import LISTED, UNLISTED

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.QUALIFIED_PERSON,
                          action="Confirming expectedness in bulk")
    if body.expectedness not in (LISTED, UNLISTED):
        raise error("PV_BAD_EXPECTEDNESS",
                    f"expectedness must be {LISTED} or {UNLISTED}.", 422)
    rsi_version_id = None
    if body.report_instance_id:
        report = _owned_report(db, body.report_instance_id, user)
        if report.pv_product_id != product.id:
            raise error("PV_REPORT_WRONG_PRODUCT",
                        "That report belongs to a different product.", 422)
        rsi_version_id = report.rsi_version_id
    if not rsi_version_id:
        raise error("PV_RSI_CONTEXT_REQUIRED",
                    "An expectedness determination has to say which reference "
                    "safety information version it was made against.", 422)

    events = db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.pv_product_id == product.id,
        func.lower(PvCaseEvent.meddra_pt) == body.meddra_pt.strip().lower())).all()
    for event in events:
        event.expectedness = body.expectedness
        event.expectedness_rsi_version_id = rsi_version_id
        event.confirmed_by = user.id
        event.confirmed_at = now()
    log_audit(db, user, "Confirmed expectedness in bulk", "pv_product", product.id,
              product.project_id, "warning",
              f"{body.meddra_pt}: {len(events)} event(s) -> {body.expectedness}")
    db.commit()
    return {"confirmed": len(events), "meddra_pt": body.meddra_pt,
            "expectedness": body.expectedness}


@router.get("/pv/products/{pv_product_id}/case-events")
def list_case_events(pv_product_id: str,
                     report_instance_id: str | None = None,
                     only: str | None = None,
                     q: str | None = None,
                     limit: int = Query(200, ge=1, le=2000),
                     offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """The Events tab of the review grid.

    `only` narrows to the two queues that gate everything downstream:
    `unconfirmed` and `coding_required`. The header count comes from the same
    query the rows do, so "X of Y confirmed" cannot disagree with the list
    underneath it.
    """
    from app.models import PvCase, PvCaseEvent

    product = _owned_product(db, pv_product_id, user)
    statement = select(PvCaseEvent).where(PvCaseEvent.pv_product_id == product.id)
    if only == "unconfirmed":
        statement = statement.where(PvCaseEvent.confirmed_by.is_(None))
    elif only == "coding_required":
        statement = statement.where(PvCaseEvent.coding_required.is_(True))
    elif only:
        raise error("PV_UNKNOWN_FILTER",
                    "only must be 'unconfirmed' or 'coding_required'.", 422)
    if (q or "").strip():
        like = f"%{q.strip()}%"
        statement = statement.where(or_(PvCaseEvent.verbatim_term.ilike(like),
                                        PvCaseEvent.meddra_pt.ilike(like),
                                        PvCaseEvent.meddra_soc.ilike(like)))
    total = db.scalar(statement.with_only_columns(
        func.count(PvCaseEvent.id)).order_by(None)) or 0
    rows = db.scalars(statement.order_by(PvCaseEvent.created_at)
                      .limit(limit).offset(offset)).all()
    cases = {c.id: c for c in db.scalars(select(PvCase).where(
        PvCase.id.in_([r.case_id for r in rows] or [""]))).all()}

    confirmed = db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.pv_product_id == product.id,
        PvCaseEvent.confirmed_by.is_not(None))) or 0
    all_events = db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.pv_product_id == product.id)) or 0
    uncoded = db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.pv_product_id == product.id,
        PvCaseEvent.coding_required.is_(True))) or 0

    out = {
        "items": [_event_out(e, case=cases.get(e.case_id)) for e in rows],
        "total": total,
        "summary": {"events": all_events, "confirmed": confirmed,
                    "unconfirmed": all_events - confirmed,
                    "coding_required": uncoded,
                    "all_confirmed": all_events > 0 and confirmed == all_events},
        "my_role": roles.role_of(db, product.id, user),
    }
    if report_instance_id:
        from app.safety import expectedness as exp

        report = _owned_report(db, report_instance_id, user)
        stale = exp.stale_determinations(db, report)
        out["stale_expectedness"] = [
            {"event_id": e.id, "meddra_pt": e.meddra_pt,
             "confirmed_against": e.expectedness_rsi_version_id} for e in stale]
    return out


@router.post("/pv/products/{pv_product_id}/duplicates:detect", status_code=202)
def detect_duplicates(pv_product_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Look for cases that might be the same case. Never merges anything."""
    from app.safety import duplicates as dup

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Detecting duplicates")
    candidates = dup.find(db, pv_product_id=product.id, org_id=user.org_id)
    written = dup.record(db, pv_product_id=product.id, org_id=user.org_id,
                         candidates=candidates)
    log_audit(db, user, "Ran duplicate detection", "pv_product", product.id,
              product.project_id, "info",
              f"{len(candidates)} candidate pair(s), {written} new")
    db.commit()
    return {"candidates": len(candidates), "new": written,
            "note": "Candidates only. Nothing is merged without a person."}


@router.get("/pv/products/{pv_product_id}/duplicates")
def list_duplicates(pv_product_id: str, status: str = "pending",
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    from app.models import PvCase, PvDuplicateCandidate

    product = _owned_product(db, pv_product_id, user)
    statement = select(PvDuplicateCandidate).where(
        PvDuplicateCandidate.pv_product_id == product.id)
    if status != "all":
        statement = statement.where(PvDuplicateCandidate.status == status)
    rows = db.scalars(statement.order_by(
        PvDuplicateCandidate.score.desc())).all()
    ids = {r.case_id for r in rows} | {r.other_case_id for r in rows}
    cases = {c.id: c for c in db.scalars(select(PvCase).where(
        PvCase.id.in_(ids or {""}))).all()}

    def brief(case_id):
        case = cases.get(case_id)
        if case is None:
            return {"id": case_id}
        return {"id": case.id, "worldwide_case_id": case.worldwide_case_id,
                "country_of_occurrence": case.country_of_occurrence,
                "initial_receipt_date": case.initial_receipt_date,
                "patient_age": case.patient_age, "patient_sex": case.patient_sex,
                "is_serious": case.is_serious}

    return {"items": [{
        "id": r.id, "score": r.score, "matched_on": r.matched_on or [],
        "status": r.status, "case": brief(r.case_id),
        "other_case": brief(r.other_case_id),
        "resolved_by": r.resolved_by,
    } for r in rows]}


class DuplicateResolution(BaseModel):
    #: merged | kept_both | linked
    action: str
    #: When merging, which of the pair survives.
    keep_case_id: str | None = None


@router.post("/pv/duplicates/{duplicate_id}/resolve")
def resolve_duplicate(duplicate_id: str, body: DuplicateResolution,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Answer one candidate pair.

    Merging changes every figure the merged case contributed to and cannot be
    undone by re-importing, so it needs the qualified-person role and it says
    which case survives -- there is no "merge these" that leaves the system to
    choose.
    """
    from app.models import (
        PvCase, PvCaseDrug, PvCaseEvent, PvCaseNarrative, PvCaseOriginal,
        PvDuplicateCandidate,
    )

    candidate = db.get(PvDuplicateCandidate, duplicate_id)
    if not candidate or candidate.org_id != user.org_id:
        raise error("PV_DUPLICATE_NOT_FOUND", "Candidate not found", 404)
    if body.action not in ("merged", "kept_both", "linked"):
        raise error("PV_BAD_DUPLICATE_ACTION",
                    "action must be 'merged', 'kept_both' or 'linked'.", 422)

    if body.action == "merged":
        roles.require_pv_role(db, candidate.pv_product_id, user,
                              roles.QUALIFIED_PERSON,
                              action="Merging two cases")
        keep = body.keep_case_id
        if keep not in (candidate.case_id, candidate.other_case_id):
            raise error("PV_MERGE_NEEDS_SURVIVOR",
                        "keep_case_id has to be one of the two cases in the pair.",
                        422)
        drop = (candidate.other_case_id if keep == candidate.case_id
                else candidate.case_id)
        survivor = db.get(PvCase, keep)
        merged_case = db.get(PvCase, drop)
        if survivor is None or merged_case is None:
            raise error("PV_CASE_NOT_FOUND", "One of the cases is gone.", 404)
        # The merged case's identifiers move to the survivor, so the case can
        # still be found by the number the other source used for it.
        identifiers = list(survivor.local_case_ids or [])
        for value in [merged_case.worldwide_case_id,
                      *(merged_case.local_case_ids or [])]:
            if value and value not in identifiers:
                identifiers.append(value)
        survivor.local_case_ids = identifiers
        for model in (PvCaseEvent, PvCaseDrug, PvCaseNarrative, PvCaseOriginal):
            for row in db.scalars(select(model).where(
                    model.case_id == merged_case.id)).all():
                db.delete(row)
        db.flush()
        merged_label = merged_case.worldwide_case_id
        db.delete(merged_case)
        log_audit(db, user, "Merged two safety cases", "pv_case", keep, None,
                  "warning",
                  f"{merged_label} merged into {survivor.worldwide_case_id}; "
                  f"score {candidate.score}")
    else:
        log_audit(db, user, "Resolved a duplicate candidate", "pv_product",
                  candidate.pv_product_id, None, "info",
                  f"{body.action}, score {candidate.score}")

    candidate.status = body.action
    candidate.resolved_by = user.id
    candidate.resolved_at = now()
    db.commit()
    return {"resolved": candidate.status}


# ================================== M5: tabulations, exposure and the registers

def _tabulation_context(db, report_instance_id: str, user: User):
    report = _owned_report(db, report_instance_id, user)
    product = _owned_product(db, report.pv_product_id, user)
    return report, product


@router.get("/pv/reports/{report_instance_id}/tabulations")
def list_tabulations(report_instance_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Which computed tables this report's sections call for, and whether each
    one can be built yet.

    Asked of the builders rather than guessed: "no data yet" and "rendered with
    holes" are different states, and the screen needs to say which.
    """
    from app.safety import tabulations as tab

    report, product = _tabulation_context(db, report_instance_id, user)
    wanted = sorted(set(trees.TABLE_KEYS.get(report.doc_type_key, {}).values()))
    items = []
    for key in wanted:
        entry = {"key": key, "available": True, "reason": None,
                 "rows": 0, "missing": 0}
        try:
            built = tab.render(db, report=report, product=product, table_key=key)
            entry.update(rows=len(built.rows), missing=len(built.missing),
                         title=built.title)
        except tab.TableUnavailable as exc:
            entry.update(available=False, reason=str(exc))
        items.append(entry)
    return {"items": items}


@router.get("/pv/reports/{report_instance_id}/tabulations/{table_key}")
def get_tabulation(report_instance_id: str, table_key: str,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """One computed table, with the provenance of every cell and the scope the
    figures describe."""
    from app.safety import tabulations as tab

    report, product = _tabulation_context(db, report_instance_id, user)
    try:
        built = tab.render(db, report=report, product=product, table_key=table_key)
    except tab.UnknownTable as exc:
        raise error("PV_UNKNOWN_TABLE", str(exc), 404)
    except tab.TableUnavailable as exc:
        raise error("PV_TABLE_UNAVAILABLE", str(exc), 409)
    return built.as_dict()


@router.get("/pv/reports/{report_instance_id}/tabulations/{table_key}/drilldown")
def drilldown(report_instance_id: str, table_key: str, cell: str,
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """The cases behind one cell.

    Read from the provenance the builder recorded while counting, not by
    re-running a filter: a second query that reconstructed "serious unlisted
    headaches in the interval" is a second code path, and the day it disagrees
    with the first the drill-down shows cases the number does not include.
    """
    from app.models import PvCase, PvCaseEvent
    from app.safety import tabulations as tab

    report, product = _tabulation_context(db, report_instance_id, user)
    try:
        built = tab.render(db, report=report, product=product, table_key=table_key)
    except tab.UnknownTable as exc:
        raise error("PV_UNKNOWN_TABLE", str(exc), 404)
    except tab.TableUnavailable as exc:
        raise error("PV_TABLE_UNAVAILABLE", str(exc), 409)
    contributors = tab.cases_for_cell(built, cell)
    cases = db.scalars(select(PvCase).where(
        PvCase.id.in_(contributors["cases"] or [""]))).all()
    events = db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.id.in_(contributors["events"] or [""]))).all()
    return {
        "cell": cell, "count": len(contributors["events"]),
        "cases": [{"id": c.id, "worldwide_case_id": c.worldwide_case_id,
                   "country_of_occurrence": c.country_of_occurrence,
                   "initial_receipt_date": c.initial_receipt_date,
                   "is_serious": c.is_serious} for c in cases],
        "events": [_event_out(e) for e in events],
    }


class ExposureIn(BaseModel):
    context: str
    measure: str
    value_text: str
    region: str | None = None
    population_descriptor: str | None = None
    calculation_method_note: str | None = None
    source_document_id: str | None = None


class ExposurePatch(BaseModel):
    context: str | None = None
    measure: str | None = None
    value_text: str | None = None
    region: str | None = None
    population_descriptor: str | None = None
    calculation_method_note: str | None = None


EXPOSURE_CONTEXTS = ("clinical_trial", "marketing")
EXPOSURE_MEASURES = ("subjects", "patient_years", "treatment_days", "units_sold",
                     "prescriptions")


def _exposure_numeric(value_text: str) -> float | None:
    """The figure as a number, for arithmetic only -- never rendered.

    A comma is a thousands separator only when it groups exactly three digits;
    anything else is refused rather than guessed, for the reason the CMC module
    refuses a bare "1,5": a guess that is wrong by a factor of a thousand is a
    reporting rate wrong by the same factor.
    """
    import re as _re

    text = (value_text or "").strip().replace(" ", "")
    if _re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", text):
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _exposure_out(e) -> dict:
    return {"id": e.id, "report_instance_id": e.report_instance_id,
            "context": e.context, "region": e.region,
            "population_descriptor": e.population_descriptor,
            "measure": e.measure, "value_text": e.value_text,
            "value_numeric": e.value_numeric,
            "calculation_method_note": e.calculation_method_note,
            "source_document_id": e.source_document_id,
            "confirmed_by": e.confirmed_by, "confirmed_at": e.confirmed_at}


def _validate_exposure(body) -> None:
    if body.get("context") is not None and body["context"] not in EXPOSURE_CONTEXTS:
        raise error("PV_BAD_EXPOSURE_CONTEXT",
                    f"context must be one of {', '.join(EXPOSURE_CONTEXTS)}.", 422)
    if body.get("measure") is not None and body["measure"] not in EXPOSURE_MEASURES:
        raise error("PV_BAD_EXPOSURE_MEASURE",
                    f"measure must be one of {', '.join(EXPOSURE_MEASURES)}.", 422)


@router.get("/pv/reports/{report_instance_id}/exposure")
def list_exposure(report_instance_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    from app.models import PvExposure

    report = _owned_report(db, report_instance_id, user)
    rows = db.scalars(select(PvExposure).where(
        PvExposure.report_instance_id == report.id)).all()
    return {"items": [_exposure_out(e) for e in rows],
            "contexts": list(EXPOSURE_CONTEXTS), "measures": list(EXPOSURE_MEASURES)}


@router.post("/pv/reports/{report_instance_id}/exposure", status_code=201)
def add_exposure(report_instance_id: str, body: ExposureIn,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """One exposure figure, kept as stated.

    `value_text` is what the document prints; the number beside it exists for
    rates only. "1,240,000" and "1.24 million" are different claims about
    precision, and the report should make the one its source made.
    """
    from app.models import PvExposure

    report = _owned_report(db, report_instance_id, user)
    roles.require_pv_role(db, report.pv_product_id, user, roles.WRITER,
                          action="Entering exposure")
    fields = body.model_dump()
    _validate_exposure(fields)
    if not body.value_text.strip():
        raise error("PV_EXPOSURE_NEEDS_VALUE", "An exposure figure needs a value.", 422)
    row = PvExposure(org_id=user.org_id, pv_product_id=report.pv_product_id,
                     report_instance_id=report.id,
                     value_numeric=_exposure_numeric(body.value_text), **fields)
    db.add(row)
    db.flush()
    log_audit(db, user, "Entered exposure", "pv_report_instance", report.id, None,
              "info", f"{body.context} {body.measure}: {body.value_text}")
    db.commit()
    db.refresh(row)
    return _exposure_out(row)


@router.patch("/pv/exposure/{exposure_id}")
def update_exposure(exposure_id: str, body: ExposurePatch,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Change a figure. A changed figure is an unconfirmed figure: whatever the
    reviewer confirmed was the old number, not this one."""
    from app.models import PvExposure

    row = db.get(PvExposure, exposure_id)
    if not row or row.org_id != user.org_id:
        raise error("PV_EXPOSURE_NOT_FOUND", "Exposure not found", 404)
    roles.require_pv_role(db, row.pv_product_id, user, roles.WRITER,
                          action="Changing exposure")
    changes = body.model_dump(exclude_unset=True)
    _validate_exposure(changes)
    before = row.value_text
    for key, value in changes.items():
        setattr(row, key, value)
    if "value_text" in changes:
        row.value_numeric = _exposure_numeric(row.value_text)
    row.confirmed_by = None
    row.confirmed_at = None
    log_audit(db, user, "Changed exposure", "pv_report_instance",
              row.report_instance_id, None, "warning",
              f"{row.measure}: {before!r} -> {row.value_text!r}; confirmation withdrawn")
    db.commit()
    db.refresh(row)
    return _exposure_out(row)


@router.post("/pv/exposure/{exposure_id}/confirm")
def confirm_exposure(exposure_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """A second person's check on a denominator. Exposure is what every rate in
    the report is divided by, so it is confirmed by a reviewer, not the writer
    alone."""
    from app.models import PvExposure

    row = db.get(PvExposure, exposure_id)
    if not row or row.org_id != user.org_id:
        raise error("PV_EXPOSURE_NOT_FOUND", "Exposure not found", 404)
    roles.require_pv_role(db, row.pv_product_id, user, roles.REVIEWER,
                          action="Confirming exposure")
    if not (row.calculation_method_note or "").strip():
        raise error("PV_EXPOSURE_NEEDS_METHOD",
                    "An exposure figure is confirmed with its method. Two reports "
                    "quoting different numbers for one interval are usually two "
                    "methods, and the note is how a reader tells.", 409)
    row.confirmed_by = user.id
    row.confirmed_at = now()
    log_audit(db, user, "Confirmed exposure", "pv_report_instance",
              row.report_instance_id, None, "info",
              f"{row.measure}: {row.value_text}")
    db.commit()
    db.refresh(row)
    return _exposure_out(row)


@router.delete("/pv/exposure/{exposure_id}")
def delete_exposure(exposure_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    from app.models import PvExposure

    row = db.get(PvExposure, exposure_id)
    if not row or row.org_id != user.org_id:
        raise error("PV_EXPOSURE_NOT_FOUND", "Exposure not found", 404)
    roles.require_pv_role(db, row.pv_product_id, user, roles.WRITER,
                          action="Deleting exposure")
    db.delete(row)
    log_audit(db, user, "Deleted exposure", "pv_report_instance",
              row.report_instance_id, None, "warning",
              f"{row.measure}: {row.value_text}")
    db.commit()
    return {"deleted": True}


# ------------------------------------------- the registers the tables read from
#
# Safety concerns, actions, studies and literature are small registers, each
# the source of one computed table. One generic pair of endpoints per register
# rather than four near-identical handlers: the only things that differ are the
# model, the fields and the vocabulary checks, and those are data.

_REGISTERS = {
    "safety-concerns": {
        "model": "PvSafetyConcern",
        "fields": ("concern_type", "title", "meddra_terms", "status",
                   "rmp_part_reference", "first_added_report_id"),
        "required": ("concern_type", "title"),
        "choices": {"concern_type": ("important_identified_risk",
                                     "important_potential_risk",
                                     "missing_information"),
                    "status": ("current", "removed")},
        "order": "title",
    },
    "safety-actions": {
        "model": "PvSafetyAction",
        "fields": ("action_type", "description", "region", "action_date", "reason",
                   "source_document_id"),
        "required": ("action_type",),
        "choices": {"action_type": ("label_change", "dhpc", "suspension",
                                    "withdrawal", "restriction", "protocol_amendment",
                                    "clinical_hold", "other")},
        "order": "action_date",
    },
    "studies": {
        "model": "PvStudy",
        "fields": ("study_id", "title", "phase", "status", "population",
                   "planned_enrolment", "actual_enrolment", "start_date",
                   "completion_date", "source_document_id"),
        "required": ("study_id",),
        "choices": {},
        "order": "study_id",
    },
    "literature": {
        "model": "PvLiteratureRef",
        "fields": ("citation", "database", "search_date", "search_strategy_ref",
                   "relevance", "linked_case_ids", "source_document_id"),
        "required": ("citation",),
        "choices": {},
        "order": "search_date",
    },
}

_DATE_FIELDS = {"action_date", "start_date", "completion_date", "search_date"}


def _register(name: str) -> dict:
    spec = _REGISTERS.get(name)
    if spec is None:
        raise error("PV_UNKNOWN_REGISTER",
                    f"{name!r} is not a register; one of {', '.join(_REGISTERS)}.",
                    404)
    return spec


def _register_row_out(row, spec) -> dict:
    out = {"id": row.id}
    for name in spec["fields"]:
        out[name] = getattr(row, name)
    return out


@router.get("/pv/products/{pv_product_id}/registers/{register}")
def list_register(pv_product_id: str, register: str,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    import app.models as models

    product = _owned_product(db, pv_product_id, user)
    spec = _register(register)
    model = getattr(models, spec["model"])
    rows = db.scalars(select(model).where(
        model.pv_product_id == product.id
    ).order_by(getattr(model, spec["order"]))).all()
    return {"items": [_register_row_out(r, spec) for r in rows],
            "fields": list(spec["fields"]),
            "choices": {k: list(v) for k, v in spec["choices"].items()}}


@router.post("/pv/products/{pv_product_id}/registers/{register}", status_code=201)
def add_to_register(pv_product_id: str, register: str, body: dict,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    import app.models as models

    product = _owned_product(db, pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action=f"Adding to the {register.replace('-', ' ')} register")
    spec = _register(register)
    values = {k: v for k, v in (body or {}).items() if k in spec["fields"]}
    for name in spec["required"]:
        if not str(values.get(name) or "").strip():
            raise error("PV_REGISTER_FIELD_REQUIRED", f"{name} is required.", 422)
    for name, allowed in spec["choices"].items():
        if values.get(name) is not None and values[name] not in allowed:
            raise error("PV_REGISTER_BAD_CHOICE",
                        f"{name} must be one of {', '.join(allowed)}.", 422)
    for name in _DATE_FIELDS & set(values):
        if values[name]:
            try:
                values[name] = date.fromisoformat(str(values[name]))
            except ValueError:
                raise error("PV_REGISTER_BAD_DATE",
                            f"{name} must be an ISO date (YYYY-MM-DD).", 422)
        else:
            values[name] = None
    model = getattr(models, spec["model"])
    row = model(org_id=user.org_id, pv_product_id=product.id, **values)
    db.add(row)
    db.flush()
    log_audit(db, user, f"Added to the {register.replace('-', ' ')} register",
              "pv_product", product.id, product.project_id, "info",
              str(values.get(spec["required"][0]))[:120])
    db.commit()
    db.refresh(row)
    return _register_row_out(row, spec)


# =========================================== M6: drafting and the workspace

def _owned_section(db: Session, section_id: str, user: User) -> PvSection:
    section = db.get(PvSection, section_id)
    if not section or section.org_id != user.org_id:
        raise error("PV_SECTION_NOT_FOUND", "Section not found", 404)
    return section


def _latest_draft(db: Session, section_id: str):
    return db.scalar(select(PvSectionDraft).where(
        PvSectionDraft.pv_section_id == section_id
    ).order_by(PvSectionDraft.version.desc()))


def _draft_out(draft) -> dict:
    from app.docgen.markers import parse_assessments, parse_data_needed, table_markers

    if draft is None:
        return None
    return {"id": draft.id, "version": draft.version, "content": draft.content,
            "origin": draft.origin, "model": draft.model,
            "prompt_version": draft.prompt_version,
            "created_by": draft.created_by, "created_at": draft.created_at,
            "data_needed": parse_data_needed(draft.content),
            "assessments_required": parse_assessments(draft.content),
            "table_markers": table_markers(draft.content),
            "source_map": (draft.generation_params or {}).get("source_map", [])}


def deid_gate(db, pv_product_id: str) -> dict:
    """Whether anything is still waiting to be masked.

    §7's S4: "no generation until the queue is clear". Checked in one place so
    generation and QC ask the same question.
    """
    from app.models import PvDeidItem, PvDocument

    pending = db.scalar(select(func.count(PvDeidItem.id)).where(
        PvDeidItem.pv_product_id == pv_product_id,
        PvDeidItem.status == "pending")) or 0
    waiting = db.scalar(select(func.count(PvDocument.id)).where(
        PvDocument.pv_product_id == pv_product_id,
        PvDocument.processing_status == ingest_mod.AWAITING_DEID)) or 0
    return {"pending": pending, "documents_waiting": waiting,
            "open": bool(pending or waiting)}


def unconfirmed_in_scope(db, product, report) -> int:
    """Events in this report's windows that no qualified person has confirmed.

    Interval and cumulative together, since a data section can print either.
    The same scope predicates the tables use, so "unconfirmed data feeds this
    section" and "this event is in that table" cannot disagree.
    """
    from sqlalchemy import or_ as _or

    from app.models import PvCase, PvCaseEvent

    scope = scope_mod.scope_for(product, report)
    windows = [scope_mod.interval(scope)]
    if scope.has_cumulative:
        windows.append(scope_mod.cumulative(scope))
    return db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.confirmed_by.is_(None),
        PvCaseEvent.case_id.in_(select(PvCase.id).where(_or(*windows))))) or 0


def _confirmed_data(db, product, report, section) -> dict:
    """What the model may quote: figures already computed, already split into
    interval and cumulative, with the scope they describe.

    Totals rather than rows. The table itself is inserted from the store at
    export, and a model shown every row is a model tempted to re-typeset it --
    the one thing rule 2 forbids. It gets the numbers a sentence would quote,
    labelled so rule 6 (never merge interval and cumulative) has something to
    hold on to.
    """
    from app.safety import tabulations as tab

    scope = scope_mod.scope_for(product, report)
    counts = scope_mod.preview(db, scope)
    data = {"scope": {"period_start": report.period_start,
                      "period_end": report.period_end,
                      "data_lock_point": report.data_lock_point,
                      "cumulative_from": scope.cumulative_from,
                      "cumulative_anchor": scope.anchor},
            "case_counts": {"interval_cases": counts["interval_cases"],
                            "cumulative_cases": counts["cumulative_cases"]}}
    if section.table_key:
        data["table_in_this_section"] = section.table_key
        try:
            built = tab.render(db, report=report, product=product,
                               table_key=section.table_key)
            data["table_totals"] = built.totals
            data["table_gaps"] = built.missing
        except tab.TableUnavailable as exc:
            data["table_totals"] = {}
            data["table_gaps"] = [str(exc)]
    return data


def _rsi_context(db, report) -> dict:
    if not report.rsi_version_id:
        return {}
    version = db.get(PvRsiVersion, report.rsi_version_id)
    if version is None:
        return {}
    return {"label": (version.rsi_type or "").upper(), "version": version.version_label,
            "effective_date": version.effective_date}


def _baseline_text(db, section) -> str | None:
    if not section.baseline_section_id:
        return None
    draft = _latest_draft(db, section.baseline_section_id)
    return draft.content if draft else None


#: §5: a previous report is retrievable, but "tagged 'prior report -- verify
#: currency before reuse'". The label goes on the extract itself, where the
#: model reads it, rather than in a legend further up the prompt.
PRIOR_REPORT_TYPE = "previous_report"
PRIOR_REPORT_LABEL = ("PRIOR REPORT -- verify currency before reuse; never carry an "
                      "interval figure forward")


def _retrieve(db, product, report, section, k: int = 16):
    """Masked chunks for this section, ranked.

    Only `pv_chunks` -- built from masked text and nothing else -- and only from
    documents that are the product's or this report's. A document uploaded for
    a different interval's report does not become evidence for this one.
    """
    from app.docgen.ranking import build_query, format_extracts, score_chunks
    from app.models import PvChunk

    statement = select(PvChunk).where(
        PvChunk.org_id == product.org_id, PvChunk.pv_product_id == product.id,
        or_(PvChunk.report_instance_id.is_(None),
            PvChunk.report_instance_id == report.id))
    if section.source_types:
        statement = statement.where(PvChunk.doc_type.in_(list(section.source_types)))
    candidates = list(db.scalars(statement.order_by(PvChunk.id)))
    query = build_query(section_number=section.section_code,
                        section_title=section.title,
                        guidance_text=section.guidance_text,
                        study_metadata={"product": product.product_name,
                                        "inn": product.inn or ""})
    scores = score_chunks(query, candidates)
    ranked = sorted(candidates, key=lambda c: (-scores.get(c.id, 0.0), c.id))
    chunks = [c for c in ranked if scores.get(c.id, 0.0) > 0][:k]
    extracts, source_map = format_extracts(
        chunks, style_reference_type=PRIOR_REPORT_TYPE,
        table_label=ingest_mod.TABLE_LABEL, reference_label=PRIOR_REPORT_LABEL)
    return chunks, extracts, source_map


class PvGenerateRequest(BaseModel):
    instruction: str | None = None


@router.post("/pv/sections/{section_id}/generate", status_code=201)
def generate_pv_section(section_id: str, body: PvGenerateRequest,
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Draft one section's prose, from masked evidence and computed figures.

    Three gates run before the model is asked anything:

    * the de-identification queue must be clear -- §7: no generation until it is;
    * a data section needs every event in the report's windows confirmed -- S5's
      "hard gate on generating data-dependent sections", since a paragraph
      written around unconfirmed figures is a paragraph about numbers that are
      about to change;
    * the text about to be sent is scanned for identifiers, and one confident
      hit refuses the call (`drafting.IdentifierInPrompt`).
    """
    from app.models import PvCitation
    from app.safety import drafting
    from app.tenancy import llm_policy_for

    section = _owned_section(db, section_id, user)
    report = _owned_report(db, section.report_instance_id, user)
    product = _owned_product(db, report.pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Generating a section")
    if section.is_container:
        raise error("PV_SECTION_IS_CONTAINER",
                    "A container heading has no prose of its own; generate its "
                    "subsections.", 422)
    if not section.enabled:
        raise error("PV_SECTION_DISABLED", "This section is excluded from the report.",
                    409)
    gate = deid_gate(db, product.id)
    if gate["open"]:
        raise error("PV_DEID_GATE_OPEN",
                    f"{gate['pending']} detection(s) and {gate['documents_waiting']} "
                    "source(s) are waiting for de-identification. Nothing is drafted "
                    "until the queue is clear.", 409, gate)
    if section.table_key:
        unconfirmed = unconfirmed_in_scope(db, product, report)
        if unconfirmed:
            raise error("PV_UNCONFIRMED_DATA",
                        f"{unconfirmed} event(s) in this report's windows are not "
                        "confirmed, and this section is written around their "
                        "figures. Confirm them in Case review first.", 409,
                        {"unconfirmed_events": unconfirmed})

    entry = registry.DELIVERABLES.get(report.doc_type_key) or {}
    chunks, extracts, source_map = _retrieve(db, product, report, section)
    previous_status = section.status
    section.status = "generating"
    db.commit()
    try:
        result = drafting.draft_section(
            section_code=section.section_code, section_title=section.title,
            deliverable_name=entry.get("name") or report.doc_type_key,
            structure_basis=entry.get("structure_basis") or "",
            target_regions=report.regions or [],
            product={"product_name": product.product_name, "inn": product.inn,
                     "mah_name": product.mah_name, "ibd": product.ibd,
                     "dibd": product.dibd},
            report={"period_start": report.period_start,
                    "period_end": report.period_end,
                    "data_lock_point": report.data_lock_point,
                    "meddra_version": report.meddra_version,
                    "sequence_number": report.sequence_number},
            rsi=_rsi_context(db, report),
            confirmed_data=_confirmed_data(db, product, report, section),
            baseline_text=_baseline_text(db, section),
            guidance=section.guidance_text,
            chunks=chunks, source_map=source_map, extracts=extracts,
            wanted_doc_types=section.source_types, instruction=body.instruction,
            llm_policy=llm_policy_for(db, user.org_id, project_id=product.project_id,
                                      user_id=user.id, subject_type="pv_section",
                                      subject_id=section.id))
    except drafting.IdentifierInPrompt as exc:
        section.status = previous_status if previous_status != "generating" else "not_started"
        db.commit()
        raise error("PV_IDENTIFIER_IN_PROMPT", str(exc), 422)
    except Exception:
        section.status = "draft" if _latest_draft(db, section.id) else "not_started"
        db.commit()
        raise

    previous = _latest_draft(db, section.id)
    draft = PvSectionDraft(
        org_id=user.org_id, pv_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=result.content, origin="model", model=result.model,
        prompt_version=result.prompt_version, created_by=user.id,
        generation_params={"instruction": body.instruction,
                           "chunk_ids": [c.id for c in chunks],
                           "source_map": source_map})
    db.add(draft)
    db.flush()
    for citation in result.citations:
        db.add(PvCitation(org_id=user.org_id, draft_id=draft.id,
                          marker=citation.get("marker", ""),
                          source_index=citation.get("source_index", 0),
                          chunk_id=citation.get("chunk_id"),
                          document_id=citation.get("document_id"),
                          page=citation.get("page"),
                          quoted_number=citation.get("quoted_number")))
    section.status = "draft"
    _withdraw_signoff(db, report, user, f"{section.section_code} was regenerated")
    if section.delta_status in ("changed", "fresh", "carried_forward"):
        # Regenerated against this interval's data: whatever the badge said
        # about the baseline no longer describes this text.
        section.delta_status = "new_data" if section.baseline_section_id else "fresh"
    section.updated_at = now()
    log_audit(db, user, "Generated a safety report section", "pv_section", section.id,
              product.project_id, "info",
              f"{section.section_code} v{draft.version} ({len(chunks)} sources)")
    db.commit()
    db.refresh(draft)
    return {"draft": _draft_out(draft), "section": _section_out(section)}


@router.get("/pv/sections/{section_id}/draft")
def get_pv_draft(section_id: str, version: int | None = None,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    section = _owned_section(db, section_id, user)
    statement = select(PvSectionDraft).where(PvSectionDraft.pv_section_id == section.id)
    if version is not None:
        draft = db.scalar(statement.where(PvSectionDraft.version == version))
    else:
        draft = db.scalar(statement.order_by(PvSectionDraft.version.desc()))
    versions = [v for (v,) in db.execute(select(PvSectionDraft.version).where(
        PvSectionDraft.pv_section_id == section.id
    ).order_by(PvSectionDraft.version)).all()]
    return {"section": _section_out(section), "draft": _draft_out(draft),
            "versions": versions}


class PvDraftEdit(BaseModel):
    content: str


@router.put("/pv/sections/{section_id}/draft", status_code=201)
def edit_pv_draft(section_id: str, body: PvDraftEdit, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Save a person's version.

    Two rules beyond "it becomes the newest version":

    * Any new version withdraws an approval. Export assembles the newest draft,
      so an approved section edited afterwards would ship text nobody approved
      under a status saying otherwise -- the defect the CMC module had.
    * Removing an `[ASSESSMENT REQUIRED: ...]` is answering it, and §11's eighth
      blocker says only a qualified person may. So an edit that leaves fewer of
      them than the version before needs that role. Deleting the marker is not
      a way round the person who has to make the judgment.

    The draft is scanned for identifiers on every save, per §11's first
    blocker, and the findings come back with it. Saving is not refused -- saving
    is how somebody removes the name -- but approval is, while any remain.
    """
    from app.docgen.markers import parse_assessments

    section = _owned_section(db, section_id, user)
    report = _owned_report(db, section.report_instance_id, user)
    roles.require_pv_role(db, report.pv_product_id, user, roles.WRITER,
                          action="Editing a section")
    if not body.content.strip():
        raise error("PV_DRAFT_EMPTY", "A draft needs content.", 422)
    previous = _latest_draft(db, section.id)
    open_before = len(parse_assessments(previous.content)) if previous else 0
    open_after = len(parse_assessments(body.content))
    if open_after < open_before:
        roles.require_pv_role(
            db, report.pv_product_id, user, roles.QUALIFIED_PERSON,
            action="Answering an [ASSESSMENT REQUIRED] judgment")

    draft = PvSectionDraft(
        org_id=user.org_id, pv_section_id=section.id,
        version=(previous.version + 1) if previous else 1,
        content=body.content, origin="edited", created_by=user.id,
        generation_params={
            "edited_from_version": previous.version if previous else None,
            "source_map": (previous.generation_params or {}).get("source_map", [])
            if previous else []})
    db.add(draft)
    was = section.status
    section.status = "draft"
    section.updated_at = now()
    _withdraw_signoff(db, report, user, f"{section.section_code} was edited")
    leaks = deident.scan(body.content)
    log_audit(db, user, "Edited a safety report section", "pv_section", section.id,
              None, "warning" if was == "approved" or open_after < open_before
              else "info",
              f"{section.section_code} v{draft.version}"
              + (f" (was {was.replace('_', ' ')}; approval withdrawn)"
                 if was in ("in_review", "approved") else "")
              + (f"; {open_before - open_after} assessment(s) answered"
                 if open_after < open_before else ""))
    db.commit()
    db.refresh(draft)
    return {"draft": _draft_out(draft), "section": _section_out(section),
            "leakage": [{"identifier_type": h.identifier_type, "text": h.text,
                         "basis": h.basis} for h in leaks]}


class PvStatusPatch(BaseModel):
    status: str


@router.patch("/pv/sections/{section_id}/status")
def set_pv_section_status(section_id: str, body: PvStatusPatch,
                          db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Move a section through draft -> in review -> approved.

    Approval is a reviewer's act and it checks the text it is approving:
    an open [ASSESSMENT REQUIRED], a data section whose table marker has gone,
    unconfirmed data behind a data section, or an identifier in the text each
    refuse it. Those are all things a reviewer reading the prose could miss,
    and all of them would otherwise ship.
    """
    from app.docgen.markers import parse_assessments, table_markers

    section = _owned_section(db, section_id, user)
    report = _owned_report(db, section.report_instance_id, user)
    product = _owned_product(db, report.pv_product_id, user)
    if body.status not in SECTION_STATUSES:
        raise error("PV_BAD_STATUS",
                    f"status must be one of {', '.join(SECTION_STATUSES)}.", 422)
    draft = _latest_draft(db, section.id)
    if draft is None:
        raise error("PV_NOTHING_TO_REVIEW", "This section has no draft yet.", 409)

    if body.status == "approved":
        roles.require_pv_role(db, product.id, user, roles.REVIEWER,
                              action="Approving a section")
        problems = []
        open_items = parse_assessments(draft.content)
        if open_items:
            problems.append(f"{len(open_items)} [ASSESSMENT REQUIRED] judgment(s) are "
                            "open; a qualified person answers them first")
        if section.table_key and section.table_key not in table_markers(draft.content):
            problems.append(f"this section's table [TABLE: {section.table_key}] is not "
                            "in its text, so it would be missing from the report")
        if section.table_key:
            unconfirmed = unconfirmed_in_scope(db, product, report)
            if unconfirmed:
                problems.append(f"{unconfirmed} event(s) behind this section's figures "
                                "are not confirmed")
        leaks = len(deident.scan(draft.content)) + len(deident.confirmed_in(
            draft.content, deident.confirmed_identifiers(db, product.id)))
        if leaks:
            problems.append(f"{leaks} likely identifier(s) are in the text")
        if problems:
            raise error("PV_CANNOT_APPROVE",
                        "This section cannot be approved: " + "; ".join(problems) + ".",
                        409, {"problems": problems})

    was = section.status
    section.status = body.status
    section.updated_at = now()
    if body.status != "approved":
        _withdraw_signoff(db, report, user,
                          f"{section.section_code} moved from {was} to {body.status}")
    log_audit(db, user, "Set a safety section status", "pv_section", section.id, None,
              "success" if body.status == "approved" else "info",
              f"{section.section_code}: {was} -> {body.status}")
    db.commit()
    return _section_out(section)


@router.get("/pv/sections/{section_id}/baseline-diff")
def baseline_diff(section_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """The previous approved report's text for this section beside this one's.

    A line diff rather than a character one: a reviewer is asking which
    statements changed, and a character diff of re-flowed prose is noise.
    """
    import difflib

    section = _owned_section(db, section_id, user)
    baseline = _baseline_text(db, section) or ""
    current = _latest_draft(db, section.id)
    current_text = current.content if current else ""
    lines = list(difflib.unified_diff(
        baseline.splitlines(), current_text.splitlines(),
        fromfile="previous report", tofile="this report", lineterm=""))
    return {"has_baseline": bool(section.baseline_section_id),
            "baseline": baseline, "current": current_text,
            "diff": lines, "changed": baseline.strip() != current_text.strip()}


@router.get("/pv/reports/{report_instance_id}/delta")
def what_changed(report_instance_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """§10's "what changed this interval", computed and not written.

    New cases by SOC, signals opened and closed, RSI changes, safety actions and
    new studies -- each read from the store with this report's dates. There is
    no model in this: a summary of what changed that a model had written would
    be one more place a figure could be typed rather than counted.
    """
    from app.models import (
        PvCase, PvCaseEvent, PvSafetyAction, PvSignal, PvStudy,
    )

    report, product = _tabulation_context(db, report_instance_id, user)
    scope = scope_mod.scope_for(product, report)
    baseline = (db.get(PvReportInstance, report.baseline_report_id)
                if report.baseline_report_id else None)
    since = baseline.data_lock_point if baseline else None

    interval_case_ids = select(PvCase.id).where(scope_mod.interval(scope))
    by_soc: dict = {}
    for event in db.scalars(select(PvCaseEvent).where(
            PvCaseEvent.case_id.in_(interval_case_ids))).all():
        soc = event.meddra_soc or "SOC not coded"
        by_soc[soc] = by_soc.get(soc, 0) + 1

    signals = db.scalars(select(PvSignal).where(
        PvSignal.pv_product_id == product.id)).all()
    opened = [s for s in signals if s.detection_date
              and scope.period_start <= s.detection_date <= scope.data_lock_point]
    closed = [s for s in signals if s.closure_date
              and scope.period_start <= s.closure_date <= scope.data_lock_point]
    actions = [a for a in db.scalars(select(PvSafetyAction).where(
        PvSafetyAction.pv_product_id == product.id)).all()
        if a.action_date and scope.period_start <= a.action_date <= scope.period_end]
    studies = [s for s in db.scalars(select(PvStudy).where(
        PvStudy.pv_product_id == product.id)).all()
        if s.start_date and scope.period_start <= s.start_date <= scope.period_end]
    rsi_changes = [v for v in db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == product.id)).all()
        if v.effective_date and scope.period_start <= v.effective_date
        <= scope.data_lock_point]
    sections = db.scalars(select(PvSection).where(
        PvSection.report_instance_id == report.id)).all()
    badges: dict = {}
    for s in sections:
        if not s.is_container:
            badges[s.delta_status] = badges.get(s.delta_status, 0) + 1

    return {
        "since_baseline_lock": since,
        "events_by_soc": dict(sorted(by_soc.items(), key=lambda kv: -kv[1])),
        "interval_cases": scope_mod.count_cases(db, scope, scope_mod.interval(scope)),
        "signals_opened": [s.signal_reference or s.id for s in opened],
        "signals_closed": [s.signal_reference or s.id for s in closed],
        "rsi_changes": [f"{v.rsi_type.upper()} {v.version_label}" for v in rsi_changes],
        "safety_actions": [f"{a.action_type.replace('_', ' ')} ({a.region or 'all'})"
                           for a in actions],
        "new_studies": [s.study_id for s in studies],
        "section_badges": badges,
        "note": "Computed from the case store and registers; no model wrote this.",
    }


@router.post("/pv/cases/{case_id}/narrative", status_code=201)
def draft_case_narrative(case_id: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """An ICSR narrative, from the case's structured fields and its MASKED
    narrative -- never the original.

    The case must be clear of de-identification first: a case still `pending`
    has a working copy that is not yet the working copy.
    """
    from app.models import (
        PvCase, PvCaseDrug, PvCaseEvent, PvCaseLab, PvCaseNarrative,
    )
    from app.safety import drafting
    from app.tenancy import llm_policy_for

    case = db.get(PvCase, case_id)
    if not case or case.org_id != user.org_id:
        raise error("PV_CASE_NOT_FOUND", "Case not found", 404)
    product = _owned_product(db, case.pv_product_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Drafting a case narrative")
    if case.deidentification_status == "pending":
        raise error("PV_DEID_GATE_OPEN",
                    "This case is still waiting for de-identification. A narrative "
                    "is drafted from masked text only.", 409)

    events = db.scalars(select(PvCaseEvent).where(PvCaseEvent.case_id == case.id)).all()
    drugs = db.scalars(select(PvCaseDrug).where(PvCaseDrug.case_id == case.id)).all()
    labs = db.scalars(select(PvCaseLab).where(PvCaseLab.case_id == case.id)).all()
    working = db.scalar(select(PvCaseNarrative).where(
        PvCaseNarrative.case_id == case.id).order_by(PvCaseNarrative.version.desc()))
    record = {
        "case_id": case.worldwide_case_id, "report_source": case.report_source,
        "country": case.country_of_occurrence,
        "patient": {"age": case.patient_age, "age_group": case.patient_age_group,
                    "sex": case.patient_sex, "pregnancy": case.is_pregnancy_case},
        "seriousness": {"serious": case.is_serious,
                        "criteria": case.seriousness_criteria or []},
        "outcome": case.case_outcome,
        "events": [{"term": e.meddra_pt or "[uncoded]", "onset": e.onset_date,
                    "outcome": e.outcome,
                    "causality_reporter": e.causality_reporter,
                    "causality_company": e.causality_company,
                    "expectedness": (e.expectedness if e.confirmed_by
                                     else "not assessed")} for e in events],
        "drugs": [{"name": d.drug_name, "role": d.role, "dose": d.dose,
                   "dose_unit": d.dose_unit, "route": d.route,
                   "start": d.start_date, "end": d.end_date,
                   "action_taken": d.action_taken, "dechallenge": d.dechallenge,
                   "rechallenge": d.rechallenge} for d in drugs],
        "laboratory": [{"test": l.test_name, "result": l.result, "unit": l.unit,
                        "reference_range": l.reference_range} for l in labs],
        "masked_source_narrative": working.raw_text_redacted if working else None,
    }
    try:
        result = drafting.draft_narrative(
            case_id=case.worldwide_case_id or case.id,
            product_name=product.product_name, case_record=record,
            llm_policy=llm_policy_for(db, user.org_id, project_id=product.project_id,
                                      user_id=user.id, subject_type="pv_case",
                                      subject_id=case.id))
    except drafting.IdentifierInPrompt as exc:
        raise error("PV_IDENTIFIER_IN_PROMPT", str(exc), 422)

    latest = db.scalar(select(PvCaseNarrative).where(
        PvCaseNarrative.case_id == case.id).order_by(PvCaseNarrative.version.desc()))
    narrative = PvCaseNarrative(
        org_id=user.org_id, pv_product_id=product.id, case_id=case.id,
        version=(latest.version + 1) if latest else 1,
        raw_text_redacted=latest.raw_text_redacted if latest else None,
        generated_text=result.content, created_by=user.id)
    db.add(narrative)
    log_audit(db, user, "Drafted a case narrative", "pv_case", case.id, None, "info",
              f"{case.worldwide_case_id} v{narrative.version}")
    db.commit()
    return {"case_id": case.id, "version": narrative.version,
            "content": result.content, "data_needed": result.data_needed,
            "model": result.model}


# ============================================================ M7: QC

@router.get("/pv/reports/{report_instance_id}/qc")
def report_qc(report_instance_id: str, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Every §11 check, grouped by severity.

    `exportable` is computed from exactly this list, and the export endpoint
    will call the same function -- so the dashboard saying a report can go and
    the endpoint agreeing are one question asked once, not two gates that can
    each pass what the other would refuse.
    """
    from app.safety import qc

    report, product = _tabulation_context(db, report_instance_id, user)
    findings = qc.run_qc(db, report=report, product=product)
    grouped: dict = {qc.BLOCKER: [], qc.WARNING: [], qc.INFO: []}
    for finding in findings:
        grouped[finding.severity].append(finding.as_dict())
    return {"findings": [f.as_dict() for f in findings],
            "blockers": grouped[qc.BLOCKER], "warnings": grouped[qc.WARNING],
            "info": grouped[qc.INFO], "exportable": qc.exportable(findings)}


# --------------------------------------------- M8: sign-off, export and audit

def _export_paths(record) -> list:
    """Every file an export wrote, as absolute paths.

    `blob_path` names the first file only -- the column exists so the
    org-offboarding sweep finds it -- and `options["files"]` names them all.
    """
    seen: list = []
    for relative in [record.blob_path] + [
            entry.get("blob_path") for entry in (record.options or {}).get("files", [])]:
        if relative and relative not in seen:
            seen.append(relative)
    return [abs_path(relative) for relative in seen]


def _export_out(record) -> dict:
    options = dict(record.options or {})
    files = options.pop("files", [])
    return {"id": record.id, "report_instance_id": record.report_instance_id,
            "created_by": record.created_by, "created_at": record.created_at,
            "files": [{"index": i, "kind": f.get("kind"), "filename": f.get("filename")}
                      for i, f in enumerate(files)],
            "options": options}


class PvSignoffRequest(BaseModel):
    statement: str | None = None


@router.post("/pv/reports/{report_instance_id}/signoff")
def sign_off_report(report_instance_id: str, body: PvSignoffRequest,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """A qualified person signs the report as it stands.

    Refused while any blocker other than the missing sign-off itself stands:
    a signature over an open [ASSESSMENT REQUIRED], an unconfirmed event or an
    identifier in the text is a signature on something unfinished. The figures
    the report states are frozen here, so the export can refuse a document
    whose numbers moved after the signature, and the next interval can check
    its cumulative against what this one said.
    """
    from app.safety import qc

    report, product = _tabulation_context(db, report_instance_id, user)
    roles.require_pv_role(db, product.id, user, roles.QUALIFIED_PERSON,
                          action="Signing off a periodic report")
    if report.qppv_signoff_by:
        raise error("PV_ALREADY_SIGNED_OFF", "This report is already signed off.", 409)
    findings = qc.run_qc(db, report=report, product=product)
    blocking = [f for f in findings
                if f.severity == qc.BLOCKER and f.code != "NOT_SIGNED_OFF"]
    if blocking:
        raise error("PV_CANNOT_SIGN_OFF",
                    f"{len(blocking)} blocker(s) stand between this report and "
                    "sign-off. Resolve them on the Checks screen first.", 409,
                    {"blockers": [f.as_dict() for f in blocking]})
    figures = qc.figures_for(db, report=report, product=product)
    report.qppv_signoff_by = user.id
    report.qppv_signoff_at = now()
    report.status = "approved"
    report.figures_at_signoff = figures
    report.updated_at = now()
    statement = (body.statement or "").strip()
    log_audit(db, user, "Signed off a periodic safety report", "pv_report_instance",
              report.id, product.project_id, "success",
              f"{report.doc_type_key} {report.period_start}..{report.period_end}; "
              f"interval cases {figures.get('interval_cases')}, cumulative cases "
              f"{figures.get('cumulative_cases')}"
              + (f"; statement: {statement[:500]}" if statement else ""))
    db.commit()
    db.refresh(report)
    return _report_out(report)


class PvWithdrawRequest(BaseModel):
    reason: str


@router.post("/pv/reports/{report_instance_id}/signoff:withdraw")
def withdraw_report_signoff(report_instance_id: str, body: PvWithdrawRequest,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """A qualified person takes a signature back, and says why."""
    report, product = _tabulation_context(db, report_instance_id, user)
    roles.require_pv_role(db, product.id, user, roles.QUALIFIED_PERSON,
                          action="Withdrawing a sign-off")
    reason = (body.reason or "").strip()
    if not reason:
        raise error("PV_REASON_REQUIRED", "Say why the sign-off is withdrawn.", 422)
    if not _withdraw_signoff(db, report, user, f"withdrawn: {reason[:500]}"):
        raise error("PV_NOT_SIGNED_OFF", "This report is not signed off.", 409)
    db.commit()
    db.refresh(report)
    return _report_out(report)


class PvAcceptFinding(BaseModel):
    key: str
    reason: str


@router.post("/pv/reports/{report_instance_id}/qc/accept")
def accept_qc_finding(report_instance_id: str, body: PvAcceptFinding,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """A qualified person accepts one heuristic blocker, with a reason.

    Only the codes in `qc.ACCEPTABLE`: the checks that read prose with a
    pattern. The acceptance is keyed on what the finding says, so if the
    figure in it changes the finding is new and is accepted again or fixed.
    It stays on the QC list as a warning naming who accepted it.
    """
    from app.safety import qc

    report, product = _tabulation_context(db, report_instance_id, user)
    roles.require_pv_role(db, product.id, user, roles.QUALIFIED_PERSON,
                          action="Accepting a QC finding")
    reason = (body.reason or "").strip()
    if not reason:
        raise error("PV_REASON_REQUIRED",
                    "An accepted finding needs the reason it is correct as written.", 422)
    findings = qc.run_qc(db, report=report, product=product)
    finding = next((f for f in findings if f.key == body.key), None)
    if finding is None:
        raise error("PV_FINDING_NOT_FOUND",
                    "No current finding has that key. It may have changed since the "
                    "checks were run; run them again.", 404)
    if finding.code not in qc.ACCEPTABLE:
        raise error("PV_FINDING_NOT_ACCEPTABLE",
                    f"{finding.code} is fixed, not accepted.", 409)
    if finding.severity != qc.BLOCKER:
        raise error("PV_FINDING_ALREADY_ACCEPTED", "That finding is already accepted.", 409)
    accepted = dict(report.accepted_findings or {})
    accepted[finding.key] = {"code": finding.code, "section_code": finding.section_code,
                             "message": finding.message, "by": user.id,
                             "at": now().isoformat(), "reason": reason[:2000]}
    # Reassigned rather than mutated: a JSON column does not notice an
    # in-place change, and the acceptance would silently not be saved.
    report.accepted_findings = accepted
    log_audit(db, user, "Accepted a QC finding", "pv_report_instance", report.id,
              product.project_id, "warning",
              f"{finding.code}"
              + (f" in {finding.section_code}" if finding.section_code else "")
              + f": {reason[:500]}")
    db.commit()
    return {"accepted": finding.key, "code": finding.code}


class PvExportRequest(BaseModel):
    appendices: list[str] = []
    citations: str = "strip"
    draft_watermark: bool = False
    tracked_changes: bool = False
    region: str | None = None
    pdf: bool = False


def _cleanup(written: list) -> None:
    for entry in written:
        abs_path(entry["blob_path"]).unlink(missing_ok=True)


@router.post("/pv/reports/{report_instance_id}/export")
def export_report(report_instance_id: str, body: PvExportRequest,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Write the signed report, and a tracked-changes copy if asked.

    The gate is `run_qc` and nothing else, so the Checks screen's verdict and
    this endpoint cannot disagree. Every table is resolved here from the
    store. The finished files are scanned for identifiers -- deleted revision
    text included -- and a hit deletes them and refuses the export.

    Nothing here produces E2B XML, a CIOMS form or a gateway payload.
    """
    from app.docgen.grids import TableError
    from app.models import PvExport, PvRsiVersion
    from app.safety import export as export_mod
    from app.safety import qc
    from app.safety import tabulations as tab

    report, product = _tabulation_context(db, report_instance_id, user)
    roles.require_pv_role(db, product.id, user, roles.WRITER,
                          action="Exporting a report")
    unknown = [a for a in body.appendices if a not in export_mod.APPENDICES]
    if unknown:
        raise error("PV_BAD_APPENDIX",
                    f"Unknown appendix {', '.join(unknown)}; one of "
                    f"{', '.join(export_mod.APPENDICES)}.", 422)
    if body.citations not in export_mod.CITATION_MODES:
        raise error("PV_BAD_CITATION_MODE",
                    f"citations must be one of {', '.join(export_mod.CITATION_MODES)}.",
                    422)
    if body.region and body.region not in (report.regions or []):
        raise error("PV_BAD_REGION",
                    f"{body.region} is not one of this report's regions.", 422)
    if body.tracked_changes and not report.baseline_report_id:
        raise error("PV_NO_BASELINE",
                    "Tracked changes compare against the previous report, and this "
                    "report has none.", 422)

    findings = qc.run_qc(db, report=report, product=product)
    blockers = [f.as_dict() for f in findings if f.severity == qc.BLOCKER]
    if blockers:
        log_audit(db, user, "Refused a safety report export", "pv_report_instance",
                  report.id, product.project_id, "warning",
                  ", ".join(sorted({b["code"] for b in blockers})))
        db.commit()
        raise error("PV_EXPORT_BLOCKED",
                    f"{len(blockers)} blocker(s) stand between this report and export.",
                    409, {"blockers": blockers})

    sections = db.scalars(select(PvSection).where(
        PvSection.report_instance_id == report.id, PvSection.enabled.is_(True)
    ).order_by(PvSection.sort_order)).all()
    texts = []
    for section in sections:
        draft = _latest_draft(db, section.id)
        texts.append(export_mod.SectionText(
            code=section.section_code, title=section.title,
            is_container=section.is_container,
            content=draft.content if draft else "",
            baseline=_baseline_text(db, section) if body.tracked_changes else None))

    chosen = [export_mod.APPENDICES[a] for a in dict.fromkeys(body.appendices)]
    keys: list = []
    for text in texts:
        keys.extend(value for kind, value in export_mod.items_of(
            export_mod.body_of(text.content, text, citations=body.citations))
            if kind == "table")
    for _heading, appendix_keys in chosen:
        keys.extend(appendix_keys)
    tables: dict = {}
    for key in dict.fromkeys(keys):
        try:
            tables[key] = tab.render(db, report=report, product=product,
                                     table_key=key).blocks
        except TableError as exc:
            tables[key] = str(exc)

    entry = registry.DELIVERABLES.get(report.doc_type_key) or {}
    name = entry.get("name") or report.doc_type_key
    rsi = db.get(PvRsiVersion, report.rsi_version_id) if report.rsi_version_id else None
    front = [name, f"Reporting interval: {report.period_start} to {report.period_end}",
             f"Data lock point: {report.data_lock_point}"]
    if rsi is not None:
        front.append(f"Reference safety information: {rsi.rsi_type.upper()} "
                     f"version {rsi.version_label}")
    if report.meddra_version:
        front.append(f"MedDRA version {report.meddra_version}")
    if body.region:
        front.append(f"Regional copy: {body.region}")
    front.append("Signed off by a qualified person on "
                 f"{report.qppv_signoff_at:%Y-%m-%d}")
    header = (f"{product.product_name} | {name} | {report.period_start} to "
              f"{report.period_end} | CONFIDENTIAL")
    stem = f"{report.doc_type_key}-{report.period_end}" + (
        f"-{body.region}" if body.region else "")
    base = f"pv-export/{product.id}"
    stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")

    written: list = []
    try:
        variants = [("report", f"{stem}.docx", False)]
        if body.tracked_changes:
            variants.append(("tracked_changes", f"{stem}-tracked-changes.docx", True))
        for kind, filename, tracked in variants:
            assembled = export_mod.assemble(
                texts, tables, front=front, title=product.product_name,
                appendices=chosen, citations=body.citations, tracked=tracked)
            relative = save_bytes(b"", base, ".docx")
            written.append({"kind": kind, "filename": filename, "blob_path": relative})
            export_mod.write(assembled, str(abs_path(relative)), header=header,
                             draft=body.draft_watermark,
                             revision_date=stamp if tracked else None)
    except export_mod.ExportError as exc:
        _cleanup(written)
        raise error("PV_EXPORT_FAILED", str(exc), 409)
    except Exception:
        _cleanup(written)
        raise

    identifiers = deident.confirmed_identifiers(db, product.id)
    found = []
    for file_entry in written:
        text = export_mod.document_text(str(abs_path(file_entry["blob_path"])))
        found.extend({**hit, "file": file_entry["filename"]}
                     for hit in export_mod.leaks(text, identifiers))
    if found:
        _cleanup(written)
        # The audit entry names the kinds of identifier and never the text: the
        # log is read by more people than the report.
        log_audit(db, user, "Refused a safety report export: identifiers in the document",
                  "pv_report_instance", report.id, product.project_id, "warning",
                  ", ".join(sorted({h["identifier_type"] for h in found})))
        db.commit()
        raise error("PV_EXPORT_LEAK",
                    f"{len(found)} likely identifier(s) were found in the finished "
                    "document, which has been deleted rather than offered for download.",
                    409, {"found": found[:50]})

    notes: list = []
    if body.pdf:
        from app.generation.pdf_renderer import PreviewUnavailable, render_pdf

        try:
            result = render_pdf(abs_path(written[0]["blob_path"]), abs_path(base))
            written.append({"kind": "pdf", "filename": f"{stem}.pdf",
                            "blob_path": f"{base}/{Path(result.pdf_path).name}"})
            notes.extend(result.notes)
        except PreviewUnavailable as exc:
            notes.append(f"No PDF was produced: {exc}")

    warnings = [f for f in findings if f.severity == qc.WARNING]
    record = PvExport(
        org_id=user.org_id, pv_product_id=product.id, report_instance_id=report.id,
        granularity="combined", blob_path=written[0]["blob_path"], created_by=user.id,
        options={
            "appendices": list(dict.fromkeys(body.appendices)),
            "citations": body.citations, "draft_watermark": body.draft_watermark,
            "tracked_changes": body.tracked_changes, "region": body.region,
            "pdf": body.pdf, "files": written, "notes": notes,
            "warnings": [{"code": w.code, "section_code": w.section_code,
                          "message": w.message} for w in warnings],
            "signed_off_by": report.qppv_signoff_by,
            "signed_off_at": report.qppv_signoff_at.isoformat(),
            "figures": report.figures_at_signoff,
            "baseline_report_id": report.baseline_report_id
            if body.tracked_changes else None,
            "leakage_scan": "passed"})
    db.add(record)
    db.flush()
    log_audit(db, user, "Exported a periodic safety report", "pv_export", record.id,
              product.project_id, "success",
              f"{report.doc_type_key} {report.period_start}..{report.period_end}: "
              + ", ".join(f["filename"] for f in written)
              + (f"; {len(warnings)} warning(s) recorded" if warnings else ""))
    db.commit()
    db.refresh(record)
    return _export_out(record)


@router.get("/pv/reports/{report_instance_id}/exports")
def list_report_exports(report_instance_id: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    from app.models import PvExport

    report = _owned_report(db, report_instance_id, user)
    rows = db.scalars(select(PvExport).where(
        PvExport.report_instance_id == report.id
    ).order_by(PvExport.created_at.desc())).all()
    return {"items": [_export_out(r) for r in rows]}


_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


@router.get("/pv/exports/{pv_export_id}/download")
def download_pv_export(pv_export_id: str, index: int = Query(0, ge=0),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """One file of an export, by its position in the export's file list."""
    from fastapi.responses import FileResponse

    from app.models import PvExport

    record = db.get(PvExport, pv_export_id)
    if not record or record.org_id != user.org_id:
        raise error("PV_EXPORT_NOT_FOUND", "Export not found", 404)
    files = (record.options or {}).get("files", [])
    if index >= len(files):
        raise error("PV_EXPORT_NO_SUCH_FILE",
                    f"This export has {len(files)} file(s); there is no file {index + 1}.",
                    404)
    entry = files[index]
    path = abs_path(entry["blob_path"])
    if not path.exists():
        raise error("PV_EXPORT_MISSING", "The exported file is no longer in storage.", 410)
    log_audit(db, user, "Downloaded a safety report export", "pv_export", record.id,
              None, "info", entry.get("filename"))
    db.commit()
    return FileResponse(str(path), filename=entry.get("filename") or path.name,
                        media_type=_MEDIA_TYPES.get(path.suffix, "application/octet-stream"))


@router.get("/pv/reports/{report_instance_id}/audit")
def report_audit(report_instance_id: str, scope: str = Query("report"),
                 limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """What happened to this report, or to the product data it is built from.

    `scope=report`: the report, its sections, its exports. `scope=product`: the
    product, its RSI versions, and every case and event decision -- what a
    reviewer reads to know how the figures came to be what they are.
    """
    from sqlalchemy import and_

    from app.models import AuditLog, PvCase, PvCaseEvent, PvExport, PvRsiVersion

    report, product = _tabulation_context(db, report_instance_id, user)
    if scope not in ("report", "product"):
        raise error("PV_BAD_AUDIT_SCOPE", "scope must be report or product.", 422)
    if scope == "report":
        ids = [report.id]
        ids += db.scalars(select(PvSection.id).where(
            PvSection.report_instance_id == report.id)).all()
        ids += db.scalars(select(PvExport.id).where(
            PvExport.report_instance_id == report.id)).all()
        condition = AuditLog.entity_id.in_(ids)
    else:
        condition = or_(
            AuditLog.entity_id == product.id,
            AuditLog.entity_id.in_(select(PvRsiVersion.id).where(
                PvRsiVersion.pv_product_id == product.id)),
            and_(AuditLog.entity_type == "pv_case",
                 AuditLog.entity_id.in_(select(PvCase.id).where(
                     PvCase.pv_product_id == product.id))),
            and_(AuditLog.entity_type == "pv_case_event",
                 AuditLog.entity_id.in_(select(PvCaseEvent.id).where(
                     PvCaseEvent.pv_product_id == product.id))))
    statement = select(AuditLog).where(AuditLog.org_id == user.org_id, condition)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                      .limit(limit).offset(offset)).all()
    return {"items": [{"id": r.id, "event": r.event, "severity": r.severity,
                       "entity_type": r.entity_type, "entity_id": r.entity_id,
                       "actor_id": r.actor_id, "actor_name": r.actor_name,
                       "target": r.target, "created_at": r.created_at} for r in rows],
            "total": total, "limit": limit, "offset": offset, "scope": scope}
