from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analytics, retention
from app.llm import pricing
from app.audit.service import log_audit
from app.authz import MANAGE_USERS, require
from app.db import get_db
from app.models import (
    AuditLog, DeletionCertificate, DocumentVersion, GeneratedDocument, GenerationJob,
    OrgDataPolicy, OrgModelRate, Project, TemplateFile, User,
)
from app.security import error, get_current_user, require_internal_endpoints
from app.tenancy import GLOBAL

router = APIRouter(tags=["admin"])


# --------------------------------------------------------------------------- analytics
#
# The queries live in `app/analytics.py`, the way §22's live in `app/metrics.py`.
# What is here is the HTTP shape and one decision: an unknown `range` is a 400
# rather than a silent fall back to the default. The page has always shown four
# range buttons; the endpoints have always ignored them, so somebody reading
# "7d" was reading thirty days of data, or all of it.


def _window(range_: str):
    try:
        return analytics.window(range_)
    except analytics.InvalidRange as exc:
        raise error("INVALID_RANGE", str(exc), 400)


@router.get("/analytics/kpis")
def analytics_kpis(range: str = analytics.DEFAULT_RANGE, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    since, until, days = _window(range)
    return analytics.kpis(db, org_id=user.org_id, since=since, until=until, days=days)


@router.get("/analytics/trend")
def analytics_trend(range: str = analytics.DEFAULT_RANGE, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    since, until, days = _window(range)
    return analytics.documents_trend(db, org_id=user.org_id, since=since, until=until, days=days)


@router.get("/analytics/by-function")
def analytics_by_function(range: str = analytics.DEFAULT_RANGE, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    since, until, _days = _window(range)
    return {"items": analytics.documents_by_function(
        db, org_id=user.org_id, since=since, until=until)}


@router.get("/analytics/top-templates")
def analytics_top_templates(range: str = analytics.DEFAULT_RANGE, limit: int = 5,
                            db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    since, until, _days = _window(range)
    return analytics.top_templates(
        db, org_id=user.org_id, since=since, until=until, limit=max(1, min(limit, 50)))


@router.get("/analytics/cost")
def analytics_cost(range: str = analytics.DEFAULT_RANGE, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Spend, and what it was spent on.

    Ungated, unlike `/metrics`: knowing what a batch cost is ordinary
    information for anyone who runs one. *Changing* the rate it is billed at is
    `MANAGE_USERS`, below.
    """
    since, until, days = _window(range)
    # The public shape: one spend series and spend by activity. Per-model
    # figures and token counts stay in `llm_calls` for the operator.
    return analytics.cost_report(db, org_id=user.org_id, since=since, until=until, days=days)


@router.get("/analytics/compiles")
def analytics_compiles(range: str = analytics.DEFAULT_RANGE, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    since, until, days = _window(range)
    return analytics.compile_summary(
        db, org_id=user.org_id, since=since, until=until, days=days)


# ------------------------------------------------------------------- model rates
#
# The vendor price catalogue names every model we can run and what it costs us,
# so these three are operator-only: 404 wherever internal endpoints are off.

_internal = [Depends(require_internal_endpoints)]


class ModelRateUpsert(BaseModel):
    model: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    note: str | None = None


class ModelRateReset(BaseModel):
    model: str


@router.get("/admin/model-rates", dependencies=_internal)
def list_model_rates(db: Session = Depends(get_db),
                     user: User = Depends(require(MANAGE_USERS))):
    """Every rate in force for this organisation, and where each came from."""
    overrides = {
        row.model: row for row in db.scalars(
            select(OrgModelRate).where(OrgModelRate.org_id == user.org_id)).all()
    }
    items = []
    for model in sorted(set(pricing.DEFAULT_RATES) | set(overrides)):
        rate = pricing.rate_for(db, org_id=user.org_id, model=model)
        if rate is None:
            continue
        items.append({**rate.as_dict(),
                      "origin": "org" if model in overrides else "default"})
    return {"items": items, "read_on": pricing.RATES_READ_ON}


@router.put("/admin/model-rates", dependencies=_internal)
def upsert_model_rate(body: ModelRateUpsert, db: Session = Depends(get_db),
                      user: User = Depends(require(MANAGE_USERS))):
    """Set this organisation's own rate for one model.

    Deliberately without a path parameter. A `{model}` segment is not a
    tenant-owned row id, so it would fail `test_tenancy_walker`'s
    wrong-tenant-404 assertion for the wrong reason and teach the next person to
    add an exemption.

    Changing a rate never restates history: `cost_micro_usd` is frozen onto each
    `llm_calls` row when the call is made.
    """
    model = pricing.canonical_model(body.model)
    if not model:
        raise error("MODEL_REQUIRED", "Name the model this rate applies to.", 400)
    if body.input_usd_per_mtok < 0 or body.output_usd_per_mtok < 0:
        raise error("RATE_NEGATIVE", "A rate cannot be negative.", 400)

    row = db.scalar(select(OrgModelRate).where(
        OrgModelRate.org_id == user.org_id, OrgModelRate.model == model))
    if row is None:
        row = OrgModelRate(org_id=user.org_id, model=model, input_micro_usd_per_ktok=0,
                           output_micro_usd_per_ktok=0, updated_by=user.id)
        db.add(row)
    row.input_micro_usd_per_ktok = round(body.input_usd_per_mtok * 1000)
    row.output_micro_usd_per_ktok = round(body.output_usd_per_mtok * 1000)
    row.note = body.note
    row.updated_by = user.id

    log_audit(db, user, "Changed a model rate", "org_model_rate", model, None, "warning",
              f"{body.input_usd_per_mtok}/{body.output_usd_per_mtok} per MTok")
    db.commit()
    return {"model": model, "input_usd_per_mtok": body.input_usd_per_mtok,
            "output_usd_per_mtok": body.output_usd_per_mtok, "origin": "org"}


@router.post("/admin/model-rates:reset", dependencies=_internal)
def reset_model_rate(body: ModelRateReset, db: Session = Depends(get_db),
                     user: User = Depends(require(MANAGE_USERS))):
    """Drop an override and fall back to the shipped rate."""
    model = pricing.canonical_model(body.model)
    row = db.scalar(select(OrgModelRate).where(
        OrgModelRate.org_id == user.org_id, OrgModelRate.model == model))
    if row is None:
        raise error("RATE_NOT_FOUND", f"This organisation has no override for {model!r}.", 404)
    db.delete(row)
    log_audit(db, user, "Reset a model rate to the default", "org_model_rate", model,
              None, "warning", model)
    db.commit()
    return {"model": model, "origin": "default"}


# --------------------------------------------------------------------------- team
@router.get("/team/members")
def team_members(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.scalars(select(User).where(User.org_id == user.org_id)).all()
    out = []
    for u in rows:
        docs = db.scalar(select(func.count()).select_from(GeneratedDocument).where(GeneratedDocument.org_id == u.org_id)) or 0
        out.append({
            "name": u.full_name, "email": u.email, "role": u.role_key, "function": u.function,
            "status": u.status, "last_active": u.last_login_at, "docs": docs,
        })
    return {"items": out}


@router.get("/team/roles-summary")
def roles_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.execute(select(User.role_key, func.count(User.id)).where(User.org_id == user.org_id).group_by(User.role_key)).all()
    return {"items": [{"role": r, "count": c} for r, c in rows]}


# --------------------------------------------------------------------------- audit log
@router.get("/audit-logs")
def list_audit_logs(entity_type: str | None = None, severity: str | None = None, limit: int = 50, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(AuditLog).where(AuditLog.org_id == user.org_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if severity:
        stmt = stmt.where(AuditLog.severity == severity)
    rows = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {"items": [{
        "id": r.id, "time": r.created_at, "actor": r.actor_name, "action": r.event,
        "target": r.target, "severity": r.severity, "entity_type": r.entity_type,
    } for r in rows]}


# ------------------------------------------------------- §16 retention & residency
#
# Every route below is gated on MANAGE_USERS. That is the capability that
# already means "may change what this organisation is", and these change what
# the organisation keeps, where its data may be processed, and -- once -- whether
# it exists at all. `generator` and `mapper` must not be able to reach any of it.

class DataPolicyUpdate(BaseModel):
    """A partial update. Omitted fields are left alone.

    `clear_document_retention` exists because "omitted" already means "leave it
    alone", and a customer withdrawing their records-retention period needs a
    way to say so that is not the same as saying nothing.
    """

    source_retention_days: int | None = None
    generated_document_retention_days: int | None = None
    clear_document_retention: bool = False
    residency: str | None = None
    zero_retention_required: bool | None = None


class OffboardRequest(BaseModel):
    """The tenant to destroy, named explicitly.

    `confirm_org_id` must equal the caller's own organisation. It is not
    redundant with the token: this endpoint deletes everything the caller can
    see, including the caller, and a request that reached it by accident should
    fail on the confirmation rather than succeed on the session.
    """

    confirm_org_id: str
    purge_identity: bool = True


def _policy_payload(policy, row) -> dict:
    return {
        "org_id": policy.org_id,
        "source_retention_days": policy.source_retention_days,
        "generated_document_retention_days": policy.generated_document_retention_days,
        "generated_documents_retained_indefinitely": policy.retains_documents_indefinitely,
        "residency": row.residency if row is not None else GLOBAL,
        "zero_retention_required": bool(row.zero_retention_required) if row is not None else False,
        "recorded": policy.recorded,
    }


def _manifest_payload(manifest) -> dict:
    return {
        "scope": manifest.scope,
        "scope_id": manifest.scope_id,
        "org_id": manifest.org_id,
        "counts": manifest.counts,
        "rows_deleted": manifest.total_rows,
        "blobs_deleted": len(manifest.blobs),
        "derived_forgotten": manifest.derived,
        "skipped": manifest.skipped,
        # The full id list, handed over once. It is never stored -- keeping an
        # inventory of what we destroyed would be a smaller copy of the thing we
        # said we destroyed -- so this response is the only place it exists.
        "deleted_ids": manifest.rows,
        "manifest_sha256": manifest.digest(),
    }


def _certificate_payload(certificate) -> dict:
    return {
        "id": certificate.id,
        "org_id": certificate.org_id,
        "org_name": certificate.org_name,
        "scope": certificate.scope,
        "scope_id": certificate.scope_id,
        "counts": certificate.counts,
        "blobs_deleted": certificate.blobs_deleted,
        "derived_forgotten": certificate.derived_forgotten,
        "manifest_sha256": certificate.manifest_sha256,
        "issued_by": certificate.issued_by,
        "issued_by_email": certificate.issued_by_email,
        "issued_at": certificate.issued_at,
    }


@router.get("/admin/data-policy")
def get_data_policy(db: Session = Depends(get_db), user: User = Depends(require(MANAGE_USERS))):
    """What this organisation has told us to keep, and where it may be processed."""
    policy = retention.policy_for(db, user.org_id)
    row = db.scalar(select(OrgDataPolicy).where(OrgDataPolicy.org_id == user.org_id))
    return _policy_payload(policy, row)


@router.put("/admin/data-policy")
def put_data_policy(
    body: DataPolicyUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require(MANAGE_USERS)),
):
    """Record a retention schedule, a residency and a zero-retention requirement."""
    try:
        row = retention.set_policy(
            db, user.org_id,
            source_retention_days=body.source_retention_days,
            generated_document_retention_days=body.generated_document_retention_days,
            clear_document_retention=body.clear_document_retention,
            residency=body.residency,
            zero_retention_required=body.zero_retention_required,
            updated_by=user.id,
        )
    except ValueError as exc:
        raise error("INVALID_DATA_POLICY", str(exc), 400)

    log_audit(
        db, user, "data_policy.updated", "organization", entity_id=user.org_id,
        severity="warning", target=user.org_id,
    )
    db.commit()
    return _policy_payload(retention.policy_for(db, user.org_id), row)


@router.post("/admin/retention/sweep")
def run_retention_sweep(db: Session = Depends(get_db), user: User = Depends(require(MANAGE_USERS))):
    """Delete what this organisation's policy says is past its date.

    Two sweeps with deliberately different characters. Sources go on our
    schedule, defaulted if the tenant has not set one, because leaving a payroll
    extract on disk indefinitely is not a neutral choice. Generated documents go
    only on the tenant's own stated period, and the response says plainly when
    there is none rather than quietly doing nothing.
    """
    sources = retention.sweep_expired_sources(db, org_id=user.org_id)
    documents = retention.sweep_expired_documents(db, org_id=user.org_id)

    certificates = []
    for manifest in (sources, documents):
        if manifest.total_rows:
            certificates.append(_certificate_payload(
                retention.certificate_for(db, manifest, actor=user)
            ))

    log_audit(
        db, user, "retention.sweep", "organization", entity_id=user.org_id,
        severity="warning", target=f"{sources.total_rows + documents.total_rows} rows",
    )
    db.commit()
    return {
        "sources": _manifest_payload(sources),
        "generated_documents": _manifest_payload(documents),
        "certificates": certificates,
    }


@router.post("/admin/offboarding", status_code=201)
def offboard(
    body: OffboardRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require(MANAGE_USERS)),
):
    """Destroy this tenant and return the certificate that accounts for it.

    Scoped to the caller's own organisation and nothing else. There is
    deliberately no path parameter and no way to name another tenant: an
    endpoint that can offboard an arbitrary organisation is one bug away from
    being the worst outage this product can have.

    The response carries the full manifest of deleted ids. It is the only copy:
    the certificate row keeps counts and a SHA-256 over that manifest, so an
    auditor holding this response can prove the row accounts for it, and we hold
    no list of what was destroyed.
    """
    if body.confirm_org_id != user.org_id:
        raise error(
            "OFFBOARDING_NOT_CONFIRMED",
            "confirm_org_id must be your own organisation id. Offboarding deletes every "
            "record this organisation holds, including your own account.",
            400,
            {"expected": user.org_id},
        )

    certificate, manifest = retention.offboard_organisation(
        db, org_id=user.org_id, actor=user, purge_identity=body.purge_identity,
    )
    db.commit()
    return {
        "certificate": _certificate_payload(certificate),
        "manifest": _manifest_payload(manifest),
    }


@router.get("/admin/deletion-certificates")
def list_deletion_certificates(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require(MANAGE_USERS)),
):
    rows = db.scalars(
        select(DeletionCertificate)
        .where(DeletionCertificate.org_id == user.org_id)
        .order_by(DeletionCertificate.issued_at.desc())
        .limit(limit)
    ).all()
    return {"items": [_certificate_payload(r) for r in rows]}
