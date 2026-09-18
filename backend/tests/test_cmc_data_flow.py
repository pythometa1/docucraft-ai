"""Sources in, verified data out: the flow the Data Review grid sits on.

Upload a specification and two certificates of analysis, process them, and
the structured store should hold the tests, the batches and the results --
each carrying the source it came from, its conformance verdict, and nobody's
signature yet. Then a person corrects one value and verifies the rest, and
what the store says changes only in the ways they asked for.
"""

import io
import time

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


SPEC_CSV = (
    "Drug Product Specification 3.2.P.5.1\n"
    "Test,Acceptance Criteria,Method\n"
    "Appearance,White film-coated tablet,Visual\n"
    "Assay,95.0 - 105.0 %,HPLC-010\n"
    "Related substance A,NMT 0.20 %,HPLC-011\n"
    "Water content,NMT 3.0 %,KF-001\n"
)

COA_ONE = (
    "Certificate of Analysis - Batch B-2026-001\n"
    "Test,Acceptance Criteria,Method,Result\n"
    "Appearance,White film-coated tablet,Visual,White film-coated tablet\n"
    "Assay,95.0 - 105.0 %,HPLC-010,99.2 %\n"
    "Related substance A,NMT 0.20 %,HPLC-011,0.050 %\n"
    "Water content,NMT 3.0 %,KF-001,2.1 %\n"
)

COA_TWO = (
    "Certificate of Analysis - Batch B-2026-002\n"
    "Test,Acceptance Criteria,Method,Result\n"
    "Assay,95.0 - 105.0 %,HPLC-010,94.1 %\n"
    "Related substance A,NMT 0.20 %,HPLC-011,0.31 %\n"
)


@pytest.fixture
def dossier(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Data flow dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Drug X Tablets",
        "dosage_form": "Film-coated tablet"}).json()
    app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                    headers=_auth(token), json={"doc_type_key": "ctd_32p"})
    return token, cmc["id"]


def _upload(app_client, token, cmc_id, files):
    payload = [("files", (name, io.BytesIO(data), "text/csv")) for name, data, _t in files]
    payload += [("doc_types", (None, doc_type)) for _n, _d, doc_type in files]
    return app_client.post(f"/api/v1/cmc/projects/{cmc_id}/documents",
                           headers=_auth(token), files=payload)


def _process(app_client, token, cmc_id):
    started = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/process", headers=_auth(token))
    assert started.status_code == 202, started.text
    for _ in range(200):
        status = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/processing-status",
                                headers=_auth(token)).json()
        if not status["in_flight"]:
            return status
        time.sleep(0.05)
    raise AssertionError("documents never finished processing")


@pytest.fixture
def loaded(app_client, dossier):
    token, cmc_id = dossier
    uploaded = _upload(app_client, token, cmc_id, [
        ("spec.csv", SPEC_CSV.encode(), "spec_dp"),
        ("coa_001.csv", COA_ONE.encode(), "coa"),
        ("coa_002.csv", COA_TWO.encode(), "coa"),
    ])
    assert uploaded.status_code == 201, uploaded.text
    status = _process(app_client, token, cmc_id)
    assert all(d["processing_status"] == "done" for d in status["items"]), status["items"]
    return token, cmc_id, status


# ---------------------------------------------------------------- ingestion

def test_the_checklist_follows_the_chosen_deliverables(app_client, dossier):
    token, cmc_id = dossier
    status = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/documents",
                            headers=_auth(token)).json()
    required = {r["doc_type"] for r in status["readiness"]["required"]}
    # A 3.2.P needs a batch record; a 3.2.S would not have been asked for one.
    assert required == {"spec_dp", "coa", "bmr"}
    assert status["readiness"]["ready_to_generate"] is False


def test_sources_are_indexed_and_read_for_values(app_client, loaded):
    _token, _cmc_id, status = loaded
    by_name = {d["filename"]: d for d in status["items"]}
    assert by_name["spec.csv"]["chunk_count"] >= 1
    # A specification defines tests and states no results.
    assert by_name["spec.csv"]["value_count"] == 0
    assert by_name["coa_001.csv"]["value_count"] == 4
    assert by_name["coa_002.csv"]["value_count"] == 2


def test_an_untagged_or_unreadable_upload_is_refused(app_client, dossier):
    token, cmc_id = dossier
    mismatched = app_client.post(
        f"/api/v1/cmc/projects/{cmc_id}/documents", headers=_auth(token),
        files=[("files", ("a.csv", io.BytesIO(b"x"), "text/csv")),
               ("files", ("b.csv", io.BytesIO(b"y"), "text/csv")),
               ("doc_types", (None, "coa"))])
    assert mismatched.status_code == 422
    assert mismatched.json()["detail"]["error"]["code"] == "CMC_TAGS_MISMATCH"

    bad_tag = _upload(app_client, token, cmc_id, [("a.csv", b"x", "nonsense")])
    assert bad_tag.status_code == 422
    assert bad_tag.json()["detail"]["error"]["code"] == "CMC_BAD_DOC_TYPE"

    bad_suffix = app_client.post(
        f"/api/v1/cmc/projects/{cmc_id}/documents", headers=_auth(token),
        files=[("files", ("scan.tiff", io.BytesIO(b"x"), "image/tiff")),
               ("doc_types", (None, "coa"))])
    assert bad_suffix.status_code == 422
    assert bad_suffix.json()["detail"]["error"]["code"] == "CMC_UNSUPPORTED_FILE"


# ---------------------------------------------------------------- the grid

def test_the_grid_shows_values_verdicts_and_provenance(app_client, loaded):
    token, cmc_id, _ = loaded

    specs = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/specifications",
                           headers=_auth(token)).json()["items"]
    by_test = {t["test_name"]: t for t in specs}
    assert by_test["Assay"]["limit_lower"] == "95.0"
    assert by_test["Assay"]["limit_upper"] == "105.0"
    assert by_test["Assay"]["method_id"] == "HPLC-010"
    # An unreducible criterion states no bound rather than an invented one.
    assert by_test["Appearance"]["limit_lower"] is None

    batches = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/batches",
                             headers=_auth(token)).json()["items"]
    assert {b["batch_number"] for b in batches} == {"B-2026-001", "B-2026-002"}

    data = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token)).json()
    rows = {(r["batch_number"], r["test_name"]): r for r in data["items"]}

    # The value is the source's own string, and it carries its verdict.
    assay_one = rows[("B-2026-001", "Assay")]
    assert assay_one["value_text"] == "99.2 %"
    assert assay_one["conformance"] == "pass"
    assert assay_one["source_document_id"]
    assert assay_one["verified_by"] is None

    impurity = rows[("B-2026-001", "Related substance A")]
    assert impurity["value_text"] == "0.050 %"   # three significant figures, kept

    # The failing batch is flagged as failing, with the reason.
    assay_two = rows[("B-2026-002", "Assay")]
    assert assay_two["conformance"] == "fail"
    assert "below the lower limit" in assay_two["conformance_reason"]
    assert rows[("B-2026-002", "Related substance A")]["conformance"] == "fail"

    # A result whose criterion cannot be reduced is never claimed as a pass.
    assert rows[("B-2026-001", "Appearance")]["conformance"] in ("pass", "unknown")

    assert data["summary"]["total"] == 6
    assert data["summary"]["verified"] == 0
    assert data["summary"]["all_verified"] is False


def test_correcting_a_value_records_what_it_was(app_client, loaded):
    token, cmc_id, _ = loaded
    rows = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token)).json()["items"]
    water = next(r for r in rows if r["test_name"] == "Water content")
    assert water["value_text"] == "2.1 %"

    fixed = app_client.patch(f"/api/v1/cmc/results/{water['id']}", headers=_auth(token),
                             json={"value_text": "2.10 %"})
    assert fixed.status_code == 200, fixed.text
    body = fixed.json()
    # Retyped verbatim: two significant figures became three because a person
    # said so, not because anything reformatted the number.
    assert body["value_text"] == "2.10 %"
    assert body["verified_by"]
    assert body["extraction_confidence"] == 1.0

    entries = app_client.get("/api/v1/audit-logs", headers=_auth(token),
                             params={"entity_type": "cmc_result"}).json()["items"]
    assert any("'2.1 %' -> '2.10 %'" in (e.get("target") or "") for e in entries), entries

    blanked = app_client.patch(f"/api/v1/cmc/results/{water['id']}",
                               headers=_auth(token), json={"value_text": "  "})
    assert blanked.status_code == 422
    assert blanked.json()["detail"]["error"]["code"] == "CMC_RESULT_NEEDS_VALUE"


def test_bulk_verification_and_the_gate(app_client, loaded):
    token, cmc_id, _ = loaded
    nothing = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                              headers=_auth(token), json={})
    assert nothing.status_code == 422
    assert nothing.json()["detail"]["error"]["code"] == "CMC_NOTHING_TO_VERIFY"

    swept = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                            headers=_auth(token), json={"all_unverified": True})
    assert swept.status_code == 200
    assert swept.json()["verified"] == 6

    data = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token)).json()
    assert data["summary"]["all_verified"] is True
    assert all(r["verified_by"] for r in data["items"])


def test_two_sources_disagreeing_must_be_resolved_by_a_person(app_client, dossier):
    token, cmc_id = dossier
    _upload(app_client, token, cmc_id, [
        ("coa_a.csv", COA_ONE.encode(), "coa"),
        ("coa_b.csv", COA_ONE.replace("99.2 %", "99.4 %").encode(), "coa"),
    ])
    _process(app_client, token, cmc_id)

    conflicts = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/conflicts",
                               headers=_auth(token)).json()["items"]
    assert len(conflicts) == 2
    assert {c["value_text"] for c in conflicts} == {"99.2 %", "99.4 %"}

    # A conflicted value is never swept up by a bulk verify.
    swept = app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                            headers=_auth(token), json={"all_unverified": True}).json()
    assert swept["skipped_conflicts"] == 2

    keep = next(c for c in conflicts if c["value_text"] == "99.4 %")
    resolved = app_client.post(f"/api/v1/cmc/results/{keep['id']}:resolve",
                               headers=_auth(token),
                               json={"keep_result_id": keep["id"]})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["value_text"] == "99.4 %"
    assert resolved.json()["discarded"] == "99.2 %"

    remaining = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/conflicts",
                               headers=_auth(token)).json()["items"]
    assert remaining == []


def test_deleting_a_source_removes_its_unverified_values(app_client, loaded):
    token, cmc_id, status = loaded
    coa = next(d for d in status["items"] if d["filename"] == "coa_001.csv")

    before = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                            headers=_auth(token)).json()["summary"]["total"]
    gone = app_client.delete(f"/api/v1/cmc/documents/{coa['id']}", headers=_auth(token))
    assert gone.status_code == 200, gone.text
    assert gone.json()["purged_values"] == 4
    assert gone.json()["kept_verified_values"] == 0

    after = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                           headers=_auth(token)).json()["summary"]["total"]
    assert after == before - 4


def test_a_verified_value_survives_its_source_being_removed(app_client, loaded):
    """Somebody accepted it, and their acceptance is its own evidence."""
    token, cmc_id, status = loaded
    app_client.post(f"/api/v1/cmc/projects/{cmc_id}/results:verify",
                    headers=_auth(token), json={"all_unverified": True})
    coa = next(d for d in status["items"] if d["filename"] == "coa_001.csv")

    gone = app_client.delete(f"/api/v1/cmc/documents/{coa['id']}",
                             headers=_auth(token)).json()
    assert gone["purged_values"] == 0
    assert gone["kept_verified_values"] == 4

    rows = app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token)).json()["items"]
    orphaned = [r for r in rows if r["batch_number"] == "B-2026-001"]
    assert orphaned and all(r["source_document_id"] is None for r in orphaned)


def test_the_other_tenant_sees_no_data(app_client, loaded, two_orgs):
    token, cmc_id, _ = loaded
    _ta, _pa, token_b, _pb = two_orgs
    assert app_client.get(f"/api/v1/cmc/projects/{cmc_id}/data/results",
                          headers=_auth(token_b)).status_code == 404
    assert app_client.get(f"/api/v1/cmc/projects/{cmc_id}/documents",
                          headers=_auth(token_b)).status_code == 404
