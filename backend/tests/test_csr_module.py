"""The CSR module, milestone M1: a project extension bound to a study, the
built-in ICH E3 template, and the section tree it seeds.

What these tests pin is the module's ground rules before any AI is involved:
a CSR belongs to exactly one Clinical/CSR portal project, always references a
study-book row, seeds the full E3 numbering, refuses to re-template over
work in progress, and purges its own data on delete.
"""

import pytest

from app.csr.ich_e3 import SECTIONS, source_types_for


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org_a(two_orgs):
    token_a, project_a, _tb, _pb = two_orgs
    return token_a, project_a


@pytest.fixture
def csr_portal_project(app_client, org_a):
    """A portal project of the right taxonomy for a CSR."""
    token, _ = org_a
    res = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "ONC-2026-014 CSR", "function": "Clinical",
        "document_type": "Clinical Study Report",
        "region": "Global", "language": "English"})
    assert res.status_code in (200, 201), res.text
    return token, res.json()["id"]


STUDY = {"protocol_number": "ONC-2026-014", "title": "A Phase 2 study of drug X",
         "sponsor": "Acme Pharma", "phase": "2", "indication": "NSCLC",
         "principal_investigator": "Dr. A. Rao"}


def _make_csr(app_client, token, project_id, **overrides):
    payload = {"project_id": project_id, "study": STUDY,
               "compound_name": "Drug X", "therapeutic_area": "Oncology",
               "blinding": "double_blind",
               "study_design_summary": "Randomised, double-blind, 12 sites.",
               **overrides}
    return app_client.post("/api/v1/csr/projects", headers=_auth(token), json=payload)


# ------------------------------------------------------------------ seed data

def test_ich_e3_structure_is_complete_and_ordered():
    numbers = [s[0] for s in SECTIONS]
    assert len(numbers) == len(set(numbers))
    for expected in ("2", "5.3", "9.4.6", "9.7", "10.1", "11.4.7", "12.2.1", "13", "16"):
        assert expected in numbers
    # Containers hold no guidance; every leaf holds some.
    for number, _title, guidance, container in SECTIONS:
        assert container == (guidance == ""), number


def test_source_mapping_prefixes_resolve():
    assert source_types_for("9.7") == ["sap"]
    assert source_types_for("9.4.6") == ["protocol", "randomization"]
    assert source_types_for("9.4.3") == ["protocol"]      # inherits "9"
    assert source_types_for("12.3") == ["tlf", "narrative"]
    assert source_types_for("4") == []                    # no filter


# ------------------------------------------------------------------ projects

def test_create_csr_project_binds_study_and_seeds_nothing_yet(
        app_client, csr_portal_project):
    token, project_id = csr_portal_project
    res = _make_csr(app_client, token, project_id)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["study"]["protocol_number"] == "ONC-2026-014"
    assert body["status"] == "setup"
    assert body["template"] is None

    # The inline study landed in the shared study book.
    studies = app_client.get("/api/v1/studies", headers=_auth(token),
                             params={"q": "ONC-2026-014"}).json()["items"]
    assert any(s["protocol_number"] == "ONC-2026-014" for s in studies)

    # One CSR per project.
    again = _make_csr(app_client, token, project_id)
    assert again.status_code == 409
    assert again.json()["detail"]["error"]["code"] == "CSR_PROJECT_EXISTS"


def test_wrong_taxonomy_and_missing_study_are_refused(app_client, org_a):
    token, hr_project_id = org_a  # the shared fixture project is not Clinical/CSR
    res = _make_csr(app_client, token, hr_project_id)
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CSR_WRONG_PROJECT"


def test_a_csr_without_a_study_is_refused(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    res = _make_csr(app_client, token, project_id, study=None)
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CSR_NEEDS_STUDY"


def test_bad_blinding_is_refused(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    res = _make_csr(app_client, token, project_id, blinding="triple_blind")
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CSR_BAD_BLINDING"


# ------------------------------------------------------------------ template

def test_builtin_template_seeds_the_full_section_tree(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    csr = _make_csr(app_client, token, project_id).json()

    chosen = app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                             headers=_auth(token), json={"source": "builtin_ich_e3"})
    assert chosen.status_code == 201, chosen.text
    sections = chosen.json()["sections"]
    assert len(sections) == len(SECTIONS)
    numbers = [s["section_number"] for s in sections]
    assert numbers == [s[0] for s in SECTIONS]  # document order, exactly
    by_number = {s["section_number"]: s for s in sections}
    assert by_number["9"]["is_container"] is True
    assert by_number["9.7"]["guidance_text"].startswith("Analysis populations")
    assert all(s["status"] == "not_started" for s in sections)

    detail = app_client.get(f"/api/v1/csr/projects/{csr['id']}",
                            headers=_auth(token)).json()
    assert detail["status"] == "ready"
    assert detail["template"]["source"] == "builtin_ich_e3"
    assert len(detail["sections"]) == len(SECTIONS)


def test_uploaded_template_is_honestly_unbuilt(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    csr = _make_csr(app_client, token, project_id).json()
    res = app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                          headers=_auth(token), json={"source": "uploaded"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CSR_TEMPLATE_UPLOAD_UNBUILT"


def test_retemplating_over_work_is_refused(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    csr = _make_csr(app_client, token, project_id).json()
    app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                    headers=_auth(token), json={"source": "builtin_ich_e3"})

    # Re-choosing over untouched sections reseeds cleanly...
    again = app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                            headers=_auth(token), json={"source": "builtin_ich_e3"})
    assert again.status_code == 201, again.text

    # ...but the moment any section carries work, the template is locked.
    from app.db import SessionLocal
    from app.models import CsrSection

    db = SessionLocal()
    try:
        section = db.query(CsrSection).filter(
            CsrSection.csr_project_id == csr["id"],
            CsrSection.section_number == "10.1").one()
        section.status = "draft"
        db.commit()
    finally:
        db.close()

    locked = app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                             headers=_auth(token), json={"source": "builtin_ich_e3"})
    assert locked.status_code == 409
    assert locked.json()["detail"]["error"]["code"] == "CSR_TEMPLATE_LOCKED"


# ------------------------------------------------------------------ sections

def test_section_toggle_and_container_refusal(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    csr = _make_csr(app_client, token, project_id).json()
    sections = app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                               headers=_auth(token),
                               json={"source": "builtin_ich_e3"}).json()["sections"]
    by_number = {s["section_number"]: s for s in sections}

    toggled = app_client.patch(f"/api/v1/csr/sections/{by_number['11.4.4']['id']}",
                               headers=_auth(token), json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False

    refused = app_client.patch(f"/api/v1/csr/sections/{by_number['9']['id']}",
                               headers=_auth(token), json={"enabled": False})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "CSR_SECTION_IS_CONTAINER"


# ------------------------------------------------------------------ purge

def test_delete_purges_sections_and_template(app_client, csr_portal_project):
    token, project_id = csr_portal_project
    csr = _make_csr(app_client, token, project_id).json()
    app_client.post(f"/api/v1/csr/projects/{csr['id']}/template",
                    headers=_auth(token), json={"source": "builtin_ich_e3"})

    gone = app_client.delete(f"/api/v1/csr/projects/{csr['id']}", headers=_auth(token))
    assert gone.status_code == 200
    assert gone.json()["purged_sections"] == len(SECTIONS)

    assert app_client.get(f"/api/v1/csr/projects/{csr['id']}",
                          headers=_auth(token)).status_code == 404
    from app.db import SessionLocal
    from app.models import CsrSection, CsrTemplate

    db = SessionLocal()
    try:
        assert db.query(CsrSection).filter(
            CsrSection.csr_project_id == csr["id"]).count() == 0
        assert db.query(CsrTemplate).filter(
            CsrTemplate.csr_project_id == csr["id"]).count() == 0
    finally:
        db.close()

    # The portal project survives; a CSR purge is not a project deletion.
    assert app_client.get(f"/api/v1/projects/{project_id}",
                          headers=_auth(token)).status_code == 200


def test_the_other_tenant_sees_nothing(app_client, csr_portal_project, two_orgs):
    token, project_id = csr_portal_project
    _ta, _pa, token_b, _pb = two_orgs
    csr = _make_csr(app_client, token, project_id).json()

    assert app_client.get(f"/api/v1/csr/projects/{csr['id']}",
                          headers=_auth(token_b)).status_code == 404
    listed_b = app_client.get("/api/v1/csr/projects",
                              headers=_auth(token_b)).json()["items"]
    assert not any(p["id"] == csr["id"] for p in listed_b)
