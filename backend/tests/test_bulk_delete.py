"""Removing several things at once, without removing more than was asked.

Four lists in this product can be multi-selected, and they do not all delete the
same way. Documents are destroyed outright, with their blobs; projects,
templates and sources are stamped `deleted_at` and left for the retention sweep
to destroy on the customer's own schedule. A bulk endpoint that quietly promoted
a soft delete to a hard one, because several rows were ticked rather than one,
would take §16's decision away from the customer at exactly the moment they were
least likely to be reading carefully.

So each of these asserts the same three things: that what was asked for went,
that nothing else did, and that a refusal is reported rather than swallowed.
"""

import pytest

from app.db import SessionLocal
from app.models import (
    DraftDocument, Project, SourceFile, TemplateFile, User,
)

API = "/api/v1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def workspace(two_orgs):
    """Three projects, each with a template and a source, owned by org A."""
    token_a, project_a, token_b, _ = two_orgs
    db = SessionLocal()
    made = {"projects": [], "templates": [], "sources": []}
    try:
        base = db.get(Project, project_a)
        org, user = base.org_id, db.query(User).filter(User.org_id == base.org_id).first()
        for i in range(3):
            p = Project(org_id=org, display_id=94000 + i, name=f"Bulk {i}", region="Europe",
                        function="Human Resources", document_type="Offer Letter",
                        language="English", status="pending", created_by=user.id)
            db.add(p)
            db.flush()
            tf = TemplateFile(org_id=org, project_id=p.id, name=f"t{i}.docx",
                              status="ready", created_by=user.id)
            sf = SourceFile(org_id=org, project_id=p.id, name=f"s{i}.csv",
                            file_type="csv", status="ready", created_by=user.id)
            db.add_all([tf, sf])
            db.flush()
            made["projects"].append(p.id)
            made["templates"].append(tf.id)
            made["sources"].append(sf.id)
        db.commit()
    finally:
        db.close()
    yield token_a, token_b, made
    db = SessionLocal()
    try:
        for model, key in ((TemplateFile, "templates"), (SourceFile, "sources")):
            db.query(model).filter(model.id.in_(made[key])).delete(synchronize_session=False)
        db.query(DraftDocument).filter(
            DraftDocument.project_id.in_(made["projects"])).delete(synchronize_session=False)
        db.query(Project).filter(
            Project.id.in_(made["projects"])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _stamps(model, ids):
    db = SessionLocal()
    try:
        return {r.id: r.deleted_at for r in db.query(model).filter(model.id.in_(ids)).all()}
    finally:
        db.close()


# ------------------------------------------------------------------- projects

def test_several_projects_are_stamped_and_the_rest_left_alone(app_client, workspace):
    token, _b, made = workspace
    doomed, kept = made["projects"][:2], made["projects"][2:]

    body = app_client.post(f"{API}/projects:delete", json={"project_ids": doomed},
                           headers=_auth(token)).json()

    assert {d["project_id"] for d in body["deleted"]} == set(doomed)
    assert body["refused"] == []
    stamps = _stamps(Project, made["projects"])
    assert all(stamps[i] is not None for i in doomed)
    assert all(stamps[i] is None for i in kept)


def test_deleting_a_project_stamps_the_children_it_reaches(app_client, workspace):
    """Without it the project vanishes while `GET /projects/{id}/templates` still
    answers for it, so anything holding a template id keeps working against a
    project the user believes is gone."""
    token, _b, made = workspace
    app_client.post(f"{API}/projects:delete", json={"project_ids": [made["projects"][0]]},
                    headers=_auth(token))
    assert _stamps(TemplateFile, [made["templates"][0]])[made["templates"][0]] is not None
    assert _stamps(SourceFile, [made["sources"][0]])[made["sources"][0]] is not None
    # And only that project's children.
    assert _stamps(TemplateFile, [made["templates"][1]])[made["templates"][1]] is None


def test_a_bulk_project_delete_is_a_stamp_and_never_a_drop(app_client, workspace):
    """§16 puts the destruction of blobs and embeddings on the retention sweep,
    which knows the customer's stated period. Selecting three instead of one does
    not change who that decision belongs to."""
    token, _b, made = workspace
    app_client.post(f"{API}/projects:delete", json={"project_ids": made["projects"]},
                    headers=_auth(token))
    db = SessionLocal()
    try:
        rows = db.query(Project).filter(Project.id.in_(made["projects"])).all()
        assert len(rows) == 3, "the rows must survive; only the sweep destroys them"
        assert all(r.deleted_at is not None for r in rows)
    finally:
        db.close()


def test_an_already_deleted_project_is_refused_not_restamped(app_client, workspace):
    token, _b, made = workspace
    one = [made["projects"][0]]
    app_client.post(f"{API}/projects:delete", json={"project_ids": one}, headers=_auth(token))
    first = _stamps(Project, one)[one[0]]

    body = app_client.post(f"{API}/projects:delete", json={"project_ids": one},
                           headers=_auth(token)).json()

    assert body["deleted"] == []
    assert body["refused"][0]["code"] == "PROJECT_NOT_FOUND"
    assert _stamps(Project, one)[one[0]] == first, "the original stamp must not move"


def test_another_tenants_project_is_untouched(app_client, workspace):
    token_a, token_b, made = workspace
    body = app_client.post(f"{API}/projects:delete", json={"project_ids": made["projects"]},
                           headers=_auth(token_b)).json()
    assert body["deleted"] == []
    assert {r["code"] for r in body["refused"]} == {"PROJECT_NOT_FOUND"}
    assert all(v is None for v in _stamps(Project, made["projects"]).values())


# ----------------------------------------------------------- templates, sources

@pytest.mark.parametrize("kind,path,key,model", [
    ("templates", "templates:delete", "template_ids", TemplateFile),
    ("sources", "sources:delete", "source_ids", SourceFile),
])
def test_several_go_at_once_and_the_rest_stay(app_client, workspace, kind, path, key, model):
    token, _b, made = workspace
    doomed, kept = made[kind][:2], made[kind][2:]

    body = app_client.post(f"{API}/{path}", json={key: doomed}, headers=_auth(token)).json()

    assert len(body["deleted"]) == 2 and body["refused"] == []
    stamps = _stamps(model, made[kind])
    assert all(stamps[i] is not None for i in doomed)
    assert all(stamps[i] is None for i in kept)


@pytest.mark.parametrize("path,key,model,kind", [
    ("templates:delete", "template_ids", TemplateFile, "templates"),
    ("sources:delete", "source_ids", SourceFile, "sources"),
])
def test_another_tenant_cannot_reach_them(app_client, workspace, path, key, model, kind):
    _a, token_b, made = workspace
    body = app_client.post(f"{API}/{path}", json={key: made[kind]},
                           headers=_auth(token_b)).json()
    assert body["deleted"] == []
    assert all(v is None for v in _stamps(model, made[kind]).values())


@pytest.mark.parametrize("path,key", [
    ("projects:delete", "project_ids"),
    ("templates:delete", "template_ids"),
    ("sources:delete", "source_ids"),
    ("documents:delete", "document_ids"),
])
def test_an_empty_selection_is_refused(app_client, workspace, path, key):
    token, _b, _made = workspace
    response = app_client.post(f"{API}/{path}", json={key: []}, headers=_auth(token))
    assert response.status_code == 422


@pytest.mark.parametrize("path,key,cap", [
    ("projects:delete", "project_ids", 50),
    ("templates:delete", "template_ids", 50),
    ("sources:delete", "source_ids", 50),
    ("documents:delete", "document_ids", 100),
])
def test_the_cap_is_checked_before_anything_is_touched(app_client, workspace, path, key, cap):
    token, _b, made = workspace
    real = made["projects"] if key == "project_ids" else []
    payload = real + [f"pad-{i}" for i in range(cap + 1)]
    response = app_client.post(f"{API}/{path}", json={key: payload}, headers=_auth(token))
    assert response.status_code == 422
    if real:
        assert all(v is None for v in _stamps(Project, real).values()), (
            "the cap must be refused before any row is stamped")


@pytest.mark.parametrize("path,key,kind", [
    ("projects:delete", "project_ids", "projects"),
    ("templates:delete", "template_ids", "templates"),
    ("sources:delete", "source_ids", "sources"),
])
def test_the_same_id_twice_counts_once(app_client, workspace, path, key, kind):
    token, _b, made = workspace
    one = [made[kind][0]]
    body = app_client.post(f"{API}/{path}", json={key: one + one},
                           headers=_auth(token)).json()
    assert body["requested"] == 1
    assert len(body["deleted"]) == 1 and body["refused"] == []


@pytest.mark.parametrize("path,key,kind,event", [
    ("projects:delete", "project_ids", "projects", "Deleted project"),
    ("templates:delete", "template_ids", "templates", "Deleted template"),
    ("sources:delete", "source_ids", "sources", "Deleted source file"),
])
def test_every_removal_is_audited_individually(app_client, workspace, path, key, kind, event):
    """A bulk gesture must not collapse into one audit line that hides what it
    removed."""
    from app.models import AuditLog

    token, _b, made = workspace
    app_client.post(f"{API}/{path}", json={key: made[kind]}, headers=_auth(token))
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(
            AuditLog.entity_id.in_(made[kind]), AuditLog.event == event).all()
        assert len(rows) == 3
        assert all("bulk of 3" in (r.target or "") for r in rows)
    finally:
        db.close()
