"""What state a document is in, worked out from the facts rather than assumed.

Two bugs made this module necessary, and they are the same bug twice.

`:revoke` set `status = "draft"` unconditionally. A document that failed QA, or
that still had unanswered review tasks, came back from a revoke looking clean --
and `:approve` would then wave it through, because the only thing it refused was
the `blocked` string that had just been overwritten.

And nothing ever moved a document *out* of `pending_review`. Resolving the last
task flipped `ManifestGeneration.qa_passed` and stopped; dismissing did not even
do that. So a document that had been fully dealt with sat in the queue forever,
with nothing left in it that could ever clear it.

Both are the same mistake: writing a status from one place's local knowledge
instead of deriving it from everything that is true. `derive_status` looks at all
of it, in one place, and every caller that changes any of the underlying facts
calls `refresh_status` afterwards.

**`approved` is never derived.** It is a person's act, not a computation, so this
refuses to invent it and refuses to take it away. A caller that wants an approval
left alone checks `approved_at` first -- which is exactly what `:revoke` does
*not* do, because withdrawing the signature is its whole job.
"""

from sqlalchemy import select

#: Worst first. The first one that applies wins, because a document that fails QA
#: *and* has an open review is defective before it is disputed, and the thing to
#: tell someone is the one they have to fix first.
DRAFT = "draft"
PENDING_REVIEW = "pending_review"
CHANGES_REQUESTED = "changes_requested"
BLOCKED = "blocked"
APPROVED = "approved"

#: States that mean a person has already signed. `refresh_status` leaves these
#: alone; only an explicit revoke moves a document out of one.
SIGNED = frozenset({APPROVED})

#: States whose bytes may leave the building.
#:
#: `"final"` is in here because `save_document_version` and the revoke endpoint
#: have always tested for the pair, and a third and fourth hand-written copy of
#: it is how the four drift apart. It lives in this module rather than in the
#: router because the unauthenticated download-grant handler needs it too, and
#: that module deliberately imports from no router.
DOWNLOADABLE = frozenset({APPROVED, "final"})


def derive_status(db, *, version, document) -> tuple:
    """`(status, reason)` this version should be in, from what is actually true.

    Not called for an approved version -- see the module docstring.
    """
    from app.models import DocumentReview, ReviewTask

    # 1. QA said the document is defective. That verdict comes from the fill
    #    engine, which is the only thing that read the finished file, so it is
    #    preserved rather than recomputed here -- recomputing it would mean
    #    re-deriving the QA policy from the manifest, and a second opinion about
    #    whether a document is broken is not an improvement.
    if version.status == BLOCKED:
        return BLOCKED, version.status_reason or (
            "This document failed its QA checks when it was generated.")

    # 2. A person has objected. An open review and a rejected one both mean the
    #    document is not to be signed: open because nobody has finished looking,
    #    rejected because somebody did and said no.
    review = db.scalars(
        select(DocumentReview)
        .where(DocumentReview.document_version_id == version.id)
        .order_by(DocumentReview.created_at.desc())
    ).first()
    if review is not None and review.state in ("open", "rejected"):
        reason = review.resolution_note or review.reason or (
            "Somebody has asked for changes to this document.")
        return CHANGES_REQUESTED, reason

    # 3. The engine has a question nobody has answered. Counted per version, not
    #    per generation, so an edited document does not inherit the questions
    #    asked about the text it replaced.
    outstanding = db.scalars(
        select(ReviewTask).where(
            ReviewTask.document_version_id == version.id,
            ReviewTask.status == "open",
        )
    ).first()
    if outstanding is not None:
        return PENDING_REVIEW, (
            "The engine could not resolve every value in this document on its own.")

    return DRAFT, None


def refresh_status(db, *, version, document) -> str:
    """Recompute and write the status onto both rows. Idempotent.

    The caller owns the commit, like everything else that writes here.

    Writing to both is not redundancy for its own sake: `StageDocuments` and the
    delete guard read `GeneratedDocument.status` without joining versions, and
    the editor reads the version. `batch_runner` already writes the same string
    to both for that reason; this keeps them agreeing after the fact too.

    **Only the current version speaks for the document.** A review or a task can
    be raised against a superseded version -- v1 of a letter whose v2 is signed --
    and writing the parent row from it published a status for text nobody is
    looking at. That was not cosmetic: `DELETE /documents/{id}` refuses only when
    `GeneratedDocument.status == "approved"`, and opening a review needs no
    capability, so raising and withdrawing a review on v1 walked the parent row
    from `approved` to `draft` and let anyone hard-delete a signed document,
    every version of it and its file, with no revoke ever recorded. The version
    row is still updated -- the objection is real and belongs on the version it
    was made about.
    """
    if version.status in SIGNED:
        # An approval is not something a recomputation may quietly withdraw.
        return version.status

    status, reason = derive_status(db, version=version, document=document)
    version.status = status
    version.status_reason = reason
    if document is not None and document.current_version_id in (None, version.id):
        document.status = status
    return status
