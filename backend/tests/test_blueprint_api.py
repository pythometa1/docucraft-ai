"""The authoring endpoints, as a caller meets them.

The loop these exist for is one sentence: put a legacy template in, have it read
and cleaned, edit it by hand, save it, take the document back. Each test here is
one link of that, plus the refusals -- because most of what makes an authoring
surface trustworthy is what it declines to do quietly.
"""

import io

import docx
import pytest

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org_a(two_orgs):
    from app.db import SessionLocal
    from app.models import User

    token_a, project_a, _token_b, _project_b = two_orgs
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "user-a@tenant.test").one()
        return token_a, project_a, user.org_id, user.id
    finally:
        db.close()


def _template_bytes() -> bytes:
    from app.templates import blueprint as bp
    from app.templates.emit_docx import emit

    body = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "Dear "),
                      bp.segment("placeholder", "<Colleague First Name>"),
                      bp.segment("static", ",")]),
        bp.paragraph([bp.segment("instruction", "Delete this line before sending.")]),
    ], "sect_pr_from": None})

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as workspace:
        path = str(Path(workspace) / "t.docx")
        emit(body, path)
        return Path(path).read_bytes()


def _upload(app_client, token, project_id, name="t.docx") -> dict:
    res = app_client.post(
        f"/api/v1/projects/{project_id}/templates", headers=_auth(token),
        files={"file": (name, _template_bytes(), DOCX_TYPE)})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def blueprint(app_client, org_a):
    """A blueprint over a real uploaded template, built without the compiler.

    `conftest` blanks every provider key so the suite can never reach a model,
    which means `:from-template` cannot run here -- that path is covered by its
    own test asserting the 503. Everything downstream of the compile is
    deterministic, so the **rule** compiler stands in for the agentic one and the
    lift, the cleaning and the gate are exercised exactly as they run in
    production.
    """
    from app.compiler.rule_compiler import compile_manifest
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateBlueprintVersion, TemplateVersion
    from app.storage import abs_path
    from app.templates.lift import mark_instructions, objects_from_compile
    from app.templates.parsers.docx_prescan import prescan
    from app.templates.read_docx import read_body

    token, project_id, org_id, user_id = org_a
    uploaded = _upload(app_client, token, project_id)

    db = SessionLocal()
    try:
        version = db.get(TemplateVersion, uploaded["current_version_id"])
        path = str(abs_path(version.blob_path))
        body, _notes = read_body(path)
        compiled = compile_manifest(prescan(path))
        objects, _lift_findings = objects_from_compile(body, compiled)
        body, _cleaned = mark_instructions(body, compiled)

        row = TemplateBlueprint(
            org_id=org_id, project_id=project_id, name="Offer letter", kind="legacy",
            status="draft", source_template_version_id=version.id,
            template_file_id=uploaded["id"], created_by=user_id)
        db.add(row)
        db.flush()
        first = TemplateBlueprintVersion(
            blueprint_id=row.id, org_id=org_id, version_no=1, body=body, objects=objects,
            findings=[], provenance={"kind": "legacy"}, created_by=user_id)
        db.add(first)
        db.flush()
        row.current_version_id = first.id
        db.commit()
        return token, project_id, row.id, body
    finally:
        db.close()


# ---- reading ----

def test_a_blueprint_comes_back_with_the_document_in_it(app_client, blueprint):
    token, _project_id, blueprint_id, body = blueprint
    res = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}", headers=_auth(token))

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["kind"] == "legacy"
    assert payload["version"]["body"] == body
    assert payload["version"]["version_no"] == 1


def test_the_list_is_scoped_to_the_project_when_one_is_named(app_client, blueprint):
    token, project_id, blueprint_id, _body = blueprint
    res = app_client.get("/api/v1/template-blueprints", headers=_auth(token),
                         params={"project_id": project_id})
    assert res.status_code == 200
    assert blueprint_id in {item["id"] for item in res.json()["items"]}


# ---- editing ----

def test_saving_an_edit_creates_a_version_rather_than_changing_one(app_client, blueprint):
    """Versioning over mutation, like every other version row in this schema.
    It is what lets `revert-to` fork from any earlier state."""
    token, _project_id, blueprint_id, body = blueprint

    edited = {**body, "blocks": [dict(b) for b in body["blocks"]]}
    edited["blocks"][0] = {**edited["blocks"][0],
                           "segments": [dict(s) for s in edited["blocks"][0]["segments"]]}
    edited["blocks"][0]["segments"][1]["text"] = "<Given Name>"

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                          headers=_auth(token),
                          json={"body": edited, "change_summary": "Renamed the placeholder."})
    assert res.status_code == 201, res.text
    assert res.json()["version_no"] == 2

    current = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}",
                             headers=_auth(token)).json()
    assert current["version"]["body"]["blocks"][0]["segments"][1]["text"] == "<Given Name>"

    versions = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                              headers=_auth(token)).json()["items"]
    assert [v["version_no"] for v in versions] == [2, 1], "version 1 is still there"


def test_a_save_built_on_a_version_somebody_has_replaced_is_refused(app_client, blueprint):
    """Two people editing the same template must not silently overwrite each
    other. The client says which version it was showing; the server says no."""
    token, _project_id, blueprint_id, body = blueprint
    app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                    headers=_auth(token), json={"body": body})

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                          headers=_auth(token),
                          json={"body": body, "expected_version_no": 1})
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "BLUEPRINT_VERSION_CONFLICT"


def test_a_saved_body_is_normalised_before_it_is_stored(app_client, blueprint):
    """One segment must mean one span. A body stored unmerged describes a
    document that does not exist, and every slot after the merge would address
    the span to its left."""
    token, _project_id, blueprint_id, _body = blueprint
    unmerged = {"blocks": [{"kind": "paragraph", "style": None, "segments": [
        {"role": "static", "text": "a"}, {"role": "static", "text": "b"}]}],
        "sect_pr_from": None}

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                          headers=_auth(token), json={"body": unmerged})
    assert res.status_code == 201, res.text
    segments = res.json()["body"]["blocks"][0]["segments"]
    assert [s["text"] for s in segments] == ["ab"]


def test_a_body_with_an_unknown_block_kind_is_refused(app_client, blueprint):
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(
        f"/api/v1/template-blueprints/{blueprint_id}/versions", headers=_auth(token),
        json={"body": {"blocks": [{"kind": "sidebar"}], "sect_pr_from": None}})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "BLUEPRINT_INVALID"


def test_reverting_forks_a_new_version_rather_than_erasing_the_ones_between(app_client, blueprint):
    """The versions in between are what somebody did. A revert that erased them
    would make the history a record of the current opinion rather than the work."""
    token, _project_id, blueprint_id, body = blueprint
    edited = {**body, "blocks": [dict(b) for b in body["blocks"]]}
    edited["blocks"][0] = {**edited["blocks"][0],
                           "segments": [dict(s) for s in edited["blocks"][0]["segments"]]}
    edited["blocks"][0]["segments"][1]["text"] = "<Wrong>"
    app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                    headers=_auth(token), json={"body": edited})

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:revert-to",
                          headers=_auth(token), json={"version_no": 1})
    assert res.status_code == 201, res.text
    assert res.json()["version_no"] == 3
    assert res.json()["body"] == body
    assert res.json()["provenance"]["reverted_to_version_no"] == 1

    versions = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                              headers=_auth(token)).json()["items"]
    assert [v["version_no"] for v in versions] == [3, 2, 1]


def test_reverting_to_a_version_that_does_not_exist_is_a_404(app_client, blueprint):
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:revert-to",
                          headers=_auth(token), json={"version_no": 99})
    assert res.status_code == 404


# ---- the artifact ----

def test_emitting_writes_a_template_version_from_the_customers_own_file(app_client, blueprint):
    """The document handed back is the uploaded file with its text edited, not a
    rebuild. Every part of the package but `word/document.xml` is the customer's
    original bytes."""
    import zipfile

    from app.db import SessionLocal
    from app.models import TemplateVersion
    from app.storage import abs_path

    token, _project_id, blueprint_id, body = blueprint

    edited = {**body, "blocks": [dict(b) for b in body["blocks"]]}
    edited["blocks"][1] = {**edited["blocks"][1],
                           "segments": [{**edited["blocks"][1]["segments"][0], "emit": False}]}
    app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                    headers=_auth(token), json={"body": edited})

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:emit",
                          headers=_auth(token))
    assert res.status_code == 201, res.text
    payload = res.json()

    db = SessionLocal()
    try:
        emitted = db.get(TemplateVersion, payload["template_version_id"])
        path = str(abs_path(emitted.blob_path))
    finally:
        db.close()

    from app.templates.parsers.docx_prescan import prescan

    scan = prescan(path)
    assert not [s for s in scan.spans if s.color == "red"], "the author instruction is gone"
    assert [s.text for s in scan.spans if s.color == "blue"] == ["<Colleague First Name>"]
    assert zipfile.ZipFile(path).testzip() is None


def test_a_template_written_from_scratch_gets_a_file_of_its_own(app_client, org_a):
    """It has no original to edit, so publishing builds a fresh package -- and it
    has no `TemplateFile` either until the first time it is written out, which is
    created then rather than at creation so a template nobody publishes leaves
    nothing behind."""
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateVersion
    from app.storage import abs_path

    token, project_id, _org_id, _user_id = org_a
    created = app_client.post("/api/v1/template-blueprints", headers=_auth(token),
                              json={"name": "Offer letter", "kit": "offer",
                                    "project_id": project_id}).json()

    res = app_client.post(f"/api/v1/template-blueprints/{created['id']}:emit",
                          headers=_auth(token))
    assert res.status_code == 201, res.text
    payload = res.json()
    assert payload["template_file_id"]

    db = SessionLocal()
    try:
        version = db.get(TemplateVersion, payload["template_version_id"])
        path = str(abs_path(version.blob_path))
        blueprint = db.get(TemplateBlueprint, created["id"])
        assert blueprint.template_file_id == payload["template_file_id"]
    finally:
        db.close()

    from app.templates.parsers.docx_prescan import prescan

    scan = prescan(path)
    # The kit's placeholders are written in the blue the pre-scanner classifies
    # as a placeholder, so a template written from nothing reads back exactly
    # like one read from a customer's file.
    assert "<Colleague First Name>" in [s.text for s in scan.spans if s.color == "blue"]


def test_a_blueprint_with_no_project_cannot_be_written_out(app_client, org_a):
    token, _project_id, _org_id, _user_id = org_a
    created = app_client.post("/api/v1/template-blueprints", headers=_auth(token),
                              json={"name": "Homeless", "kit": "blank"}).json()

    res = app_client.post(f"/api/v1/template-blueprints/{created['id']}:emit",
                          headers=_auth(token))
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "BLUEPRINT_HAS_NO_PROJECT"


def test_the_kits_are_documents_rather_than_field_lists(app_client, org_a):
    token, _project_id, _org_id, _user_id = org_a
    kits = app_client.get("/api/v1/template-blueprint-kits", headers=_auth(token)).json()["items"]
    assert {k["id"] for k in kits} == {"blank", "offer", "contract", "clinical", "medaff"}
    assert all(k["paragraph_count"] > 0 for k in kits), "a kit with no prose is a form"


def test_a_kit_arrives_with_its_placeholders_already_understood(app_client, org_a):
    """Derived from the prose, not from the kit's field list. A name listed but
    not written into the document would be a field that can never appear -- the
    `orphaned_field` defect the publish gate refuses, and this module's fault
    rather than the author's."""
    token, project_id, _org_id, _user_id = org_a
    created = app_client.post("/api/v1/template-blueprints", headers=_auth(token),
                              json={"name": "Offer", "kit": "offer",
                                    "project_id": project_id}).json()
    ids = {o["object_id"] for o in created["version"]["objects"]}
    assert {"colleague_first_name", "position_title", "annual_salary"} <= ids
    assert all(o["slots"] for o in created["version"]["objects"])


def test_an_unknown_kit_is_refused(app_client, org_a):
    token, _project_id, _org_id, _user_id = org_a
    res = app_client.post("/api/v1/template-blueprints", headers=_auth(token),
                          json={"name": "x", "kit": "does-not-exist"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "UNKNOWN_KIT"


# ---- refusals ----

def test_reading_a_template_needs_a_model_and_says_so_when_there_is_none(app_client, org_a):
    """The suite can never reach a provider, which is exactly the condition this
    has to answer honestly. A blueprint built from an empty reading would look
    identical to one built from a good reading of a template with nothing in it."""
    token, project_id, _org_id, _user_id = org_a
    uploaded = _upload(app_client, token, project_id, "needs-a-model.docx")

    res = app_client.post("/api/v1/template-blueprints:from-template", headers=_auth(token),
                          json={"template_file_id": uploaded["id"]})
    assert res.status_code == 503
    assert res.json()["detail"]["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_deleting_a_blueprint_whose_template_has_an_approved_manifest_is_refused(
        app_client, blueprint):
    """Documents name the manifest they were generated from, and that manifest
    names this template. Removing it would leave the lineage pointing at nothing."""
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateManifest

    token, _project_id, blueprint_id, _body = blueprint
    db = SessionLocal()
    try:
        row = db.get(TemplateBlueprint, blueprint_id)
        db.add(TemplateManifest(
            org_id=row.org_id, template_file_id=row.template_file_id,
            template_version_id=row.source_template_version_id, version_no=1,
            status="approved", fields=[], conditions=[], blocks=[], delete_always=[],
            confidence=1.0, compiled_by="rule_based", prescan_summary={},
            created_by=row.created_by))
        db.commit()
    finally:
        db.close()

    res = app_client.delete(f"/api/v1/template-blueprints/{blueprint_id}", headers=_auth(token))
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "BLUEPRINT_IN_USE"


def test_a_deleted_blueprint_stops_being_addressable(app_client, blueprint):
    token, _project_id, blueprint_id, _body = blueprint
    assert app_client.delete(f"/api/v1/template-blueprints/{blueprint_id}",
                             headers=_auth(token)).status_code == 200
    assert app_client.get(f"/api/v1/template-blueprints/{blueprint_id}",
                          headers=_auth(token)).status_code == 404


# ---- the gate ----

def test_lint_answers_from_the_body_without_writing_a_document(app_client, blueprint):
    """Cheap enough for an editor to call on every save: structure, the object
    model and approval's own rules all answer from the body."""
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}/lint",
                         headers=_auth(token))
    assert res.status_code == 200, res.text
    payload = res.json()
    assert set(payload) == {"findings", "blocking", "can_publish"}


def test_publishing_writes_a_manifest_that_addresses_the_published_file(app_client, blueprint):
    """The order is the point. The document is written first, the objects are
    re-addressed against it, and only then is anything checked -- because
    publishing renumbers spans, and a manifest carrying the old numbering would
    fill the span to the left of every slot for the rest of its paragraph."""
    from app.db import SessionLocal
    from app.models import TemplateManifest, TemplateVersion
    from app.storage import abs_path

    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                          headers=_auth(token), json={})
    assert res.status_code == 201, res.text
    payload = res.json()
    assert payload["lint"]["can_publish"] is True

    db = SessionLocal()
    try:
        manifest = db.get(TemplateManifest, payload["manifest_id"])
        emitted = db.get(TemplateVersion, payload["template_version_id"])
        # The manifest is pinned to the file that was published, not the one
        # that was read.
        assert manifest.template_version_id == emitted.id
        assert manifest.compiled_by.startswith("blueprint:")
        # Both projections, never one without the other.
        assert manifest.objects and manifest.fields
        assert {o["object_id"] for o in manifest.objects} == {f["id"] for f in manifest.fields}
        path = str(abs_path(emitted.blob_path))
    finally:
        db.close()

    from app.generation.docx_renderer import fill_template
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as workspace:
        out = str(Path(workspace) / "letter.docx")
        result = fill_template(
            path, out, {"fields": manifest.fields, "conditions": manifest.conditions,
                        "blocks": manifest.blocks, "delete_always": []},
            {f["id"]: "Priya Sharma" for f in manifest.fields})
        assert result.qa_passed, result.qa_notes
        import docx
        text = "\n".join(p.text for p in docx.Document(out).paragraphs)
        assert "Dear Priya Sharma," in text
        assert "Delete this line before sending." not in text


def test_a_template_that_would_produce_wrong_documents_is_refused(app_client, blueprint):
    """And the refusal carries the findings, so the editor can show them rather
    than telling somebody to look somewhere else."""
    from app.db import SessionLocal
    from app.models import TemplateBlueprintVersion

    token, _project_id, blueprint_id, _body = blueprint
    db = SessionLocal()
    try:
        version = db.query(TemplateBlueprintVersion).filter(
            TemplateBlueprintVersion.blueprint_id == blueprint_id).one()
        version.objects = list(version.objects) + [
            {"object_id": "ghost", "object_type": "FIELD", "type": "string", "slots": [],
             "source_ref": "source.ghost", "format": None, "on_missing": "BLANK",
             "status": "PROPOSED", "anchor": None}]
        db.commit()
    finally:
        db.close()

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                          headers=_auth(token), json={})
    assert res.status_code == 422
    body = res.json()["detail"]["error"]
    assert body["code"] == "BLUEPRINT_NOT_PUBLISHABLE"
    assert "field_without_slot" in {f["code"] for f in body["details"]["lint"]["findings"]}


def test_a_blocker_somebody_has_answered_no_longer_stops_the_publish(app_client, blueprint):
    from app.db import SessionLocal
    from app.models import TemplateBlueprintVersion

    token, _project_id, blueprint_id, _body = blueprint
    db = SessionLocal()
    try:
        version = db.query(TemplateBlueprintVersion).filter(
            TemplateBlueprintVersion.blueprint_id == blueprint_id).one()
        version.objects = list(version.objects) + [
            {"object_id": "ghost", "object_type": "FIELD", "type": "string", "slots": [],
             "source_ref": "source.ghost", "format": None, "on_missing": "BLANK",
             "status": "PROPOSED", "anchor": None}]
        db.commit()
    finally:
        db.close()

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                          headers=_auth(token), json={"dispositions": ["field_without_slot"]})
    assert res.status_code == 201, res.text


# ---- editing by asking ----

def test_the_copilot_needs_a_model_and_says_so_when_there_is_none(app_client, blueprint):
    """The suite can never reach a provider, which is the condition this has to
    answer honestly rather than by returning an empty proposal."""
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/copilot",
                          headers=_auth(token),
                          json={"message": "rename the name field", "mode": "author"})
    assert res.status_code == 503
    assert res.json()["detail"]["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_a_mode_nobody_defined_is_refused_rather_than_guessed(app_client, blueprint):
    """Guessing between "explain this condition" and "change this condition" is
    the class of guess this codebase refuses everywhere, and the wrong guess
    edits a legal template."""
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/copilot",
                          headers=_auth(token), json={"message": "hello", "mode": "whatever"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "UNKNOWN_COPILOT_MODE"


def test_operations_are_applied_as_a_new_version(app_client, blueprint):
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/operations",
                          headers=_auth(token),
                          json={"ops": [{"op": "rename_field", "id": "colleague_first_name",
                                         "new_id": "employee_first_name"}]})
    assert res.status_code == 201, res.text
    payload = res.json()
    assert payload["version_no"] == 2
    assert "employee_first_name" in {o["object_id"] for o in payload["objects"]}
    assert payload["applied"] and not payload["rejected"]


def test_a_batch_where_nothing_could_be_applied_is_refused_with_the_reasons(
        app_client, blueprint):
    """A save that reports success and changed nothing is the failure this whole
    path is careful about."""
    token, _project_id, blueprint_id, _body = blueprint
    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/operations",
                          headers=_auth(token),
                          json={"ops": [{"op": "rename_field", "id": "ghost", "new_id": "x"}]})
    assert res.status_code == 422
    body = res.json()["detail"]["error"]
    assert body["code"] == "NO_OPERATION_APPLIED"
    assert body["details"]["rejected"][0]["reason"]


def test_operations_proposed_against_a_stale_version_are_refused(app_client, blueprint):
    token, _project_id, blueprint_id, body = blueprint
    app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/versions",
                    headers=_auth(token), json={"body": body})

    res = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}/operations",
                          headers=_auth(token),
                          json={"ops": [{"op": "rename_field", "id": "colleague_first_name",
                                         "new_id": "x"}],
                                "expected_version_no": 1})
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "BLUEPRINT_VERSION_CONFLICT"
