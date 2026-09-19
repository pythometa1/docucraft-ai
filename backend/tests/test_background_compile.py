"""Reading a template in the background.

A reading takes minutes. Held inside the request it dies with a closed tab or a
proxy timeout, and the person watching it can do nothing else. `background=true`
answers 202 at once, reads on the server's own time, and leaves the progress, the
counts found so far and the result on the job row the client polls.

`conftest` blanks every provider key, so the rule compiler stands in for the
model here, the way `test_blueprint_api` does it: everything this file cares
about -- the 202, the job row, the list, the refusals -- is around the reading,
not inside it.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_ip_boundary import assert_no_leak

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DISPLAY_IDS = iter(range(97001, 98000))

#: Words that describe how the product works. None may reach a job the
#: customer polls, in a key or in a value.
PIPELINE_WORDS = ("round", "chunk", "token_usage", "kind", "detail", "agentic",
                  "retrieval", "embedding", "transcript")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org(app_client):
    """A fresh organisation per test, so a running reading left by one test is
    never another test's 409."""
    from app.db import SessionLocal
    from app.models import Organization, Project, User
    from app.security import create_access_token, hash_password

    db = SessionLocal()
    try:
        o = Organization(name="BackgroundReadOrg")
        db.add(o)
        db.flush()
        u = User(org_id=o.id, email=f"bg-{o.id[:8]}@read.test", full_name="BG",
                 password_hash=hash_password("pw"), role_key="org_admin")
        db.add(u)
        db.flush()
        p = Project(org_id=o.id, display_id=next(_DISPLAY_IDS), name="BG", region="Europe",
                    function="Human Resources", document_type="Offer Letter",
                    language="English", status="pending", created_by=u.id)
        db.add(p)
        db.flush()
        out = (create_access_token(u.id, o.id), p.id, o.id, u.id)
        db.commit()
        return out
    finally:
        db.close()


def _template_bytes() -> bytes:
    import tempfile
    from pathlib import Path

    from app.templates import blueprint as bp
    from app.templates.emit_docx import emit

    body = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "Dear "),
                      bp.segment("placeholder", "<Colleague First Name>"),
                      bp.segment("static", ", your start date is "),
                      bp.segment("placeholder", "<Start Date>"),
                      bp.segment("static", ".")]),
        bp.paragraph([bp.segment("instruction", "Delete this line before sending.")]),
    ], "sect_pr_from": None})
    with tempfile.TemporaryDirectory() as workspace:
        path = str(Path(workspace) / "t.docx")
        emit(body, path)
        return Path(path).read_bytes()


def _upload(app_client, token, project_id) -> str:
    res = app_client.post(f"/api/v1/projects/{project_id}/templates", headers=_auth(token),
                          files={"file": ("bg.docx", _template_bytes(), DOCX_TYPE)})
    assert res.status_code == 201, res.text
    return res.json()["id"]


class _Outcome:
    ok = True
    reason = None

    def __init__(self, manifest):
        self.manifest = manifest

    def transcript_dicts(self):
        return [{"stage": "stub", "round": 1, "chunk": 0}]


@pytest.fixture
def rule_reader(monkeypatch):
    """The rule compiler in place of the model, marked the way a model reading
    is marked, so a leak of `compiled_by` would show."""
    from app.compiler.rule_compiler import compile_manifest
    from app.routers import manifests as mod
    from app.templates.parsers.docx_prescan import prescan

    def fake_compile(path, **kwargs):
        compiled = compile_manifest(prescan(path))
        compiled.compiled_by = "llm:claude-stub"
        return _Outcome(compiled)

    monkeypatch.setattr(mod, "compile_agentic_template", fake_compile)


def _job(app_client, token, job_id) -> dict:
    res = app_client.get(f"/api/v1/jobs/{job_id}", headers=_auth(token))
    assert res.status_code == 200, res.text
    return res.json()


def _assert_nothing_internal(body):
    # Fractions allowed: `elapsed_seconds` is a duration, not a score.
    assert_no_leak(body, check_fractions=False)
    import json

    text = json.dumps(body, default=str).lower()
    for word in PIPELINE_WORDS:
        assert word not in text, f"{word!r} reached the job"


# ------------------------------------------------------------ it runs

def test_background_answers_202_and_the_job_completes(app_client, org, rule_reader):
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    progress_token = str(uuid.uuid4())

    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true", "progress_token": progress_token},
                          headers=_auth(token))
    assert res.status_code == 202, res.text
    assert res.json() == {"progress_token": progress_token, "status": "running"}

    # TestClient runs background tasks after the response, before returning.
    job = _job(app_client, token, progress_token)
    assert job["status"] == "done"
    assert [s["key"] for s in job["stages"]] == ["read", "analyse", "finish"]
    assert all(s["status"] == "done" for s in job["stages"])
    assert job["template_file_id"] == template_id


def test_the_result_and_the_facts_are_recorded(app_client, org, rule_reader):
    from app.db import SessionLocal
    from app.models import TemplateManifest

    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true"}, headers=_auth(token))
    assert res.status_code == 202, res.text
    job = _job(app_client, token, res.json()["progress_token"])

    facts = job["facts"]
    assert facts["placeholders_found"] == 2
    assert facts["instructions_found"] == 1
    assert facts["word_fields_found"] == 0
    assert "values_found" in facts and "optional_sections_found" in facts

    result = job["result"]
    db = SessionLocal()
    try:
        manifest = db.get(TemplateManifest, result["manifest_id"])
        assert manifest.template_file_id == template_id
        assert result["status"] == manifest.status
        assert result["field_count"] == len(manifest.fields) == facts["values_found"]
        assert result["condition_count"] == len(manifest.conditions)
        assert result["warning_count"] == len(manifest.warnings or [])
    finally:
        db.close()
    assert result["document_summary"]["placeholder_count"] == 2
    _assert_nothing_internal(job)


def test_the_synchronous_mode_is_unchanged(app_client, org, rule_reader):
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    progress_token = str(uuid.uuid4())
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"progress_token": progress_token}, headers=_auth(token))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["template_file_id"] == template_id
    assert body["fields"]
    assert "approval_blocked_reason" in body
    # And it still leaves its steps where a poller can read them.
    assert _job(app_client, token, progress_token)["status"] == "done"


# ------------------------------------------------------------ while running

def _running_reading(org_id, project_id, user_id, template_id) -> str:
    """A reading that is mid-flight: the row a background run writes before
    its first stage, then one step under way."""
    from app.compile_progress import DETERMINISTIC, CompileProgress

    token = str(uuid.uuid4())
    progress = CompileProgress(token, org_id=org_id, project_id=project_id, user_id=user_id,
                               template_file_id=template_id)
    progress.start()
    with progress.stage("parse", "Reading the document", DETERMINISTIC):
        progress.fact(placeholders_found=3)
    progress._stages.append({"key": "agentic", "label": "x", "kind": "model",
                             "status": "running", "started_at": None, "detail": "round 2"})
    progress._write(status="running")
    return token


def test_a_second_reading_of_the_same_template_is_refused(app_client, org, rule_reader):
    token, project_id, org_id, user_id = org
    template_id = _upload(app_client, token, project_id)
    _running_reading(org_id, project_id, user_id, template_id)

    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true"}, headers=_auth(token))
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "COMPILE_IN_PROGRESS"


def test_the_list_shows_a_reading_while_it_runs(app_client, org):
    token, project_id, org_id, user_id = org
    template_id = _upload(app_client, token, project_id)
    idle = app_client.get(f"/api/v1/projects/{project_id}/templates", headers=_auth(token)).json()
    assert next(t for t in idle["items"] if t["id"] == template_id)["reading"] is None

    job_token = _running_reading(org_id, project_id, user_id, template_id)
    items = app_client.get(f"/api/v1/projects/{project_id}/templates",
                           headers=_auth(token)).json()["items"]
    reading = next(t for t in items if t["id"] == template_id)["reading"]
    assert reading == {"progress_token": job_token, "status": "running", "step_index": 1}

    job = _job(app_client, token, job_token)
    assert job["facts"] == {"placeholders_found": 3}
    assert job["result"] is None
    _assert_nothing_internal(job)


def test_a_stale_reading_does_not_hold_the_template(app_client, org):
    """A restart mid-read leaves a row marked running forever. Past the horizon
    it is not shown, and does not refuse a retry."""
    from datetime import datetime, timedelta, timezone

    from app.compile_progress import STALE_AFTER_SECONDS
    from app.db import SessionLocal
    from app.models import GenerationJob

    token, project_id, org_id, user_id = org
    template_id = _upload(app_client, token, project_id)
    job_token = _running_reading(org_id, project_id, user_id, template_id)
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_token)
        job.started_at = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS + 60)
        db.commit()
    finally:
        db.close()
    items = app_client.get(f"/api/v1/projects/{project_id}/templates",
                           headers=_auth(token)).json()["items"]
    assert next(t for t in items if t["id"] == template_id)["reading"] is None


def test_a_token_that_is_not_a_uuid_is_refused(app_client, org):
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true", "progress_token": "1"},
                          headers=_auth(token))
    assert res.status_code == 422


# ------------------------------------------------------------ when it fails

def test_a_failure_is_generic_and_leaks_nothing(app_client, org, monkeypatch):
    from app.routers import manifests as mod

    def explode(path, **kwargs):
        raise RuntimeError("anthropic 529 overloaded at /srv/app/compiler/chunk_3.py round 4")

    monkeypatch.setattr(mod, "compile_agentic_template", explode)
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true"}, headers=_auth(token))
    assert res.status_code == 202, res.text

    job = _job(app_client, token, res.json()["progress_token"])
    assert job["status"] == "failed"
    assert job["error"]
    for secret in ("anthropic", "529", "/srv", "chunk", "round", "RuntimeError"):
        assert secret not in job["error"]
    assert job["result"] is None
    _assert_nothing_internal(job)

    # And the template is free to be read again.
    items = app_client.get(f"/api/v1/projects/{project_id}/templates",
                           headers=_auth(token)).json()["items"]
    assert next(t for t in items if t["id"] == template_id)["reading"] is None


def test_a_reading_that_does_not_converge_ends_failed_with_the_public_reason(app_client, org, monkeypatch):
    from app.manifests.public import GENERIC_READ_FAILURE
    from app.routers import manifests as mod

    class _Failed:
        ok = False
        reason = "3 fault(s) remained after 4 review round(s)"

        class manifest:  # noqa: N801
            compiled_by = "llm_failed"
            fields = []
            conditions = []
            blocks = []
            delete_always = []
            confidence = 0.0
            prescan_summary = {}
            notes = ["3 fault(s) remained after 4 review round(s); the first is: claim it"]

        def transcript_dicts(self):
            return []

    monkeypatch.setattr(mod, "compile_agentic_template", lambda path, **k: _Failed())
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true"}, headers=_auth(token))
    job = _job(app_client, token, res.json()["progress_token"])
    assert job["status"] == "failed"
    assert job["error"] == GENERIC_READ_FAILURE
    assert job["result"]["status"] == "failed"
    assert job["result"]["manifest_id"]
    _assert_nothing_internal(job)


def test_another_tenant_cannot_read_the_job(app_client, org, two_orgs, rule_reader):
    token, project_id, *_ = org
    template_id = _upload(app_client, token, project_id)
    res = app_client.post(f"/api/v1/templates/{template_id}/compile-manifest",
                          params={"background": "true"}, headers=_auth(token))
    other_token = two_orgs[2]
    assert app_client.get(f"/api/v1/jobs/{res.json()['progress_token']}",
                          headers=_auth(other_token)).status_code == 404
