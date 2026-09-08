"""The CSR module end to end: upload sources, index them, draft a section
FROM them, and prove the draft is grounded in what was uploaded.

This is the module's whole promise under test. Not "does an endpoint return
201" -- does the section the model wrote actually come from the study
documents this project uploaded, cite them by marker, refuse to invent what
they do not contain, and stay inside this tenant's own sources.

The model is stubbed: what is being pinned here is the pipeline around it --
retrieval, the prompt it is handed, citation resolution, versioning and the
gaps -- not the vendor's prose.
"""

import io

import pytest

from app.csr.ich_e3 import SECTIONS


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org_a(two_orgs):
    token_a, project_a, _tb, _pb = two_orgs
    return token_a, project_a


@pytest.fixture
def csr_project(app_client, org_a):
    """A CSR project with the built-in ICH E3 tree, ready for sources."""
    token, _ = org_a
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "ONC-2026-014 CSR flow", "function": "Clinical",
        "document_type": "Clinical Study Report",
        "region": "Global", "language": "English"}).json()
    csr = app_client.post("/api/v1/csr/projects", headers=_auth(token), json={
        "project_id": portal["id"],
        "study": {"protocol_number": "ONC-2026-014",
                  "title": "A Phase 2 study of drug X", "sponsor": "Acme Pharma",
                  "phase": "2", "indication": "NSCLC",
                  "principal_investigator": "Dr. A. Rao"},
        "compound_name": "Drug X", "blinding": "double_blind"}).json()
    app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                    headers=_auth(token), json={"source": "builtin_ich_e3"})
    return token, csr["id"]


PROTOCOL_TEXT = (
    "9.1 Overall Study Design\n"
    "This was a randomised, double-blind, placebo-controlled study conducted at 12 sites.\n"
    "9.4.6 Blinding\n"
    "Patients, investigators and the sponsor remained blinded until database lock.\n"
)

SAP_TEXT = (
    "Statistical Analysis Plan\n"
    "The primary efficacy analysis used analysis of covariance on the full analysis set, "
    "with treatment and baseline score as covariates. A sample size of 120 patients gives "
    "90% power at a two-sided alpha of 0.05.\n"
)

TLF_CSV = (
    "Table 14.1.1 Disposition of Patients\n"
    "Site,Enrolled,Completed,Withdrawn\n"
    "Pune General,80,70,10\n"
    "Mumbai Central,40,34,6\n"
)


def _upload(app_client, token, csr_id, files):
    """files: list of (filename, bytes, doc_type)."""
    payload = [("files", (name, io.BytesIO(data), "text/plain")) for name, data, _t in files]
    payload += [("doc_types", (None, doc_type)) for _n, _d, doc_type in files]
    return app_client.post(f"/api/v1/csr/projects/{csr_id}/documents",
                           headers=_auth(token), files=payload)


def _process(app_client, token, csr_id):
    started = app_client.post(f"/api/v1/csr/projects/{csr_id}/process",
                              headers=_auth(token))
    assert started.status_code == 202, started.text
    # The ingest threads are daemons; poll until every file settles.
    for _ in range(200):
        status = app_client.get(f"/api/v1/csr/projects/{csr_id}/processing-status",
                                headers=_auth(token)).json()
        if not status["in_flight"]:
            return status
        import time
        time.sleep(0.05)
    raise AssertionError("documents never finished processing")


@pytest.fixture
def indexed_csr(app_client, csr_project):
    token, csr_id = csr_project
    uploaded = _upload(app_client, token, csr_id, [
        ("protocol.txt", PROTOCOL_TEXT.encode(), "protocol"),
        ("sap.txt", SAP_TEXT.encode(), "sap"),
        ("tlf_14_1_1.csv", TLF_CSV.encode(), "tlf"),
    ])
    assert uploaded.status_code == 201, uploaded.text
    status = _process(app_client, token, csr_id)
    assert all(d["processing_status"] == "done" for d in status["items"]), status["items"]
    return token, csr_id, status


@pytest.fixture
def stub_model(monkeypatch):
    """A provider that answers with whatever the test wants, and records the
    prompt it was handed -- so a test can assert the model was SHOWN the
    uploaded sources, not merely that it replied."""
    import app.csr.drafting as drafting

    seen = {}

    def install(reply: str):
        class _Result:
            def __init__(self):
                self.text = reply
                self.content = reply
                self.data = {"content": reply}
                self.model = "stub-model"
                self.error = None

        class _Provider:
            def generate(self, **kwargs):
                seen.update(kwargs)
                return _Result()

            def structured(self, *, system, prompt, schema, purpose="generate"):
                seen["system"] = system
                seen["prompt"] = prompt
                return _Result()

        monkeypatch.setattr(drafting, "get_llm_provider", lambda *a, **k: _Provider())
        return seen

    return install


# ---------------------------------------------------------------- ingestion

def test_uploaded_sources_become_retrievable(app_client, indexed_csr):
    _token, _csr_id, status = indexed_csr
    assert status["readiness"]["ready_to_generate"] is True
    assert status["readiness"]["missing_required"] == []
    by_type = {d["doc_type"]: d for d in status["items"]}
    assert by_type["tlf"]["chunk_count"] >= 1
    assert by_type["protocol"]["chunk_count"] >= 1


def test_an_untagged_or_unreadable_upload_is_refused(app_client, csr_project):
    token, csr_id = csr_project
    mismatched = app_client.post(
        f"/api/v1/csr/projects/{csr_id}/documents", headers=_auth(token),
        files=[("files", ("a.txt", io.BytesIO(b"x"), "text/plain")),
               ("files", ("b.txt", io.BytesIO(b"y"), "text/plain")),
               ("doc_types", (None, "protocol"))])
    assert mismatched.status_code == 422
    assert mismatched.json()["detail"]["error"]["code"] == "CSR_TAGS_MISMATCH"

    bad_type = _upload(app_client, token, csr_id,
                       [("a.txt", b"x", "not_a_doc_type")])
    assert bad_type.status_code == 422
    assert bad_type.json()["detail"]["error"]["code"] == "CSR_BAD_DOC_TYPE"

    bad_suffix = app_client.post(
        f"/api/v1/csr/projects/{csr_id}/documents", headers=_auth(token),
        files=[("files", ("scan.tiff", io.BytesIO(b"x"), "image/tiff")),
               ("doc_types", (None, "protocol"))])
    assert bad_suffix.status_code == 422
    assert bad_suffix.json()["detail"]["error"]["code"] == "CSR_UNSUPPORTED_FILE"


def test_one_failed_file_does_not_block_the_others(app_client, csr_project):
    """A source that cannot be read fails alone, with its reason on its own
    row, and is retryable by itself."""
    token, csr_id = csr_project
    _upload(app_client, token, csr_id, [
        ("good.txt", PROTOCOL_TEXT.encode(), "protocol"),
        ("empty.csv", b"   \n  \n", "tlf"),
    ])
    status = _process(app_client, token, csr_id)
    by_name = {d["filename"]: d for d in status["items"]}
    assert by_name["good.txt"]["processing_status"] == "done"
    assert by_name["empty.csv"]["processing_status"] == "failed"
    assert by_name["empty.csv"]["error_message"]

    retried = app_client.post(f"/api/v1/csr/documents/{by_name['empty.csv']['id']}/retry",
                              headers=_auth(token))
    assert retried.status_code == 202


# ---------------------------------------------------------------- generation

def test_a_section_is_drafted_from_the_uploaded_sources(
        app_client, indexed_csr, stub_model):
    """The heart of the module: the model is handed THIS project's extracts,
    and the draft it returns is stored with its citations resolved to the
    chunks those extracts came from."""
    token, csr_id, _status = indexed_csr
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    disposition = next(s for s in sections if s["section_number"] == "10.1")

    seen = stub_model(
        "10.1 Disposition of Patients\n"
        "A total of 120 patients were enrolled [S1, Table 14.1.1], of whom 104 completed "
        "the study [S1, Table 14.1.1] and 16 withdrew [S1, Table 14.1.1].\n"
        "[DATA NEEDED: reasons for discontinuation by category]")

    res = app_client.post(f"/api/v1/csr/sections/{disposition['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 201, res.text
    body = res.json()

    # The model was SHOWN this project's own sources. The extracts live in the
    # system message (that is where the spec's prompt template puts them), so
    # what matters is that they reached the model at all.
    shown = f"{seen.get('system') or ''}\n{seen.get('prompt') or ''}"
    assert "Table 14.1.1" in shown, "the disposition table never reached the model"
    assert "ONC-2026-014" in shown
    assert "[S1" in shown

    # What came back is stored, versioned, and its citations resolved.
    assert body["version"] == 1
    assert body["created_by"] == "ai"
    assert body["section"]["status"] == "draft"
    assert body["data_needed"] == ["reasons for discontinuation by category"]
    assert body["citations"], "citations were not resolved"
    resolved = [c for c in body["citations"] if c["chunk_id"]]
    assert resolved, "no citation resolved to an uploaded chunk"
    assert any(c["cited_value"] in ("120", "104", "16") for c in resolved)

    # The generation record answers "what did the model see?"
    assert body["generation_params"]["chunk_ids"]
    assert body["generation_params"]["prompt_version"]

    # And the Sources panel can show exactly those chunks.
    draft = app_client.get(f"/api/v1/csr/sections/{disposition['id']}/draft",
                           headers=_auth(token)).json()
    assert draft["sources"], "the draft carries no retrievable sources"
    assert any(s["table_id"] == "14.1.1" for s in draft["sources"])
    assert any("Enrolled" in s["content"] for s in draft["sources"])


def test_a_missing_source_becomes_a_gap_not_an_invention(
        app_client, csr_project, stub_model):
    """With no SAP uploaded, Section 9.7 must say what is missing rather than
    describe a statistical method nobody supplied."""
    token, csr_id = csr_project
    _upload(app_client, token, csr_id, [("protocol.txt", PROTOCOL_TEXT.encode(), "protocol")])
    _process(app_client, token, csr_id)

    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    stats = next(s for s in sections if s["section_number"] == "9.7")

    seen = stub_model("9.7 Statistical Methods\nANCOVA was used.")
    res = app_client.post(f"/api/v1/csr/sections/{stats['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 201, res.text
    body = res.json()

    # Section 9.7 retrieves from the SAP, and no SAP was uploaded. Rather than
    # ask the model to write statistics from a protocol, the engine declines
    # to call it at all and records what is missing -- an invented method is
    # worse than a stated gap.
    assert body["data_needed"], "a section with no evidence produced no gap"
    assert "sap" in body["data_needed"][0].lower()
    assert not seen.get("system"), "the model was called with no evidence to write from"


def test_generation_without_sources_is_refused(app_client, csr_project):
    token, csr_id = csr_project
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    intro = next(s for s in sections if s["section_number"] == "7")
    res = app_client.post(f"/api/v1/csr/sections/{intro['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 409
    assert res.json()["detail"]["error"]["code"] == "CSR_NO_SOURCES"


def test_regeneration_and_editing_both_append_versions(
        app_client, indexed_csr, stub_model):
    """Neither the model nor the writer ever overwrites: what each produced
    stays recoverable, which is the question asked of an AI-assisted CSR."""
    token, csr_id, _ = indexed_csr
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    design = next(s for s in sections if s["section_number"] == "9.1")

    seen = stub_model("9.1 Overall Study Design\nRandomised, double-blind [S1, p.1].")
    first = app_client.post(f"/api/v1/csr/sections/{design['id']}/generate",
                            headers=_auth(token), json={}).json()
    assert first["version"] == 1

    second = app_client.post(f"/api/v1/csr/sections/{design['id']}/generate",
                             headers=_auth(token),
                             json={"instruction": "shorten to one sentence"}).json()
    assert second["version"] == 2
    assert second["generation_params"]["instruction"] == "shorten to one sentence"
    assert "shorten to one sentence" in f"{seen.get('system') or ''}\n{seen.get('prompt') or ''}"

    edited = app_client.put(f"/api/v1/csr/sections/{design['id']}/draft",
                            headers=_auth(token),
                            json={"content": "9.1 Overall Study Design\nHand-written [S1, p.1]."})
    assert edited.status_code == 201
    assert edited.json()["version"] == 3
    assert edited.json()["created_by"] != "ai"

    listing = app_client.get(f"/api/v1/csr/sections/{design['id']}/draft",
                             headers=_auth(token)).json()
    assert listing["versions"] == [1, 2, 3]
    v1 = app_client.get(f"/api/v1/csr/sections/{design['id']}/draft",
                        headers=_auth(token), params={"version": 1}).json()
    assert "Randomised, double-blind" in v1["draft"]["content"]


def test_approval_needs_something_to_approve(app_client, indexed_csr, stub_model):
    token, csr_id, _ = indexed_csr
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    ethics = next(s for s in sections if s["section_number"] == "5.2")

    empty = app_client.patch(f"/api/v1/csr/sections/{ethics['id']}/status",
                             headers=_auth(token), json={"status": "approved"})
    assert empty.status_code == 409
    assert empty.json()["detail"]["error"]["code"] == "CSR_NOTHING_TO_REVIEW"

    stub_model("5.2 Ethical Conduct\nThe study followed GCP [S1, p.1].")
    app_client.post(f"/api/v1/csr/sections/{ethics['id']}/generate",
                    headers=_auth(token), json={})
    approved = app_client.patch(f"/api/v1/csr/sections/{ethics['id']}/status",
                                headers=_auth(token), json={"status": "approved"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


def test_a_container_heading_never_generates(app_client, indexed_csr):
    token, csr_id, _ = indexed_csr
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    container = next(s for s in sections if s["section_number"] == "9")
    res = app_client.post(f"/api/v1/csr/sections/{container['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CSR_SECTION_IS_CONTAINER"


# ---------------------------------------------------------------- isolation

def test_retrieval_never_crosses_projects(app_client, indexed_csr, org_a, stub_model):
    """A second CSR in the same org, with its own sources, must never be
    written from the first one's documents."""
    token, first_id, _ = indexed_csr
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Second study CSR", "function": "Clinical",
        "document_type": "Clinical Study Report",
        "region": "Global", "language": "English"}).json()
    second = app_client.post("/api/v1/csr/projects", headers=_auth(token), json={
        "project_id": portal["id"],
        "study": {"protocol_number": "CARD-2027-001", "title": "A cardiology study"}}).json()
    app_client.post(f"/api/v1/csr/projects/{second['id']}/template",
                    headers=_auth(token), json={"source": "builtin_ich_e3"})
    _upload(app_client, token, second["id"],
            [("other_protocol.txt", b"9.1 Overall Design\nAn open-label cardiology study.\n", "protocol"),
             ("other_sap.txt", b"Analysis by t-test.\n", "sap"),
             ("other_tlf.csv", b"Table 14.1.1 Disposition\nSite,Enrolled\nDelhi,25\n", "tlf")])
    _process(app_client, token, second["id"])

    sections = app_client.get(f"/api/v1/csr/projects/{second['id']}/sections",
                              headers=_auth(token)).json()["items"]
    disposition = next(s for s in sections if s["section_number"] == "10.1")
    seen = stub_model("10.1 Disposition\n25 patients were enrolled [S1, Table 14.1.1].")
    res = app_client.post(f"/api/v1/csr/sections/{disposition['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 201, res.text

    shown = f"{seen.get('system') or ''}\n{seen.get('prompt') or ''}"
    assert "Delhi" in shown, "the second project's own source never reached the model"
    assert "Pune General" not in shown, "the FIRST project's sources leaked into this draft"
    assert "Mumbai Central" not in shown


def test_the_other_tenant_cannot_read_or_generate(app_client, indexed_csr, two_orgs):
    token, csr_id, _ = indexed_csr
    _ta, _pa, token_b, _pb = two_orgs
    sections = app_client.get(f"/api/v1/csr/projects/{csr_id}/sections",
                              headers=_auth(token)).json()["items"]
    section_id = next(s for s in sections if s["section_number"] == "10.1")["id"]

    assert app_client.get(f"/api/v1/csr/projects/{csr_id}/documents",
                          headers=_auth(token_b)).status_code == 404
    assert app_client.post(f"/api/v1/csr/sections/{section_id}/generate",
                           headers=_auth(token_b), json={}).status_code == 404


# ---------------------------------------------------------------- purge

def test_deleting_the_csr_purges_sources_and_chunks(app_client, indexed_csr):
    """Spec acceptance criterion 6: files, chunks and vectors verifiably gone."""
    token, csr_id, _ = indexed_csr

    from app.db import SessionLocal
    from app.models import CsrChunk, CsrDocument

    db = SessionLocal()
    try:
        assert db.query(CsrChunk).filter(CsrChunk.csr_project_id == csr_id).count() > 0
        paths = [d.storage_path for d in db.query(CsrDocument).filter(
            CsrDocument.csr_project_id == csr_id).all()]
    finally:
        db.close()
    assert paths

    gone = app_client.delete(f"/api/v1/csr/projects/{csr_id}", headers=_auth(token))
    assert gone.status_code == 200, gone.text

    db = SessionLocal()
    try:
        assert db.query(CsrChunk).filter(CsrChunk.csr_project_id == csr_id).count() == 0
        assert db.query(CsrDocument).filter(
            CsrDocument.csr_project_id == csr_id).count() == 0
    finally:
        db.close()

    from app.storage import abs_path
    for path in paths:
        assert not abs_path(path).exists(), f"{path} survived the purge"
