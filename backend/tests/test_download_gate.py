"""Only an approved document leaves the building.

Downloading is the moment a letter stops being something the product is working
on and becomes something a customer received. Approval is the act that authorises
that, so every path that hands over bytes asks whether it happened -- and there
are five of them, which is the reason this file exists rather than one assertion
next to one endpoint.

Hiding the button is not the gate. `POST /documents:download` takes a JSON array,
`GET .../download` is a plain GET, the grant handler is deliberately
unauthenticated, and the batch zip is reachable with nothing but a job id. Each
one is reachable from a terminal.

What is deliberately *not* gated is reading: the person deciding whether to
approve has to be able to see the document first.
"""

import io
import zipfile

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import DocumentVersion, GeneratedDocument, Project, User
from app.storage import abs_path


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make(project, user, *, display_id, status, name):
    rel = f"generated/{project.id}/{name}.docx"
    abs_path(rel).parent.mkdir(parents=True, exist_ok=True)
    abs_path(rel).write_bytes(b"PK\x03\x04 a real file, not a real docx")
    doc = GeneratedDocument(
        org_id=project.org_id, project_id=project.id, display_id=display_id,
        language="en", status=status)
    version = DocumentVersion(
        document_id=None, org_id=project.org_id, version_no=1, blob_path=rel,
        status=status, created_by=user.id)
    return doc, version, rel


@pytest.fixture()
def pair(app_client, two_orgs):
    """One approved document and one that is not, in the same project."""
    token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))
        made = {}
        for key, status, display_id in (("approved", "approved", 73001), ("draft", "draft", 73002)):
            doc, version, _rel = _make(project, user, display_id=display_id,
                                       status=status, name=f"gate-{key}")
            db.add(doc)
            db.flush()
            version.document_id = doc.id
            db.add(version)
            db.flush()
            doc.current_version_id = version.id
            made[key] = (doc.id, version.id)
        db.commit()
        return token_a, made
    finally:
        db.close()


# ------------------------------------------------------------ the direct download

def test_an_unapproved_document_cannot_be_downloaded(app_client, pair):
    token, made = pair
    _doc_id, version_id = made["draft"]
    res = app_client.get(f"/api/v1/document-versions/{version_id}/download", headers=_auth(token))
    assert res.status_code == 409, res.text
    body = res.json()["detail"]["error"]
    assert body["code"] == "DOCUMENT_NOT_APPROVED"
    assert body["details"]["status"] == "draft"


def test_an_approved_one_still_can(app_client, pair):
    token, made = pair
    _doc_id, version_id = made["approved"]
    res = app_client.get(f"/api/v1/document-versions/{version_id}/download", headers=_auth(token))
    assert res.status_code == 200, res.text


def test_the_format_is_checked_before_the_approval(app_client, pair):
    """An unknown format is still answered as one. Reordering these two turns a
    clear 422 about `rtf` into a 409 about approval, which is a different and
    less useful thing to tell someone."""
    token, made = pair
    _doc_id, version_id = made["draft"]
    res = app_client.get(f"/api/v1/document-versions/{version_id}/download?format=rtf",
                         headers=_auth(token))
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["error"]["code"] == "UNSUPPORTED_FORMAT"


# ------------------------------------------------------------ the grant

def test_an_unapproved_document_mints_no_grant(app_client, pair):
    """Refused before the link exists. A link that exists is a link that can be
    followed, and the handler that follows it has no user to check."""
    token, made = pair
    _doc_id, version_id = made["draft"]
    res = app_client.post(f"/api/v1/document-versions/{version_id}/download-url",
                          headers=_auth(token))
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_NOT_APPROVED"


def test_a_grant_stops_working_when_the_approval_is_withdrawn(app_client, pair):
    """The gate at mint time is not enough on its own.

    "Mint a link, have the approval withdrawn, follow the link" is a real
    sequence, and a single-use token does not make it fewer than one. So the
    approval is re-read on the way out as well."""
    token, made = pair
    doc_id, version_id = made["approved"]

    minted = app_client.post(f"/api/v1/document-versions/{version_id}/download-url",
                             headers=_auth(token))
    assert minted.status_code == 201, minted.text
    url = minted.json()["url"]

    db = SessionLocal()
    try:
        db.get(DocumentVersion, version_id).status = "changes_requested"
        db.get(GeneratedDocument, doc_id).status = "changes_requested"
        db.commit()
    finally:
        db.close()

    res = app_client.get(url)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_NOT_APPROVED"


# ------------------------------------------------------------ the bulk zip

def test_the_bulk_zip_skips_the_unapproved_and_lists_them(app_client, pair):
    """Not refused whole. A reviewer with a hundred and ninety signed letters and
    ten unsigned ones needs the hundred and ninety plus a list -- the same call
    the bulk delete makes one endpoint up."""
    token, made = pair
    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [made["approved"][0], made["draft"][0]], "format": "docx"},
        headers=_auth(token))
    assert res.status_code == 200, res.text

    archive = zipfile.ZipFile(io.BytesIO(res.content))
    names = archive.namelist()
    assert "_FAILED.txt" in names
    assert len(names) == 2, "one document and the note about the other"
    note = archive.read("_FAILED.txt").decode()
    assert "not approved" in note and "draft" in note


def test_a_selection_of_only_unapproved_documents_is_refused_outright(app_client, pair):
    """Which is "refuse the whole request", arrived at by the truth rather than
    by a rule."""
    token, made = pair
    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [made["draft"][0]], "format": "docx"},
        headers=_auth(token))
    assert res.status_code == 422, res.text
    body = res.json()["detail"]["error"]
    assert body["code"] == "NOTHING_TO_DOWNLOAD"
    assert "not approved" in body["message"]


# ------------------------------------------------------------ what stays open

def test_reviewing_a_document_does_not_need_it_approved(app_client, pair):
    """The one that stops the gate being widened by mistake.

    Gating reading on approval would lock the approver out of the thing they are
    deciding about, which is the obvious misreading of this feature and the wrong
    one."""
    token, made = pair
    _doc_id, version_id = made["draft"]
    assert app_client.get(f"/api/v1/document-versions/{version_id}",
                          headers=_auth(token)).status_code == 200


def test_the_documents_list_says_which_ones_can_be_downloaded(app_client, pair, two_orgs):
    """So the screen can disable the control rather than let somebody discover
    the refusal by pressing it."""
    token, made = pair
    _token_a, project_a, *_ = two_orgs
    res = app_client.get(f"/api/v1/projects/{project_a}/documents", headers=_auth(token))
    assert res.status_code == 200, res.text
    by_id = {d["id"]: d for d in res.json()["items"]}
    assert by_id[made["approved"][0]]["downloadable"] is True
    assert by_id[made["draft"][0]]["downloadable"] is False
