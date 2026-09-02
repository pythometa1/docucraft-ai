"""Somebody read the letter and was not happy with it.

The product could generate a document, approve one and delete one; it had no way
to say "this is wrong". These tests are the rules that keep the resulting queue
from being decorative -- a rejection carries a reason, the author is not the
judge, and an objection cannot be signed straight past -- plus the three defects
in `:approve` and `:revoke` that made the approval state machine unsound.
"""

import pytest

from app.db import SessionLocal
from app.models import (
    DocumentReview, DocumentVersion, GeneratedDocument, ManifestGeneration, Project,
    ReviewComment, ReviewTask, User,
)
from app.security import create_access_token, hash_password

API = "/api/v1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def party(two_orgs, app_client):
    """One document plus three people in org A with different roles.

    `author` wrote the version, `reviewer` may close reviews, `generator` may do
    neither -- which is what the two capability tests below need.
    """
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    made = {}
    try:
        project = db.get(Project, project_a)
        org = project.org_id
        # `author` holds the reviewer permission deliberately: the rule under
        # test is that they may not close the review of *their own* text, and a
        # missing capability would refuse them first and prove nothing.
        for tag, role in (("author", "approver"), ("reviewer", "approver"),
                          ("generator", "generator")):
            email = f"{tag}-{project.id[:6]}@review.test"
            user = db.query(User).filter(User.email == email).one_or_none()
            if user is None:
                user = User(org_id=org, email=email, full_name=f"{tag.title()} Person",
                            password_hash=hash_password("pw"), role_key=role)
                db.add(user)
                db.flush()
            made[tag] = (user.id, create_access_token(user.id, org))

        document = GeneratedDocument(org_id=org, project_id=project.id, display_id=96001,
                                     language="en", status="draft")
        db.add(document)
        db.flush()
        version = DocumentVersion(document_id=document.id, org_id=org, version_no=1,
                                  blob_path="x.docx", status="draft",
                                  created_by=made["author"][0])
        db.add(version)
        db.flush()
        document.current_version_id = version.id
        db.commit()
        ids = {
            "org_id": org, "project_id": project.id,
            "document_id": document.id, "version_id": version.id,
            **{f"{tag}_id": v[0] for tag, v in made.items()},
            **{f"{tag}_token": v[1] for tag, v in made.items()},
        }
    finally:
        db.close()
    yield ids
    db = SessionLocal()
    try:
        # Children before parents, and every version rather than the one this
        # fixture made. Two tests mint successors -- the text editor writes a new
        # version by design -- and one adds a predecessor, so deleting only
        # `ids["version_id"]` left rows behind. On SQLite that was invisible;
        # PostgreSQL enforces the foreign keys and refused the delete.
        version_ids = [
            v.id for v in db.query(DocumentVersion).filter(
                DocumentVersion.document_id == ids["document_id"]).all()
        ]
        review_ids = [
            r.id for r in db.query(DocumentReview).filter(
                DocumentReview.document_id == ids["document_id"]).all()
        ]
        if review_ids:
            db.query(ReviewComment).filter(
                ReviewComment.review_id.in_(review_ids)).delete(synchronize_session=False)
            db.query(DocumentReview).filter(
                DocumentReview.id.in_(review_ids)).delete(synchronize_session=False)
        if version_ids:
            db.query(ReviewTask).filter(
                ReviewTask.document_version_id.in_(version_ids)).delete(
                    synchronize_session=False)
            db.query(ManifestGeneration).filter(
                ManifestGeneration.document_version_id.in_(version_ids)).delete(
                    synchronize_session=False)
        # This session is created with autoflush=False, so ORM deletes queued
        # above would otherwise still be pending when the bulk delete below runs.
        db.flush()
        db.query(DocumentVersion).filter(
            DocumentVersion.document_id == ids["document_id"]).delete(
                synchronize_session=False)
        db.query(GeneratedDocument).filter(
            GeneratedDocument.id == ids["document_id"]).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _open(app_client, ids, *, as_="reviewer", reason="the salary is wrong"):
    return app_client.post(
        f"{API}/document-versions/{ids['version_id']}/reviews",
        json={"reason": reason}, headers=_auth(ids[f"{as_}_token"]))


# ------------------------------------------------------------------- opening it

def test_anyone_can_say_a_document_is_wrong(app_client, party):
    """Noticing a mistake is not a privilege. Closing the review is."""
    response = _open(app_client, party, as_="generator")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "open"
    assert body["reason"] == "the salary is wrong"
    assert body["comments"] == []


def test_the_document_says_changes_were_requested(app_client, party):
    _open(app_client, party)
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "changes_requested"


def test_an_objection_needs_words(app_client, party):
    response = _open(app_client, party, reason="   ")
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "REASON_REQUIRED"


def test_a_second_open_review_is_refused(app_client, party):
    """Two open reviews mean two people closing one document independently."""
    first = _open(app_client, party)
    second = _open(app_client, party, as_="generator")
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["details"]["review_id"] == first.json()["id"]


def test_a_closed_review_leaves_room_for_another(app_client, party):
    first = _open(app_client, party).json()
    app_client.post(f"{API}/reviews/{first['id']}:reject", json={"note": "fix the band"},
                    headers=_auth(party["reviewer_token"]))
    assert _open(app_client, party).status_code == 201


def test_authorship_is_frozen_at_open_time(app_client, party):
    """So a later edit cannot launder who wrote it."""
    body = _open(app_client, party).json()
    assert body["authored_by"] == party["author_id"]


def test_request_changes_is_the_same_thing_from_the_list(app_client, party):
    response = app_client.post(
        f"{API}/document-versions/{party['version_id']}:request-changes",
        json={"reason": "wrong start date"}, headers=_auth(party["generator_token"]))
    assert response.status_code == 201
    assert response.json()["reason"] == "wrong start date"
    # Not a second mechanism: it produces a row the other endpoint can see.
    listed = app_client.get(f"{API}/document-versions/{party['version_id']}/reviews",
                            headers=_auth(party["reviewer_token"])).json()
    assert [r["id"] for r in listed["items"]] == [response.json()["id"]]


# ---------------------------------------------------------------- talking about it

def test_a_comment_can_be_pinned_to_a_run(app_client, party):
    """`(paragraph_index, span_index)` is the coordinate everything else speaks."""
    review = _open(app_client, party).json()
    response = app_client.post(
        f"{API}/reviews/{review['id']}/comments",
        json={"body": "this figure", "paragraph_index": 12, "span_index": 3,
              "quoted_text": "GBP 48,000"},
        headers=_auth(party["author_token"]))
    assert response.status_code == 201
    body = response.json()
    assert (body["paragraph_index"], body["span_index"]) == (12, 3)
    # What the run said when the remark was written: "this figure is wrong" is
    # worthless once somebody has changed the figure.
    assert body["quoted_text"] == "GBP 48,000"
    assert body["author_name"] == "Author Person"


def test_an_empty_comment_is_refused(app_client, party):
    review = _open(app_client, party).json()
    response = app_client.post(f"{API}/reviews/{review['id']}/comments",
                               json={"body": "  "}, headers=_auth(party["author_token"]))
    assert response.status_code == 400


def test_comments_come_back_with_the_review(app_client, party):
    review = _open(app_client, party).json()
    for text in ("first", "second"):
        app_client.post(f"{API}/reviews/{review['id']}/comments", json={"body": text},
                        headers=_auth(party["author_token"]))
    fetched = app_client.get(f"{API}/reviews/{review['id']}",
                             headers=_auth(party["reviewer_token"])).json()
    assert [c["body"] for c in fetched["comments"]] == ["first", "second"]
    assert fetched["document"]["status"] == "changes_requested"


def test_a_comment_can_be_marked_dealt_with(app_client, party):
    review = _open(app_client, party).json()
    comment = app_client.post(f"{API}/reviews/{review['id']}/comments",
                              json={"body": "fix this"},
                              headers=_auth(party["author_token"])).json()
    response = app_client.post(
        f"{API}/reviews/{review['id']}/comments/{comment['id']}:resolve",
        headers=_auth(party["reviewer_token"]))
    assert response.status_code == 200 and response.json()["resolved_at"]


def test_a_comment_from_another_review_is_not_found(app_client, party):
    review = _open(app_client, party).json()
    response = app_client.post(
        f"{API}/reviews/{review['id']}/comments/no-such-comment:resolve",
        headers=_auth(party["reviewer_token"]))
    assert response.status_code == 404


# ------------------------------------------------------------------- closing it

def test_the_author_cannot_close_the_review_of_their_own_text(app_client, party):
    """The separation `check_manifest_approval` makes for a manifest, made per
    letter -- where it matters more, because it happens per letter."""
    review = _open(app_client, party).json()
    response = app_client.post(f"{API}/reviews/{review['id']}:approve", json={},
                               headers=_auth(party["author_token"]))
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "SELF_REVIEW_REFUSED"


def test_the_disabled_button_can_explain_itself(app_client, party):
    """`cannot_resolve_reason` so a reviewer learns the rule before typing, not
    as a 403 afterwards."""
    review = _open(app_client, party).json()
    as_author = app_client.get(f"{API}/reviews/{review['id']}",
                               headers=_auth(party["author_token"])).json()
    assert as_author["can_resolve"] is False
    assert "wrote the version" in as_author["cannot_resolve_reason"]

    as_reviewer = app_client.get(f"{API}/reviews/{review['id']}",
                                 headers=_auth(party["reviewer_token"])).json()
    assert as_reviewer["can_resolve"] is True
    assert as_reviewer["cannot_resolve_reason"] is None


def test_without_the_permission_the_reason_says_so(app_client, party):
    review = _open(app_client, party).json()
    seen = app_client.get(f"{API}/reviews/{review['id']}",
                          headers=_auth(party["generator_token"])).json()
    assert seen["can_resolve"] is False
    assert "reviewer permission" in seen["cannot_resolve_reason"]


def test_closing_needs_the_reviewer_permission(app_client, party):
    review = _open(app_client, party).json()
    response = app_client.post(f"{API}/reviews/{review['id']}:approve", json={},
                               headers=_auth(party["generator_token"]))
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "CAPABILITY_REQUIRED"


def test_approving_the_review_releases_the_document(app_client, party):
    """"The objection does not stand" -- the letter goes back to being a draft."""
    review = _open(app_client, party).json()
    closed = app_client.post(f"{API}/reviews/{review['id']}:approve",
                             json={"note": "checked against the band, it is right"},
                             headers=_auth(party["reviewer_token"]))
    assert closed.status_code == 200
    assert closed.json()["state"] == "approved"
    assert closed.json()["resolved_by_name"] == "Reviewer Person"

    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "draft"


def test_rejecting_keeps_the_document_out_of_signature(app_client, party):
    review = _open(app_client, party).json()
    closed = app_client.post(f"{API}/reviews/{review['id']}:reject",
                             json={"note": "use the 2026 band"},
                             headers=_auth(party["reviewer_token"])).json()
    assert closed["state"] == "rejected"
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "changes_requested"
    assert seen["status_reason"] == "use the 2026 band"


def test_a_rejection_carries_a_reason(app_client, party):
    """A rejection with no note is the outcome of a button with no required text,
    and it costs the next person a conversation."""
    review = _open(app_client, party).json()
    response = app_client.post(f"{API}/reviews/{review['id']}:reject", json={"note": " "},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "NOTE_REQUIRED"


def test_a_review_is_only_closed_once(app_client, party):
    review = _open(app_client, party).json()
    app_client.post(f"{API}/reviews/{review['id']}:approve", json={},
                    headers=_auth(party["reviewer_token"]))
    again = app_client.post(f"{API}/reviews/{review['id']}:reject", json={"note": "no"},
                            headers=_auth(party["reviewer_token"]))
    assert again.status_code == 409


def test_only_the_person_who_raised_it_can_withdraw_it(app_client, party):
    review = _open(app_client, party, as_="generator").json()
    refused = app_client.post(f"{API}/reviews/{review['id']}:withdraw",
                              headers=_auth(party["reviewer_token"]))
    assert refused.status_code == 403

    withdrawn = app_client.post(f"{API}/reviews/{review['id']}:withdraw",
                                headers=_auth(party["generator_token"]))
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn"
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "draft"


def test_a_withdrawn_review_cannot_be_withdrawn_again(app_client, party):
    review = _open(app_client, party, as_="generator").json()
    app_client.post(f"{API}/reviews/{review['id']}:withdraw",
                    headers=_auth(party["generator_token"]))
    again = app_client.post(f"{API}/reviews/{review['id']}:withdraw",
                            headers=_auth(party["generator_token"]))
    assert again.status_code == 409


def test_a_review_can_be_put_on_somebody(app_client, party):
    review = _open(app_client, party).json()
    assigned = app_client.post(f"{API}/reviews/{review['id']}:assign",
                               json={"user_id": party["reviewer_id"]},
                               headers=_auth(party["reviewer_token"])).json()
    assert assigned["assigned_to_name"] == "Reviewer Person"

    stranger = app_client.post(f"{API}/reviews/{review['id']}:assign",
                               json={"user_id": "nobody"},
                               headers=_auth(party["reviewer_token"]))
    assert stranger.status_code == 404


# -------------------------------------------------------- what it protects

def test_a_disputed_document_cannot_be_approved(app_client, party):
    """The whole point. An objection that an approval can step over is a note
    somebody can sign straight past."""
    _open(app_client, party)
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "CHANGES_REQUESTED"


def test_an_objection_raised_while_the_page_was_open_still_stops_it(app_client, party):
    """`:approve` recomputes rather than reading the row it was handed.

    A review opened after the document list was rendered has not touched
    `dv.status` unless something called refresh -- so approving on the stale
    string is exactly the race this guards.
    """
    db = SessionLocal()
    try:
        review = DocumentReview(
            org_id=party["org_id"], document_id=party["document_id"],
            document_version_id=party["version_id"], state="open",
            reason="raised behind your back", requested_by=party["generator_id"],
            authored_by=party["author_id"])
        db.add(review)
        version = db.get(DocumentVersion, party["version_id"])
        version.status = "draft"  # as the client last saw it
        db.commit()
    finally:
        db.close()

    response = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 409


def test_a_document_with_open_questions_cannot_be_approved(app_client, party):
    """Approving over an unanswered question records a human decision nobody made."""
    db = SessionLocal()
    try:
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="u1", kind="calculation", question="pro-rata?",
                          context={}, status="open",
                          document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "DOCUMENT_PENDING_REVIEW"


def test_approving_needs_the_capability(app_client, party):
    """Live behaviour change: `APPROVE_DOCUMENT` was declared and enforced nowhere,
    so any member of the org could sign any document in it."""
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                               headers=_auth(party["generator_token"]))
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["details"]["required_capability"] == (
        "approve_document")


def test_a_clean_document_still_approves(app_client, party):
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 200 and response.json()["status"] == "approved"


# ----------------------------------------------------------------- withdrawal

def test_a_revoke_says_why(app_client, party):
    app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                    headers=_auth(party["reviewer_token"]))
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:revoke",
                               json={"reason": "  "},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 400


def test_there_is_nothing_to_revoke_on_an_unsigned_document(app_client, party):
    response = app_client.post(f"{API}/document-versions/{party['version_id']}:revoke",
                               json={"reason": "changed my mind"},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 409


def test_a_revoke_no_longer_launders_a_defective_document(app_client, party):
    """The bug: `:revoke` wrote `"draft"` unconditionally.

    A document with an open review came back from a revoke looking clean, and
    `:approve` would then wave it through -- because the only thing it refused
    was the string the revoke had just overwritten.
    """
    app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                    headers=_auth(party["reviewer_token"]))
    _open(app_client, party, as_="generator")

    revoked = app_client.post(f"{API}/document-versions/{party['version_id']}:revoke",
                              json={"reason": "the band changed"},
                              headers=_auth(party["reviewer_token"]))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "changes_requested"

    again = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                            headers=_auth(party["reviewer_token"]))
    assert again.status_code == 409


def test_a_revoke_writes_an_audit_row(app_client, party):
    """Granting an approval was traceable; withdrawing one was not."""
    from app.models import AuditLog

    app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                    headers=_auth(party["reviewer_token"]))
    app_client.post(f"{API}/document-versions/{party['version_id']}:revoke",
                    json={"reason": "signed the wrong version"},
                    headers=_auth(party["reviewer_token"]))
    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter(
            AuditLog.entity_id == party["version_id"],
            AuditLog.event == "Withdrew document approval").one()
        assert "signed the wrong version" in row.target
    finally:
        db.close()


# ---------------------------------------------------------------- one queue

def test_the_queue_holds_both_kinds_of_thing(app_client, party):
    """A person objecting and the engine asking land on the same desk, and a
    person with two queues checks one."""
    db = SessionLocal()
    try:
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="u1", kind="narrative", question="ground this",
                          context={}, status="open",
                          document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()
    _open(app_client, party, as_="generator")

    queue = app_client.get(f"{API}/review-queue",
                           headers=_auth(party["reviewer_token"])).json()["items"]
    kinds = {item["kind"] for item in queue}
    assert kinds == {"document_review", "unit_task"}


def test_the_queue_ranks_a_disputed_broken_document_first(app_client, party):
    """Both broken and disputed, and somebody is waiting."""
    _open(app_client, party, as_="generator")
    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, party["version_id"])
        version.status = "blocked"
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="u1", kind="narrative", question="ground this",
                          context={}, status="open",
                          document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()

    queue = app_client.get(f"{API}/review-queue",
                           headers=_auth(party["reviewer_token"])).json()["items"]
    mine = [i for i in queue if i["document_version_id"] == party["version_id"]]
    assert mine[0]["kind"] == "document_review"
    assert mine[0]["priority"] < mine[-1]["priority"]


def test_a_calculation_outranks_a_sentence(app_client, party):
    """A wrong dose calculation is worse than a clumsy paragraph."""
    db = SessionLocal()
    try:
        for kind in ("narrative", "calculation"):
            db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                              unit_id=f"u-{kind}", kind=kind, question=kind, context={},
                              status="open", document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()
    queue = app_client.get(f"{API}/review-queue?project_id=" + party["project_id"],
                           headers=_auth(party["reviewer_token"])).json()["items"]
    order = [i["task_kind"] for i in queue if i["kind"] == "unit_task"]
    assert order.index("calculation") < order.index("narrative")


def test_the_queue_can_be_narrowed_to_me(app_client, party):
    review = _open(app_client, party).json()
    app_client.post(f"{API}/reviews/{review['id']}:assign",
                    json={"user_id": party["reviewer_id"]},
                    headers=_auth(party["reviewer_token"]))
    mine = app_client.get(f"{API}/review-queue?assigned_to=me",
                          headers=_auth(party["reviewer_token"])).json()["items"]
    assert [i["id"] for i in mine if i["kind"] == "document_review"] == [review["id"]]
    theirs = app_client.get(f"{API}/review-queue?assigned_to=me",
                            headers=_auth(party["generator_token"])).json()["items"]
    assert not [i for i in theirs if i["id"] == review["id"]]


def test_the_review_list_filters(app_client, party):
    review = _open(app_client, party).json()
    open_only = app_client.get(f"{API}/reviews?state=open",
                               headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] in [r["id"] for r in open_only]

    app_client.post(f"{API}/reviews/{review['id']}:approve", json={},
                    headers=_auth(party["reviewer_token"]))
    still_open = app_client.get(f"{API}/reviews?state=open",
                                headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] not in [r["id"] for r in still_open]

    by_project = app_client.get(f"{API}/reviews?project_id={party['project_id']}",
                                headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] in [r["id"] for r in by_project]


def test_the_queue_can_show_everything(app_client, party):
    review = _open(app_client, party).json()
    app_client.post(f"{API}/reviews/{review['id']}:approve", json={},
                    headers=_auth(party["reviewer_token"]))
    everything = app_client.get(f"{API}/review-queue?state=all",
                                headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] in [i["id"] for i in everything]


def test_a_review_in_another_tenant_is_not_found(app_client, party, two_orgs):
    _token_a, _project_a, token_b, _project_b = two_orgs
    review = _open(app_client, party).json()
    assert app_client.get(f"{API}/reviews/{review['id']}",
                          headers=_auth(token_b)).status_code == 404


# ------------------------------------------------ the other half of the queue
#
# `ReviewTask` is the engine parking a question it could not answer. These are
# the P0 defects in it: a filter that filtered nothing, a dismiss that stranded
# the document it settled, and a resolution whose resolver was invisible.

@pytest.fixture
def task(party):
    db = SessionLocal()
    try:
        row = ReviewTask(
            org_id=party["org_id"], project_id=party["project_id"], unit_id="pro_rata",
            kind="calculation", question="What is the pro-rata bonus?", context={},
            proposed_value="1200", status="open",
            document_version_id=party["version_id"])
        db.add(row)
        db.commit()
        yield row.id
    finally:
        db.close()


def test_the_queue_filter_actually_filters(app_client, party, task):
    """It did not. The client sends `?status=open` and the handler declared
    `status_`, so FastAPI looked for `?status_=` -- and the "open" and "resolved"
    tabs rendered the same unfiltered list.
    """
    token = _auth(party["reviewer_token"])
    open_ids = [t["id"] for t in app_client.get(
        f"{API}/review-tasks?status=open", headers=token).json()["items"]]
    assert task in open_ids

    resolved_ids = [t["id"] for t in app_client.get(
        f"{API}/review-tasks?status=resolved", headers=token).json()["items"]]
    assert task not in resolved_ids


def test_resolving_a_task_records_who_decided(app_client, party, task):
    """A resolution whose resolver is invisible is half an audit record."""
    response = app_client.post(
        f"{API}/review-tasks/{task}:resolve",
        json={"resolved_value": "1250", "rationale": "confirmed against the plan doc"},
        headers=_auth(party["reviewer_token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["resolved_by_name"] == "Reviewer Person"
    assert body["resolved_value"] == "1250"


def test_a_decision_records_why(app_client, party, task):
    response = app_client.post(f"{API}/review-tasks/{task}:resolve",
                               json={"resolved_value": "1250", "rationale": "  "},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 400


def test_answering_the_last_question_releases_the_document(app_client, party, task):
    """The bug that made `pending_review` permanent.

    Resolving flipped `qa_passed` on the generation record and stopped. Nothing
    read it back to move the document, so a letter that had been fully dealt
    with sat in the queue forever.
    """
    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, party["version_id"])
        version.status = "pending_review"
        db.get(GeneratedDocument, party["document_id"]).status = "pending_review"
        db.commit()
    finally:
        db.close()

    app_client.post(f"{API}/review-tasks/{task}:resolve",
                    json={"resolved_value": "1250", "rationale": "checked"},
                    headers=_auth(party["reviewer_token"]))
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "draft"


def test_one_answered_question_out_of_two_holds_the_document(app_client, party, task):
    db = SessionLocal()
    try:
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="other", kind="condition", question="?", context={},
                          status="open", document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()
    app_client.post(f"{API}/review-tasks/{task}:resolve",
                    json={"resolved_value": "1250", "rationale": "checked"},
                    headers=_auth(party["reviewer_token"]))
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "pending_review"


def test_dismissing_settles_the_document_as_surely_as_answering(app_client, party, task):
    """Dismissing was worse than resolving: it did not even recompute
    `qa_passed`, so it stranded the document with nothing left that could ever
    clear it."""
    response = app_client.post(f"{API}/review-tasks/{task}:dismiss",
                               json={"rationale": "the field is not used in this variant"},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 200 and response.json()["status"] == "dismissed"
    seen = app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "draft"


def test_a_dismissal_says_why(app_client, party, task):
    response = app_client.post(f"{API}/review-tasks/{task}:dismiss",
                               json={"rationale": " "},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 400


def test_a_settled_task_is_not_silently_overwritten(app_client, party, task):
    """No `status != "open"` guard meant an already-resolved task could be
    dismissed and its recorded decision lost."""
    app_client.post(f"{API}/review-tasks/{task}:resolve",
                    json={"resolved_value": "1250", "rationale": "checked"},
                    headers=_auth(party["reviewer_token"]))
    again = app_client.post(f"{API}/review-tasks/{task}:dismiss",
                            json={"rationale": "actually no"},
                            headers=_auth(party["reviewer_token"]))
    assert again.status_code == 409


def test_a_dismissal_is_in_the_audit_record(app_client, party, task):
    from app.models import AuditLog

    app_client.post(f"{API}/review-tasks/{task}:dismiss",
                    json={"rationale": "not applicable here"},
                    headers=_auth(party["reviewer_token"]))
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(
            AuditLog.entity_id == task,
            AuditLog.event == "Dismissed review task").count() == 1
    finally:
        db.close()


def test_the_summary_counts_the_three_outcomes(app_client, party, task):
    body = app_client.get(f"{API}/review-tasks/summary",
                          headers=_auth(party["reviewer_token"])).json()
    assert body["open"] >= 1
    assert body["by_kind"]["calculation"] >= 1


def test_a_task_carries_the_document_it_is_about(app_client, party, task):
    """It never used to. Reaching the document meant joining on a string path,
    and a task whose generation record was swept lost it entirely."""
    body = app_client.get(f"{API}/review-tasks/{task}",
                          headers=_auth(party["reviewer_token"])).json()
    assert body["document_version_id"] == party["version_id"]


# ------------------------------------------------------------ what a role may do

def test_me_says_what_this_role_can_do(app_client, party):
    """So a control the server would refuse can be disabled rather than 403-ing
    after somebody has typed a rejection note.

    Not a security boundary -- every capability is still checked by `require()`.
    This is the same list, sent early enough to be useful.
    """
    reviewer = app_client.get(f"{API}/me", headers=_auth(party["reviewer_token"])).json()
    assert "review_document" in reviewer["capabilities"]
    assert "approve_document" in reviewer["capabilities"]

    generator = app_client.get(f"{API}/me", headers=_auth(party["generator_token"])).json()
    assert "review_document" not in generator["capabilities"]
    assert generator["capabilities"] == ["generate_document"]


def test_the_review_list_can_be_narrowed_to_one_person(app_client, party):
    review = _open(app_client, party).json()
    app_client.post(f"{API}/reviews/{review['id']}:assign",
                    json={"user_id": party["generator_id"]},
                    headers=_auth(party["reviewer_token"]))
    theirs = app_client.get(f"{API}/reviews?assigned_to={party['generator_id']}",
                            headers=_auth(party["reviewer_token"])).json()["items"]
    assert [r["id"] for r in theirs] == [review["id"]]


def test_the_queue_can_be_narrowed_to_one_project(app_client, party):
    """A review whose document belongs to another project drops out.

    Filtered on the *document's* project rather than the review's, because a
    review has no project of its own -- and reading it off the review row would
    mean storing the same fact twice.
    """
    review = _open(app_client, party).json()
    mine = app_client.get(f"{API}/review-queue?project_id={party['project_id']}",
                          headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] in [i["id"] for i in mine]

    elsewhere = app_client.get(f"{API}/review-queue?project_id=no-such-project",
                               headers=_auth(party["reviewer_token"])).json()["items"]
    assert review["id"] not in [i["id"] for i in elsewhere]


# ------------------------------------------- editing must not launder a gate
#
# Both of these were reachable by real HTTP calls before the guards below
# existed, and both walked past a capability check: editing a document's text
# requires nothing but a login, while approving it requires APPROVE_DOCUMENT.

@pytest.fixture
def real_letter(party, tmp_path):
    """A version whose blob is a genuine .docx, so the text editor will run."""
    import docx

    from app.storage import abs_path

    rel = f"generated/{party['project_id']}/edit-probe.docx"
    target = abs_path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    d.add_paragraph("Dear Amelia,")
    d.add_paragraph("Your salary is $82,000.00 per annum.")
    d.save(str(target))

    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, party["version_id"])
        version.blob_path = rel
        version.renderer = "ooxml_fill/1.0"
        db.commit()
    finally:
        db.close()
    return rel


def _edit_the_salary(app_client, party, as_="generator"):
    """One span edit through the real endpoint, returning the new version id."""
    text = app_client.get(f"{API}/document-versions/{party['version_id']}/text",
                          headers=_auth(party[f"{as_}_token"])).json()
    span = next(s for p in text["paragraphs"] for s in p["spans"] if "82,000" in s["text"])
    return app_client.post(
        f"{API}/document-versions/{party['version_id']}/text",
        json={"edits": [{"paragraph_index": span["paragraph_index"],
                         "span_index": span["span_index"],
                         "text": span["text"].replace("82,000.00", "84,000.00")}],
              "change_summary": "corrected the figure"},
        headers=_auth(party[f"{as_}_token"]))


def test_editing_a_qa_blocked_document_does_not_clear_the_qa_verdict(
        app_client, party, real_letter):
    """The laundering path.

    A QA verdict comes from the fill engine, which is the only thing that read
    the finished file. Rewording a sentence does not re-run it -- so a successor
    version that started at "draft" was asserting the document had been fixed
    rather than establishing it, and `:approve` had nothing left to refuse.
    Editing needs no capability; approving needs APPROVE_DOCUMENT.
    """
    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, party["version_id"])
        version.status = "blocked"
        version.status_reason = "leftover {{salary}} scaffolding"
        db.get(GeneratedDocument, party["document_id"]).status = "blocked"
        db.commit()
    finally:
        db.close()

    blocked = app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                              headers=_auth(party["reviewer_token"]))
    assert blocked.status_code == 409

    edited = _edit_the_salary(app_client, party)
    assert edited.status_code == 201, edited.text
    new_version = edited.json()["version_id"]

    seen = app_client.get(f"{API}/document-versions/{new_version}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "blocked"
    assert seen["status_reason"] == "leftover {{salary}} scaffolding"

    still_refused = app_client.post(f"{API}/document-versions/{new_version}:approve",
                                    headers=_auth(party["reviewer_token"]))
    assert still_refused.status_code == 409
    assert still_refused.json()["detail"]["error"]["code"] == "DOCUMENT_BLOCKED"


def test_editing_a_document_does_not_orphan_the_questions_asked_about_it(
        app_client, party, real_letter):
    """A `ReviewTask` asks what a manifest unit should resolve to -- "what is the
    pro-rata bonus?" -- and rewriting the sentence around the figure does not
    answer it. Left pointing at the superseded version they became invisible to
    `derive_status`, which counts per version, and the second gate opened too.
    """
    db = SessionLocal()
    try:
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="pro_rata", kind="calculation", question="pro-rata?",
                          context={}, status="open",
                          document_version_id=party["version_id"]))
        db.commit()
    finally:
        db.close()

    assert app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                           headers=_auth(party["reviewer_token"])).status_code == 409

    new_version = _edit_the_salary(app_client, party).json()["version_id"]

    seen = app_client.get(f"{API}/document-versions/{new_version}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "pending_review"

    refused = app_client.post(f"{API}/document-versions/{new_version}:approve",
                              headers=_auth(party["reviewer_token"]))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "DOCUMENT_PENDING_REVIEW"

    # The task moved with the document rather than being answered by the edit.
    db = SessionLocal()
    try:
        task = db.query(ReviewTask).filter(
            ReviewTask.unit_id == "pro_rata",
            ReviewTask.project_id == party["project_id"]).one()
        assert task.document_version_id == new_version
        assert task.status == "open"
    finally:
        db.close()


def test_an_edit_still_answers_an_objection(app_client, party, real_letter):
    """The one status an edit genuinely does clear.

    `changes_requested` belongs to the text that was objected to. Carrying it
    forward would mean a document could never be fixed -- which is why the guard
    above is scoped to the QA verdict and the open questions, not to everything.
    """
    _open(app_client, party, as_="generator")
    assert app_client.get(f"{API}/document-versions/{party['version_id']}",
                          headers=_auth(party["reviewer_token"])).json()["status"] == (
        "changes_requested")

    new_version = _edit_the_salary(app_client, party).json()["version_id"]
    seen = app_client.get(f"{API}/document-versions/{new_version}",
                          headers=_auth(party["reviewer_token"])).json()
    assert seen["status"] == "draft"
    assert app_client.post(f"{API}/document-versions/{new_version}:approve",
                           headers=_auth(party["reviewer_token"])).status_code == 200


def test_an_approved_version_cannot_be_rewritten_in_place(app_client, party):
    """`PATCH /document-versions/{id}` overwrites the blob at the same path.

    Its sibling `apply_version_text` mints a new version precisely so a signature
    cannot move to text nobody signed; this one had no equivalent guard, needed
    no capability, and left `status`, `approved_by` and `approved_at` untouched
    while changing the figure underneath them.
    """
    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, party["version_id"])
        version.renderer = "html_assembly/1.0"
        version.html_content = "<p>Salary 100000</p>"
        version.blob_path = None
        db.commit()
    finally:
        db.close()

    assert app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                           headers=_auth(party["reviewer_token"])).status_code == 200

    refused = app_client.patch(f"{API}/document-versions/{party['version_id']}",
                               json={"html_content": "<p>Salary 999999</p>"},
                               headers=_auth(party["generator_token"]))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "VERSION_APPROVED"

    db = SessionLocal()
    try:
        assert db.get(DocumentVersion,
                      party["version_id"]).html_content == "<p>Salary 100000</p>"
    finally:
        db.close()


def test_a_review_on_a_superseded_version_does_not_move_the_document(app_client, party):
    """Only the current version speaks for the parent row.

    `DELETE /documents/{id}` refuses only when `GeneratedDocument.status ==
    "approved"`, and opening a review needs no capability -- so raising and
    withdrawing a review on an old version walked the parent from `approved` to
    `draft` and let anyone hard-delete a signed document, every version of it and
    its file, with no revoke ever recorded.
    """
    db = SessionLocal()
    try:
        old = DocumentVersion(document_id=party["document_id"], org_id=party["org_id"],
                              version_no=0, blob_path="old.docx", status="draft",
                              created_by=party["author_id"])
        db.add(old)
        db.flush()
        old_id = old.id
        db.commit()
    finally:
        db.close()

    # The current version is signed; the document row says so.
    assert app_client.post(f"{API}/document-versions/{party['version_id']}:approve",
                           headers=_auth(party["reviewer_token"])).status_code == 200

    opened = app_client.post(f"{API}/document-versions/{old_id}/reviews",
                             json={"reason": "nitpick on the old draft"},
                             headers=_auth(party["generator_token"]))
    assert opened.status_code == 201

    db = SessionLocal()
    try:
        # The objection lands on the version it was made about...
        assert db.get(DocumentVersion, old_id).status == "changes_requested"
        # ...and does not repaint the document, which still speaks for its
        # current, signed version.
        assert db.get(GeneratedDocument, party["document_id"]).status == "approved"
    finally:
        db.close()

    assert app_client.delete(f"{API}/documents/{party['document_id']}",
                             headers=_auth(party["reviewer_token"])).status_code == 409


# ---------------------------------------------------- deleting several at once
#
# Selecting forty letters and deleting them is one gesture on the screen and
# forty independent deletions on the server. These are the rules that make the
# difference safe: refusals are per document and come back as data, and a
# document nobody may delete does not stop the ones they may.

def _make_documents(party, count, *, status="draft", start=95500):
    """`count` documents in the party's project, returning their ids."""
    db = SessionLocal()
    made = []
    try:
        for i in range(count):
            doc = GeneratedDocument(
                org_id=party["org_id"], project_id=party["project_id"],
                display_id=start + i, language="en", status=status)
            db.add(doc)
            db.flush()
            version = DocumentVersion(
                document_id=doc.id, org_id=party["org_id"], version_no=1,
                blob_path=None, status=status, created_by=party["author_id"])
            db.add(version)
            db.flush()
            doc.current_version_id = version.id
            made.append(doc.id)
        db.commit()
    finally:
        db.close()
    return made


def _alive(document_ids):
    db = SessionLocal()
    try:
        return {d.id for d in db.query(GeneratedDocument).filter(
            GeneratedDocument.id.in_(document_ids)).all()}
    finally:
        db.close()


def test_several_documents_go_in_one_request(app_client, party):
    ids = _make_documents(party, 3)
    response = app_client.post(f"{API}/documents:delete", json={"document_ids": ids},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] == 3
    assert {d["document_id"] for d in body["deleted"]} == set(ids)
    assert body["refused"] == []
    assert _alive(ids) == set()


def test_an_approved_document_is_refused_without_stopping_the_others(app_client, party):
    """The rule that makes bulk delete usable.

    Failing the whole request because one selected letter is signed means the
    reviewer clears the checkbox and retries -- or revokes an approval to get the
    button to work, which is the outcome the approval guard exists to prevent.
    """
    keep, *rest = _make_documents(party, 3, start=95600)
    db = SessionLocal()
    try:
        db.get(GeneratedDocument, keep).status = "approved"
        db.commit()
    finally:
        db.close()

    body = app_client.post(f"{API}/documents:delete",
                           json={"document_ids": [keep, *rest]},
                           headers=_auth(party["reviewer_token"])).json()

    assert {d["document_id"] for d in body["deleted"]} == set(rest)
    assert [r["document_id"] for r in body["refused"]] == [keep]
    assert body["refused"][0]["code"] == "DOCUMENT_APPROVED"
    # Named, so the dialog can say which one was left and why.
    assert body["refused"][0]["filename"]
    assert "Withdraw the approval" in body["refused"][0]["reason"]
    assert _alive([keep, *rest]) == {keep}


def test_another_tenants_document_is_neither_deleted_nor_acknowledged(
        app_client, party, two_orgs):
    """Ownership is checked per document, not once for the list. A bulk endpoint
    that trusts the array is how one tenant destroys another's letters."""
    _token_a, _project_a, token_b, _project_b = two_orgs
    ids = _make_documents(party, 2, start=95700)

    body = app_client.post(f"{API}/documents:delete", json={"document_ids": ids},
                           headers=_auth(token_b)).json()

    assert body["deleted"] == []
    # Indistinguishable from an id that never existed, which is what the tenancy
    # rule wants -- a cross-tenant probe learns nothing.
    assert {r["code"] for r in body["refused"]} == {"DOCUMENT_NOT_FOUND"}
    assert _alive(ids) == set(ids)


def test_an_id_that_is_not_a_document_is_reported_rather_than_raising(app_client, party):
    ids = _make_documents(party, 1, start=95800)
    body = app_client.post(f"{API}/documents:delete",
                           json={"document_ids": [*ids, "no-such-document"]},
                           headers=_auth(party["reviewer_token"])).json()
    assert [d["document_id"] for d in body["deleted"]] == ids
    assert [r["document_id"] for r in body["refused"]] == ["no-such-document"]


def test_the_same_document_twice_is_deleted_once(app_client, party):
    """Otherwise the second pass reports "no longer exists" for a document the
    caller selected once, which reads as a failure."""
    ids = _make_documents(party, 1, start=95900)
    body = app_client.post(f"{API}/documents:delete",
                           json={"document_ids": ids + ids},
                           headers=_auth(party["reviewer_token"])).json()
    assert body["requested"] == 1
    assert len(body["deleted"]) == 1 and body["refused"] == []


def test_an_empty_selection_is_refused(app_client, party):
    response = app_client.post(f"{API}/documents:delete", json={"document_ids": []},
                               headers=_auth(party["reviewer_token"]))
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "NO_DOCUMENTS"


def test_too_many_at_once_is_refused_before_anything_is_destroyed(app_client, party):
    ids = _make_documents(party, 2, start=96100)
    response = app_client.post(
        f"{API}/documents:delete",
        json={"document_ids": ids + [f"pad-{i}" for i in range(200)]},
        headers=_auth(party["reviewer_token"]))
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "TOO_MANY_DOCUMENTS"
    assert _alive(ids) == set(ids), "the cap must be checked before any deletion runs"


def test_every_deletion_is_in_the_audit_record_individually(app_client, party):
    """One row per document, the same as the single-document endpoint writes.
    A bulk gesture must not become a single line that hides what it destroyed."""
    from app.models import AuditLog

    ids = _make_documents(party, 3, start=96200)
    app_client.post(f"{API}/documents:delete", json={"document_ids": ids},
                    headers=_auth(party["reviewer_token"]))

    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(
            AuditLog.entity_id.in_(ids),
            AuditLog.event == "Deleted generated document").all()
        assert len(rows) == 3
        assert all("bulk of 3" in (r.target or "") for r in rows)
    finally:
        db.close()


def test_the_cascade_is_the_same_one_a_single_delete_runs(app_client, party):
    """Versions, reviews, comments and parked questions all go -- the bulk path
    calls `delete_generated_document`, it does not reimplement it."""
    ids = _make_documents(party, 1, start=96300)
    db = SessionLocal()
    try:
        doc = db.get(GeneratedDocument, ids[0])
        review = DocumentReview(org_id=party["org_id"], document_id=doc.id,
                                document_version_id=doc.current_version_id,
                                state="open", reason="wrong", requested_by=party["author_id"],
                                authored_by=party["author_id"])
        db.add(review)
        db.flush()
        db.add(ReviewComment(org_id=party["org_id"], review_id=review.id,
                             author_id=party["author_id"], body="here",
                             quoted_text="GBP 48,000"))
        db.add(ReviewTask(org_id=party["org_id"], project_id=party["project_id"],
                          unit_id="u1", kind="calculation", question="?",
                          context={"available": {"salary": "48000"}}, status="open",
                          document_version_id=doc.current_version_id))
        db.commit()
        version_id = doc.current_version_id
    finally:
        db.close()

    app_client.post(f"{API}/documents:delete", json={"document_ids": ids},
                    headers=_auth(party["reviewer_token"]))

    db = SessionLocal()
    try:
        assert db.query(DocumentVersion).filter(DocumentVersion.id == version_id).all() == []
        assert db.query(DocumentReview).filter(
            DocumentReview.document_version_id == version_id).all() == []
        assert db.query(ReviewTask).filter(
            ReviewTask.document_version_id == version_id).all() == []
    finally:
        db.close()


def test_a_document_that_fails_mid_delete_is_not_reported_as_left_alone(
        app_client, party, monkeypatch):
    """It is neither deleted nor untouched, and saying either would be a claim
    the customer acts on.

    `delete_generated_document` unlinks the rendered files before its final
    flush, so a failure after that point leaves rows the rollback restored and
    files it cannot. The honest answer is its own outcome.
    """
    from app.routers import generation as gen

    good, bad = _make_documents(party, 2, start=96400)
    real = gen.delete_generated_document

    def explode(db, *, org_id, document_id):
        if document_id == bad:
            raise OSError(f"[Errno 13] Permission denied: '/srv/documind/storage/{document_id}.docx'")
        return real(db, org_id=org_id, document_id=document_id)

    monkeypatch.setattr(gen, "delete_generated_document", explode)

    body = app_client.post(f"{API}/documents:delete", json={"document_ids": [good, bad]},
                           headers=_auth(party["reviewer_token"])).json()

    # The healthy one still went: one bad document does not strand the rest.
    assert [d["document_id"] for d in body["deleted"]] == [good]
    assert _alive([good]) == set()

    refusal = body["refused"][0]
    assert refusal["document_id"] == bad
    assert refusal["code"] == "DELETE_INCOMPLETE"
    # Not "left alone" -- the file may be gone even though the row came back.
    assert "may already be gone" in refusal["reason"]
    # And the raw exception is not handed to the client: an OS error carries the
    # absolute storage path, a SQLAlchemy one carries SQL.
    assert "/srv/documind" not in refusal["reason"]
    assert "Errno" not in refusal["reason"]

    # Clean up the row the rollback restored.
    db = SessionLocal()
    try:
        db.query(DocumentVersion).filter(DocumentVersion.document_id == bad).delete()
        db.query(GeneratedDocument).filter(GeneratedDocument.id == bad).delete()
        db.commit()
    finally:
        db.close()


def test_a_commit_failure_does_not_discard_the_report_of_what_was_destroyed(
        app_client, party, monkeypatch):
    """The audit row and the commit sit inside the per-document try.

    They used to sit after it, so a commit failure escaped the loop as a 500 --
    throwing away the response that was supposed to name everything already
    destroyed, on the one path where that record matters most.
    """
    from app.routers import generation as gen

    good, bad = _make_documents(party, 2, start=96500)
    real = gen.log_audit

    def explode(db, user, event, entity_type, entity_id=None, *a, **k):
        if entity_id == bad:
            raise RuntimeError("database is locked")
        return real(db, user, event, entity_type, entity_id, *a, **k)

    monkeypatch.setattr(gen, "log_audit", explode)

    response = app_client.post(f"{API}/documents:delete",
                               json={"document_ids": [good, bad]},
                               headers=_auth(party["reviewer_token"]))

    assert response.status_code == 200, "a failure on one document must not 500 the request"
    body = response.json()
    assert [d["document_id"] for d in body["deleted"]] == [good]
    assert body["refused"][0]["document_id"] == bad

    db = SessionLocal()
    try:
        db.query(DocumentVersion).filter(DocumentVersion.document_id == bad).delete()
        db.query(GeneratedDocument).filter(GeneratedDocument.id == bad).delete()
        db.commit()
    finally:
        db.close()
