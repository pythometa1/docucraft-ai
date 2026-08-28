"""Download grants: short-lived, single-use, and on the record.

§16 asks for downloads "served through short-lived, single-use signed URLs that
are themselves audited". The endpoints served files straight off a bearer GET,
so a link was permanent, replayable, and left no trace that a salary letter had
ever been fetched — the one event a leak investigation or a data-subject access
request actually needs.
"""

import os

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import SessionLocal
from app.downloads import GRANT_TTL_SECONDS, issue, redeem
from app.models import AuditLog, DocumentVersion, GeneratedDocument, Project, User
from app.storage import abs_path


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def a_document(app_client, two_orgs):
    """A stored document version with a real file behind it."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))

        rel = f"generated/{project.id}/download-test.docx"
        os.makedirs(str(abs_path(rel).parent), exist_ok=True)
        abs_path(rel).write_bytes(b"PK\x03\x04 not really a docx, but a real file")

        doc = GeneratedDocument(
            org_id=project.org_id, project_id=project.id, display_id=71001, language="en",
        )
        db.add(doc)
        db.flush()
        version = DocumentVersion(
            document_id=doc.id, org_id=doc.org_id, version_no=1, blob_path=rel,
            status="draft", created_by=user.id,
        )
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        db.commit()
        return version.id, doc.org_id, user.id
    finally:
        db.close()


def _audit_count(org_id: str, event_contains: str) -> int:
    db = SessionLocal()
    try:
        rows = db.scalars(select(AuditLog).where(AuditLog.org_id == org_id)).all()
        return sum(1 for r in rows if event_contains.lower() in (r.event or "").lower())
    finally:
        db.close()


# --------------------------------------------------------------- the grant

def test_a_grant_is_single_use(a_document):
    """The property that stops a leaked link being a standing key."""
    version_id, org_id, user_id = a_document
    grant = issue(version_id=version_id, org_id=org_id, user_id=user_id)

    first = redeem(grant.token)
    assert first.version_id == version_id
    assert first.org_id == org_id

    with pytest.raises(HTTPException) as caught:
        redeem(grant.token)
    assert caught.value.status_code == 410
    assert caught.value.detail["error"]["code"] == "DOWNLOAD_LINK_EXPIRED"


def test_an_unknown_token_is_refused():
    with pytest.raises(HTTPException) as caught:
        redeem("not-a-real-token")
    assert caught.value.status_code == 410


def test_grants_are_unguessable_and_distinct(a_document):
    version_id, org_id, user_id = a_document
    tokens = {issue(version_id=version_id, org_id=org_id, user_id=user_id).token for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 32 for t in tokens)


def test_the_grant_is_short_lived():
    """Long enough to follow a redirect, short enough that a link in a chat log
    is dead by the time anyone reads it."""
    assert 0 < GRANT_TTL_SECONDS <= 300


# ------------------------------------------------------------ over the API

def test_requesting_a_link_then_following_it_returns_the_file(app_client, two_orgs, a_document):
    token_a, *_ = two_orgs
    version_id, _org_id, _user_id = a_document

    minted = app_client.post(f"/api/v1/document-versions/{version_id}/download-url", headers=_auth(token_a))
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["expires_in"] == GRANT_TTL_SECONDS
    assert body["filename"].endswith(".docx")

    # Followed with no bearer token at all: the grant is the authorisation.
    fetched = app_client.get(body["url"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.content.startswith(b"PK\x03\x04")


def test_following_the_same_link_twice_fails(app_client, two_orgs, a_document):
    token_a, *_ = two_orgs
    version_id, *_ = a_document

    url = app_client.post(
        f"/api/v1/document-versions/{version_id}/download-url", headers=_auth(token_a)
    ).json()["url"]

    assert app_client.get(url).status_code == 200
    replayed = app_client.get(url)
    assert replayed.status_code == 410
    assert replayed.json()["detail"]["error"]["code"] == "DOWNLOAD_LINK_EXPIRED"


def test_another_tenant_cannot_mint_a_link(app_client, two_orgs, a_document):
    _token_a, _project_a, token_b, _project_b = two_orgs
    version_id, *_ = a_document

    res = app_client.post(f"/api/v1/document-versions/{version_id}/download-url", headers=_auth(token_b))
    assert res.status_code == 404, "a cross-tenant probe must not learn the document exists"


# ------------------------------------------------------------------ audit

def test_minting_a_link_is_audited(app_client, two_orgs, a_document):
    token_a, *_ = two_orgs
    version_id, org_id, _user_id = a_document

    before = _audit_count(org_id, "download link")
    app_client.post(f"/api/v1/document-versions/{version_id}/download-url", headers=_auth(token_a))
    assert _audit_count(org_id, "download link") == before + 1


def test_the_transfer_itself_is_audited(app_client, two_orgs, a_document):
    """Minting and fetching are separate events. A link that is requested and
    never followed is a different fact from a document that left."""
    token_a, *_ = two_orgs
    version_id, org_id, _user_id = a_document

    url = app_client.post(
        f"/api/v1/document-versions/{version_id}/download-url", headers=_auth(token_a)
    ).json()["url"]

    before = _audit_count(org_id, "downloaded a document")
    app_client.get(url)
    assert _audit_count(org_id, "downloaded a document") == before + 1


def test_the_direct_authenticated_download_is_also_audited(app_client, two_orgs, a_document):
    """The §16 gap was never that this route existed -- it was that a fetch left
    no trace. The frontend fetches with a bearer token rather than navigating,
    so removing the route would break every download in the app."""
    token_a, *_ = two_orgs
    version_id, org_id, _user_id = a_document

    before = _audit_count(org_id, "downloaded a document")
    res = app_client.get(f"/api/v1/document-versions/{version_id}/download", headers=_auth(token_a))
    assert res.status_code == 200
    assert _audit_count(org_id, "downloaded a document") == before + 1
