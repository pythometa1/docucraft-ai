"""What a client is allowed to learn about how the product works.

Exception text names paths on the host, the libraries that read documents, the
model vendor and the settings that configure it. These tests hold the lines
where that text used to reach a response: the 500 handler, the job endpoint,
the 503 for an unconfigured model, and the API schema in production.
"""

import os

import docx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    GeneratedDocument, GenerationJob, Project, TemplateFile, TemplateManifest, TemplateVersion,
    User,
)
from app.storage import abs_path

#: Strings that would each give away something about the build.
SECRETS = ("/srv/documind/app/", "OPENAI_API_KEY", "backend/.env", "Traceback", "RuntimeError")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} reached the client: {text}"


# ------------------------------------------------------------- the 500 handler
def test_an_unhandled_exception_answers_with_a_reference_not_its_text():
    from app.main import create_app

    app = create_app("development")

    @app.get("/__boom")
    def boom():
        raise RuntimeError("could not open /srv/documind/app/secret.docx; OPENAI_API_KEY rejected")

    res = TestClient(app, raise_server_exceptions=False).get("/__boom")

    assert res.status_code == 500
    body = res.json()["error"]
    assert body["code"] == "INTERNAL_ERROR"
    ref = body["details"]["ref"]
    assert len(ref) == 32 and int(ref, 16) >= 0
    assert body["message"] == (
        "Something went wrong on our side. If it keeps happening, contact support "
        f"with reference {ref}.")
    _assert_clean(res.text)


# ------------------------------------------------------ docs off in production
@pytest.mark.parametrize("env", ["production", "prod", "staging"])
def test_production_serves_no_schema_or_docs(env):
    from app.main import create_app

    client = TestClient(create_app(env))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_development_keeps_the_docs():
    from app.main import create_app

    client = TestClient(create_app("development"))
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_readyz_names_no_components_in_production(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_readiness", lambda: (False, {"database": False, "redis": True}))
    res = TestClient(main.create_app("production")).get("/readyz")
    assert res.status_code == 503
    assert res.json() == {"status": "unavailable"}

    monkeypatch.setattr(main, "_readiness", lambda: (True, {"database": True, "redis": True}))
    res = TestClient(main.create_app("production")).get("/readyz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    # Development may say which one, because the person reading it is us.
    dev = TestClient(main.create_app("development")).get("/readyz")
    assert dev.json()["checks"] == {"database": True, "redis": True}


# ----------------------------------------------------- the unconfigured model
def test_llm_not_configured_is_vendor_neutral(app_client, two_orgs):
    token_a, project_a, *_ = two_orgs
    created = app_client.post(f"/api/v1/projects/{project_a}/conversations",
                              headers=_headers(token_a), json={"title": "t"})
    res = app_client.post(f"/api/v1/conversations/{created.json()['id']}/messages",
                          headers=_headers(token_a), json={"text": "hello"})

    assert res.status_code == 503, res.text
    body = res.json()["error"]
    assert body["code"] == "LLM_NOT_CONFIGURED"  # the frontend switches on this
    assert body["message"] == (
        "AI features are not available right now. Please contact your administrator.")
    for leak in ("API_KEY", "LLM_PROVIDER", ".env", "anthropic", "openai", "gemini"):
        assert leak.lower() not in res.text.lower(), leak


def test_a_provider_exception_is_not_carried_on_the_result(monkeypatch):
    from app.llm import provider as llm

    class _Boom:
        def create(self, **_kwargs):
            raise RuntimeError("401 from https://api.openai.com/v1 with key sk-live-abc")

    p = llm.AnthropicProvider("k", "m", "cm")
    monkeypatch.setattr(p, "_client", type("C", (), {"messages": _Boom()})(), raising=False)
    result = p.structured(system="s", prompt="p", schema={"type": "object"})
    assert result.data is None
    assert result.error == llm.PROVIDER_CALL_FAILED
    assert "openai" not in result.error and "sk-" not in result.error


# ----------------------------------------------------------- the job endpoint
@pytest.fixture()
def one_field_manifest(app_client, two_orgs):
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))
        template_rel = f"templates/{project.id}/public-errors.docx"
        os.makedirs(str(abs_path(template_rel).parent), exist_ok=True)
        document = docx.Document()
        document.add_paragraph("Dear <full_name>,")
        document.save(str(abs_path(template_rel)))
        template_file = TemplateFile(org_id=project.org_id, project_id=project.id,
                                     name="public-errors.docx", status="parsed", created_by=user.id)
        db.add(template_file)
        db.flush()
        version = TemplateVersion(template_file_id=template_file.id, org_id=project.org_id,
                                  version_no=1, blob_path=template_rel, created_by=user.id)
        db.add(version)
        db.flush()
        template_file.current_version_id = version.id
        manifest = TemplateManifest(
            org_id=project.org_id, template_file_id=template_file.id,
            template_version_id=version.id, version_no=1, status="approved",
            fields=[{"id": "full_name", "type": "text", "required": True,
                     "slots": [{"paragraph_index": 0, "span_index": 0, "text": "<full_name>",
                                "field_id": "full_name"}]}],
            conditions=[], blocks=[], delete_always=[], created_by=user.id)
        db.add(manifest)
        db.commit()
        return project.org_id, project.id, manifest.id, user.id
    finally:
        db.close()


def _queued_job(org_id, project_id, user_id) -> str:
    db = SessionLocal()
    try:
        job = GenerationJob(org_id=org_id, project_id=project_id, status="queued",
                            languages=["en"], created_by=user_id,
                            token_usage={"input": 1234, "output": 56, "model": "claude-x"})
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_a_crashed_batch_returns_no_traceback_or_path(app_client, two_orgs, one_field_manifest):
    from app.generation.batch_runner import BATCH_FAILED_MESSAGE, run_batch

    token_a = two_orgs[0]
    org_id, project_id, manifest_id, user_id = one_field_manifest
    job_id = _queued_job(org_id, project_id, user_id)

    # A source that is not on disk: the runner dies inside its own try, with an
    # exception that carries the absolute storage path.
    run_batch(job_id=job_id, manifest_id=manifest_id,
              source_blob_path=f"sources/{project_id}/does-not-exist.csv",
              source_file_type="csv", sheet=None, field_bindings={"full_name": "full_name"},
              value_map={}, row_indices=None, language="en", locale_override=None,
              user_id=user_id)

    res = app_client.get(f"/api/v1/jobs/{job_id}", headers=_headers(token_a))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "failed"
    assert body["error"] == BATCH_FAILED_MESSAGE
    assert "token_usage" not in body
    assert "does-not-exist" not in res.text
    assert str(abs_path("")) not in res.text
    assert "claude" not in res.text.lower()
    _assert_clean(res.text)


def test_a_row_that_raises_is_reported_without_its_exception(
        app_client, two_orgs, one_field_manifest, monkeypatch):
    from app.generation import batch_runner

    token_a = two_orgs[0]
    org_id, project_id, manifest_id, user_id = one_field_manifest
    job_id = _queued_job(org_id, project_id, user_id)
    source_rel = f"sources/{project_id}/public-errors.csv"
    os.makedirs(str(abs_path(source_rel).parent), exist_ok=True)
    abs_path(source_rel).write_text("full_name\nAda\nGrace\nLinus\n")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("lxml choked on /srv/documind/app/generation/docx_renderer.py:412")

    monkeypatch.setattr(batch_runner, "fill_template", _explode)
    batch_runner.run_batch(
        job_id=job_id, manifest_id=manifest_id, source_blob_path=source_rel,
        source_file_type="csv", sheet=None, field_bindings={"full_name": "full_name"},
        value_map={}, row_indices=None, language="en", locale_override=None, user_id=user_id)

    res = app_client.get(f"/api/v1/jobs/{job_id}", headers=_headers(token_a))
    assert res.status_code == 200, res.text
    rows = res.json()["progress"]["rows"]
    assert rows and all(r["status"] == "failed" for r in rows)
    assert {r["error"] for r in rows} == {batch_runner.ROW_FAILED_MESSAGE}
    assert "lxml" not in res.text and "docx_renderer" not in res.text
    _assert_clean(res.text)


# ------------------------------------------- the import generation.py lacked
def test_library_generation_reaches_a_stored_document(app_client, two_orgs, monkeypatch):
    """`generate_from_library` called `_next_display_id` without importing it,
    so every request past token resolution answered 500. No model is needed to
    prove the line runs: the provider and the resolver are stood in for."""
    from app.routers import generation

    token_a, project_a, *_ = two_orgs
    created = app_client.post("/api/v1/template-library", headers=_headers(token_a), json={
        "name": "Import regression", "category": "test",
        "content_html": '<p>Hello <span data-token="prompt" data-prompt="Greet">x</span></p>'})
    assert created.status_code == 201, created.text

    monkeypatch.setattr(generation, "get_llm_provider", lambda *a, **k: object())
    monkeypatch.setattr(generation, "resolve_token_unit", lambda **_k: "a greeting")

    res = app_client.post(f"/api/v1/template-library/{created.json()['id']}/generate",
                          headers=_headers(token_a), json={"project_id": project_a})
    assert res.status_code == 201, res.text
    document_id = res.json()["document_id"]

    db = SessionLocal()
    try:
        document = db.get(GeneratedDocument, document_id)
        assert document is not None and document.current_version_id is not None
        assert document.display_id > 50000
    finally:
        db.close()
