"""Deleting a project and deleting a generated document.

Two different verbs on purpose, and the difference is the point.

A project is stamped `deleted_at`. The column already existed and both
`list_projects` and `get_project` already honoured it, and §16 puts the
destruction of blobs and embeddings on the retention sweep, which knows the
period the customer stated. A one-click shredder for signed letters and their
audit lineage would take that decision away from them.

A generated document is destroyed outright, because `generated_documents` has no
such column and §16 requires deletion to reach the blob. A flagged row that left
the rendered DOCX on disk would be telling the customer something untrue.
"""

import io

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    DocumentVersion, GeneratedDocument, ManifestGeneration, Project, SourceFile,
    TemplateFile, TemplateManifest, TemplateVersion, User,
)
from app.security import create_access_token, hash_password
from app.storage import abs_path


@pytest.fixture
def org_and_token(app_client):
    """A fresh organisation with an admin, isolated from other tests' rows."""
    from app.models import Organization

    db = SessionLocal()
    try:
        org = Organization(name="DeletionTestOrg")
        db.add(org)
        db.flush()
        user = User(
            org_id=org.id, email=f"del-{org.id[:8]}@test.local", full_name="Del Admin",
            password_hash=hash_password("pw"), role_key="org_admin",
        )
        db.add(user)
        db.flush()
        token = create_access_token(user.id, org.id)
        db.commit()
        return token, org.id, user.id
    finally:
        db.close()


def _make_project(client, token, name="Deletable") -> str:
    res = client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": name, "region": "Europe", "function": "Human Resources",
            "document_type": "Offer Letter", "language": "English",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ----------------------------------------------------------------- projects

def test_deleting_a_project_hides_it_from_the_list(app_client, org_and_token):
    token, _, _ = org_and_token
    h = {"Authorization": f"Bearer {token}"}
    pid = _make_project(app_client, token)

    assert any(p["id"] == pid for p in app_client.get("/api/v1/projects", headers=h).json()["items"])

    assert app_client.delete(f"/api/v1/projects/{pid}", headers=h).status_code == 200

    assert not any(p["id"] == pid for p in app_client.get("/api/v1/projects", headers=h).json()["items"])
    assert app_client.get(f"/api/v1/projects/{pid}", headers=h).status_code == 404


def test_deleting_a_project_is_not_a_drop(app_client, org_and_token):
    """The row survives with a stamp. §16 hands destruction to the sweep, which
    knows the customer's stated retention period; this only takes it out of the
    workspace."""
    token, _, _ = org_and_token
    pid = _make_project(app_client, token, "Stamped")
    app_client.delete(f"/api/v1/projects/{pid}", headers={"Authorization": f"Bearer {token}"})

    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        assert row is not None, "the project row was dropped; deletion here is a stamp"
        assert row.deleted_at is not None
    finally:
        db.close()


def test_deleting_a_project_stamps_its_children(app_client, org_and_token):
    """Otherwise the project vanishes while its templates still answer for it,
    and anything holding a template id keeps working against a project the user
    believes is gone."""
    token, org_id, user_id = org_and_token
    pid = _make_project(app_client, token, "WithChildren")

    db = SessionLocal()
    try:
        db.add(TemplateFile(org_id=org_id, project_id=pid, name="t.docx", created_by=user_id))
        db.add(SourceFile(org_id=org_id, project_id=pid, name="s.xlsx", file_type="xlsx", created_by=user_id))
        db.commit()
    finally:
        db.close()

    app_client.delete(f"/api/v1/projects/{pid}", headers={"Authorization": f"Bearer {token}"})

    db = SessionLocal()
    try:
        templates = db.scalars(select(TemplateFile).where(TemplateFile.project_id == pid)).all()
        sources = db.scalars(select(SourceFile).where(SourceFile.project_id == pid)).all()
        assert templates and all(t.deleted_at is not None for t in templates)
        assert sources and all(s.deleted_at is not None for s in sources)
    finally:
        db.close()


def test_deleting_a_project_twice_is_a_404(app_client, org_and_token):
    token, _, _ = org_and_token
    h = {"Authorization": f"Bearer {token}"}
    pid = _make_project(app_client, token, "Twice")
    assert app_client.delete(f"/api/v1/projects/{pid}", headers=h).status_code == 200
    assert app_client.delete(f"/api/v1/projects/{pid}", headers=h).status_code == 404


def test_one_org_cannot_delete_anothers_project(app_client, two_orgs):
    """The check that matters most. Deleting by id without the tenant test is how
    one customer's request destroys another's data."""
    token_a, project_a, token_b, _ = two_orgs
    res = app_client.delete(f"/api/v1/projects/{project_a}", headers={"Authorization": f"Bearer {token_b}"})
    assert res.status_code == 404

    db = SessionLocal()
    try:
        assert db.get(Project, project_a).deleted_at is None
    finally:
        db.close()


# ----------------------------------------------------------------- paging

def test_project_list_pages(app_client, org_and_token):
    token, _, _ = org_and_token
    h = {"Authorization": f"Bearer {token}"}
    for i in range(3):
        _make_project(app_client, token, f"Paged {i}")

    first = app_client.get("/api/v1/projects", headers=h, params={"limit": 2, "offset": 0}).json()
    second = app_client.get("/api/v1/projects", headers=h, params={"limit": 2, "offset": 2}).json()

    assert len(first["items"]) == 2
    assert first["limit"] == 2 and first["offset"] == 0
    assert {p["id"] for p in first["items"]}.isdisjoint({p["id"] for p in second["items"]})


def test_total_counts_the_filtered_set_not_the_whole_org(app_client, org_and_token):
    """`total` is what a pager divides. Counting every project in the
    organisation made a filtered list offer pages that were always empty."""
    token, _, _ = org_and_token
    h = {"Authorization": f"Bearer {token}"}
    _make_project(app_client, token, "Findable Unicorn")
    _make_project(app_client, token, "Something Else")

    filtered = app_client.get("/api/v1/projects", headers=h, params={"q": "Findable Unicorn"}).json()
    assert filtered["total"] == len(filtered["items"]) == 1


# ---------------------------------------------------------------- documents

def _make_document(org_id: str, project_id: str, user_id: str, *, status="draft"):
    """A generated document with a version and a real file on disk."""
    db = SessionLocal()
    try:
        gd = GeneratedDocument(
            org_id=org_id, project_id=project_id, draft_id=None, display_id=1,
            language="en", status=status,
        )
        db.add(gd)
        db.flush()
        rel = f"generated/{project_id}/{gd.id}.docx"
        target = abs_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PK\x03\x04 pretend docx")
        dv = DocumentVersion(
            document_id=gd.id, org_id=org_id, version_no=1, blob_path=rel,
            renderer="ooxml_fill", status=status, created_by=user_id,
        )
        db.add(dv)
        db.flush()
        gd.current_version_id = dv.id
        # A real manifest row, not an invented id. `manifest_generations.manifest_id`
        # carries a foreign key that SQLite does not enforce and PostgreSQL does,
        # so a fabricated id passes locally and fails on the database that ships.
        tf = TemplateFile(org_id=org_id, project_id=project_id, name="t.docx", created_by=user_id)
        db.add(tf)
        db.flush()
        tv = TemplateVersion(
            template_file_id=tf.id, org_id=org_id, blob_path=f"templates/{tf.id}/v1.docx",
            created_by=user_id,
        )
        db.add(tv)
        db.flush()
        tm = TemplateManifest(
            org_id=org_id, template_file_id=tf.id, template_version_id=tv.id,
            version_no=1, status="approved", created_by=user_id,
        )
        db.add(tm)
        db.flush()
        db.add(ManifestGeneration(
            org_id=org_id, manifest_id=tm.id, source_record={"Name": "Chris"},
            field_lineage=[], condition_lineage=[], qa_passed=True, qa_notes=[],
            blob_path=rel, created_by=user_id,
        ))
        db.commit()
        return gd.id, rel
    finally:
        db.close()


def test_deleting_a_document_removes_rows_versions_and_the_file(app_client, org_and_token):
    token, org_id, user_id = org_and_token
    pid = _make_project(app_client, token, "DocHolder")
    doc_id, rel = _make_document(org_id, pid, user_id)

    assert abs_path(rel).exists(), "precondition: the rendered file is on disk"

    res = app_client.delete(f"/api/v1/documents/{doc_id}", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    assert res.json()["blobs_deleted"] == 1

    db = SessionLocal()
    try:
        assert db.get(GeneratedDocument, doc_id) is None
        assert db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc_id)).all() == []
        # §16: "an embedding derived from deleted data is still derived from it" --
        # the generation record holds the source values written into the letter.
        assert db.scalars(select(ManifestGeneration).where(ManifestGeneration.blob_path == rel)).all() == []
    finally:
        db.close()
    assert not abs_path(rel).exists(), "the rendered DOCX survived a deletion that reported success"


def test_an_approved_document_must_be_revoked_before_it_can_be_deleted(app_client, org_and_token):
    """Approval is where somebody put their name to the contents. §16's four-eyes
    rule means erasing that is not a single signer's call."""
    token, org_id, user_id = org_and_token
    pid = _make_project(app_client, token, "ApprovedHolder")
    doc_id, rel = _make_document(org_id, pid, user_id, status="approved")

    res = app_client.delete(f"/api/v1/documents/{doc_id}", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 409
    assert abs_path(rel).exists(), "a refused deletion must not have removed the file"

    db = SessionLocal()
    try:
        assert db.get(GeneratedDocument, doc_id) is not None
    finally:
        db.close()


def test_one_org_cannot_delete_anothers_document(app_client, two_orgs, org_and_token):
    token_other, org_id, user_id = org_and_token
    token_a, project_a, _, _ = two_orgs

    db = SessionLocal()
    try:
        owner_org = db.get(Project, project_a).org_id
        owner = db.scalar(select(User).where(User.org_id == owner_org))
    finally:
        db.close()
    doc_id, rel = _make_document(owner_org, project_a, owner.id)

    res = app_client.delete(f"/api/v1/documents/{doc_id}", headers={"Authorization": f"Bearer {token_other}"})
    assert res.status_code == 404
    assert abs_path(rel).exists()

    db = SessionLocal()
    try:
        assert db.get(GeneratedDocument, doc_id) is not None
    finally:
        db.close()


# ------------------------------------------------- searching by the printed id

def test_projects_can_be_found_by_the_number_printed_on_the_row(app_client, org_and_token):
    """`display_id` is what people quote to each other. It was absent from the
    server-side filter, so the one search users actually type returned nothing."""
    token, _, _ = org_and_token
    h = {"Authorization": f"Bearer {token}"}
    pid = _make_project(app_client, token, "Findable By Number")

    display_id = app_client.get(f"/api/v1/projects/{pid}", headers=h).json()["display_id"]
    found = app_client.get("/api/v1/projects", headers=h, params={"q": str(display_id)}).json()
    assert [p["id"] for p in found["items"]] == [pid]
