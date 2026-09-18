"""The gates between a draft and a dossier, and the records they leave.

Four defects, all of them in the space between "the system knows" and "the
system acts on what it knows":

* an APPROVED section stayed approved when a new draft was saved over it, so
  export assembled text nobody had approved under a status saying otherwise;
* the verification gate counted `cmc_results` for every table, so a section
  whose table is built from `cmc_batch_formula` could be approved with every
  quantity in it unverified;
* a correction that moved a result to a different stability cell was audited
  as though nothing had changed;
* the correction endpoint returned no conformance verdict, so the grid kept
  showing the verdict computed for the value that had just been replaced.
"""

from decimal import Decimal

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def dossier(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Gates dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Gatezol Tablets"}).json()
    added = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                            headers=_auth(token), json={"doc_type_key": "ctd_32p"}).json()
    return token, cmc["id"], added["id"], added["sections"]


# ------------------------------------------------- a new draft withdraws approval

def test_saving_a_draft_over_an_approved_section_withdraws_the_approval(
        app_client, dossier):
    token, cmc_id, _deliverable_id, sections = dossier
    section = next(s for s in sections
                   if not s["is_container"] and not s.get("table_key"))

    app_client.put(f"/api/v1/cmc/sections/{section['id']}/draft",
                   headers=_auth(token), json={"content": "The reviewed text."})
    approved = app_client.patch(f"/api/v1/cmc/sections/{section['id']}/status",
                                headers=_auth(token), json={"status": "approved"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    # A second version over the top of an approved section.
    app_client.put(f"/api/v1/cmc/sections/{section['id']}/draft",
                   headers=_auth(token), json={"content": "Text nobody approved."})

    listed = app_client.get(
        f"/api/v1/cmc/deliverables/{_deliverable_id}/sections",
        headers=_auth(token)).json()["items"]
    now = next(s for s in listed if s["id"] == section["id"])
    assert now["status"] == "draft", "an edited section cannot stay approved"


def test_the_withdrawal_is_audited_as_a_withdrawal(app_client, dossier):
    """Not an "Edited a CMC section" line among a hundred others: losing an
    approval is the event, and a reviewer looking for why a section went back
    to draft has to be able to find it."""
    token, cmc_id, _d, sections = dossier
    section = next(s for s in sections
                   if not s["is_container"] and not s.get("table_key"))
    app_client.put(f"/api/v1/cmc/sections/{section['id']}/draft",
                   headers=_auth(token), json={"content": "First."})
    app_client.patch(f"/api/v1/cmc/sections/{section['id']}/status",
                     headers=_auth(token), json={"status": "approved"})
    app_client.put(f"/api/v1/cmc/sections/{section['id']}/draft",
                   headers=_auth(token), json={"content": "Second."})

    from app.db import SessionLocal
    from app.models import AuditLog

    db = SessionLocal()
    try:
        entries = db.query(AuditLog).filter(
            AuditLog.entity_type == "cmc_section",
            AuditLog.entity_id == section["id"]).all()
    finally:
        db.close()
    withdrawn = [e for e in entries if "approval withdrawn" in (e.target or "")]
    assert withdrawn, [e.target for e in entries]
    # Severity says it too: losing an approval is not routine.
    assert withdrawn[0].severity == "warning"


def test_an_untouched_draft_status_is_not_dressed_up_as_a_withdrawal(
        app_client, dossier):
    """Read from the audit table by entity id rather than through the listing
    endpoint: the listing is org-scoped and every test in this file shares one
    org, so "no withdrawal was logged" has to mean "not for THIS section"."""
    from app.db import SessionLocal
    from app.models import AuditLog

    token, cmc_id, _d, sections = dossier
    section = next(s for s in sections
                   if not s["is_container"] and not s.get("table_key"))
    app_client.put(f"/api/v1/cmc/sections/{section['id']}/draft",
                   headers=_auth(token), json={"content": "First."})

    db = SessionLocal()
    try:
        entries = db.query(AuditLog).filter(
            AuditLog.entity_type == "cmc_section",
            AuditLog.entity_id == section["id"]).all()
        assert entries, "the edit itself is still audited"
        assert not any("approval withdrawn" in (e.target or "") for e in entries)
        assert all(e.severity == "info" for e in entries)
    finally:
        db.close()


# -------------------------------------- the gate counts the rows it prints

@pytest.fixture
def formula_rows(app_client, dossier):
    """A composition table's worth of unverified quantities, and nothing in
    `cmc_results` at all."""
    from app.db import SessionLocal
    from app.models import CmcBatchFormula, CmcProject, CmcResult

    token, cmc_id, deliverable_id, sections = dossier
    db = SessionLocal()
    cmc = db.get(CmcProject, cmc_id)
    for order, (component, quantity) in enumerate(
            [("Gatezol", "10.0 mg"), ("Lactose monohydrate", "80.0 mg")]):
        db.add(CmcBatchFormula(
            org_id=cmc.org_id, cmc_project_id=cmc_id, component_name=component,
            quantity_per_unit=quantity, sort_order=order))
    db.commit()
    # The point of the fixture: nothing here is a `cmc_results` row, so a gate
    # that counts those sees a fully verified project.
    assert db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc_id).count() == 0
    try:
        yield token, cmc_id, deliverable_id, sections, db
    finally:
        db.close()


def test_a_formula_backed_section_cannot_be_approved_over_unverified_quantities(
        app_client, formula_rows):
    token, cmc_id, _deliverable_id, sections, _db = formula_rows
    composition = next(s for s in sections if s["section_code"] == "P.1")
    assert composition["table_key"] == "composition_table"

    app_client.put(f"/api/v1/cmc/sections/{composition['id']}/draft",
                   headers=_auth(token),
                   json={"content": "The composition is below.\n\n[TABLE: composition_table]"})
    refused = app_client.patch(f"/api/v1/cmc/sections/{composition['id']}/status",
                               headers=_auth(token), json={"status": "approved"})
    assert refused.status_code == 409, refused.text
    error = refused.json()["detail"]["error"]
    assert error["code"] == "CMC_UNVERIFIED_DATA"
    assert "formula line" in error["message"], error["message"]


def test_the_same_section_approves_once_the_quantities_are_verified(
        app_client, formula_rows):
    from app.models import CmcBatchFormula, CmcProject, User

    token, cmc_id, _deliverable_id, sections, db = formula_rows
    composition = next(s for s in sections if s["section_code"] == "P.1")
    app_client.put(f"/api/v1/cmc/sections/{composition['id']}/draft",
                   headers=_auth(token),
                   json={"content": "The composition is below.\n\n[TABLE: composition_table]"})

    org_id = db.get(CmcProject, cmc_id).org_id
    verifier = db.query(User).filter(User.org_id == org_id).first()
    for row in db.query(CmcBatchFormula).filter(
            CmcBatchFormula.cmc_project_id == cmc_id).all():
        row.verified_by = verifier.id
    db.commit()

    allowed = app_client.patch(f"/api/v1/cmc/sections/{composition['id']}/status",
                               headers=_auth(token), json={"status": "approved"})
    assert allowed.status_code == 200, allowed.text


# ------------------------------------------------ what a correction records

@pytest.fixture
def one_result(app_client, dossier):
    from app.db import SessionLocal
    from app.models import CmcBatch, CmcMaterial, CmcProject, CmcResult, CmcTest

    token, cmc_id, _deliverable_id, _sections = dossier
    db = SessionLocal()
    cmc = db.get(CmcProject, cmc_id)
    scope = {"org_id": cmc.org_id, "cmc_project_id": cmc_id}

    def add(row):
        db.add(row)
        db.flush()
        return row

    material = add(CmcMaterial(kind="drug_product", name="Gatezol Tablets", **scope))
    test = add(CmcTest(material_id=material.id, test_name="Related substance A",
                       acceptance_criterion_text="NMT 0.20 %", sort_order=0, **scope))
    batch = add(CmcBatch(material_id=material.id, batch_number="B-1", **scope))
    result = add(CmcResult(batch_id=batch.id, test_id=test.id, value_text="0.12 %",
                           value_numeric=Decimal("0.12"), unit="%",
                           storage_condition="25C/60RH", timepoint_months=6.0, **scope))
    db.commit()
    result_id = result.id
    try:
        yield token, cmc_id, result_id, db
    finally:
        db.close()


def test_moving_a_result_to_another_stability_cell_is_audited_as_a_move(
        app_client, one_result):
    """The correction endpoint snapshotted the stability coordinates and then
    reported only the value, so a result moved from 6 months at 25C/60RH to 3
    months at 40C/75RH -- a different cell of a different table -- was logged
    as "verified '0.12 %'"."""
    token, _cmc_id, result_id, _db = one_result
    moved = app_client.patch(f"/api/v1/cmc/results/{result_id}", headers=_auth(token),
                             json={"storage_condition": "40C/75RH",
                                   "timepoint_months": 3.0, "verify": True})
    assert moved.status_code == 200, moved.text

    entries = app_client.get("/api/v1/audit-logs", headers=_auth(token),
                             params={"entity_type": "cmc_result"}).json()["items"]
    target = " ".join(e.get("target") or "" for e in entries)
    assert "storage_condition" in target and "40C/75RH" in target, target
    assert "timepoint_months" in target, target


def test_a_correction_returns_the_recomputed_verdict(app_client, one_result):
    """The grid merges this response over the row it edited. Without the
    verdict it kept the old value's conformance beside the new value."""
    token, _cmc_id, result_id, _db = one_result
    corrected = app_client.patch(f"/api/v1/cmc/results/{result_id}",
                                 headers=_auth(token),
                                 json={"value_text": "0.31 %", "verify": True})
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["value_text"] == "0.31 %"
    # 0.31 % against NMT 0.20 % is a failure, and the response says so.
    assert body["conformance"] == "fail"
    assert "0.31 %" in body["conformance_reason"]


def test_a_correction_into_specification_returns_a_pass(app_client, one_result):
    token, _cmc_id, result_id, _db = one_result
    body = app_client.patch(f"/api/v1/cmc/results/{result_id}", headers=_auth(token),
                            json={"value_text": "0.08 %", "verify": True}).json()
    assert body["conformance"] == "pass"
