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

def _manifest(org_id: str, status: str, *, legally_binding: bool | None = None) -> str:
    """A manifest in `status`. Pass `legally_binding` to attach a template file
    carrying that flag, which is the only thing that still demands a signature."""
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.org_id == org_id))
        template_version = db.scalar(select(TemplateVersion))
        template_file_id = None
        if legally_binding is not None:
            from app.models import Project as _Project, TemplateFile

            project = db.scalar(select(_Project).where(_Project.org_id == org_id))
            tf = TemplateFile(
                org_id=org_id, project_id=project.id, name="binding.docx",
                status="parsed", legally_binding=legally_binding, created_by=user.id,
            )
            db.add(tf)
            db.flush()
            template_file_id = tf.id
        manifest = TemplateManifest(
            org_id=org_id, template_file_id=template_file_id,
            template_version_id=template_version.id if template_version else "tv-missing",
            version_no=1, status=status, fields=[], conditions=[], blocks=[], delete_always=[],
            created_by=user.id,
        )
        db.add(manifest)
        db.commit()
        return manifest.id
    finally:
        db.close()


def _generate_batch(app_client, token, manifest_id):
    """Drive the batch endpoint with a source version that does not exist.

    The two gates under test sit immediately before the source lookup, so a 404
    saying `SOURCE_NOT_FOUND` is proof that both of them let the request through
    -- without having to render a document to find out. A gate that refuses
    answers 409 before ever reaching it.
    """
    return app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/generate-batch",
        json={"source_version_id": "no-such-source", "language": "en"},
        headers=_auth(token),
    )


def _code(res):
    return res.json().get("detail", {}).get("error", {}).get("code")


@pytest.mark.parametrize("status", ["draft", "in_review", "approved"])
def test_an_unapproved_manifest_is_no_longer_refused(app_client, org_context, status):
    """Approval has stopped being a precondition for generating.

    It used to be, and it could not be met without going through a review:
    `validate_manifest` turns every undispositioned compiler warning into a
    failure, and a manifest that has just been read has warnings and no
    dispositions by construction. So the gate did not mean "somebody looked at
    this" -- it meant "acknowledge every warning in writing, then sign, then
    generate", imposed on every template whether or not anybody had asked for a
    review."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, status))

    assert _code(res) == "SOURCE_NOT_FOUND", res.text


def test_a_manifest_the_compiler_could_not_read_still_cannot_generate(app_client, org_context):
    """One of the two checks that replaced it, and the one that was always the
    real point: a failed compile produced no usable reading, so there is nothing
    to fill from and every row would fail the same way for the same reason."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, "failed"))

    assert res.status_code == 409, res.text
    assert _code(res) == "MANIFEST_NOT_READ"


@pytest.mark.parametrize("status", ["superseded", "deprecated"])
def test_a_retired_reading_still_cannot_generate(app_client, org_context, status):
    """The other one, and the reason relaxing the gate is not the same as
    removing it.

    A superseded manifest is not an *unsigned* reading, it is a *replaced* one --
    a newer compile of the same template took its place. Generating from it
    produces letters built from a version the project has moved off, which is
    exactly what the one-approved-manifest-per-template rule exists to stop."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, status))

    assert res.status_code == 409, res.text
    assert _code(res) == "MANIFEST_RETIRED"


def test_a_legally_binding_template_still_needs_its_signature(app_client, org_context):
    """The one approval that survives. §16 asks for four eyes on a template
    flagged legally binding, and relaxing the gate everywhere would have deleted
    that rule along with the bookkeeping it was buried in."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, "draft", legally_binding=True))

    assert res.status_code == 409, res.text
    assert _code(res) == "MANIFEST_NOT_APPROVED"
    assert "legally binding" in res.json()["detail"]["error"]["message"]


def test_the_flag_is_what_holds_it_not_the_template_file(app_client, org_context):
    """The same shape with the flag off, so the test above pins the flag rather
    than the mere presence of a template file."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, "draft", legally_binding=False))

    assert _code(res) == "SOURCE_NOT_FOUND", res.text


def test_an_approved_legally_binding_template_generates(app_client, org_context):
    """And the signature, once given, is what releases it."""
    token, org_id, _project_id = org_context
    res = _generate_batch(app_client, token, _manifest(org_id, "approved", legally_binding=True))

    assert _code(res) == "SOURCE_NOT_FOUND", res.text


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
