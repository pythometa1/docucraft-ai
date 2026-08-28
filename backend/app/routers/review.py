"""The human-in-the-loop queue.

Some values genuinely need judgement -- a dose calculation a toxicologist must
sign off, a condition whose inputs are ambiguous, a narrative that came back
poorly grounded. Rather than guessing, the engine parks the document here with
enough context for a person to decide, and the decision is recorded in the
generation's lineage alongside everything the machine resolved.

Resolving a task can also promote it into the manifest, so the same judgement
is not asked for twice. That is the mechanism by which human-touch rate falls
as an estate is processed -- the metric that decides whether this scales.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.db import get_db
from app.models import ManifestGeneration, ReviewTask, TemplateManifest, User
from app.ownership import owned_manifest
from app.security import error, get_current_user

router = APIRouter(tags=["review"])


def _task_out(task: ReviewTask) -> dict:
    return {
        "id": task.id,
        "project_id": task.project_id,
        "generation_id": task.generation_id,
        "manifest_id": task.manifest_id,
        "unit_id": task.unit_id,
        "kind": task.kind,
        "question": task.question,
        "context": task.context,
        "proposed_value": task.proposed_value,
        "resolved_value": task.resolved_value,
        "status": task.status,
        "assigned_to": task.assigned_to,
        "rationale": task.rationale,
        "created_at": task.created_at,
        "resolved_at": task.resolved_at,
    }


@router.get("/review-tasks")
def list_review_tasks(
    status_: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(ReviewTask).where(ReviewTask.org_id == user.org_id)
    if status_:
        stmt = stmt.where(ReviewTask.status == status_)
    if project_id:
        stmt = stmt.where(ReviewTask.project_id == project_id)
    if kind:
        stmt = stmt.where(ReviewTask.kind == kind)
    rows = db.scalars(stmt.order_by(ReviewTask.created_at.desc()).limit(limit)).all()
    return {"items": [_task_out(t) for t in rows]}


@router.get("/review-tasks/summary")
def review_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Human-touch rate is the number that decides whether an estate scales."""
    rows = db.scalars(select(ReviewTask).where(ReviewTask.org_id == user.org_id)).all()
    open_tasks = [t for t in rows if t.status == "open"]
    by_kind: dict[str, int] = {}
    for t in open_tasks:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    return {
        "open": len(open_tasks),
        "resolved": sum(1 for t in rows if t.status == "resolved"),
        "dismissed": sum(1 for t in rows if t.status == "dismissed"),
        "by_kind": by_kind,
    }


@router.get("/review-tasks/{task_id}")
def get_review_task(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.get(ReviewTask, task_id)
    if not task or task.org_id != user.org_id:
        raise error("TASK_NOT_FOUND", "Review task not found", 404)
    return _task_out(task)


class ResolveRequest(BaseModel):
    resolved_value: str | None = None
    rationale: str
    # Turn a one-off decision into a manifest rule so it is never asked again.
    promote_to_manifest: bool = False
    promote_as: str | None = None  # "formula" | "condition_expression" | "constant"


@router.post("/review-tasks/{task_id}:resolve")
def resolve_review_task(
    task_id: str,
    body: ResolveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.get(ReviewTask, task_id)
    if not task or task.org_id != user.org_id:
        raise error("TASK_NOT_FOUND", "Review task not found", 404)
    if task.status != "open":
        raise error("TASK_ALREADY_RESOLVED", f"This task is already {task.status}.", 409)
    if not body.rationale.strip():
        # A regulated document needs to record *why*, not just what.
        raise error("RATIONALE_REQUIRED", "Explain the decision -- it becomes part of the audit record.", 400)

    task.resolved_value = body.resolved_value
    task.rationale = body.rationale
    task.status = "resolved"
    task.resolved_by = user.id
    task.resolved_at = datetime.now(timezone.utc)

    if task.generation_id:
        generation = db.get(ManifestGeneration, task.generation_id)
        if generation:
            generation.field_lineage = [
                *(generation.field_lineage or []),
                {
                    "unit_id": task.unit_id,
                    "kind": task.kind,
                    "value": body.resolved_value,
                    "source": "human",
                    "resolved_by": user.full_name,
                    "rationale": body.rationale,
                },
            ]
            remaining = db.scalars(
                select(ReviewTask).where(
                    ReviewTask.generation_id == task.generation_id,
                    ReviewTask.status == "open",
                    ReviewTask.id != task.id,
                )
            ).all()
            generation.qa_passed = not remaining

    promoted = None
    if body.promote_to_manifest and task.manifest_id:
        promoted = _promote(db, user, task, body)

    log_audit(db, user, "Resolved review task", "review_task", task.id, task.project_id, "success", task.unit_id)
    db.commit()
    db.refresh(task)
    return {**_task_out(task), "promoted": promoted}


def _promote(db: Session, user: User, task: ReviewTask, body: ResolveRequest) -> dict | None:
    """Write the decision back into the manifest.

    Approved manifests are immutable by design, so promotion targets the draft
    that supersedes it rather than editing history.
    """
    manifest = owned_manifest(db, task.manifest_id, user)
    if manifest.status == "approved":
        return {"applied": False, "reason": "Manifest is approved and immutable -- compile a new version to apply this."}

    mode = body.promote_as or ("formula" if task.kind == "calculation" else "constant")
    if mode == "formula" and body.resolved_value:
        fields = [dict(f) for f in manifest.fields]
        for f in fields:
            if f["id"] == task.unit_id:
                f["kind"] = "computed"
                f["formula"] = body.resolved_value
                f["requires_human_check"] = False
                break
        manifest.fields = fields
        return {"applied": True, "as": "formula", "unit_id": task.unit_id}

    if mode == "condition_expression" and body.resolved_value:
        conditions = [dict(c) for c in manifest.conditions]
        for c in conditions:
            if c["id"] == task.unit_id:
                c["expression"] = body.resolved_value
                c["condition_kind"] = "exact"  # a rule now, no longer a judgement call
                break
        manifest.conditions = conditions
        return {"applied": True, "as": "condition_expression", "unit_id": task.unit_id}

    return {"applied": False, "reason": f"Nothing to promote for mode '{mode}'."}


@router.post("/review-tasks/{task_id}:dismiss")
def dismiss_review_task(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.get(ReviewTask, task_id)
    if not task or task.org_id != user.org_id:
        raise error("TASK_NOT_FOUND", "Review task not found", 404)
    task.status = "dismissed"
    task.resolved_by = user.id
    task.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "dismissed"}
