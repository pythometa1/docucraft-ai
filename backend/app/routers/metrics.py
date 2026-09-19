"""The §22 numbers, and the §18 targets next to what was actually measured.

§22's metrics existed only as a table in the architecture record. Anyone who
wanted to know an organisation's QA block rate or family reuse rate had to write
SQL, which means in practice nobody knew, and the two numbers §22 says every
roadmap decision should be weighed against -- escaped error rate first,
automation second -- were never in front of the people making those decisions.

Three things are deliberate about the payload.

The metric list is ordered rather than keyed, escaped error rate first and
auto-map rate last, because §22's ranking is part of the metric set: "Auto-map
rate is the vanity metric. Escaped error rate is the real one."

A metric that cannot be computed comes back with `available: false` and a
sentence saying why. It never comes back as 0.0. A zero escaped error rate on an
organisation with no way to report a defect is the most flattering wrong number
this system could print.

Service level objectives report `target` and `measured` in separate fields, and
`status: "unmeasured"` where nothing has been timed. §18 opens by warning that
none of its figures is a benchmark result; a row that inherited its target and
rendered green would be that warning being ignored in code.

Everything is scoped to the caller's organisation and gated on READ_AUDIT --
these numbers are a summary of a customer's estate, its error rate and its
review behaviour, and the roles that may read the audit trail are exactly the
roles that may read this.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics as metrics_service
from app.audit.service import log_audit
from app.authz import APPROVE_DOCUMENT, READ_AUDIT, require
from app.db import get_db
from app.models import SuggestionLog, User
from app.ownership import owned_document_version
from app.security import error, require_internal_endpoints

router = APIRouter(tags=["metrics"])

#: Travels with the payload so the ranking survives being read by someone who
#: has not read §22.
ORDERING_NOTE = (
    "Ordered by §22's own ranking, not by the order of its table. Escaped error rate is the real "
    "metric and is reported first; auto-map rate is the vanity metric and is reported last. Every "
    "threshold change and model swap is evaluated against escaped errors first and automation second."
)

MAX_LOG_PAGE = 500


@router.get("/metrics", dependencies=[Depends(require_internal_endpoints)])
def read_metrics(
    window_days: int = metrics_service.DEFAULT_WINDOW_DAYS,
    db: Session = Depends(get_db),
    user: User = Depends(require(READ_AUDIT)),
):
    """§22's metrics, §18's SLOs, and how far the §13 weights are from a fit."""
    try:
        computed = metrics_service.compute_metrics(db, org_id=user.org_id, window_days=window_days)
        slos = metrics_service.slo_report(db, org_id=user.org_id, window_days=window_days)
    except ValueError as exc:
        # A window outside the accepted range is the caller's mistake, and
        # clamping it silently would answer a question nobody asked.
        raise error("INVALID_WINDOW", str(exc), 400)

    return {
        "window_days": window_days,
        "ordering_note": ORDERING_NOTE,
        "metrics": [m.as_dict() for m in computed],
        "unavailable": [m.key for m in computed if not m.available],
        "slos": slos,
        "calibration": metrics_service.calibration_status(db, org_id=user.org_id),
    }


@router.get("/metrics/calibration-log", dependencies=[Depends(require_internal_endpoints)])
def read_calibration_log(
    limit: int = 100,
    offset: int = 0,
    decision: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require(READ_AUDIT)),
):
    """The raw suggestion log, newest first.

    §22 calls this "the only dataset that can ever calibrate the weights in
    §13", and a dataset nobody can read is not a dataset. This is how the corpus
    leaves the database for a fit, so it returns the score, the evidence and the
    decision together -- the three things a fit needs and the three things that
    cannot be reconstructed later.
    """
    if limit < 1 or limit > MAX_LOG_PAGE:
        raise error("INVALID_LIMIT", f"limit must be between 1 and {MAX_LOG_PAGE}.", 400)
    if offset < 0:
        raise error("INVALID_OFFSET", "offset cannot be negative.", 400)
    if decision is not None and decision not in metrics_service.DECISIONS:
        raise error(
            "INVALID_DECISION",
            f"decision must be one of {', '.join(metrics_service.DECISIONS)}.",
            400,
        )

    stmt = select(SuggestionLog).where(SuggestionLog.org_id == user.org_id)
    if decision:
        stmt = stmt.where(SuggestionLog.reviewer_decision == decision)
    rows = db.scalars(
        stmt.order_by(SuggestionLog.created_at.desc(), SuggestionLog.id).offset(offset).limit(limit)
    ).all()

    return {
        "items": [
            {
                "id": r.id,
                "manifest_id": r.manifest_id,
                "source_version_id": r.source_version_id,
                "object_id": r.object_id,
                "suggested_column": r.suggested_column,
                "method": r.method,
                "score": r.score,
                "band": r.band,
                "vetoes": r.vetoes,
                "evidence": r.evidence,
                "weights_calibrated": r.weights_calibrated,
                "reviewer_decision": r.reviewer_decision,
                "final_column": r.final_column,
                "decided_by": r.decided_by,
                "decided_at": r.decided_at,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    }


class EscapedErrorReport(BaseModel):
    document_version_id: str
    object_id: str
    detail: str
    check_name: str = metrics_service.ESCAPED_WRONG_VALUE


@router.post("/metrics/escaped-errors", status_code=201,
             dependencies=[Depends(require_internal_endpoints)])
def report_escaped_error(
    body: EscapedErrorReport,
    db: Session = Depends(get_db),
    user: User = Depends(require(APPROVE_DOCUMENT)),
):
    """Record that a wrong value was found in a document that was already approved.

    This is the only input to §22's most important metric that cannot be derived
    from something the system already stores: a value is wrong because a person
    who knows the correct answer says so. Without this path, escaped error rate
    is permanently unmeasurable -- and, worse, indistinguishable from perfect.

    Gated on APPROVE_DOCUMENT rather than READ_AUDIT: putting a document into
    production and admitting one that went out was wrong are the same
    responsibility, and an auditor's role reads the record without writing to it.
    """
    dv, gd = owned_document_version(db, body.document_version_id, user)
    if not body.object_id.strip():
        raise error(
            "OBJECT_REQUIRED",
            "Name the field that carried the wrong value. An escaped error with no object cannot be "
            "traced back to the mapping that produced it, which is the only reason to record it.",
            400,
        )

    try:
        row = metrics_service.record_escaped_error(
            db, org_id=user.org_id, document_version_id=dv.id,
            object_id=body.object_id.strip(), detail=body.detail, check_name=body.check_name,
        )
    except ValueError as exc:
        raise error("INVALID_ESCAPED_ERROR", str(exc), 400)

    log_audit(
        db, user, "Reported an escaped error", "document_version", dv.id, gd.project_id,
        "warning", body.object_id.strip(),
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "document_version_id": row.document_version_id,
        "object_id": row.object_id,
        "check_name": row.check_name,
        "detail": row.detail,
        "phase": row.phase,
        "created_at": row.created_at,
    }
