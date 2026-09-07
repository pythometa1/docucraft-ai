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
    assert {k["id"] for k in kits} == {"blank", "offer", "contract", "clinical", "medaff",
                                       "invoice", "invoice_gst", "invoice_intl",
        "clinical_icf", "clinical_protocol"}
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


# ---- opening an editor without paying for a compile ----
#
# A compile fails on roughly a third of templates here, and the fix is usually
# the document rather than the reading. So the project needs an "edit this"
# button -- and a button that costs one model call per chunk plus a reconcile
# plus up to twelve review rounds, budgeted five minutes at p95 by §18, is not a
# button. These pin the three ways in, cheapest first.

def _manifest_for(db, *, org_id, template_file_id, template_version_id, user_id,
                  fields, version_no=1, status="approved"):
    from app.models import TemplateManifest

    m = TemplateManifest(
        org_id=org_id, template_file_id=template_file_id,
        template_version_id=template_version_id, version_no=version_no, status=status,
        fields=fields, conditions=[], blocks=[], delete_always=[], confidence=0.9,
        compiled_by="llm:gpt-5", prescan_summary={}, created_by=user_id)
    db.add(m)
    db.flush()
    return m


def test_a_template_already_read_opens_with_no_model_call(app_client, org_a, monkeypatch):
    """The path that makes an Edit button possible at all.

    Reading and placing were always separate jobs: the compile decides what the
    template means, `objects_from_compile` decides where those meanings sit. The
    second needs no model, and `TemplateManifest` already stores everything the
    first produced. Re-deriving the meaning to redo the placement was an accident
    of the order this endpoint was written in.
    """
    from app.db import SessionLocal
    from app.models import TemplateVersion
    from app.routers import blueprints as mod

    token, project_id, org_id, user_id = org_a
    uploaded = _upload(app_client, token, project_id)

    def explode(*a, **k):  # noqa: ANN001
        raise AssertionError("a compile must not run when a reading already exists")

    monkeypatch.setattr(mod, "compile_agentic_template", explode)

    db = SessionLocal()
    try:
        version = db.get(TemplateVersion, uploaded["current_version_id"])
        _manifest_for(db, org_id=org_id, template_file_id=uploaded["id"],
                      template_version_id=version.id, user_id=user_id,
                      fields=[{"id": "colleague_name", "type": "string",
                               "slots": [{"kind": "text_match", "text": "<Colleague Name>",
                                          "paragraph_index": 0}]}])
        db.commit()
    finally:
        db.close()

    response = app_client.post("/api/v1/template-blueprints:from-template",
                               json={"template_file_id": uploaded["id"]},
                               headers=_auth(token))
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["kind"] == "legacy"
    assert created["version"]["body"]["blocks"], "the document must still be readable"
    # And it says which of the three paths produced it.
    assert created["version"]["provenance"]["read_from"].startswith("manifest:")


def test_asking_twice_returns_the_same_blueprint(app_client, org_a, monkeypatch):
    """`template_file_id` carries no unique constraint and nothing queried by it,
    so every call minted a new blueprint and paid for its own compile. Two
    clicks of an Edit button meant two blueprints able to emit onto one file."""
    from app.db import SessionLocal
    from app.models import TemplateVersion
    from app.routers import blueprints as mod

    token, project_id, org_id, user_id = org_a
    uploaded = _upload(app_client, token, project_id)
    monkeypatch.setattr(mod, "compile_agentic_template",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no compile")))

    db = SessionLocal()
    try:
        version = db.get(TemplateVersion, uploaded["current_version_id"])
        _manifest_for(db, org_id=org_id, template_file_id=uploaded["id"],
                      template_version_id=version.id, user_id=user_id,
                      fields=[{"id": "x", "type": "string",
                               "slots": [{"kind": "text_match", "text": "<Colleague Name>",
                                          "paragraph_index": 0}]}])
        db.commit()
    finally:
        db.close()

    body = {"template_file_id": uploaded["id"]}
    first = app_client.post("/api/v1/template-blueprints:from-template", json=body,
                            headers=_auth(token)).json()
    second = app_client.post("/api/v1/template-blueprints:from-template", json=body,
                             headers=_auth(token)).json()
    assert first["id"] == second["id"]

    listed = app_client.get(
        f"/api/v1/template-blueprints?template_file_id={uploaded['id']}",
        headers=_auth(token)).json()["items"]
    assert [b["id"] for b in listed] == [first["id"]]


def test_a_failed_reading_is_not_reused(app_client, org_a):
    """A failed compile stores a manifest with nothing in it, and a blueprint
    built from one could never be published -- `_lint_current` turns every
    assertion fault into a blocker, so each unclaimed placeholder would block and
    the author would have to hand-write the whole reading first. Better to spend
    the compile, which is what the 503 here proves it tried to do."""
    from app.db import SessionLocal
    from app.models import TemplateVersion

    token, project_id, org_id, user_id = org_a
    uploaded = _upload(app_client, token, project_id)

    db = SessionLocal()
    try:
        version = db.get(TemplateVersion, uploaded["current_version_id"])
        _manifest_for(db, org_id=org_id, template_file_id=uploaded["id"],
                      template_version_id=version.id, user_id=user_id,
                      fields=[], status="failed")
        db.commit()
    finally:
        db.close()

    # No provider is configured in the suite, so reaching for the compiler is
    # observable as the refusal it produces.
    response = app_client.post("/api/v1/template-blueprints:from-template",
                               json={"template_file_id": uploaded["id"]},
                               headers=_auth(token))
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_the_project_row_says_whether_a_blueprint_is_open(app_client, org_a, blueprint):
    """So the row can offer "continue editing" rather than "edit", and clicking
    it does not have to guess. The join key has been on `TemplateBlueprint` since
    it was written; nothing on the template side had looked at it."""
    from app.db import SessionLocal
    from app.models import TemplateBlueprint

    token, project_id, blueprint_id, _body = blueprint
    db = SessionLocal()
    try:
        template_file_id = db.get(TemplateBlueprint, blueprint_id).template_file_id
    finally:
        db.close()

    items = app_client.get(f"/api/v1/projects/{project_id}/templates",
                           headers=_auth(token)).json()["items"]
    mine = next(t for t in items if t["id"] == template_file_id)
    assert mine["blueprint_id"] == blueprint_id

    # And a template nobody has opened reports None rather than omitting the key,
    # so the row can branch on it without guessing.
    fresh = _upload(app_client, token, project_id)
    items = app_client.get(f"/api/v1/projects/{project_id}/templates",
                           headers=_auth(token)).json()["items"]
    assert next(t for t in items if t["id"] == fresh["id"])["blueprint_id"] is None


def test_a_refused_publish_leaves_the_template_untouched(app_client, org_a, blueprint):
    """`emit_blueprint` commits. Writing the real version before the lint had
    decided meant a publish this endpoint then refused had already appended a
    version to the customer's template and moved `current_version_id` onto it --
    a rejected publish that silently changed which file the project fills from.
    """
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateBlueprintVersion, TemplateFile, TemplateVersion

    token, _project_id, blueprint_id, body = blueprint

    db = SessionLocal()
    try:
        row = db.get(TemplateBlueprint, blueprint_id)
        template_file_id = row.template_file_id
        before_versions = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        before_current = db.get(TemplateFile, template_file_id).current_version_id

        # Two objects claiming one id is a blocking lint finding no disposition
        # in this request answers, so publish must refuse.
        current = db.get(TemplateBlueprintVersion, row.current_version_id)
        objects = list(current.objects or [])
        assert objects, "the fixture must have produced at least one object"
        clashing = {**objects[0], "object_id": objects[0]["object_id"]}
        current.objects = objects + [clashing]
        db.commit()
    finally:
        db.close()

    response = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                               json={}, headers=_auth(token))
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "BLUEPRINT_NOT_PUBLISHABLE"

    db = SessionLocal()
    try:
        after_versions = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        after_current = db.get(TemplateFile, template_file_id).current_version_id
        assert after_versions == before_versions, "a refused publish wrote a template version"
        assert after_current == before_current, "a refused publish moved current_version_id"
    finally:
        db.close()


# ---- publishing a template whose reading has gone stale ----

def test_publish_refuses_a_stale_reading_and_says_there_is_another_way(app_client, blueprint):
    """The dead end, and the sign out of it.

    A blueprint's objects and its document drift apart in exactly the case the
    editor exists for: you open a template to repair a placeholder the compiler
    could not claim, you repair it, and the objects still do not claim it. The
    ordinary publish writes the manifest *from those objects*, so it is right to
    refuse -- but a refusal with no alternative reads as "this template cannot be
    published", which is false.
    """
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateBlueprintVersion

    token, _project_id, blueprint_id, _body = blueprint
    db = SessionLocal()
    try:
        row = db.get(TemplateBlueprint, blueprint_id)
        current = db.get(TemplateBlueprintVersion, row.current_version_id)
        # A reading that accounts for nothing: every placeholder is now unclaimed.
        current.objects = []
        db.commit()
    finally:
        db.close()

    response = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                               json={}, headers=_auth(token))
    assert response.status_code == 422
    details = response.json()["detail"]["error"]["details"]
    assert details["can_recompile"] is True, "the refusal must name the way out"


def test_re_reading_does_not_gate_on_the_stale_objects(app_client, blueprint, monkeypatch):
    """`recompile` publishes the document and lets the compiler read it.

    The objects are not what ships on this path, so gating on them would refuse a
    template that is now correct for a reading that is merely out of date. Here
    the objects account for nothing at all and the publish still goes through,
    because what ships is the compiler's reading of the bytes that shipped.
    """
    from app.compiler.rule_compiler import compile_manifest
    from app.db import SessionLocal
    from app.models import (
        TemplateBlueprint, TemplateBlueprintVersion, TemplateFile, TemplateManifest,
        TemplateVersion,
    )
    from app.routers import blueprints as mod
    from app.templates.parsers.docx_prescan import prescan

    token, _project_id, blueprint_id, _body = blueprint

    class _Outcome:
        ok = True
        reason = None

        def __init__(self, manifest):
            self.manifest = manifest

        def transcript_dicts(self):
            return [{"stage": "stub"}]

    def fake_compile(path, **kwargs):
        compiled = compile_manifest(prescan(path))
        compiled.compiled_by = "llm:stub"
        return _Outcome(compiled)

    monkeypatch.setattr(mod, "compile_agentic_template", fake_compile)

    db = SessionLocal()
    try:
        row = db.get(TemplateBlueprint, blueprint_id)
        template_file_id = row.template_file_id
        before = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        current = db.get(TemplateBlueprintVersion, row.current_version_id)
        current.objects = []
        db.commit()
    finally:
        db.close()

    response = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                               json={"recompile": True}, headers=_auth(token))
    assert response.status_code == 201, response.text
    published = response.json()

    db = SessionLocal()
    try:
        # The document was appended to the original template file, as an ordinary
        # publish would, and the file now points at it.
        after = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        assert after == before + 1
        tf = db.get(TemplateFile, template_file_id)
        assert tf.current_version_id == published["template_version_id"]

        # And the manifest is the compiler's reading of what was written, not the
        # blueprint's empty one.
        manifest = db.get(TemplateManifest, published["manifest_id"])
        assert manifest.template_version_id == published["template_version_id"]
        assert manifest.compiled_by == "llm:stub"
        assert manifest.fields, "the compiler's reading must be what ships"
        assert manifest.prescan_summary["read_again_after_publish"] is True
        # Approved, by the person who published it, and this is a change.
        #
        # It used to assert "approval stays a person's act" and leave the
        # manifest in `draft` -- which meant a republished template did not take
        # effect: the project kept generating from whatever was approved *before*
        # the edit, and nothing on the screen said so. The act is still a
        # person's, and still theirs to be capable of: `try_auto_approve` refuses
        # for a role without APPROVE_MANIFEST and for any legally-binding
        # template, which is what the two tests below pin.
        assert manifest.status == "approved"
        assert manifest.approved_by
        assert published["approval_blocked_reason"] is None
    finally:
        db.close()


def test_publishing_does_not_confer_an_approval_the_role_withholds(app_client, blueprint, monkeypatch):
    """A mapper may publish a template and still not sign one off.

    Publishing writes a manifest and the manifest is approved on the way out, so
    without a capability check publishing would be an approval button for a role
    defined by not having one."""
    from app.compiler.rule_compiler import compile_manifest
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateManifest, User
    from app.routers import blueprints as mod
    from app.templates.parsers.docx_prescan import prescan
    from sqlalchemy import select

    token, _project_id, blueprint_id, _body = blueprint

    class _Outcome:
        ok = True
        reason = None

        def __init__(self, manifest):
            self.manifest = manifest

        def transcript_dicts(self):
            return [{"stage": "stub"}]

    def fake_compile(path, **kwargs):
        compiled = compile_manifest(prescan(path))
        compiled.compiled_by = "llm:stub"
        return _Outcome(compiled)

    monkeypatch.setattr(mod, "compile_agentic_template", fake_compile)

    # Demoted for the length of this test and put back in the `finally`. The
    # database lives for the whole session, so a role left changed here is a role
    # changed for every test that runs afterwards -- which is a 403 out of nowhere
    # in a file that never mentions roles. This is the shape `test_authz` uses
    # for the same reason.
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "user-a@tenant.test"))
        original = user.role_key
        user.role_key = "mapper"  # may compile and publish, may not approve
        db.commit()
    finally:
        db.close()

    try:
        response = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                                   json={"recompile": True}, headers=_auth(token))
        assert response.status_code == 201, response.text
        published = response.json()
        assert published["manifest_status"] == "draft"
        assert "cannot approve" in published["approval_blocked_reason"]

        db = SessionLocal()
        try:
            assert db.get(TemplateManifest, published["manifest_id"]).status == "draft"
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            db.scalar(select(User).where(User.email == "user-a@tenant.test")).role_key = original
            db.commit()
        finally:
            db.close()


def test_a_re_read_that_cannot_be_read_publishes_nothing(app_client, blueprint, monkeypatch):
    """The compile runs before anything is written, so a model that cannot read
    the document leaves the customer's template exactly as it was rather than
    appending a version whose manifest never arrived."""
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateFile, TemplateVersion
    from app.routers import blueprints as mod

    token, _project_id, blueprint_id, _body = blueprint

    class _Failed:
        ok = False
        reason = "the model returned nothing usable"

        class manifest:  # noqa: N801
            compiled_by = "llm_failed"
            fields = []
            conditions = []
            blocks = []
            delete_always = []
            confidence = 0.0
            prescan_summary = {}

        def transcript_dicts(self):
            return []

    monkeypatch.setattr(mod, "compile_agentic_template", lambda path, **k: _Failed())

    db = SessionLocal()
    try:
        row = db.get(TemplateBlueprint, blueprint_id)
        template_file_id = row.template_file_id
        before = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        before_current = db.get(TemplateFile, template_file_id).current_version_id
    finally:
        db.close()

    response = app_client.post(f"/api/v1/template-blueprints/{blueprint_id}:publish",
                               json={"recompile": True}, headers=_auth(token))
    assert response.status_code == 502
    assert response.json()["detail"]["error"]["code"] == "TEMPLATE_NOT_READ"

    db = SessionLocal()
    try:
        after = db.query(TemplateVersion).filter(
            TemplateVersion.template_file_id == template_file_id).count()
        assert after == before, "a failed re-read wrote a template version"
        assert db.get(TemplateFile, template_file_id).current_version_id == before_current
    finally:
        db.close()
