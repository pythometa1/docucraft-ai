"""What a signed-in customer can learn about how the product works.

Nothing, is the answer these tests hold the API to. A response may say what was
found and what to do about it; it may not say which AI vendor or model did the
work, how a mapping was scored (signals, weights, bands, thresholds), or how a
template was read (stages, parts, review rounds, the model's reasoning).

Each test builds a row that carries every internal value the engine really
stores -- a model name in `compiled_by`, a confidence, a reasoning string, colour
counts, a review transcript -- and checks none of it reaches the wire.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

VENDOR_MARKERS = ("gpt", "claude", "gemini", "openai", "anthropic", "llm:")
FORBIDDEN_KEYS = ("score", "weight", "weights", "band", "bands", "confidence",
                  "compiled_by", "model", "model_profile", "evidence", "similarity",
                  "source_hint", "compiled_from", "boundary_method", "blue_spans", "red_spans",
                  "agent_log", "compile_transcript", "rules_would_have_fallen_short",
                  "rule_shortfall_reason", "prescan_summary")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _keys(value) -> set:
    if isinstance(value, dict):
        out = set(value)
        for v in value.values():
            out |= _keys(v)
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= _keys(v)
        return out
    return set()


def _fractions(value) -> list:
    """Every float strictly between 0 and 1 -- the shape of a score or threshold."""
    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        return [value] if 0.0 < value < 1.0 else []
    if isinstance(value, dict):
        return [f for v in value.values() for f in _fractions(v)]
    if isinstance(value, list):
        return [f for v in value for f in _fractions(v)]
    return []


def assert_no_leak(body, *, allow_keys=(), check_fractions=True):
    text = json.dumps(body, default=str).lower()
    for marker in VENDOR_MARKERS:
        assert marker not in text, f"{marker!r} reached the response"
    keys = _keys(body)
    for key in FORBIDDEN_KEYS:
        if key not in allow_keys:
            assert key not in keys, f"key {key!r} reached the response"
    if check_fractions:
        assert _fractions(body) == [], "a score or threshold reached the response"


_DISPLAY_IDS = iter(range(95001, 96000))


@pytest.fixture
def org(app_client):
    """A fresh organisation per test, so the spend and rows made here never
    land in another module's tenant. `display_id` is unique across the table."""
    from app.db import SessionLocal
    from app.models import Organization, Project, User
    from app.security import create_access_token, hash_password

    db = SessionLocal()
    try:
        o = Organization(name="IpBoundaryOrg")
        db.add(o)
        db.flush()
        u = User(org_id=o.id, email=f"ip-{o.id[:8]}@boundary.test", full_name="IP",
                 password_hash=hash_password("pw"), role_key="org_admin")
        db.add(u)
        db.flush()
        p = Project(org_id=o.id, display_id=next(_DISPLAY_IDS), name="IP", region="Europe",
                    function="Human Resources", document_type="Offer Letter",
                    language="English", status="pending", created_by=u.id)
        db.add(p)
        db.flush()
        out = (create_access_token(u.id, o.id), p.id, o.id, u.id)
        db.commit()
        return out
    finally:
        db.close()


# ---- manifests ----

def _internal_manifest(org_id, project_id, user_id, *, status="draft", notes=None):
    """A manifest row carrying every internal value a real compile stores."""
    from app.db import SessionLocal
    from app.models import TemplateFile, TemplateManifest, TemplateVersion

    db = SessionLocal()
    try:
        tf = TemplateFile(org_id=org_id, project_id=project_id, name="ip.docx",
                          status="ready", created_by=user_id)
        db.add(tf)
        db.flush()
        tv = TemplateVersion(template_file_id=tf.id, org_id=org_id, version_no=1,
                             blob_path="templates/missing.docx", created_by=user_id)
        db.add(tv)
        db.flush()
        tf.current_version_id = tv.id
        m = TemplateManifest(
            org_id=org_id, template_file_id=tf.id, template_version_id=tv.id, version_no=1,
            status=status,
            fields=[{"id": "name", "type": "string", "required": True,
                     "source_hint": "The model thinks this is the colleague's name",
                     "compiled_from": "The model thinks this is the colleague's name",
                     "confidence": 0.83,
                     "slots": [{"kind": "text_match", "text": "<Name>",
                                "paragraph_index": 0, "span_index": 1}]}],
            conditions=[{"id": "is_full_time", "expression": "employment_type == 'Full time'",
                         "keeps_blocks": ["blk_0"], "compiled_from": "Delete if part time",
                         "confidence": 0.71}],
            blocks=[{"id": "blk_0", "start_paragraph": 1, "end_paragraph": 2,
                     "boundary_method": "llm_refined", "contains_fields": []}],
            delete_always=[], confidence=0.62, compiled_by="llm:claude-opus-5",
            prescan_summary={
                "paragraph_count": 12, "blue_spans": 3, "red_spans": 2, "mergefields": 1,
                "notes": notes or ["Read by claude-opus-5 in 2 part(s) over 3 review round(s)."],
                "agent_log": [{"stage": "scan", "chunks": 2,
                               "rules_would_have_fallen_short": True,
                               "rule_shortfall_reason": "no colour"}],
            },
            compile_transcript=[{"stage": "review", "round": 1, "model": "claude-opus-5"}],
            created_by=user_id,
        )
        db.add(m)
        db.commit()
        return m.id, tf.id
    finally:
        db.close()


def test_the_manifest_get_is_an_allow_list(app_client, org):
    token, project_id, org_id, user_id = org
    manifest_id, template_id = _internal_manifest(org_id, project_id, user_id)

    body = app_client.get(f"/api/v1/template-manifests/{manifest_id}", headers=_auth(token)).json()

    assert_no_leak(body)
    assert set(body) == {
        "id", "template_file_id", "template_version_id", "version_no", "status", "fields",
        "conditions", "blocks", "document_summary", "failure_reason", "created_at",
        "approved_by", "approved_at", "warnings", "warning_dispositions",
    }
    # What the screens need survives: slots for highlighting, the condition's
    # sentence, the block range.
    assert body["fields"][0]["slots"][0]["paragraph_index"] == 0
    assert body["conditions"][0]["expression"]
    assert body["blocks"][0]["start_paragraph"] == 1
    # The colour counts under the document's own names.
    assert body["document_summary"] == {"paragraph_count": 12, "placeholder_count": 3,
                                        "instruction_count": 2, "word_field_count": 1}

    listed = app_client.get(f"/api/v1/templates/{template_id}/manifests", headers=_auth(token)).json()
    assert_no_leak(listed)


def test_a_review_edit_cannot_erase_what_the_client_never_saw(app_client, org):
    """The screen sends back the scrubbed items. The stored reasoning and
    confidence must survive the round trip, or indexing and the compile
    assertions lose what they read."""
    from app.db import SessionLocal
    from app.models import TemplateManifest

    token, project_id, org_id, user_id = org
    manifest_id, _ = _internal_manifest(org_id, project_id, user_id)
    served = app_client.get(f"/api/v1/template-manifests/{manifest_id}", headers=_auth(token)).json()

    fields = served["fields"]
    fields[0]["required"] = False
    res = app_client.patch(f"/api/v1/template-manifests/{manifest_id}", headers=_auth(token),
                           json={"fields": fields, "conditions": served["conditions"],
                                 "blocks": served["blocks"]})
    assert res.status_code == 200, res.text
    assert_no_leak(res.json())

    db = SessionLocal()
    try:
        row = db.get(TemplateManifest, manifest_id)
        assert row.fields[0]["required"] is False
        assert row.fields[0]["source_hint"].startswith("The model thinks")
        assert row.fields[0]["confidence"] == 0.83
        assert row.conditions[0]["compiled_from"] == "Delete if part time"
        assert row.blocks[0]["boundary_method"] == "llm_refined"
    finally:
        db.close()


def test_a_failed_reading_says_so_without_the_pipeline(app_client, org):
    token, project_id, org_id, user_id = org
    manifest_id, template_id = _internal_manifest(
        org_id, project_id, user_id, status="failed",
        notes=["3 fault(s) remained after 4 review round(s); the first is: claim it"])

    body = app_client.get(f"/api/v1/template-manifests/{manifest_id}", headers=_auth(token)).json()
    assert_no_leak(body)
    reason = body["failure_reason"]
    assert reason and "round" not in reason and not any(ch.isdigit() for ch in reason)

    templates = app_client.get(f"/api/v1/projects/{project_id}/templates",
                               headers=_auth(token)).json()
    row = next(t for t in templates["items"] if t["id"] == template_id)
    assert row["compile_error"] == reason


# ---- blueprints ----

def test_a_blueprint_hides_the_model_that_read_it(app_client, org):
    from app.db import SessionLocal
    from app.models import TemplateBlueprint, TemplateBlueprintVersion

    token, project_id, org_id, user_id = org
    db = SessionLocal()
    try:
        row = TemplateBlueprint(org_id=org_id, project_id=project_id, name="IP blueprint",
                                kind="legacy", status="draft", created_by=user_id)
        db.add(row)
        db.flush()
        version = TemplateBlueprintVersion(
            blueprint_id=row.id, org_id=org_id, version_no=1,
            body={"blocks": []},
            objects=[
                {"object_id": "name", "object_type": "FIELD", "confidence": 0.8,
                 "source_hint": "model reasoning", "compiled_from": "model reasoning",
                 "evidence": ["family_inheritance:0.93"]},
                {"object_id": "is_full_time", "object_type": "CONDITION",
                 "compiled_from": "Delete if part time", "confidence": 0.7},
                {"object_id": "blk_0", "object_type": "SECTION", "boundary_method": "llm"},
            ],
            findings=[],
            provenance={"kind": "legacy", "compiled_by": "llm:gpt-5", "confidence": 0.62,
                        "model": "gemini-2.5-pro", "read_from": "compile"},
            created_by=user_id)
        db.add(version)
        db.flush()
        row.current_version_id = version.id
        db.commit()
        blueprint_id = row.id
    finally:
        db.close()

    body = app_client.get(f"/api/v1/template-blueprints/{blueprint_id}", headers=_auth(token)).json()
    # A condition keeps the author's own sentence: the studio joins runs to
    # conditions on it.
    assert_no_leak(body, allow_keys=("compiled_from",))
    objects = {o["object_id"]: o for o in body["version"]["objects"]}
    assert "compiled_from" not in objects["name"]
    assert objects["is_full_time"]["compiled_from"] == "Delete if part time"
    assert body["version"]["provenance"] == {"kind": "legacy", "read_from": "compile"}


# ---- analytics ----

def test_the_cost_report_names_no_model_and_counts_no_tokens(app_client, org):
    from app.db import SessionLocal
    from app.llm.metering import UsageMeter

    token, _project_id, org_id, _user_id = org
    db = SessionLocal()
    try:
        meter = UsageMeter(db=db, org_id=org_id)
        meter.record(capability="Compiling a template", purpose="compile",
                     model="claude-opus-5", input_tokens=1000, output_tokens=500, outcome="ok")
        meter.record(capability="Chat", purpose="generate", model="llama-9-enormous",
                     input_tokens=10, output_tokens=5, outcome="ok")
        db.commit()
    finally:
        db.close()

    body = app_client.get("/api/v1/analytics/cost", params={"range": "7d"},
                          headers=_auth(token)).json()
    # Money is legitimately a fraction of a dollar, so only keys and names are checked.
    assert_no_leak(body, check_fractions=False)
    assert "token" not in json.dumps(body)
    assert "llama" not in json.dumps(body)
    assert set(body) == {"summary", "trend", "by_activity", "by_template"}
    assert body["summary"]["unpriced_calls"] >= 1
    assert all(set(item) == {"bucket", "cost_usd"} for item in body["trend"]["items"])
    assert {row["activity"] for row in body["by_activity"]} <= {
        "Template reading", "Drafting", "Chat", "Editing", "Other"}
    assert "Template reading" in {row["activity"] for row in body["by_activity"]}

    kpis = app_client.get("/api/v1/analytics/kpis", params={"range": "7d"},
                          headers=_auth(token)).json()
    assert "llama" not in json.dumps(kpis) and "claude" not in json.dumps(kpis)

    compiles = app_client.get("/api/v1/analytics/compiles", params={"range": "7d"},
                              headers=_auth(token)).json()
    assert "agentic" not in json.dumps(compiles)
    assert "rationale" not in _keys(compiles)
    assert compiles["duration"]["label"] == "Template reading time"


# ---- compile progress ----

def test_compile_progress_is_three_plain_steps(app_client, org):
    from app.compile_progress import DETERMINISTIC, EMBEDDING, MODEL, RETRIEVAL, CompileProgress

    token, project_id, org_id, user_id = org
    job_token = str(uuid.uuid4())
    progress = CompileProgress(job_token, org_id=org_id, project_id=project_id, user_id=user_id)
    with progress.stage("parse", "Reading the document", DETERMINISTIC):
        progress.note("12 paragraphs, 3 marked runs, 1 merge fields")
    with progress.stage("retrieval", "Looking up column names", RETRIEVAL):
        progress.note("16 column(s) found")
    with progress.stage("agentic", "Reading and reviewing the template", MODEL):
        with progress.stage("chunk", "Splitting the template for reading", DETERMINISTIC):
            pass
        with progress.stage("review", "Reviewing the reading (round 2)", MODEL):
            progress.note("applied 3 correction(s)")

    mid = app_client.get(f"/api/v1/jobs/{job_token}", headers=_auth(token)).json()
    steps = mid["progress"]["stages"]
    assert [s["key"] for s in steps] == ["read", "analyse", "finish"]
    assert [s["status"] for s in steps] == ["done", "done", "pending"]
    for step in steps:
        # No `kind` (model / retrieval / embedding) and no `detail`.
        assert set(step) == {"key", "label", "status", "elapsed_seconds"}
    assert_no_leak(mid, check_fractions=False)
    text = json.dumps(mid).lower()
    for word in ("round", "chunk", "part(s)", "correction", "column(s)", "retrieval", "embedding"):
        assert word not in text, word

    with progress.stage("embedding", "Indexing the fields for future templates", EMBEDDING):
        pass
    progress.finish()
    done = app_client.get(f"/api/v1/jobs/{job_token}", headers=_auth(token)).json()
    assert [s["status"] for s in done["progress"]["stages"]] == ["done", "done", "done"]
    assert [s["label"] for s in done["progress"]["stages"]] == [
        "Reading your template", "Understanding the structure", "Finishing up"]


def test_a_failed_step_is_named_by_its_public_label(app_client, org):
    from app.compile_progress import MODEL, CompileProgress

    token, project_id, org_id, user_id = org
    job_token = str(uuid.uuid4())
    progress = CompileProgress(job_token, org_id=org_id, project_id=project_id, user_id=user_id)
    with pytest.raises(RuntimeError):
        with progress.stage("write", "Reading 3 part(s) of the template", MODEL):
            raise RuntimeError("anthropic said no")

    body = app_client.get(f"/api/v1/jobs/{job_token}", headers=_auth(token)).json()
    assert body["status"] == "failed"
    assert [s["status"] for s in body["progress"]["stages"]] == ["done", "failed", "pending"]
    assert body["error"].startswith("Understanding the structure failed.")
    assert_no_leak(body, check_fractions=False)


# ---- drafts ----

@pytest.mark.parametrize("module", ["app.csr.router", "app.cmc.router", "app.safety.router"])
def test_a_draft_does_not_name_the_model_that_wrote_it(module):
    import importlib

    draft_out = importlib.import_module(module)._draft_out
    draft = SimpleNamespace(
        id="d1", version=1, content="Draft text.", created_by="ai", origin="model",
        model="claude-sonnet-5", prompt_version="v1", generation_params={"source_map": []},
        created_at=None)
    body = draft_out(draft)
    assert "model" not in body
    assert "claude" not in json.dumps(body, default=str)


# ---- internal endpoints ----

INTERNAL_ROUTES = (
    ("get", "/api/v1/metrics", None),
    ("get", "/api/v1/metrics/calibration-log", None),
    ("get", "/api/v1/admin/model-rates", None),
    ("put", "/api/v1/admin/model-rates",
     {"model": "claude-opus-5", "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}),
    ("post", "/api/v1/admin/model-rates:reset", {"model": "claude-opus-5"}),
    ("post", "/api/v1/metrics/escaped-errors",
     {"document_version_id": "nope", "object_id": "x", "detail": "wrong"}),
)


@pytest.mark.parametrize("method,path,body", INTERNAL_ROUTES)
def test_internal_endpoints_are_not_found_in_production(org, method, path, body):
    from fastapi.testclient import TestClient

    from app.main import create_app

    token, *_ = org
    client = TestClient(create_app("production"))
    res = getattr(client, method)(path, headers=_auth(token),
                                  **({"json": body} if body is not None else {}))
    assert res.status_code == 404, res.text
    assert res.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_internal_endpoints_answer_in_development(org):
    from fastapi.testclient import TestClient

    from app.main import create_app

    token, *_ = org
    client = TestClient(create_app("development"))
    assert client.get("/api/v1/metrics", headers=_auth(token)).status_code == 200
    assert client.get("/api/v1/metrics/calibration-log", headers=_auth(token)).status_code == 200
    assert client.get("/api/v1/admin/model-rates", headers=_auth(token)).status_code == 200
    # Reaches the handler: an unknown version is the handler's 404, not the gate's.
    res = client.post("/api/v1/metrics/escaped-errors", headers=_auth(token),
                      json={"document_version_id": "nope", "object_id": "x", "detail": "wrong"})
    assert res.json()["detail"]["error"]["code"] != "NOT_FOUND"


def test_the_setting_overrides_the_environment(org, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import create_app

    token, *_ = org
    monkeypatch.setattr(settings, "internal_endpoints_enabled", True)
    assert TestClient(create_app("production")).get(
        "/api/v1/admin/model-rates", headers=_auth(token)).status_code == 200
    monkeypatch.setattr(settings, "internal_endpoints_enabled", False)
    assert TestClient(create_app("development")).get(
        "/api/v1/admin/model-rates", headers=_auth(token)).status_code == 404
