"""Setting up a product and a reporting interval.

M1's whole job is the part every later milestone depends on: the three dates,
the pinned reference safety information, and who is allowed to touch either.
What is asserted here is mostly refusals -- the dates that cannot be set, the
baseline that cannot be used, the role nobody acquires by accident -- because
a setup screen that accepts a wrong interval produces a report whose every
figure is right about the wrong window.
"""

from datetime import date

import pytest

from app.safety import registry, trees


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _project(client, token, name="Safety product"):
    return client.post("/api/v1/projects", headers=_auth(token), json={
        "name": name, "function": "Safety", "document_type": "PSUR",
        "region": "Global", "language": "English"}).json()


@pytest.fixture
def product(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = _project(app_client, token)
    created = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Vigilazine",
        "inn": "vigilazine", "mah_name": "Acme Pharma",
        "ibd": "2020-03-01", "dibd": "2016-09-01",
        "regions": ["EU", "US"]})
    assert created.status_code == 201, created.text
    return token, created.json()


# --------------------------------------------------------------- the product

def test_a_safety_product_needs_a_safety_project(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Not safety", "function": "Clinical",
        "document_type": "Clinical Study Report", "region": "Global",
        "language": "English"}).json()
    refused = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Wrongazine"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_WRONG_PROJECT"


def test_one_product_per_project(app_client, two_orgs, product):
    token, created = product
    again = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": created["project_id"], "product_name": "Duplicazine"})
    assert again.status_code == 409
    assert again.json()["detail"]["error"]["code"] == "PV_PRODUCT_EXISTS"


def test_an_unknown_region_is_refused(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = _project(app_client, token, "Region check")
    refused = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Regionazine",
        "regions": ["Narnia"]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_BAD_REGION"


def test_moving_a_birth_date_is_audited_as_a_change_not_an_edit(app_client, product):
    """The birth date is where cumulative counting starts. Moving it silently
    rewrites every cumulative figure in every report of this product."""
    from app.db import SessionLocal
    from app.models import AuditLog

    token, created = product
    app_client.patch(f"/api/v1/pv/products/{created['id']}", headers=_auth(token),
                     json={"ibd": "2019-01-01"})
    db = SessionLocal()
    try:
        entries = db.query(AuditLog).filter(
            AuditLog.entity_type == "pv_product",
            AuditLog.entity_id == created["id"]).all()
    finally:
        db.close()
    moved = [e for e in entries if "ibd" in (e.target or "")]
    assert moved, [e.target for e in entries]
    assert moved[0].severity == "warning"


# ------------------------------------------------------------------- roles

def test_the_creator_is_a_writer_and_not_a_qualified_person(app_client, product):
    """Nobody acquires the authority to confirm a causality assessment by
    being the first person to press a button."""
    token, created = product
    fetched = app_client.get(f"/api/v1/pv/products/{created['id']}",
                             headers=_auth(token)).json()
    assert fetched["my_role"] == "writer"


def test_pinning_the_rsi_needs_the_qualified_person_role(app_client, product):
    token, created = product
    version = app_client.post(
        f"/api/v1/pv/products/{created['id']}/rsi-versions", headers=_auth(token),
        json={"rsi_type": "ccds", "version_label": "3.1",
              "effective_date": "2025-06-01"}).json()

    refused = app_client.post(f"/api/v1/pv/rsi-versions/{version['id']}/pin",
                              headers=_auth(token))
    assert refused.status_code == 403
    error = refused.json()["detail"]["error"]
    assert error["code"] == "PV_ROLE_REQUIRED"
    assert error["details"]["required_role"] == "qualified_person"
    assert error["details"]["actual_role"] == "writer"


def test_an_org_admin_can_name_a_qualified_person_without_becoming_one(
        app_client, two_orgs, product):
    """The bootstrap. Requiring an existing qualified person to name the first
    one would deadlock every new product; letting the capability CONFER the
    role would be the silent default the module forbids."""
    token, created = product
    users = app_client.get("/api/v1/pv/products/{}/members".format(created["id"]),
                           headers=_auth(token)).json()
    me = users["items"][0]["user_id"]

    granted = app_client.post(f"/api/v1/pv/products/{created['id']}/members",
                              headers=_auth(token),
                              json={"user_id": me, "pv_role": "qualified_person"})
    assert granted.status_code == 201, granted.text
    assert granted.json()["pv_role"] == "qualified_person"


def test_once_qualified_the_pin_goes_through(app_client, product):
    token, created = product
    members = app_client.get(f"/api/v1/pv/products/{created['id']}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{created['id']}/members",
                    headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "qualified_person"})
    version = app_client.post(
        f"/api/v1/pv/products/{created['id']}/rsi-versions", headers=_auth(token),
        json={"rsi_type": "ccds", "version_label": "3.2"}).json()
    pinned = app_client.post(f"/api/v1/pv/rsi-versions/{version['id']}/pin",
                             headers=_auth(token))
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned"]["is_current"] is True


def test_pinning_a_new_version_supersedes_the_old_one_and_leaves_reports_alone(
        app_client, product):
    """An expectedness confirmed against 3.1 was a determination about 3.1.
    Re-pointing it at 3.2 would be recording a judgment nobody made."""
    token, created = product
    members = app_client.get(f"/api/v1/pv/products/{created['id']}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{created['id']}/members",
                    headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "qualified_person"})
    first = app_client.post(
        f"/api/v1/pv/products/{created['id']}/rsi-versions", headers=_auth(token),
        json={"rsi_type": "ccds", "version_label": "1.0"}).json()
    app_client.post(f"/api/v1/pv/rsi-versions/{first['id']}/pin", headers=_auth(token))
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15",
              "rsi_version_id": first["id"]}).json()

    second = app_client.post(
        f"/api/v1/pv/products/{created['id']}/rsi-versions", headers=_auth(token),
        json={"rsi_type": "ccds", "version_label": "2.0"}).json()
    result = app_client.post(f"/api/v1/pv/rsi-versions/{second['id']}/pin",
                             headers=_auth(token)).json()
    assert result["superseded"] == [first["id"]]
    assert result["open_reports_on_previous_version"] == 1

    unchanged = app_client.get(f"/api/v1/pv/reports/{report['id']}",
                               headers=_auth(token)).json()
    assert unchanged["rsi_version_id"] == first["id"], "the report keeps its pin"


# ------------------------------------------------------ the reporting interval

def test_a_lock_before_the_period_ends_is_refused(app_client, product):
    """It would exclude the tail of the very interval the report is about, and
    every figure would be short by however many weeks."""
    token, created = product
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-05-01"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_DLP_BEFORE_PERIOD_END"


def test_a_period_that_ends_before_it_starts_is_refused(app_client, product):
    token, created = product
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-06-30",
              "period_end": "2026-01-01", "data_lock_point": "2026-07-15"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_BAD_PERIOD"


def test_an_unknown_report_type_is_refused(app_client, product):
    token, created = product
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "psur_v2", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_UNKNOWN_REPORT_TYPE"


def test_creating_a_report_seeds_its_whole_section_tree(app_client, product):
    token, created = product
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"})
    assert report.status_code == 201, report.text
    body = report.json()
    assert body["section_count"] == len(trees.DSUR)
    codes = [s["section_code"] for s in body["sections"]]
    assert codes == [c for c, _t in trees.DSUR], "document order, not insertion order"

    # A container holds no text and carries no table.
    section_7 = next(s for s in body["sections"] if s["section_code"] == "7")
    assert section_7["is_container"] is True
    assert section_7["table_key"] is None
    # Its data-bearing child does carry one.
    section_73 = next(s for s in body["sections"] if s["section_code"] == "7.3")
    assert section_73["table_key"] == "summary_tab_soc_pt"
    assert section_73["level"] == 2


def test_every_report_type_seeds_without_error(app_client, product):
    """The registry and the trees have to agree about every key, or a report
    type is offered in the picker and fails on creation."""
    token, created = product
    for index, key in enumerate(sorted(registry.DELIVERABLES)):
        made = app_client.post(
            f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
            json={"doc_type_key": key, "period_start": f"20{20 + index}-01-01",
                  "period_end": f"20{20 + index}-06-30",
                  "data_lock_point": f"20{20 + index}-07-15"})
        assert made.status_code == 201, (key, made.text)
        assert made.json()["section_count"] == len(trees.TREES[key])


def test_a_declared_table_key_is_one_a_builder_could_serve(app_client, product):
    """Every table key named in the trees has to be a real one. A section
    declaring a key nothing renders would block its own export forever."""
    known = {
        "summary_tab_soc_pt", "summary_tab_trials", "line_listing_sar",
        "exposure_table", "signal_overview", "safety_concern_table",
        "action_table", "study_inventory", "approval_status_table",
        "literature_table",
    }
    for key, mapping in trees.TABLE_KEYS.items():
        unknown = set(mapping.values()) - known
        assert not unknown, f"{key} names table(s) nothing builds: {unknown}"


# ---------------------------------------------------------------- the baseline

@pytest.fixture
def approved_first_report(app_client, product):
    """A first DSUR with one section carrying text, ready to be a baseline."""
    from app.db import SessionLocal
    from app.models import PvReportInstance, PvSection, PvSectionDraft

    token, created = product
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2025-01-01",
              "period_end": "2025-06-30", "data_lock_point": "2025-07-15"}).json()

    db = SessionLocal()
    try:
        sections = {s.section_code: s for s in db.query(PvSection).filter(
            PvSection.report_instance_id == report["id"]).all()}
        # A narrative section with text...
        intro = sections["1"]
        db.add(PvSectionDraft(org_id=intro.org_id, pv_section_id=intro.id, version=1,
                              content="This DSUR covers the first period.",
                              origin="edited", created_by="tester"))
        # ...and a data section with text, which must NOT carry forward.
        tabulation = sections["7.3"]
        db.add(PvSectionDraft(org_id=tabulation.org_id, pv_section_id=tabulation.id,
                              version=1,
                              content="Last period's tabulation.\n\n"
                                      "[TABLE: summary_tab_soc_pt]",
                              origin="model", created_by="tester"))
        row = db.get(PvReportInstance, report["id"])
        row.status = "approved"
        db.commit()
    finally:
        db.close()
    return token, created, report


def test_a_baseline_carries_narrative_text_forward(app_client, approved_first_report):
    token, created, baseline = approved_first_report
    second = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2025-07-01",
              "period_end": "2025-12-31", "data_lock_point": "2026-01-15",
              "baseline_report_id": baseline["id"]})
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["carried_forward"] == 1

    intro = next(s for s in body["sections"] if s["section_code"] == "1")
    assert intro["delta_status"] == "carried_forward"
    assert intro["status"] == "draft"
    assert intro["baseline_section_id"] == baseline_section_id(
        app_client, token, baseline["id"], "1")

    draft = app_client.get(f"/api/v1/pv/reports/{body['id']}/sections",
                           headers=_auth(token)).json()
    assert any(s["delta_status"] == "carried_forward" for s in draft["items"])


def baseline_section_id(client, token, report_id, code):
    sections = client.get(f"/api/v1/pv/reports/{report_id}/sections",
                          headers=_auth(token)).json()["items"]
    return next(s["id"] for s in sections if s["section_code"] == code)


def test_a_data_section_is_marked_changed_rather_than_carried_forward(
        app_client, approved_first_report):
    """Its table is rendered from THIS interval's data. Carrying last
    interval's sentences over this interval's numbers is how a report comes to
    describe a table it does not contain."""
    token, created, baseline = approved_first_report
    body = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2025-07-01",
              "period_end": "2025-12-31", "data_lock_point": "2026-01-15",
              "baseline_report_id": baseline["id"]}).json()
    tabulation = next(s for s in body["sections"] if s["section_code"] == "7.3")
    assert tabulation["delta_status"] == "changed"
    assert tabulation["status"] == "not_started"


def test_a_baseline_of_a_different_report_type_is_refused(
        app_client, approved_first_report):
    token, created, baseline = approved_first_report
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "pbrer", "period_start": "2025-07-01",
              "period_end": "2025-12-31", "data_lock_point": "2026-01-15",
              "baseline_report_id": baseline["id"]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_BASELINE_WRONG_TYPE"


def test_overlapping_intervals_are_refused(app_client, approved_first_report):
    """Two intervals that overlap count the same cases twice."""
    token, created, baseline = approved_first_report
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2025-05-01",
              "period_end": "2025-12-31", "data_lock_point": "2026-01-15",
              "baseline_report_id": baseline["id"]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_BASELINE_OVERLAPS"


def test_an_approved_report_cannot_be_deleted(app_client, approved_first_report):
    token, _created, baseline = approved_first_report
    refused = app_client.delete(f"/api/v1/pv/reports/{baseline['id']}",
                                headers=_auth(token))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_REPORT_APPROVED"


def test_a_report_that_is_a_baseline_cannot_be_deleted(
        app_client, approved_first_report):
    """Independently of approval: deleting it would leave the report that
    carried text forward from it pointing at nothing."""
    from app.db import SessionLocal
    from app.models import PvReportInstance

    token, created, baseline = approved_first_report
    app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2025-07-01",
              "period_end": "2025-12-31", "data_lock_point": "2026-01-15",
              "baseline_report_id": baseline["id"]})
    # Take the approval off, so the refusal under test is the only one left.
    db = SessionLocal()
    try:
        db.get(PvReportInstance, baseline["id"]).status = "in_progress"
        db.commit()
    finally:
        db.close()

    refused = app_client.delete(f"/api/v1/pv/reports/{baseline['id']}",
                                headers=_auth(token))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_REPORT_IS_BASELINE"


# ------------------------------------------------------------- scope preview

def test_the_preview_answers_before_the_instance_exists(app_client, product):
    """Screen S2 shows the counts before anybody commits, because the three
    dates are the hardest thing to change afterwards."""
    token, created = product
    preview = app_client.post(
        f"/api/v1/pv/products/{created['id']}/scope-preview", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["interval_cases"] == 0
    assert body["cumulative_cases"] == 0
    assert body["cumulative_anchor"] == "dibd"
    assert body["cumulative_from"] == "2016-09-01"


def test_the_preview_refuses_the_same_dates_the_creation_endpoint_refuses(
        app_client, product):
    """One validation, both doors. A preview that accepted an interval the
    creation endpoint rejects would show somebody figures for a report they
    cannot make."""
    token, created = product
    refused = app_client.post(
        f"/api/v1/pv/products/{created['id']}/scope-preview", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-05-01"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_DLP_BEFORE_PERIOD_END"


def test_a_pbrer_preview_uses_the_approval_birth_date(app_client, product):
    token, created = product
    body = app_client.post(
        f"/api/v1/pv/products/{created['id']}/scope-preview", headers=_auth(token),
        json={"doc_type_key": "pbrer", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    assert body["cumulative_anchor"] == "ibd"
    assert body["cumulative_from"] == "2020-03-01"


# --------------------------------------------------------------- the calendar

def test_the_calendar_carries_its_disclaimer_with_the_dates(app_client, product):
    """§2's third principle. The disclaimer travels with the data so a caller
    cannot render the dates without it."""
    token, created = product
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    app_client.post(f"/api/v1/pv/reports/{report['id']}/due-dates",
                    headers=_auth(token),
                    json={"region": "EU", "submission_due_date": "2026-09-15",
                          "basis_note": "90 days from the data lock point"})

    calendar = app_client.get(f"/api/v1/pv/products/{created['id']}/calendar",
                              headers=_auth(token)).json()
    assert "Informational only" in calendar["disclaimer"]
    assert "system of record" in calendar["disclaimer"]
    entry = calendar["items"][0]
    assert entry["due_dates"][0]["region"] == "EU"
    assert entry["due_dates"][0]["is_informational"] is True


def test_a_due_date_is_always_informational(app_client, product):
    token, created = product
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    due = app_client.post(f"/api/v1/pv/reports/{report['id']}/due-dates",
                          headers=_auth(token),
                          json={"region": "US"}).json()
    assert due["is_informational"] is True


# ---------------------------------------------------------------- the registry

def test_the_report_type_catalogue_is_the_registry(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    body = app_client.get("/api/v1/pv/report-types", headers=_auth(token)).json()
    keys = {entry["key"] for entry in body["items"]}
    assert keys == set(registry.DELIVERABLES)
    dsur = next(e for e in body["items"] if e["key"] == "dsur")
    assert dsur["cumulative_anchor"] == "dibd"
    assert dsur["section_count"] == len(trees.DSUR)
    assert "previous_report" in body["doc_types"]


def test_the_status_of_a_report_is_not_how_it_gets_approved(app_client, product):
    """Approval is sign-off, which is a qualified-person act with a signature
    on it. Letting the status field carry it would make approval a dropdown."""
    token, created = product
    report = app_client.post(
        f"/api/v1/pv/products/{created['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    refused = app_client.patch(f"/api/v1/pv/reports/{report['id']}",
                               headers=_auth(token), json={"status": "approved"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_SIGNOFF_REQUIRED"
