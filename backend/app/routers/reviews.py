"""Somebody read the letter and was not happy with it.

The product could generate a document, approve one and delete one. It could not
let a person say "this is wrong" -- so the only ways to express dissatisfaction
were to leave it unapproved, silently, or to delete it.

`ReviewTask`, which the /review screen already showed, is a different thing: the
*engine* parking a question it could not answer. Every row is written by the
batch runner and nothing a person does creates one. Both belong in one queue and
neither fits in the other's row, so this adds the missing half and
`GET /review-queue` presents them together, ranked.

Three rules the endpoints enforce, each because the alternative makes the queue
decorative:

* **A rejection carries a reason.** "Rejected, no idea why" is the outcome of a
  button with no required text, and it costs the next person a conversation.
* **The author does not close the review of their own text.** The separation
  `check_manifest_approval` already makes for a manifest, made per letter, where
  it matters more because it happens per letter.
* **A document with an open review cannot be approved.** Otherwise the objection
  is a note somebody can sign straight past.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.authz import (
    REVIEW_DOCUMENT, check_document_review_resolution, has_capability, require,
)
from app.db import get_db
from app.generation.document_status import refresh_status
from app.models import (
    DocumentReview, DocumentVersion, GeneratedDocument, ReviewComment, ReviewTask, User, now,
)
from app.ownership import owned_document_version, owned_project
from app.security import error, get_current_user

router = APIRouter(tags=["reviews"])

OPEN = "open"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"

CLOSED_STATES = (APPROVED, REJECTED, WITHDRAWN)


def _name(db: Session, user_id: str | None) -> str | None:
    if not user_id:
        return None
    row = db.get(User, user_id)
    return row.full_name if row else None


def _comment_out(db: Session, comment: ReviewComment) -> dict:
    return {
        "id": comment.id,
        "parent_id": comment.parent_id,
        "author_id": comment.author_id,
        "author_name": _name(db, comment.author_id),
        "body": comment.body,
        "paragraph_index": comment.paragraph_index,
        "span_index": comment.span_index,
        "quoted_text": comment.quoted_text,
        "resolved_at": comment.resolved_at,
        "created_at": comment.created_at,
    }


def _review_out(db: Session, review: DocumentReview, *, user: User | None = None,
                comments: bool = False) -> dict:
    out = {
        "id": review.id,
        "document_id": review.document_id,
        "document_version_id": review.document_version_id,
        "state": review.state,
        "title": review.title,
        "reason": review.reason,
        "requested_by": review.requested_by,
        "requested_by_name": _name(db, review.requested_by),
        "assigned_to": review.assigned_to,
        "assigned_to_name": _name(db, review.assigned_to),
        "authored_by": review.authored_by,
        "resolved_by": review.resolved_by,
        "resolved_by_name": _name(db, review.resolved_by),
        "resolved_at": review.resolved_at,
        "resolution_note": review.resolution_note,
        "created_at": review.created_at,
    }
    if user is not None:
        # Sent with the review so a disabled button can say why it is disabled,
        # rather than the reviewer discovering the rule as a 403 after typing.
        verdict = check_document_review_resolution(
            resolver_id=user.id, authored_by_id=review.authored_by)
        permitted = has_capability(user, REVIEW_DOCUMENT)
        out["can_resolve"] = bool(verdict.allowed and permitted and review.state == OPEN)
        out["cannot_resolve_reason"] = (
            None if out["can_resolve"]
            else "This review is already closed." if review.state != OPEN
            else verdict.reason if not verdict.allowed
            else "Closing a review needs the reviewer permission."
        )
    if comments:
        rows = db.scalars(
            select(ReviewComment)
            .where(ReviewComment.review_id == review.id)
            .order_by(ReviewComment.created_at)
        ).all()
        out["comments"] = [_comment_out(db, c) for c in rows]
    return out


def _owned_review(db: Session, review_id: str, user: User) -> DocumentReview:
    review = db.get(DocumentReview, review_id)
    if not review or review.org_id != user.org_id:
        raise error("REVIEW_NOT_FOUND", "Review not found", 404)
    return review


# ---- opening one ----

class OpenReviewRequest(BaseModel):
    reason: str
    title: str | None = None
    assigned_to: str | None = None


@router.post("/document-versions/{version_id}/reviews", status_code=201)
def open_review(version_id: str, body: OpenReviewRequest, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Say that this document is not right.

    Anyone may open one -- noticing a mistake is not a privilege. Closing one is,
    and that is `REVIEW_DOCUMENT` below.
    """
    version, document = owned_document_version(db, version_id, user)
    if not body.reason.strip():
        raise error("REASON_REQUIRED",
                    "Say what is wrong. A rejection with no reason costs the next person a "
                    "conversation to find out.", 400)

    existing = db.scalars(
        select(DocumentReview).where(
            DocumentReview.document_version_id == version_id,
            DocumentReview.state == OPEN,
        )
    ).first()
    if existing is not None:
        raise error(
            "REVIEW_ALREADY_OPEN",
            "This version already has an open review. Add a comment to it rather than opening "
            "a second -- two open reviews mean two people closing one document independently.",
            409, details={"review_id": existing.id})

    review = DocumentReview(
        org_id=user.org_id,
        document_id=document.id,
        document_version_id=version.id,
        state=OPEN,
        title=(body.title or "").strip() or None,
        reason=body.reason.strip(),
        requested_by=user.id,
        assigned_to=body.assigned_to,
        # Frozen now, not read at resolve time: editing the document afterwards
        # must not be able to launder who wrote it and let them close their own
        # review.
        authored_by=version.created_by,
    )
    db.add(review)
    db.flush()

    refresh_status(db, version=version, document=document)
    log_audit(db, user, "Requested changes to a document", "document_version", version.id,
              document.project_id, "warning", body.reason.strip()[:200])
    db.commit()
    db.refresh(review)
    return _review_out(db, review, user=user, comments=True)


@router.get("/document-versions/{version_id}/reviews")
def list_version_reviews(version_id: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    owned_document_version(db, version_id, user)
    rows = db.scalars(
        select(DocumentReview)
        .where(DocumentReview.document_version_id == version_id)
        .order_by(DocumentReview.created_at.desc())
    ).all()
    return {"items": [_review_out(db, r, user=user) for r in rows]}


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    review = _owned_review(db, review_id, user)
    version = db.get(DocumentVersion, review.document_version_id)
    document = db.get(GeneratedDocument, review.document_id)
    return {
        **_review_out(db, review, user=user, comments=True),
        "document": {
            "id": document.id if document else None,
            "display_id": document.display_id if document else None,
            "status": version.status if version else None,
            "version_no": version.version_no if version else None,
            "project_id": document.project_id if document else None,
        },
    }


@router.get("/reviews")
def list_reviews(state: str | None = None, assigned_to: str | None = None,
                 project_id: str | None = None, limit: int = 100,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(DocumentReview).where(DocumentReview.org_id == user.org_id)
    if state:
        stmt = stmt.where(DocumentReview.state == state)
    if assigned_to:
        stmt = stmt.where(DocumentReview.assigned_to ==
                          (user.id if assigned_to == "me" else assigned_to))
    if project_id:
        owned_project(db, project_id, user)
        stmt = stmt.where(DocumentReview.document_id.in_(
            select(GeneratedDocument.id).where(GeneratedDocument.project_id == project_id)))
    rows = db.scalars(
        stmt.order_by(DocumentReview.created_at.desc()).limit(max(1, min(limit, 200)))
    ).all()
    return {"items": [_review_out(db, r, user=user) for r in rows]}


# ---- talking about it ----

class CommentRequest(BaseModel):
    body: str
    parent_id: str | None = None
    paragraph_index: int | None = None
    span_index: int | None = None
    quoted_text: str | None = None


@router.post("/reviews/{review_id}/comments", status_code=201)
def add_comment(review_id: str, body: CommentRequest, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Say something about the document, optionally pinned to one run of it.

    `(paragraph_index, span_index)` is the coordinate the compiler, the fill
    engine, the QA gates and the text editor all speak, so a comment and the run
    it is about point at the same place with no translation to get wrong.
    """
    review = _owned_review(db, review_id, user)
    if not body.body.strip():
        raise error("COMMENT_EMPTY", "A comment needs something in it.", 400)

    comment = ReviewComment(
        org_id=review.org_id, review_id=review.id,
        parent_id=body.parent_id, author_id=user.id, body=body.body.strip(),
        paragraph_index=body.paragraph_index, span_index=body.span_index,
        # What the run said when the remark was written. "This figure is wrong"
        # is worthless once somebody has changed the figure and nothing remembers
        # what it was.
        quoted_text=body.quoted_text,
    )
    db.add(comment)
    review.updated_at = now()
    db.flush()
    db.commit()
    db.refresh(comment)
    return _comment_out(db, comment)


@router.post("/reviews/{review_id}/comments/{comment_id}:resolve")
def resolve_comment(review_id: str, comment_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    review = _owned_review(db, review_id, user)
    comment = db.get(ReviewComment, comment_id)
    if not comment or comment.review_id != review.id:
        raise error("COMMENT_NOT_FOUND", "Comment not found", 404)
    comment.resolved_by = user.id
    comment.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return _comment_out(db, comment)


# ---- closing it ----

class ResolveReviewRequest(BaseModel):
    note: str | None = None


def _close(db: Session, user: User, review: DocumentReview, *, state: str, note: str | None,
           event: str):
    if review.state != OPEN:
        raise error("REVIEW_ALREADY_CLOSED", f"This review is already {review.state}.", 409)

    verdict = check_document_review_resolution(
        resolver_id=user.id, authored_by_id=review.authored_by)
    if not verdict.allowed:
        raise error("SELF_REVIEW_REFUSED", verdict.reason, 403)

    review.state = state
    review.resolved_by = user.id
    review.resolved_at = datetime.now(timezone.utc)
    review.resolution_note = (note or "").strip() or None
    review.updated_at = now()

    version = db.get(DocumentVersion, review.document_version_id)
    document = db.get(GeneratedDocument, review.document_id)
    if version is not None:
        refresh_status(db, version=version, document=document)

    log_audit(db, user, event, "document_review", review.id,
              document.project_id if document else None,
              "success" if state == APPROVED else "warning",
              (review.resolution_note or "")[:200])
    db.commit()
    db.refresh(review)
    return _review_out(db, review, user=user, comments=True)


@router.post("/reviews/{review_id}:approve")
def approve_review(review_id: str, body: ResolveReviewRequest | None = None,
                   db: Session = Depends(get_db),
                   user: User = Depends(require(REVIEW_DOCUMENT))):
    """The objection does not stand: the document is fine as it is.

    The document returns to whatever it would be without this review -- draft if
    nothing else is outstanding, and still `blocked` or `pending_review` if
    something is. That is `derive_status`'s job, and the reason this does not
    simply write "draft".
    """
    review = _owned_review(db, review_id, user)
    return _close(db, user, review, state=APPROVED,
                  note=(body.note if body else None),
                  event="Closed a document review as no change needed")


class RejectReviewRequest(BaseModel):
    note: str


@router.post("/reviews/{review_id}:reject")
def reject_review(review_id: str, body: RejectReviewRequest, db: Session = Depends(get_db),
                  user: User = Depends(require(REVIEW_DOCUMENT))):
    """The objection stands: the document needs changing.

    The note is required. It is the thing the person fixing the document reads,
    and a rejection without one turns into a conversation.
    """
    review = _owned_review(db, review_id, user)
    if not body.note.strip():
        raise error("NOTE_REQUIRED",
                    "Say what needs to change. It is what whoever fixes this will read.", 400)
    return _close(db, user, review, state=REJECTED, note=body.note,
                  event="Confirmed a document needs changes")


@router.post("/reviews/{review_id}:withdraw")
def withdraw_review(review_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """The person who raised it no longer thinks there is a problem.

    Only they may do this, and it needs no reviewer permission: taking back your
    own complaint is not a ruling on it.
    """
    review = _owned_review(db, review_id, user)
    if review.requested_by != user.id:
        raise error("NOT_YOURS_TO_WITHDRAW",
                    "Only the person who raised a review can withdraw it. A reviewer closes it "
                    "instead.", 403)
    if review.state != OPEN:
        raise error("REVIEW_ALREADY_CLOSED", f"This review is already {review.state}.", 409)

    review.state = WITHDRAWN
    review.resolved_by = user.id
    review.resolved_at = datetime.now(timezone.utc)
    version = db.get(DocumentVersion, review.document_version_id)
    document = db.get(GeneratedDocument, review.document_id)
    if version is not None:
        refresh_status(db, version=version, document=document)
    log_audit(db, user, "Withdrew a document review", "document_review", review.id,
              document.project_id if document else None, "info")
    db.commit()
    db.refresh(review)
    return _review_out(db, review, user=user)


class AssignRequest(BaseModel):
    user_id: str | None = None


@router.post("/reviews/{review_id}:assign")
def assign_review(review_id: str, body: AssignRequest, db: Session = Depends(get_db),
                  user: User = Depends(require(REVIEW_DOCUMENT))):
    review = _owned_review(db, review_id, user)
    if body.user_id:
        target = db.get(User, body.user_id)
        if not target or target.org_id != user.org_id:
            raise error("USER_NOT_FOUND", "That person is not in this organisation.", 404)
    review.assigned_to = body.user_id
    review.updated_at = now()
    db.commit()
    db.refresh(review)
    return _review_out(db, review, user=user)


# ---- the shortcut from the documents list ----

class RequestChangesRequest(BaseModel):
    reason: str


@router.post("/document-versions/{version_id}:request-changes", status_code=201)
def request_changes(version_id: str, body: RequestChangesRequest,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Open a review from the documents list, in one step.

    The same thing as `POST .../reviews` and deliberately not a second mechanism:
    it calls it. The separate route exists because the affordance on a row of a
    list is "this one is wrong", and making that person navigate into a document
    first to say so is how objections stop being raised.
    """
    return open_review(version_id, OpenReviewRequest(reason=body.reason), db=db, user=user)


# ---- one queue ----

#: Lower sorts first. The ordering is a judgement made once, here, rather than in
#: whichever client renders the list.
PRIORITY_BLOCKED_REVIEW = 0
PRIORITY_MINE = 1
PRIORITY_BLOCKED_TASK = 2
PRIORITY_TASK_KIND = {"calculation": 3, "condition": 4, "binding": 5, "narrative": 6}
PRIORITY_OTHER = 7


@router.get("/review-queue")
def review_queue(state: str = "open", assigned_to: str | None = None,
                 project_id: str | None = None, limit: int = 200,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Everything waiting for a person, in one list.

    Two kinds of item, ranked together: a document somebody objected to, and a
    value the engine could not work out. They are genuinely different questions
    -- one is "is this letter right", the other is "what should this number be"
    -- but they land on the same desk, and a person with two queues checks one.

    The ranking puts a review of a *defective* document first: it is both broken
    and disputed, and somebody is waiting. A wrong dose calculation outranks a
    clumsy sentence for the same reason.
    """
    items = []

    review_state = None if state == "all" else (OPEN if state == "open" else state)
    review_stmt = select(DocumentReview).where(DocumentReview.org_id == user.org_id)
    if review_state:
        review_stmt = review_stmt.where(DocumentReview.state == review_state)
    if assigned_to == "me":
        review_stmt = review_stmt.where(DocumentReview.assigned_to == user.id)

    reviews = db.scalars(review_stmt.order_by(DocumentReview.created_at.desc()).limit(limit)).all()
    version_ids = [r.document_version_id for r in reviews]
    versions = {
        v.id: v for v in db.scalars(
            select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))).all()
    } if version_ids else {}
    documents = {
        d.id: d for d in db.scalars(
            select(GeneratedDocument).where(
                GeneratedDocument.id.in_([r.document_id for r in reviews]))).all()
    } if reviews else {}

    for review in reviews:
        document = documents.get(review.document_id)
        version = versions.get(review.document_version_id)
        if project_id and (document is None or document.project_id != project_id):
            continue
        blocked = version is not None and version.status == "blocked"
        items.append({
            "kind": "document_review",
            "priority": (PRIORITY_BLOCKED_REVIEW if blocked
                         else PRIORITY_MINE if review.assigned_to == user.id
                         else PRIORITY_OTHER),
            "id": review.id,
            "title": review.title or review.reason,
            "state": review.state,
            "project_id": document.project_id if document else None,
            "document_id": review.document_id,
            "document_version_id": review.document_version_id,
            "document_status": version.status if version else None,
            "requested_by_name": _name(db, review.requested_by),
            "assigned_to": review.assigned_to,
            "created_at": review.created_at,
        })

    task_stmt = select(ReviewTask).where(ReviewTask.org_id == user.org_id)
    if state != "all":
        task_stmt = task_stmt.where(ReviewTask.status == state)
    if project_id:
        task_stmt = task_stmt.where(ReviewTask.project_id == project_id)
    tasks = db.scalars(task_stmt.order_by(ReviewTask.created_at.desc()).limit(limit)).all()

    task_versions = {
        v.id: v for v in db.scalars(
            select(DocumentVersion).where(DocumentVersion.id.in_(
                [t.document_version_id for t in tasks if t.document_version_id]))).all()
    } if tasks else {}

    for task in tasks:
        version = task_versions.get(task.document_version_id)
        blocked = version is not None and version.status == "blocked"
        items.append({
            "kind": "unit_task",
            "priority": (PRIORITY_BLOCKED_TASK if blocked
                         else PRIORITY_TASK_KIND.get(task.kind, PRIORITY_OTHER)),
            "id": task.id,
            "title": task.question,
            "state": task.status,
            "task_kind": task.kind,
            "unit_id": task.unit_id,
            "project_id": task.project_id,
            "document_version_id": task.document_version_id,
            "document_status": version.status if version else None,
            "created_at": task.created_at,
        })

    # Priority first, newest within a band. Two stable passes rather than one
    # `(priority, -timestamp)` key: the two kinds of item come from different
    # tables, and a single tuple key means inventing a fallback for a missing
    # timestamp -- which is either a string, and raises when compared against a
    # real datetime, or an epoch constant nobody chose.
    items.sort(key=lambda item: item["created_at"], reverse=True)
    items.sort(key=lambda item: item["priority"])
    return {"items": items[:limit]}
