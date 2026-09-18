"""Where a person says a document is, layered over what is true of it.

`document_status.derive_status` answers "what state is this document in?" from
facts -- the QA gate, open reviews, questions the engine parked -- worst first,
and it is not negotiable. This answers a different question: "where has somebody
put this in their own process?"

The two must not be able to fight, and they cannot, because each column has
exactly one kind of writer and neither is derived from the other:

  * `GeneratedDocument.status` is written by `refresh_status` and by the approve
    and revoke endpoints. System-driven.
  * `GeneratedDocument.workflow_status` is written by the workflow PATCH, and
    stores only the three states a person can assert. User-driven.

What layers them is `effective`, which is a pure function of both and writes
neither. Teaching `refresh_status` about the workflow column would put back
exactly the bug its own docstring records: a status written from one place's
local knowledge instead of derived from everything that is true.
"""

from app.generation.document_status import APPROVED, BLOCKED

WORK_IN_PROGRESS = "work_in_progress"
COMPLETED = "completed"
CANCELLED = "cancelled"

#: The three a person may assert. `approved` and `blocked` are deliberately
#: absent: they are things that happen to a document, not labels for it.
#: `approved` is a signature -- the approve endpoint records who and when, and a
#: PATCH that could write it would be a signature nobody signed. `blocked` is the
#: fill engine's QA verdict, and a label that could clear it would be a defective
#: letter marked fit by hand.
SETTABLE = (WORK_IN_PROGRESS, COMPLETED, CANCELLED)

#: Everything a reader can see, in the order `effective` resolves them.
ALL = (WORK_IN_PROGRESS, COMPLETED, APPROVED, BLOCKED, CANCELLED)

LABELS = {
    WORK_IN_PROGRESS: "Work in progress",
    COMPLETED: "Completed",
    APPROVED: "Approved",
    BLOCKED: "Blocked",
    CANCELLED: "Cancelled",
}


def effective(document) -> str:
    """What to show, from the system's verdict and the person's intent.

    1. `approved` -- somebody signed it. It outranks everything, including a
       cancellation, because unmaking a signature is the revoke endpoint's job
       and nothing else may do it quietly.
    2. `cancelled` -- the person has said this letter is not going to be used. It
       outranks `blocked` on purpose: a QA verdict on a document nobody will send
       is not the thing to put in front of a reader, and cancelling is the
       ordinary way to answer a document that cannot be fixed.
    3. `blocked` -- the fill engine found it defective. Above the remaining two
       because "completed" written over a QA failure is a claim nobody can act
       on.
    4. Whatever the person set.

    Note what this does *not* do: it never writes. A document marked `completed`
    whose next version fails QA reads `blocked` here and still has `completed`
    stored, so when the block is fixed it reads `completed` again -- nobody
    changed their mind about it, and nothing had to remember to put it back.
    """
    if document.status == APPROVED:
        return APPROVED
    if document.workflow_status == CANCELLED:
        return CANCELLED
    if document.status == BLOCKED:
        return BLOCKED
    return document.workflow_status or WORK_IN_PROGRESS
