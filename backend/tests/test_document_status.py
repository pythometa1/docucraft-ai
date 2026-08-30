"""What state a document is in, and the two bugs that made deriving it necessary.

Both bugs are the same mistake: a status written from one caller's local
knowledge instead of established from everything that is true. `:revoke` wrote
`"draft"` over a QA failure; nothing at all wrote a document *out* of
`pending_review`. The first let a defective letter be signed, the second left a
finished one in the queue forever.
"""

import pytest

from app.generation.document_status import (
    APPROVED, BLOCKED, CHANGES_REQUESTED, DRAFT, PENDING_REVIEW, derive_status, refresh_status,
)


@pytest.fixture
def doc(two_orgs):
    """One generated document with one version, owned by org A."""
    from app.db import SessionLocal
    from app.models import DocumentVersion, GeneratedDocument, Project, User

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.query(User).filter(User.org_id == project.org_id).first()
        document = GeneratedDocument(
            org_id=project.org_id, project_id=project.id, display_id=97001,
            language="en", status=DRAFT)
        db.add(document)
        db.flush()
        version = DocumentVersion(
            document_id=document.id, org_id=project.org_id, version_no=1,
            blob_path="x.docx", status=DRAFT, created_by=user.id)
        db.add(version)
        db.flush()
        document.current_version_id = version.id
        db.flush()
        yield db, document, version, user
        db.rollback()
    finally:
        db.close()


def _open_review(db, document, version, *, author=None, state="open"):
    from app.models import DocumentReview

    review = DocumentReview(
        org_id=document.org_id, document_id=document.id, document_version_id=version.id,
        state=state, reason="the salary is wrong", requested_by=author or "someone",
        authored_by=author)
    db.add(review)
    db.flush()
    return review


def _open_task(db, document, version, *, status="open"):
    from app.models import ReviewTask

    task = ReviewTask(
        org_id=document.org_id, project_id=document.project_id, unit_id="u1",
        kind="calculation", question="what is the pro-rata?", context={},
        status=status, document_version_id=version.id)
    db.add(task)
    db.flush()
    return task


# ------------------------------------------------------------- the plain cases

def test_a_document_with_nothing_outstanding_is_a_draft(doc):
    db, document, version, _user = doc
    assert derive_status(db, version=version, document=document) == (DRAFT, None)


def test_an_open_task_means_pending_review(doc):
    db, document, version, _user = doc
    _open_task(db, document, version)
    status, reason = derive_status(db, version=version, document=document)
    assert status == PENDING_REVIEW
    assert "could not resolve" in reason


def test_a_settled_task_no_longer_holds_the_document(doc):
    """The bug that made `pending_review` permanent.

    Resolving the last task flipped `qa_passed` on the generation record and
    stopped. Nothing read it back, so the letter stayed in the queue with
    nothing left in it that could ever clear it.
    """
    db, document, version, _user = doc
    task = _open_task(db, document, version)
    assert refresh_status(db, version=version, document=document) == PENDING_REVIEW

    task.status = "resolved"
    db.flush()
    assert refresh_status(db, version=version, document=document) == DRAFT
    assert version.status_reason is None


def test_tasks_are_counted_per_version_not_per_document(doc):
    """An edit does not inherit the questions asked about the text it replaced."""
    from app.models import DocumentVersion

    db, document, version, user = doc
    _open_task(db, document, version)
    v2 = DocumentVersion(document_id=document.id, org_id=document.org_id, version_no=2,
                         blob_path="y.docx", status=DRAFT, created_by=user.id)
    db.add(v2)
    db.flush()

    assert derive_status(db, version=version, document=document)[0] == PENDING_REVIEW
    assert derive_status(db, version=v2, document=document)[0] == DRAFT


# ------------------------------------------------------------------- objection

def test_an_open_review_means_changes_requested(doc):
    db, document, version, _user = doc
    _open_review(db, document, version)
    status, reason = derive_status(db, version=version, document=document)
    assert status == CHANGES_REQUESTED
    assert reason == "the salary is wrong"


def test_a_rejected_review_still_means_changes_requested(doc):
    """`rejected` is the review's word; the document's is `changes_requested`.

    The affordance after a rejection is edit-and-resubmit, not a dead end.
    """
    db, document, version, _user = doc
    review = _open_review(db, document, version, state="rejected")
    review.resolution_note = "use the 2026 band"
    db.flush()
    assert derive_status(db, version=version, document=document) == (
        CHANGES_REQUESTED, "use the 2026 band")


@pytest.mark.parametrize("state", ["approved", "withdrawn"])
def test_a_closed_review_releases_the_document(doc, state):
    db, document, version, _user = doc
    review = _open_review(db, document, version)
    assert refresh_status(db, version=version, document=document) == CHANGES_REQUESTED
    review.state = state
    db.flush()
    assert refresh_status(db, version=version, document=document) == DRAFT


def test_only_the_latest_review_decides(doc):
    """Two reviews, the newer one closed: the document is free.

    Reading any-open-review instead would mean an old withdrawn objection kept a
    document hostage forever.
    """
    from datetime import datetime, timedelta, timezone

    db, document, version, _user = doc
    old = _open_review(db, document, version, state="approved")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    newer = _open_review(db, document, version, state="approved")
    newer.created_at = datetime.now(timezone.utc)
    db.flush()
    assert derive_status(db, version=version, document=document)[0] == DRAFT


# ------------------------------------------------------------------ precedence

def test_defective_outranks_disputed(doc):
    """A QA failure and an open review at once reports the QA failure.

    The thing to tell somebody is the one they have to fix first, and a document
    that fails its checks is broken before it is argued about.
    """
    db, document, version, _user = doc
    version.status = BLOCKED
    version.status_reason = "an unresolved required field"
    _open_review(db, document, version)
    _open_task(db, document, version)
    assert derive_status(db, version=version, document=document) == (
        BLOCKED, "an unresolved required field")


def test_blocked_with_no_recorded_reason_still_says_something(doc):
    db, document, version, _user = doc
    version.status = BLOCKED
    version.status_reason = None
    status, reason = derive_status(db, version=version, document=document)
    assert status == BLOCKED and "QA" in reason


def test_a_person_outranks_the_engine(doc):
    """An objection and an open question at once reports the objection.

    Somebody is waiting on the objection; the engine is not waiting on anything.
    """
    db, document, version, _user = doc
    _open_review(db, document, version)
    _open_task(db, document, version)
    assert derive_status(db, version=version, document=document)[0] == CHANGES_REQUESTED


def test_an_open_review_with_no_words_in_it_still_explains_itself(doc):
    db, document, version, _user = doc
    review = _open_review(db, document, version)
    review.reason = None
    db.flush()
    _status, reason = derive_status(db, version=version, document=document)
    assert reason and "changes" in reason


# ----------------------------------------------------------- what it will not do

def test_an_approval_is_never_withdrawn_by_a_recomputation(doc):
    """`approved` is a person's act, not a computation.

    An open review on an approved document does *not* silently unsign it. The
    only thing that withdraws a signature is somebody withdrawing it.
    """
    db, document, version, _user = doc
    version.status = APPROVED
    document.status = APPROVED
    _open_review(db, document, version)

    assert refresh_status(db, version=version, document=document) == APPROVED
    assert version.status == APPROVED and document.status == APPROVED


def test_an_approval_is_never_invented(doc):
    """Nothing derives its way to `approved`."""
    db, document, version, _user = doc
    assert derive_status(db, version=version, document=document)[0] != APPROVED


def test_refresh_writes_both_rows(doc):
    """`StageDocuments` and the delete guard read the document; the editor reads
    the version. They have to agree."""
    db, document, version, _user = doc
    _open_review(db, document, version)
    refresh_status(db, version=version, document=document)
    assert version.status == document.status == CHANGES_REQUESTED
    assert version.status_reason == "the salary is wrong"


def test_refresh_survives_a_document_that_is_not_there(doc):
    """A version whose parent has already been swept still recomputes."""
    db, document, version, _user = doc
    assert refresh_status(db, version=version, document=None) == DRAFT


def test_refresh_is_idempotent(doc):
    db, document, version, _user = doc
    _open_task(db, document, version)
    first = refresh_status(db, version=version, document=document)
    assert refresh_status(db, version=version, document=document) == first
