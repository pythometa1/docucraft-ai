"""The second axis: where a person says a document is.

`generated_documents.status` is what the engine and the reviewers say about a
letter -- it failed QA, somebody objected, a value could not be worked out.
`workflow_status` is where somebody has put it in their own process. The two are
written by different actors and neither derives from the other, which is the
whole reason this can exist without the QA verdict and the person's answer
overwriting each other in turn.

What these tests protect is the boundary between them: the three states a person
may assert, the two they may not, and the fact that a QA block shows over the top
of somebody's answer without erasing it.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.generation.workflow_status import (
    CANCELLED, COMPLETED, WORK_IN_PROGRESS, effective,
)
from app.models import AuditLog, DocumentVersion, GeneratedDocument, Project, User


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def document(app_client, two_orgs):
    """(token, document_id, version_id) for a freshly generated document."""
    token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))
        doc = GeneratedDocument(
            org_id=project.org_id, project_id=project.id, display_id=72001, language="en")
        db.add(doc)
        db.flush()
        version = DocumentVersion(
            document_id=doc.id, org_id=doc.org_id, version_no=1,
            blob_path=f"generated/{project.id}/workflow.docx",
            status="draft", created_by=user.id)
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        db.commit()
        return token_a, doc.id, version.id
    finally:
        db.close()


def _set(app_client, token, document_id, value):
    return app_client.patch(
        f"/api/v1/documents/{document_id}/workflow",
        json={"workflow_status": value}, headers=_auth(token))


def _mutate(document_id, **fields):
    db = SessionLocal()
    try:
        doc = db.get(GeneratedDocument, document_id)
        for key, value in fields.items():
            setattr(doc, key, value)
        db.commit()
    finally:
        db.close()


def _read(app_client, token, document_id):
    res = app_client.get(f"/api/v1/documents/{document_id}", headers=_auth(token))
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------ the three a person may set

def test_a_new_document_starts_as_work_in_progress(app_client, document):
    """Not derived from anything. Nobody has picked it up, which is the truth
    about every document the moment it is generated."""
    token, document_id, _version_id = document
    assert _read(app_client, token, document_id)["workflow_status"] == WORK_IN_PROGRESS


@pytest.mark.parametrize("value", [COMPLETED, CANCELLED, WORK_IN_PROGRESS])
def test_a_person_can_move_it_through_their_own_lane(app_client, document, value):
    token, document_id, _version_id = document
    res = _set(app_client, token, document_id, value)
    assert res.status_code == 200, res.text
    assert res.json()["workflow_status"] == value


# ------------------------------------------------------------ the two they may not

def test_approved_cannot_be_hand_set(app_client, document):
    """A signature is not a label. Setting it here would record an approval with
    nobody's name on it and no audit row saying when."""
    token, document_id, version_id = document
    res = _set(app_client, token, document_id, "approved")
    assert res.status_code == 409, res.text
    body = res.json()["detail"]["error"]
    assert body["code"] == "APPROVAL_IS_NOT_A_LABEL"
    # And it says where to go instead, rather than only refusing.
    assert version_id in body["details"]["approve_with"]


def test_blocked_cannot_be_hand_set(app_client, document):
    """Blocked is what the QA gate found, so it is not anyone's to apply -- and,
    more to the point, not anyone's to clear."""
    token, document_id, _version_id = document
    res = _set(app_client, token, document_id, "blocked")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "BLOCK_IS_A_VERDICT"


def test_an_unknown_value_is_refused_with_the_list(app_client, document):
    """Named in prose rather than left to a schema error, so the answer to
    "then what may I send?" is in the refusal itself."""
    token, document_id, _version_id = document
    res = _set(app_client, token, document_id, "nearly_done")
    assert res.status_code == 422, res.text
    body = res.json()["detail"]["error"]
    assert body["code"] == "UNKNOWN_WORKFLOW_STATUS"
    assert COMPLETED in body["message"] and CANCELLED in body["message"]


# ------------------------------------------------------------ how the two axes layer

def test_the_qa_verdict_shows_over_what_the_person_set(app_client, document):
    token, document_id, _version_id = document
    assert _set(app_client, token, document_id, COMPLETED).status_code == 200
    _mutate(document_id, status="blocked")
    assert _read(app_client, token, document_id)["workflow_status"] == "blocked"


def test_the_persons_answer_survives_the_block(app_client, document):
    """Shown over, not overwritten. Nobody changed their mind about this
    document, so when the block is fixed it reads `completed` again without
    anything having had to remember to put it back."""
    token, document_id, _version_id = document
    _set(app_client, token, document_id, COMPLETED)
    _mutate(document_id, status="blocked")

    body = _read(app_client, token, document_id)
    assert body["workflow_status"] == "blocked"
    assert body["workflow_status_set"] == COMPLETED, "the dropdown must not lose the setting"

    _mutate(document_id, status="draft")
    assert _read(app_client, token, document_id)["workflow_status"] == COMPLETED


def test_a_signature_outranks_a_cancellation(app_client, document):
    token, document_id, _version_id = document
    _set(app_client, token, document_id, CANCELLED)
    _mutate(document_id, status="approved")
    assert _read(app_client, token, document_id)["workflow_status"] == "approved"


def test_a_cancellation_outranks_a_block(app_client, document):
    """Deliberate. A QA verdict on a letter nobody is going to send is not the
    thing to put in front of a reader."""
    token, document_id, _version_id = document
    _mutate(document_id, status="blocked")
    assert _set(app_client, token, document_id, CANCELLED).status_code == 200
    assert _read(app_client, token, document_id)["workflow_status"] == CANCELLED


# ------------------------------------------------------------ refusals that depend on state

def test_a_blocked_document_cannot_be_marked_completed(app_client, document):
    token, document_id, _version_id = document
    _mutate(document_id, status="blocked")
    res = _set(app_client, token, document_id, COMPLETED)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_BLOCKED"


def test_a_blocked_document_can_still_be_cancelled(app_client, document):
    """The asymmetry above is the point: giving up on a letter that cannot be
    fixed is the ordinary answer to one, and refusing it would leave the reader
    no move at all."""
    token, document_id, _version_id = document
    _mutate(document_id, status="blocked")
    assert _set(app_client, token, document_id, CANCELLED).status_code == 200


def test_an_approved_document_cannot_be_moved(app_client, document):
    token, document_id, _version_id = document
    _mutate(document_id, status="approved")
    res = _set(app_client, token, document_id, COMPLETED)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_APPROVED"


# ------------------------------------------------------------ the invariants underneath

def test_refresh_status_never_writes_the_workflow_column():
    """One writer per column is what stops the two axes fighting. If this fails,
    somebody has taught the system's recompute about the person's answer, and the
    person's answer is about to start disappearing."""
    import inspect

    from app.generation import document_status

    source = inspect.getsource(document_status)
    assert "workflow_status" not in source, (
        "document_status must not know about the workflow column -- see workflow_status.effective")


def test_every_move_is_audited(app_client, document):
    token, document_id, _version_id = document
    _set(app_client, token, document_id, CANCELLED)

    db = SessionLocal()
    try:
        rows = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == document_id,
            AuditLog.event == "Moved a document in the workflow")).all()
        assert len(rows) == 1
        assert rows[0].target == f"{WORK_IN_PROGRESS} -> {CANCELLED}"
        # Cancelling is the destructive-shaped one of the three.
        assert rows[0].severity == "warning"
    finally:
        db.close()


def test_setting_the_same_value_twice_writes_one_audit_row(app_client, document):
    """A no-op is not an event. Recording one makes the trail read as though
    somebody kept changing their mind."""
    token, document_id, _version_id = document
    _set(app_client, token, document_id, COMPLETED)
    _set(app_client, token, document_id, COMPLETED)

    db = SessionLocal()
    try:
        rows = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == document_id,
            AuditLog.event == "Moved a document in the workflow")).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_another_tenant_cannot_move_a_document(app_client, two_orgs, document):
    _token_a, document_id, _version_id = document
    _token_a2, _project_a, token_b, _project_b = two_orgs
    res = _set(app_client, token_b, document_id, COMPLETED)
    assert res.status_code == 404, res.text


def test_effective_reads_a_document_without_writing_to_it():
    """Pure, and this is load-bearing: it is called from `_doc_out`, which runs
    on every list of every project's documents."""
    class _Doc:
        status = "blocked"
        workflow_status = COMPLETED

    doc = _Doc()
    assert effective(doc) == "blocked"
    assert doc.workflow_status == COMPLETED
