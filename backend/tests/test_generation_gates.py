"""The gates on the API surface: what a caller is allowed to do to a document.

Three refusals live here. Only an approved manifest may produce a document; a
document whose layout came from Word is not editable as HTML; and a document
that failed QA is stored as `blocked` rather than filed alongside the clean
ones. Each of these was reachable before -- not through a clever request, just
through the ordinary one.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    DocumentVersion, GeneratedDocument, Organization, Project, TemplateManifest,
    TemplateVersion, User,
)
from app.generation.renderers import DOCX_TEMPLATE_ASSEMBLY, HTML_ASSEMBLY, OOXML_FILL


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def org_context(app_client, two_orgs):
    """(token, org_id, project_id) for the first tenant."""
    token_a, project_a, _token_b, _project_b = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        yield token_a, project.org_id, project_a
    finally:
        db.close()


def _document_version(org_id: str, project_id: str, *, renderer: str | None, html: str | None = "<p>Hi</p>"):
    """A stored document version, written straight to the database.

    Going through the real generation endpoints would need a model provider;
    what these tests exercise is the save path's decision, which depends only on
    what is recorded about the document.
    """
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.org_id == org_id))
        doc = GeneratedDocument(org_id=org_id, project_id=project_id, display_id=70001, language="en")
        db.add(doc)
        db.flush()
        version = DocumentVersion(
            document_id=doc.id, org_id=org_id, version_no=1, blob_path=f"generated/{project_id}/x.docx",
            html_content=html, renderer=renderer, status="draft", created_by=user.id,
        )
        db.add(version)
        db.flush()
        doc.current_version_id = version.id
        db.commit()
        return version.id
    finally:
        db.close()


# ------------------------------------------------- the HTML round-trip guard

@pytest.mark.parametrize("renderer", [OOXML_FILL, DOCX_TEMPLATE_ASSEMBLY, None])
def test_saving_html_over_a_word_rendered_document_is_refused(app_client, org_context, renderer):
    """The §20 CRITICAL defect.

    Note `html_content` is populated: the legacy generation path stores an HTML
    preview next to a template-filled .docx, which is exactly why the old guard
    ("does this row have any HTML?") never fired on the path it was written for.
    """
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=renderer, html="<p>preview</p>")

    res = app_client.patch(
        f"/api/v1/document-versions/{version_id}",
        json={"html_content": "<p>rewritten</p>"},
        headers=_auth(token),
    )

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_NOT_HTML_EDITABLE"


def test_saving_html_over_an_html_document_still_works(app_client, org_context):
    """The guard must not take the editor away from documents it was built for."""
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=HTML_ASSEMBLY)

    res = app_client.patch(
        f"/api/v1/document-versions/{version_id}",
        json={"html_content": "<p>rewritten</p>"},
        headers=_auth(token),
    )

    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        assert db.get(DocumentVersion, version_id).html_content == "<p>rewritten</p>"
    finally:
        db.close()


def test_the_old_guard_would_have_let_this_through(app_client, org_context):
    """Pins the specific regression: a template-rendered document that carries
    an HTML preview. Under the previous rule this saved and destroyed the file.
    """
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=DOCX_TEMPLATE_ASSEMBLY, html="<p>preview</p>")

    db = SessionLocal()
    try:
        version = db.get(DocumentVersion, version_id)
        old_guard_would_refuse = bool(version.blob_path) and not (version.html_content or "").strip()
        assert old_guard_would_refuse is False  # i.e. the old guard allowed it
    finally:
        db.close()

    res = app_client.patch(
        f"/api/v1/document-versions/{version_id}",
        json={"html_content": "<p>rewritten</p>"},
        headers=_auth(token),
    )
    assert res.status_code == 409


@pytest.mark.parametrize("renderer,expected", [
    (HTML_ASSEMBLY, True), (OOXML_FILL, False), (DOCX_TEMPLATE_ASSEMBLY, False), (None, False),
])
def test_the_read_endpoint_tells_the_client_whether_to_offer_an_editor(
    app_client, org_context, renderer, expected
):
    """The editor used to decide this itself, from whether `html_content` was
    populated -- the same wrong test the old backend guard used. One decision,
    made once, sent to the client."""
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=renderer, html="<p>preview</p>")

    res = app_client.get(f"/api/v1/document-versions/{version_id}", headers=_auth(token))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["html_editable"] is expected
    assert body["renderer"] == renderer
    # The flag must not be inferable from the payload it replaces.
    assert body["html_content"] == "<p>preview</p>"


# ------------------------------------------------------- the manifest lock gate

def _manifest(org_id: str, status: str) -> str:
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.org_id == org_id))
        template_version = db.scalar(select(TemplateVersion))
        manifest = TemplateManifest(
            org_id=org_id, template_file_id=None,
            template_version_id=template_version.id if template_version else "tv-missing",
            version_no=1, status=status, fields=[], conditions=[], blocks=[], delete_always=[],
            created_by=user.id,
        )
        db.add(manifest)
        db.commit()
        return manifest.id
    finally:
        db.close()


@pytest.mark.parametrize("status", ["draft", "in_review", "deprecated"])
def test_only_an_approved_manifest_can_generate(app_client, org_context, status):
    """The batch endpoint has always refused these. The single-record endpoint
    did not, so the same unapproved manifest could still issue a real letter one
    row at a time."""
    token, org_id, project_id = org_context
    manifest_id = _manifest(org_id, status)

    res = app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/generate",
        json={"source_record": {"name": "Dana"}, "project_id": project_id},
        headers=_auth(token),
    )

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "MANIFEST_NOT_APPROVED"


# ------------------------------------------------ QA blocking is not advisory

def test_a_blocked_document_cannot_be_approved(app_client, org_context):
    """A QA failure an approval can step over is not a gate."""
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=OOXML_FILL, html=None)
    db = SessionLocal()
    try:
        db.get(DocumentVersion, version_id).status = "blocked"
        db.commit()
    finally:
        db.close()

    res = app_client.post(f"/api/v1/document-versions/{version_id}:approve", headers=_auth(token))

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_BLOCKED"
    db = SessionLocal()
    try:
        assert db.get(DocumentVersion, version_id).status == "blocked"
    finally:
        db.close()


def test_a_clean_document_still_approves(app_client, org_context):
    token, org_id, project_id = org_context
    version_id = _document_version(org_id, project_id, renderer=OOXML_FILL, html=None)

    res = app_client.post(f"/api/v1/document-versions/{version_id}:approve", headers=_auth(token))

    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        assert db.get(DocumentVersion, version_id).status == "approved"
    finally:
        db.close()


def test_another_tenants_manifest_is_not_generatable(app_client, two_orgs):
    """The lock check must not become a way to probe for other orgs' ids: a
    manifest belonging to someone else is missing, not unapproved."""
    _token_a, _project_a, token_b, _project_b = two_orgs
    db = SessionLocal()
    try:
        org_a = db.scalar(select(Organization).where(Organization.name == "TenantOrga"))
        manifest_id = _manifest(org_a.id, "approved")
    finally:
        db.close()

    res = app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/generate",
        json={"source_record": {}},
        headers=_auth(token_b),
    )
    assert res.status_code == 404
    assert res.json()["detail"]["error"]["code"] == "MANIFEST_NOT_FOUND"
