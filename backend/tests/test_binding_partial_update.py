"""Saving part of a binding must not destroy the rest of it.

`BindingUpsert` defaulted `field_bindings` and `value_map` to `{}` and assigned
both unconditionally, so leaving a key out of the request did not mean "no
change to this" -- it meant "set this to empty".

That is invisible until generation. `value_map` is what maps an observed source
value onto a branch the template offers; without it a row's condition selects no
branch, and the letter is produced with a section missing. Nothing fails, and
nobody finds out by looking at the screen that erased it.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ManifestBinding


def _binding(manifest_id, source_version_id):
    db = SessionLocal()
    try:
        return db.scalar(
            select(ManifestBinding).where(
                ManifestBinding.manifest_id == manifest_id,
                ManifestBinding.source_version_id == source_version_id,
            )
        )
    finally:
        db.close()


@pytest.fixture(scope="module")
def bound_manifest(app_client):
    """One manifest over one CSV -- the minimum a binding needs to exist."""
    from app.models import (
        Organization, Project, SourceFile, SourceVersion, TemplateFile,
        TemplateManifest, TemplateVersion, User,
    )
    from app.security import create_access_token, hash_password
    from app.storage import save_bytes

    csv = "First Name,Transfer Type\nOlivia,PERM\n"
    fields = [{"id": "colleague_first_name", "type": "string",
               "slots": [{"kind": "blue_placeholder", "text": "<First Name>"}]}]

    db = SessionLocal()
    try:
        org = Organization(name="PartialBindingOrg")
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email="partial@binding.test", full_name="Partial Binder",
                    password_hash=hash_password("pw"), role_key="org_admin")
        db.add(user)
        db.flush()
        project = Project(org_id=org.id, display_id=91777, name="Partial Binding Project",
                          region="Europe", function="Human Resources", document_type="Offer Letter",
                          language="English", status="pending", created_by=user.id)
        db.add(project)
        db.flush()
        template = TemplateFile(org_id=org.id, project_id=project.id, name="t.docx",
                                status="ready", created_by=user.id)
        source = SourceFile(org_id=org.id, project_id=project.id, name="s.csv",
                            file_type="csv", status="ready", created_by=user.id)
        db.add_all([template, source])
        db.flush()
        tv = TemplateVersion(template_file_id=template.id, org_id=org.id, version_no=1,
                             blob_path="templates/none.docx", created_by=user.id)
        sv = SourceVersion(source_file_id=source.id, org_id=org.id, version_no=1,
                           blob_path=save_bytes(csv.encode(), "sources", ".csv"),
                           created_by=user.id)
        db.add_all([tv, sv])
        db.flush()
        manifest = TemplateManifest(
            org_id=org.id, template_file_id=template.id, template_version_id=tv.id,
            version_no=1, status="draft", fields=fields, conditions=[], blocks=[],
            delete_always=[], confidence=1.0, compiled_by="rule_based", prescan_summary={},
            created_by=user.id,
        )
        db.add(manifest)
        db.flush()
        ids = (create_access_token(user.id, org.id), manifest.id, sv.id)
        db.commit()
        return ids
    finally:
        db.close()


@pytest.fixture
def saved_binding(app_client, bound_manifest):
    """A binding carrying both halves, saved the way the UI saves one."""
    token, manifest_id, source_version_id = bound_manifest
    res = app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/bindings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_version_id": source_version_id,
            "field_bindings": {"colleague_first_name": "First Name"},
            "value_map": {"transfer_type": {"PERM": "Permanent"}},
        },
    )
    assert res.status_code == 201, res.text
    return token, manifest_id, source_version_id


def test_saving_columns_again_keeps_the_value_map(app_client, saved_binding):
    """The scenario that produced wrong documents: a reviewer maps a branch value
    on Monday, returns on Tuesday, adjusts one column and saves. The screen sends
    no value_map because it never loaded one."""
    token, manifest_id, source_version_id = saved_binding

    app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/bindings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_version_id": source_version_id,
            "field_bindings": {"colleague_first_name": "Colleague First Name"},
        },
    )

    row = _binding(manifest_id, source_version_id)
    assert row.value_map == {"transfer_type": {"PERM": "Permanent"}}, (
        "omitting value_map erased it; a row whose condition value no longer resolves "
        "generates a letter with a section missing"
    )
    assert row.field_bindings == {"colleague_first_name": "Colleague First Name"}


def test_saving_a_value_map_alone_keeps_the_columns(app_client, saved_binding):
    token, manifest_id, source_version_id = saved_binding

    app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/bindings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_version_id": source_version_id,
            "value_map": {"transfer_type": {"TEMP": "Temporary"}},
        },
    )

    row = _binding(manifest_id, source_version_id)
    assert row.field_bindings == {"colleague_first_name": "First Name"}
    assert row.value_map == {"transfer_type": {"TEMP": "Temporary"}}


def test_an_explicit_empty_object_still_clears(app_client, saved_binding):
    """Omitting and clearing have to stay distinguishable, or there is no way to
    remove a mapping a reviewer no longer wants."""
    token, manifest_id, source_version_id = saved_binding

    app_client.post(
        f"/api/v1/template-manifests/{manifest_id}/bindings",
        headers={"Authorization": f"Bearer {token}"},
        json={"source_version_id": source_version_id, "value_map": {}},
    )

    assert _binding(manifest_id, source_version_id).value_map == {}
