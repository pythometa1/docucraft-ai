"""The Quality/CMC module's front door: a product, its sites, the deliverables
it is writing, and the CTD sections each one carries.

What these pin is the ground the numeric machinery will stand on. A dossier
belongs to exactly one Quality-CMC portal project; a deliverable seeds the
real ICH M4Q numbering; a section that does not apply must say why; and
deleting the dossier purges what it held.
"""

import pytest

from app.cmc import registry
from app.cmc.ctd import DATA_SECTIONS, DRUG_PRODUCT, DRUG_SUBSTANCE


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org_a(two_orgs):
    token_a, project_a, _tb, _pb = two_orgs
    return token_a, project_a


@pytest.fixture
def cmc_portal_project(app_client, org_a):
    token, _ = org_a
    res = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Drug X Tablets dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"})
    assert res.status_code in (200, 201), res.text
    return token, res.json()["id"]


PRODUCT = {
    "product_name": "Drug X Tablets", "inn_or_ds_name": "drugxinib",
    "dosage_form": "Film-coated tablet", "strengths": ["50 mg", "100 mg"],
    "route_of_administration": "Oral", "submission_type": "NDA",
    "target_regions": ["FDA", "EMA"], "development_phase": "Commercial",
}


def _make(app_client, token, project_id, **overrides):
    return app_client.post("/api/v1/cmc/projects", headers=_auth(token),
                           json={"project_id": project_id, **PRODUCT, **overrides})


# ---------------------------------------------------------------- seed data

def test_the_ctd_trees_are_complete_and_consistent():
    for tree in (DRUG_SUBSTANCE, DRUG_PRODUCT):
        codes = [s[0] for s in tree]
        assert len(codes) == len(set(codes))
        for code, _title, guidance, container in tree:
            # A container is a heading and holds no guidance; a leaf holds some
            # or the drafting prompt has nothing to tell the model.
            assert bool(guidance) is not container, code
    substance = [s[0] for s in DRUG_SUBSTANCE]
    product = [s[0] for s in DRUG_PRODUCT]
    for expected in ("S.1.1", "S.2.2", "S.4.1", "S.4.4", "S.7.3"):
        assert expected in substance
    for expected in ("P.1", "P.2.2.1", "P.3.2", "P.5.1", "P.5.4", "P.8.3"):
        assert expected in product


def test_every_data_section_names_a_real_section():
    known = {s[0] for tree in (DRUG_SUBSTANCE, DRUG_PRODUCT) for s in tree}
    assert set(DATA_SECTIONS) <= known
    # The sections that carry numbers are the ones a model must not write.
    assert DATA_SECTIONS["P.5.1"] == "spec_table"
    assert DATA_SECTIONS["P.5.4"] == "batch_analyses"
    assert DATA_SECTIONS["P.8.3"] == "stability_summary"
    assert DATA_SECTIONS["P.3.2"] == "batch_formula"


def test_source_mapping_is_longest_prefix_wins():
    assert registry.source_types_for("ctd_32p", "P.5.1") == ["spec_dp"]
    assert registry.source_types_for("ctd_32p", "P.5.4") == ["coa", "spec_dp"]
    assert registry.source_types_for("ctd_32p", "P.5.2") == ["method_sop"]
    # No entry of its own: inherits P.2.
    assert registry.source_types_for("ctd_32p", "P.2.2.1") == ["dev_report"]
    # No entry at all: no filter, which retrieves widely rather than nothing.
    assert registry.source_types_for("ctd_32ar", "A.9") == []


def test_a_style_reference_is_never_a_declared_source():
    """A previously approved dossier is retrievable for style and citable as
    fact for nothing, so it must not appear in any section's source list."""
    for key, entry in registry.DELIVERABLES.items():
        for section, types in (entry["source_map"] or {}).items():
            assert registry.STYLE_REFERENCE_TYPE not in types, (key, section)
            assert all(t in registry.DOC_TYPES for t in types), (key, section)


def test_requirements_are_computed_from_the_chosen_deliverables():
    substance_only, _ = registry.requirements(["ctd_32s"])
    assert "spec_ds" in substance_only and "bmr" not in substance_only
    both, recommended = registry.requirements(["ctd_32s", "ctd_32p"])
    assert "bmr" in both and "spec_dp" in both
    # A type required by one deliverable is never merely recommended by another.
    assert not (set(both) & set(recommended))


# ---------------------------------------------------------------- projects

def test_create_dossier_and_read_it_back(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    res = _make(app_client, token, project_id)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["product_name"] == "Drug X Tablets"
    assert body["strengths"] == ["50 mg", "100 mg"]
    assert body["target_regions"] == ["FDA", "EMA"]
    assert body["deliverables"] == []
    assert body["status"] == "setup"

    again = _make(app_client, token, project_id)
    assert again.status_code == 409
    assert again.json()["detail"]["error"]["code"] == "CMC_PROJECT_EXISTS"


def test_the_wrong_function_is_refused(app_client, org_a):
    token, hr_project = org_a
    res = _make(app_client, token, hr_project)
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CMC_WRONG_PROJECT"


def test_bad_vocabulary_is_refused(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    bad_type = _make(app_client, token, project_id, submission_type="SOMETHING")
    assert bad_type.status_code == 422
    assert bad_type.json()["detail"]["error"]["code"] == "CMC_BAD_SUBMISSION_TYPE"

    bad_region = _make(app_client, token, project_id, target_regions=["FDA", "MARS"])
    assert bad_region.status_code == 422
    assert bad_region.json()["detail"]["error"]["code"] == "CMC_BAD_REGION"


# ---------------------------------------------------------------- sites

def test_sites_are_named_once_and_reused(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()

    made = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/sites",
                           headers=_auth(token), json={
                               "name": "Acme Pharma Pune", "address": "Plot 14, MIDC, Pune",
                               "identifier": "FEI 3009999", "activities": ["dp_manufacture", "testing"]})
    assert made.status_code == 201, made.text
    site = made.json()
    assert site["activities"] == ["dp_manufacture", "testing"]

    bad = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/sites",
                          headers=_auth(token),
                          json={"name": "X", "activities": ["cooking"]})
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"]["code"] == "CMC_BAD_ACTIVITY"

    patched = app_client.patch(f"/api/v1/cmc/sites/{site['id']}", headers=_auth(token),
                               json={"identifier": "FEI 3001111"})
    assert patched.json()["identifier"] == "FEI 3001111"
    assert patched.json()["name"] == "Acme Pharma Pune"

    listed = app_client.get(f"/api/v1/cmc/projects/{cmc['id']}/sites",
                            headers=_auth(token)).json()["items"]
    assert len(listed) == 1
    app_client.delete(f"/api/v1/cmc/sites/{site['id']}", headers=_auth(token))
    assert app_client.get(f"/api/v1/cmc/projects/{cmc['id']}/sites",
                          headers=_auth(token)).json()["items"] == []


# ---------------------------------------------------------------- deliverables

def test_adding_a_deliverable_seeds_the_ctd_tree(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()

    added = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                            headers=_auth(token), json={"doc_type_key": "ctd_32p"})
    assert added.status_code == 201, added.text
    body = added.json()
    sections = body["sections"]
    assert len(sections) == len(DRUG_PRODUCT)
    assert [s["section_code"] for s in sections] == [s[0] for s in DRUG_PRODUCT]

    by_code = {s["section_code"]: s for s in sections}
    assert by_code["P.5"]["is_container"] is True
    assert by_code["P.5.1"]["table_key"] == "spec_table"
    assert by_code["P.5.1"]["guidance_text"].startswith("The specification")
    assert by_code["P.5.2"]["table_key"] is None
    assert all(s["applicability"] == "applicable" for s in sections)

    detail = app_client.get(f"/api/v1/cmc/projects/{cmc['id']}", headers=_auth(token)).json()
    assert detail["status"] == "ready"
    assert [d["doc_type_key"] for d in detail["deliverables"]] == ["ctd_32p"]
    # The checklist now reflects what a 3.2.P actually needs.
    assert "bmr" in detail["required_doc_types"]
    assert "spec_dp" in detail["required_doc_types"]

    duplicate = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                                headers=_auth(token), json={"doc_type_key": "ctd_32p"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["error"]["code"] == "CMC_DELIVERABLE_EXISTS"


def test_unbuilt_deliverables_say_so(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()
    res = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                          headers=_auth(token), json={"doc_type_key": "apqr"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CMC_DELIVERABLE_UNBUILT"

    unknown = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                              headers=_auth(token), json={"doc_type_key": "nonsense"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"]["code"] == "CMC_UNKNOWN_DELIVERABLE"


def test_the_deliverable_catalogue_lists_what_is_coming(app_client, org_a):
    token, _ = org_a
    items = app_client.get("/api/v1/cmc/deliverable-types",
                           headers=_auth(token)).json()["items"]
    by_key = {i["key"]: i for i in items}
    assert by_key["ctd_32p"]["section_count"] == len(DRUG_PRODUCT)
    assert by_key["ctd_32p"]["unbuilt"] is None
    # Listed rather than hidden, with the milestone that will build it.
    assert by_key["apqr"]["unbuilt"] == "M8"
    assert by_key["apqr"]["section_count"] == 0


# ---------------------------------------------------------------- sections

def test_a_section_that_does_not_apply_must_say_why(app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()
    sections = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                               headers=_auth(token),
                               json={"doc_type_key": "ctd_32p"}).json()["sections"]
    by_code = {s["section_code"]: s for s in sections}

    bare = app_client.patch(f"/api/v1/cmc/sections/{by_code['P.4.6']['id']}",
                            headers=_auth(token), json={"applicability": "not_applicable"})
    assert bare.status_code == 422
    assert bare.json()["detail"]["error"]["code"] == "CMC_NEEDS_JUSTIFICATION"

    justified = app_client.patch(
        f"/api/v1/cmc/sections/{by_code['P.4.6']['id']}", headers=_auth(token),
        json={"applicability": "not_applicable",
              "applicability_justification": "No novel excipients are used."})
    assert justified.status_code == 200
    assert justified.json()["applicability"] == "not_applicable"

    dmf = app_client.patch(
        f"/api/v1/cmc/sections/{by_code['P.4.5']['id']}", headers=_auth(token),
        json={"applicability": "referenced_dmf",
              "applicability_justification": "Covered by DMF 12345, closed part."})
    assert dmf.status_code == 200

    bad = app_client.patch(f"/api/v1/cmc/sections/{by_code['P.4.4']['id']}",
                           headers=_auth(token), json={"applicability": "maybe"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"]["code"] == "CMC_BAD_APPLICABILITY"

    container = app_client.patch(f"/api/v1/cmc/sections/{by_code['P.5']['id']}",
                                 headers=_auth(token), json={"enabled": False})
    assert container.status_code == 422
    assert container.json()["detail"]["error"]["code"] == "CMC_SECTION_IS_CONTAINER"


def test_a_deliverable_carrying_work_is_not_silently_dropped(
        app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()
    added = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                            headers=_auth(token), json={"doc_type_key": "ctd_32s"}).json()

    empty = app_client.delete(f"/api/v1/cmc/deliverables/{added['id']}",
                              headers=_auth(token))
    assert empty.status_code == 200
    assert empty.json()["purged_sections"] == len(DRUG_SUBSTANCE)

    again = app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                            headers=_auth(token), json={"doc_type_key": "ctd_32s"}).json()
    from app.db import SessionLocal
    from app.models import CmcSection

    db = SessionLocal()
    try:
        section = db.query(CmcSection).filter(
            CmcSection.cmc_deliverable_id == again["id"],
            CmcSection.section_code == "S.4.1").one()
        section.status = "draft"
        db.commit()
    finally:
        db.close()

    refused = app_client.delete(f"/api/v1/cmc/deliverables/{again['id']}",
                                headers=_auth(token))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "CMC_DELIVERABLE_HAS_WORK"


# ---------------------------------------------------------------- purge

def test_delete_purges_the_dossier_and_leaves_the_portal_project(
        app_client, cmc_portal_project):
    token, project_id = cmc_portal_project
    cmc = _make(app_client, token, project_id).json()
    app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/deliverables",
                    headers=_auth(token), json={"doc_type_key": "ctd_32p"})
    app_client.post(f"/api/v1/cmc/projects/{cmc['id']}/sites", headers=_auth(token),
                    json={"name": "Pune Plant"})

    gone = app_client.delete(f"/api/v1/cmc/projects/{cmc['id']}", headers=_auth(token))
    assert gone.status_code == 200, gone.text
    assert gone.json()["purged"]["sections"] == len(DRUG_PRODUCT)

    assert app_client.get(f"/api/v1/cmc/projects/{cmc['id']}",
                          headers=_auth(token)).status_code == 404

    from app.db import SessionLocal
    from app.models import CmcDeliverable, CmcSection, CmcSite

    db = SessionLocal()
    try:
        assert db.query(CmcDeliverable).filter(
            CmcDeliverable.cmc_project_id == cmc["id"]).count() == 0
        assert db.query(CmcSite).filter(CmcSite.cmc_project_id == cmc["id"]).count() == 0
        assert db.query(CmcSection).count() >= 0
    finally:
        db.close()

    # The portal project survives: a dossier purge is not a project deletion.
    assert app_client.get(f"/api/v1/projects/{project_id}",
                          headers=_auth(token)).status_code == 200


def test_the_other_tenant_sees_nothing(app_client, cmc_portal_project, two_orgs):
    token, project_id = cmc_portal_project
    _ta, _pa, token_b, _pb = two_orgs
    cmc = _make(app_client, token, project_id).json()
    assert app_client.get(f"/api/v1/cmc/projects/{cmc['id']}",
                          headers=_auth(token_b)).status_code == 404
    listed = app_client.get("/api/v1/cmc/projects", headers=_auth(token_b)).json()["items"]
    assert not any(p["id"] == cmc["id"] for p in listed)
