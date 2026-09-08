"""The whole dossier, end to end: sources in, a document out.

What this pins is the property the two-flow split exists for -- a value
corrected in the Data Review grid reaches every rendered table without a
single section being regenerated -- and the gates around it: an unverified
value cannot be approved into a document, an out-of-specification result
blocks an export, and an override is recorded rather than silent.

The model is stubbed. What is under test is the pipeline around it: retrieval,
the [TABLE: key] contract, the verification gate, the renderer and the
assembler.
"""

import io
import time

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


SPEC = (
    "Drug Product Specification - Drug X Tablets\n"
    "Test,Acceptance Criteria,Method\n"
    "Assay,95.0 - 105.0 % of label claim,HPLC-010\n"
    "Related substance A,NMT 0.20 %,HPLC-011\n"
    "Water content,NMT 3.0 %,KF-001\n"
)

COA = (
    "Certificate of Analysis - Batch {batch}\n"
    "Test,Acceptance Criteria,Method,Result\n"
    "Assay,95.0 - 105.0 % of label claim,HPLC-010,{assay}\n"
    "Related substance A,NMT 0.20 %,HPLC-011,{impurity}\n"
    "Water content,NMT 3.0 %,KF-001,2.10 %\n"
)


@pytest.fixture
def dossier(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Full flow dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Drug X Tablets",
        "dosage_form": "Film-coated tablet", "submission_type": "NDA",
        "target_regions": ["FDA"]}).json()
    added = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                            headers=_auth(token), json={"doc_type_key": "ctd_32p"}).json()
    return token, cmc["id"], added["id"], added["sections"]


def _upload_and_process(app_client, token, cmc_id, files):
    payload = [("files", (n, io.BytesIO(d), "text/csv")) for n, d, _t in files]
    payload += [("doc_types", (None, t)) for _n, _d, t in files]
    up = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/documents",
                         headers=_auth(token), files=payload)
    assert up.status_code == 201, up.text
    app_client.post(f"/api/v1/cmc/projects/{cmc_id}/process", headers=_auth(token))
    for _ in range(200):
        status = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/processing-status",
                                headers=_auth(token)).json()
        if not status["in_flight"]:
            return status
        time.sleep(0.05)
    raise AssertionError("processing never settled")


@pytest.fixture
def loaded(app_client, dossier):
    token, cmc_id, deliverable_id, sections = dossier
    _upload_and_process(app_client, token, cmc_id, [
        ("spec.csv", SPEC.encode(), "spec_dp"),
        ("coa1.csv", COA.format(batch="B-001", assay="99.2 %",
                                impurity="0.050 %").encode(), "coa"),
    ])
    return token, cmc_id, deliverable_id, sections


@pytest.fixture
def stub_model(monkeypatch):
    """A provider injected through draft_section's own parameter, so the patch
    target is unambiguous."""
    import app.cmc.router as router_mod

    def install(reply: str):
        seen = {}

        class _Result:
            data = {"content": reply}
            model = "stub-model"
            error = None

        class _Provider:
            def structured(self, *, system, prompt, schema, purpose="generate"):
                seen["system"] = system
                seen["prompt"] = prompt
                return _Result()

        real = router_mod.__dict__.get("_draft_section_impl")

        import app.cmc.drafting as drafting_mod
        original = drafting_mod.draft_section

        def patched(**kwargs):
            kwargs.setdefault("get_provider", lambda *a, **k: _Provider())
            kwargs["get_provider"] = lambda *a, **k: _Provider()
            return original(**kwargs)

        monkeypatch.setattr(drafting_mod, "draft_section", patched)
        return seen

    return install


# ---------------------------------------------------------------- rendering

def test_a_table_renders_the_stored_strings(app_client, loaded):
    token, cmc_id, _deliverable_id, _sections = loaded
    preview = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/tables/spec_table",
                             headers=_auth(token))
    assert preview.status_code == 200, preview.text
    body = preview.json()
    flat = [cell for row in body["rows"] for cell in row]
    assert "95.0 - 105.0 % of label claim" in flat
    assert "NMT 0.20 %" in flat

    analyses = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/tables/batch_analyses",
                              headers=_auth(token)).json()
    flat = [cell for row in analyses["rows"] for cell in row]
    # Three significant figures survive the whole way to the rendered cell.
    assert "0.050 %" in flat
    assert "99.2 %" in flat
    # Nothing is verified yet, and the renderer says so rather than hiding it.
    assert analyses["unverified"] > 0


def test_an_unknown_table_key_is_refused(app_client, loaded):
    token, cmc_id, _d, _s = loaded
    res = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/tables/not_a_table",
                         headers=_auth(token))
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CMC_UNKNOWN_TABLE"


# ---------------------------------------------------------------- drafting

def test_a_section_is_drafted_with_a_table_marker_not_numbers(
        app_client, loaded, stub_model):
    token, cmc_id, _deliverable_id, sections = loaded
    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    assert spec_section["table_key"] == "spec_table"

    seen = stub_model(
        "P.5.1 Specification(s)\n\n"
        "The specification for the drug product is presented below.\n\n"
        "[TABLE: spec_table]\n\n"
        "The acceptance criteria were justified in P.5.6 [S1, p.1].")
    res = app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                          headers=_auth(token), json={})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["table_markers"] == ["spec_table"]
    # The model was shown the sources and told the rules.
    shown = f"{seen.get('system') or ''}\n{seen.get('prompt') or ''}"
    assert "DO NOT WRITE DATA TABLES" in shown
    assert "Drug X Tablets" in shown


def test_a_section_carrying_unverified_data_cannot_be_approved(
        app_client, loaded, stub_model):
    token, cmc_id, _d, sections = loaded
    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    stub_model("P.5.1 Specification(s)\n\n[TABLE: spec_table]")
    app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                    headers=_auth(token), json={})

    refused = app_client.patch(f"/api/v1/cmc/sections/{spec_section['id']}/status",
                               headers=_auth(token), json={"status": "approved"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "CMC_UNVERIFIED_DATA"

    app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                    headers=_auth(token), json={"all_unverified": True})
    allowed = app_client.patch(f"/api/v1/cmc/sections/{spec_section['id']}/status",
                               headers=_auth(token), json={"status": "approved"})
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "approved"


# ---------------------------------------------------------------- QC

def test_an_out_of_specification_result_is_a_blocker(app_client, dossier):
    token, cmc_id, _d, _s = dossier
    _upload_and_process(app_client, token, cmc_id, [
        ("spec.csv", SPEC.encode(), "spec_dp"),
        ("coa_bad.csv", COA.format(batch="B-009", assay="94.1 %",
                                   impurity="0.31 %").encode(), "coa"),
    ])
    qc = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/qc", headers=_auth(token)).json()
    codes = {f["code"] for f in qc["blockers"]}
    assert "OUT_OF_SPECIFICATION" in codes
    assert qc["exportable"] is False
    oos = [f for f in qc["blockers"] if f["code"] == "OUT_OF_SPECIFICATION"]
    assert any("B-009" in (f["message"] + str(f["detail"])) for f in oos)


# ---------------------------------------------------------------- export

def test_export_is_blocked_until_approved_and_records_an_override(
        app_client, loaded, stub_model):
    token, cmc_id, deliverable_id, sections = loaded
    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    stub_model("P.5.1 Specification(s)\n\nThe specification follows.\n\n[TABLE: spec_table]")
    app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                    headers=_auth(token), json={})

    blocked = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/export",
                              headers=_auth(token),
                              json={"deliverable_id": deliverable_id,
                                    "granularity": "combined"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["code"] == "CMC_EXPORT_BLOCKED"

    forced = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/export",
                             headers=_auth(token),
                             json={"deliverable_id": deliverable_id,
                                   "granularity": "combined",
                                   "override_approval": True})
    assert forced.status_code == 201, forced.text
    assert forced.json()["overridden"] is True

    entries = app_client.get("/api/v1/audit-logs", headers=_auth(token),
                             params={"entity_type": "cmc_export"}).json()["items"]
    assert any("overridden" in (e.get("target") or "") for e in entries)


def test_the_exported_document_carries_the_stored_values(
        app_client, loaded, stub_model):
    """The end of the whole chain: a real .docx whose table cells are the
    strings the certificate of analysis printed."""
    token, cmc_id, deliverable_id, sections = loaded
    app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                    headers=_auth(token), json={"all_unverified": True})

    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    stub_model("P.5.1 Specification(s)\n\nThe specification follows.\n\n[TABLE: spec_table]")
    app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                    headers=_auth(token), json={})
    app_client.patch(f"/api/v1/cmc/sections/{spec_section['id']}/status",
                     headers=_auth(token), json={"status": "approved"})

    # Every other enabled section is excluded so the export gate has one
    # section to judge rather than thirty-eight empty ones.
    for section in sections:
        if section["is_container"] or section["id"] == spec_section["id"]:
            continue
        app_client.patch(f"/api/v1/cmc/sections/{section['id']}",
                         headers=_auth(token), json={"enabled": False})

    exported = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/export",
                               headers=_auth(token),
                               json={"deliverable_id": deliverable_id,
                                     "granularity": "combined"})
    assert exported.status_code == 201, exported.text
    record = exported.json()
    assert record["files"], record

    import docx as docx_lib

    from app.storage import abs_path

    path = abs_path(record["files"][0]["storage_path"])
    document = docx_lib.Document(str(path))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "P.5.1 Specification(s)" in text
    assert "The specification follows." in text
    # The marker itself never reaches the document; the table does.
    assert "[TABLE:" not in text
    assert document.tables, "the rendered specification table is missing"
    cells = [c.text for t in document.tables for r in t.rows for c in r.cells]
    assert "95.0 - 105.0 % of label claim" in cells
    assert "NMT 0.20 %" in cells

    downloaded = app_client.get(f"/api/v1/cmc/exports/{record['id']}/download",
                                headers=_auth(token))
    assert downloaded.status_code == 200
    assert len(downloaded.content) > 5000


def test_a_correction_reaches_the_table_without_regenerating_the_section(
        app_client, loaded, stub_model):
    """The property the two-flow split exists to buy."""
    token, cmc_id, _deliverable_id, sections = loaded
    spec_section = next(s for s in sections if s["section_code"] == "P.5.4")

    stub_model("P.5.4 Batch Analyses\n\nResults follow.\n\n[TABLE: batch_analyses]")
    generated = app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                                headers=_auth(token), json={}).json()
    version_before = generated["version"]

    rows = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token)).json()["items"]
    assay = next(r for r in rows if r["test_name"] == "Assay")
    app_client.patch(f"/api/v1/cmc/results/{assay['id']}", headers=_auth(token),
                     json={"value_text": "99.25 %"})

    table = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/tables/batch_analyses",
                           headers=_auth(token)).json()
    flat = [cell for row in table["rows"] for cell in row]
    assert "99.25 %" in flat
    assert "99.2 %" not in flat

    # The section was never regenerated: same version, same text.
    draft = app_client.get(f"/api/v1/cmc/sections/{spec_section['id']}/draft",
                           headers=_auth(token)).json()
    assert draft["draft"]["version"] == version_before
    assert "[TABLE: batch_analyses]" in draft["draft"]["content"]


def test_a_table_key_the_model_invented_blocks_rather_than_crashes(
        app_client, loaded, stub_model):
    """A live run had the model emit [TABLE: spec_dp] -- a plausible key drawn
    from the document type it had been reading, and no builder at all. The
    export raised straight out of the renderer and answered 500, telling
    somebody the server broke when what happened is that the dossier is not
    ready. Every way a builder can decline is now a blocker."""
    token, cmc_id, deliverable_id, sections = loaded
    app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                    headers=_auth(token), json={"all_unverified": True})

    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    stub_model("P.5.1 Specification(s)\n\nThe specification follows.\n\n[TABLE: spec_dp]")
    app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                    headers=_auth(token), json={})
    app_client.patch(f"/api/v1/cmc/sections/{spec_section['id']}/status",
                     headers=_auth(token), json={"status": "approved"})
    for section in sections:
        if section["is_container"] or section["id"] == spec_section["id"]:
            continue
        app_client.patch(f"/api/v1/cmc/sections/{section['id']}",
                         headers=_auth(token), json={"enabled": False})

    res = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/export",
                          headers=_auth(token),
                          json={"deliverable_id": deliverable_id, "granularity": "combined"})
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]["error"]
    assert detail["code"] == "CMC_EXPORT_BLOCKED"
    blockers = detail["details"]["blockers"]
    assert any(b["code"] == "TABLE_UNRESOLVED" and "spec_dp" in b["message"]
               for b in blockers), blockers


def test_the_prompt_names_the_one_table_key_that_exists(app_client, loaded, stub_model):
    """Rule 2 told the model to emit a marker and never said which key, so it
    guessed. The section knows its own key; the guidance now states it."""
    token, _cmc_id, _deliverable_id, sections = loaded
    spec_section = next(s for s in sections if s["section_code"] == "P.5.1")
    assert spec_section["table_key"] == "spec_table"

    import app.cmc.drafting as drafting_mod

    seen = {}
    original = drafting_mod.build_prompt

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    stub_model("P.5.1 Specification(s)\n\n[TABLE: spec_table]")
    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(drafting_mod, "build_prompt", spy)
    try:
        app_client.post(f"/api/v1/cmc/sections/{spec_section['id']}/generate",
                        headers=_auth(token), json={})
    finally:
        monkey.undo()

    guidance = seen.get("guidance") or ""
    assert "[TABLE: spec_table]" in guidance
    assert "ONLY table key" in guidance

    # A section with no table is told so, rather than left to invent one.
    # Asserted on the helper rather than through a generation, because a
    # prose section's sources may not be uploaded and a run with no chunks
    # never builds a prompt at all.
    from app.cmc.router import _guidance_with_table

    class _Section:
        guidance_text = "The analytical procedures used for each test."
        table_key = None

    assert "no data table" in _guidance_with_table(_Section())

    class _WithTable(_Section):
        table_key = "batch_analyses"

    assert "[TABLE: batch_analyses]" in _guidance_with_table(_WithTable())
