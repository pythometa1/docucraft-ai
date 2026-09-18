"""The computed tables, and the three promises they make.

1. **One path to a count.** Every case table filters through
   `app.safety.scope`, so a case after the lock is in no cell of any table.
2. **Every cell knows where it came from.** The drill-down reads the provenance
   the builder recorded while counting, not a second query.
3. **Nothing unconfirmed is counted as confirmed.** An event nobody has decided
   the expectedness of is in a column that says so, never in "listed" or
   "unlisted".

And acceptance criterion 6, which is the point of computing tables at all:
change one event's confirmed seriousness in the grid and every table that
counts it moves, without a single section being regenerated.
"""

from datetime import date

import pytest

from app.safety import tabulations as tab


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def store(app_client, two_orgs):
    """A DSUR over H1 2026, locked 15 July, with cases on each side of every
    boundary that matters."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Tabulated safety", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Tabulazine",
        "ibd": "2020-01-01", "dibd": "2016-01-01"}).json()
    rsi = app_client.post(f"/api/v1/pv/products/{product['id']}/rsi-versions",
                          headers=_auth(token),
                          json={"rsi_type": "ib", "version_label": "7"}).json()
    report = app_client.post(
        f"/api/v1/pv/products/{product['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15",
              "rsi_version_id": rsi["id"]}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    ids = {}

    def case(label, received, *, serious=False, source="spontaneous", events=()):
        row = PvCase(org_id=org_id, pv_product_id=product["id"],
                     worldwide_case_id=label, initial_receipt_date=received,
                     latest_receipt_date=received, is_serious=serious,
                     seriousness_criteria=["hospitalisation"] if serious else [],
                     report_source=source, country_of_occurrence="GB",
                     patient_age=40.0, patient_sex="female")
        db.add(row)
        db.flush()
        ids[label] = row.id
        for pt, soc, extra in events:
            event = PvCaseEvent(org_id=org_id, pv_product_id=product["id"],
                                case_id=row.id, verbatim_term=f"{pt} (reported)",
                                meddra_pt=pt, meddra_soc=soc, **extra)
            db.add(event)
            db.flush()
            ids[f"{label}:{pt}"] = event.id
        return row

    nerv = "Nervous system disorders"
    gi = "Gastrointestinal disorders"
    # In the interval: a serious case with a confirmed unlisted headache and an
    # unconfirmed nausea.
    case("INT-1", date(2026, 3, 4), serious=True, source="clinical_trial", events=[
        ("Headache", nerv, dict(expectedness="unlisted", confirmed_by="qp",
                                is_serious=True, causality_company="related")),
        ("Nausea", gi, {}),
    ])
    # In the interval, non-serious, confirmed listed.
    case("INT-2", date(2026, 4, 11), events=[
        ("Headache", nerv, dict(expectedness="listed", confirmed_by="qp")),
    ])
    # Before the interval: cumulative only.
    case("OLD-1", date(2022, 6, 1), serious=True, source="clinical_trial", events=[
        ("Headache", nerv, {}),
    ])
    # After the lock: in no cell of any table.
    case("LATE-1", date(2026, 8, 1), serious=True, events=[
        ("Headache", nerv, dict(expectedness="unlisted", confirmed_by="qp")),
    ])
    # In the interval, serious, with no causality at all.
    case("INT-3", date(2026, 5, 5), serious=True, events=[("Dizziness", nerv, {})])
    db.commit()
    db.close()
    return token, product["id"], report["id"], ids


def _render(product_id, report_id, key):
    from app.db import SessionLocal
    from app.models import PvProduct, PvReportInstance

    db = SessionLocal()
    try:
        return tab.render(db, report=db.get(PvReportInstance, report_id),
                          product=db.get(PvProduct, product_id), table_key=key)
    finally:
        db.close()


def _cell(table, pt, column_label):
    row = next(r for r in table.rows if r[1] == pt)
    return int(row[table.columns.index(column_label)])


# ------------------------------------------------------------ the summary

def test_interval_and_cumulative_are_separate_columns(store):
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    # Interval: INT-1 (serious, confirmed unlisted) and INT-2 (non-serious,
    # confirmed listed).
    assert _cell(table, "Headache", "Interval: Serious unlisted") == 1
    assert _cell(table, "Headache", "Interval: Non-serious listed") == 1
    # Cumulative adds OLD-1, whose headache nobody has confirmed.
    assert _cell(table, "Headache",
                 "Cumulative: Serious, expectedness not confirmed") == 1
    assert _cell(table, "Headache", "Cumulative: Serious unlisted") == 1


def test_a_case_after_the_lock_is_in_no_cell(store):
    """Acceptance criterion 4, through the table rather than the predicate."""
    _t, product_id, report_id, ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    every_case = {c for cell in table.cells.values() for c in cell["cases"]}
    assert ids["LATE-1"] not in every_case
    assert ids["INT-1"] in every_case and ids["OLD-1"] in every_case


def test_a_suggestion_is_counted_nowhere(store):
    """Acceptance criterion 3. Nausea has no confirmed expectedness, so it is in
    the "not confirmed" column and in neither "listed" nor "unlisted"."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    assert _cell(table, "Nausea", "Interval: Serious, expectedness not confirmed") == 1
    assert _cell(table, "Nausea", "Interval: Serious unlisted") == 0
    assert _cell(table, "Nausea", "Interval: Serious listed") == 0
    assert any("no confirmed expectedness" in gap for gap in table.missing)


def test_an_event_inherits_its_cases_seriousness_until_somebody_decides(store):
    """E2B states seriousness at case level. Nausea on a serious case has no
    event flag of its own, and reading that absence as "not serious" would move
    every event of every serious case into the non-serious column."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    assert _cell(table, "Nausea", "Interval: Non-serious, expectedness not confirmed") == 0
    assert _cell(table, "Nausea", "Interval: Serious, expectedness not confirmed") == 1


def test_the_totals_reconcile_with_the_cells(store):
    """QC reconciles figures from `totals`, so they must be the cells added up
    rather than a second count."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    interval_columns = [i for i, c in enumerate(table.columns)
                        if c.startswith("Interval:")]
    summed = sum(int(row[i]) for row in table.rows for i in interval_columns)
    assert summed == table.totals["interval_events"] == 4


def test_every_numeric_cell_carries_its_provenance(store):
    _t, product_id, report_id, ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    row = next(i for i, r in enumerate(table.rows) if r[1] == "Headache")
    column = table.columns.index("Interval: Serious unlisted")
    cell = table.cells[f"r{row}c{column}"]
    assert cell["cases"] == [ids["INT-1"]]
    assert cell["events"] == [ids["INT-1:Headache"]]
    # And the count printed is the number of contributors recorded.
    assert int(table.rows[row][column]) == len(cell["events"])


def test_the_scope_travels_with_the_table(store):
    """So a figure quoted in prose can be checked against the window it came
    from."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    assert table.scope["data_lock_point"] == "2026-07-15"
    assert table.scope["cumulative_anchor"] == "dibd"


def test_the_rendered_blocks_carry_the_same_cells(store):
    """`rows` for assertions and `blocks` for the document come from the same
    computed cells, so the printed table and the tested one cannot drift."""
    import tempfile

    import docx

    from app.docgen import grids

    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_soc_pt")
    with tempfile.TemporaryDirectory() as folder:
        path = grids.write_docx(table.blocks, f"{folder}/t.docx")
        printed = docx.Document(path).tables[0]
        header = [c.text for c in printed.rows[0].cells]
        assert header == table.columns
        body = [[c.text for c in r.cells] for r in printed.rows[1:]]
        assert body == table.rows


# ---------------------------------------- acceptance criterion 6, end to end

def test_a_seriousness_correction_moves_the_table_without_regenerating(
        app_client, store):
    """The whole point of computing tables. INT-2's headache is confirmed
    non-serious; a qualified person corrects it to serious, and the tabulation
    moves -- no section drafted, no regeneration, nothing re-typed."""
    token, product_id, report_id, ids = store
    before = _render(product_id, report_id, "summary_tab_soc_pt")
    assert _cell(before, "Headache", "Interval: Non-serious listed") == 1
    assert _cell(before, "Headache", "Interval: Serious listed") == 0

    members = app_client.get(f"/api/v1/pv/products/{product_id}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{product_id}/members", headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "qualified_person"})
    fixed = app_client.patch(f"/api/v1/pv/case-events/{ids['INT-2:Headache']}/confirm",
                             headers=_auth(token),
                             params={"report_instance_id": report_id},
                             json={"is_serious": True})
    assert fixed.status_code == 200, fixed.text

    after = _render(product_id, report_id, "summary_tab_soc_pt")
    assert _cell(after, "Headache", "Interval: Non-serious listed") == 0
    assert _cell(after, "Headache", "Interval: Serious listed") == 1


# ------------------------------------------------------- the line listing

def test_the_line_listing_holds_serious_related_events_only(store):
    _t, product_id, report_id, ids = store
    table = _render(product_id, report_id, "line_listing_sar")
    assert [r[0] for r in table.rows] == ["INT-1"]
    assert table.rows[0][3] == "Headache"
    assert table.totals["interval_sar_rows"] == 1


def test_an_unassessed_serious_event_is_named_not_dropped(store):
    """"Nobody assessed it" and "unrelated" are different statements. A line
    listing that confused them would be short by exactly the cases nobody got
    round to."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "line_listing_sar")
    assert any("no causality assessment" in gap for gap in table.missing)
    assert table.totals["interval_serious_unassessed"] >= 1


def test_the_line_listing_prints_the_coded_term_not_the_reporters_words(store):
    """A reporter's own words are free text, and free text is where identifiers
    hide."""
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "line_listing_sar")
    flat = " ".join(" ".join(r) for r in table.rows)
    assert "(reported)" not in flat


# -------------------------------------------------- the trials tabulation

def test_the_trials_table_is_cumulative_serious_and_trial_only(store):
    _t, product_id, report_id, _ids = store
    table = _render(product_id, report_id, "summary_tab_trials")
    assert not any(c.startswith("Interval:") for c in table.columns)
    assert not any("Non-serious" in c for c in table.columns)
    headache = next(r for r in table.rows if r[1] == "Headache")
    assert sum(int(v) for v in headache[2:]) == 2   # INT-1 and OLD-1, both trials
    assert any("pooled across arms" in note for note in table.notes)


def test_a_product_with_no_birth_date_shows_no_cumulative_columns(
        app_client, two_orgs):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Undated", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Nodibdazine"}).json()
    report = app_client.post(
        f"/api/v1/pv/products/{product['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    row = PvCase(org_id=org_id, pv_product_id=product["id"], worldwide_case_id="U-1",
                 initial_receipt_date=date(2026, 3, 1),
                 latest_receipt_date=date(2026, 3, 1))
    db.add(row)
    db.flush()
    db.add(PvCaseEvent(org_id=org_id, pv_product_id=product["id"], case_id=row.id,
                       meddra_pt="Rash"))
    db.commit()
    db.close()

    table = _render(product["id"], report["id"], "summary_tab_soc_pt")
    assert not any(c.startswith("Cumulative:") for c in table.columns)
    assert any("DIBD" in note for note in table.notes)


# ------------------------------------------------------------ the endpoints

def test_the_table_list_says_which_tables_can_be_built(app_client, store):
    token, _product_id, report_id, _ids = store
    listed = app_client.get(f"/api/v1/pv/reports/{report_id}/tabulations",
                            headers=_auth(token)).json()["items"]
    by_key = {item["key"]: item for item in listed}
    assert by_key["summary_tab_soc_pt"]["available"] is True
    assert by_key["exposure_table"]["available"] is False
    assert "no exposure" in by_key["exposure_table"]["reason"]


def test_an_unknown_table_is_a_404_and_an_empty_one_a_409(app_client, store):
    token, _product_id, report_id, _ids = store
    unknown = app_client.get(f"/api/v1/pv/reports/{report_id}/tabulations/nonsense",
                             headers=_auth(token))
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"]["code"] == "PV_UNKNOWN_TABLE"
    empty = app_client.get(f"/api/v1/pv/reports/{report_id}/tabulations/exposure_table",
                           headers=_auth(token))
    assert empty.status_code == 409
    assert empty.json()["detail"]["error"]["code"] == "PV_TABLE_UNAVAILABLE"


def test_the_drilldown_returns_the_cases_behind_a_cell(app_client, store):
    """Acceptance criterion 5: clicking a cell drills to the contributing
    cases."""
    token, _product_id, report_id, _ids = store
    table = app_client.get(
        f"/api/v1/pv/reports/{report_id}/tabulations/summary_tab_soc_pt",
        headers=_auth(token)).json()
    row = next(i for i, r in enumerate(table["rows"]) if r[1] == "Headache")
    column = table["columns"].index("Interval: Serious unlisted")
    drilled = app_client.get(
        f"/api/v1/pv/reports/{report_id}/tabulations/summary_tab_soc_pt/drilldown",
        headers=_auth(token), params={"cell": f"r{row}c{column}"}).json()
    assert drilled["count"] == 1
    assert [c["worldwide_case_id"] for c in drilled["cases"]] == ["INT-1"]


# -------------------------------------------------------------- exposure

def test_exposure_keeps_its_stated_value_and_computes_a_number_beside_it(
        app_client, store):
    token, _product_id, report_id, _ids = store
    made = app_client.post(f"/api/v1/pv/reports/{report_id}/exposure",
                           headers=_auth(token),
                           json={"context": "clinical_trial", "measure": "subjects",
                                 "value_text": "1,240",
                                 "calculation_method_note": "enrolled and dosed"})
    assert made.status_code == 201, made.text
    assert made.json()["value_text"] == "1,240"
    assert made.json()["value_numeric"] == 1240.0


def test_an_ambiguous_comma_is_not_turned_into_a_number(app_client, store):
    """"1,5" is one and a half or fifteen depending on who wrote it. A rate
    divided by the wrong one is wrong by a factor of ten."""
    token, _product_id, report_id, _ids = store
    made = app_client.post(f"/api/v1/pv/reports/{report_id}/exposure",
                           headers=_auth(token),
                           json={"context": "marketing", "measure": "patient_years",
                                 "value_text": "1,5"}).json()
    assert made["value_numeric"] is None


def test_exposure_is_confirmed_by_a_reviewer_with_its_method(app_client, store):
    token, product_id, report_id, _ids = store
    row = app_client.post(f"/api/v1/pv/reports/{report_id}/exposure",
                          headers=_auth(token),
                          json={"context": "marketing", "measure": "units_sold",
                                "value_text": "2,000,000"}).json()
    refused = app_client.post(f"/api/v1/pv/exposure/{row['id']}/confirm",
                              headers=_auth(token))
    assert refused.status_code == 403, "a writer does not confirm a denominator"

    members = app_client.get(f"/api/v1/pv/products/{product_id}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{product_id}/members", headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "reviewer"})
    no_method = app_client.post(f"/api/v1/pv/exposure/{row['id']}/confirm",
                                headers=_auth(token))
    assert no_method.status_code == 409
    assert no_method.json()["detail"]["error"]["code"] == "PV_EXPOSURE_NEEDS_METHOD"

    app_client.patch(f"/api/v1/pv/exposure/{row['id']}", headers=_auth(token),
                     json={"calculation_method_note": "IQVIA unit sales, all packs"})
    done = app_client.post(f"/api/v1/pv/exposure/{row['id']}/confirm",
                           headers=_auth(token))
    assert done.status_code == 200
    assert done.json()["confirmed_by"]


def test_changing_a_confirmed_figure_withdraws_the_confirmation(app_client, store):
    """Whatever the reviewer confirmed was the old number, not this one."""
    token, product_id, report_id, _ids = store
    members = app_client.get(f"/api/v1/pv/products/{product_id}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{product_id}/members", headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "reviewer"})
    row = app_client.post(f"/api/v1/pv/reports/{report_id}/exposure",
                          headers=_auth(token),
                          json={"context": "marketing", "measure": "units_sold",
                                "value_text": "2,000,000",
                                "calculation_method_note": "unit sales"}).json()
    app_client.post(f"/api/v1/pv/exposure/{row['id']}/confirm", headers=_auth(token))
    changed = app_client.patch(f"/api/v1/pv/exposure/{row['id']}",
                               headers=_auth(token),
                               json={"value_text": "2,100,000"}).json()
    assert changed["confirmed_by"] is None
    assert changed["value_numeric"] == 2100000.0


def test_an_unconfirmed_exposure_is_printed_and_named(app_client, store):
    token, product_id, report_id, _ids = store
    app_client.post(f"/api/v1/pv/reports/{report_id}/exposure", headers=_auth(token),
                    json={"context": "clinical_trial", "measure": "subjects",
                          "value_text": "1,240"})
    table = _render(product_id, report_id, "exposure_table")
    assert table.rows[0][4] == "1,240"
    assert any("not confirmed" in gap for gap in table.missing)
    assert any("no calculation method" in gap for gap in table.missing)


def test_a_bad_exposure_measure_is_refused(app_client, store):
    token, _product_id, report_id, _ids = store
    res = app_client.post(f"/api/v1/pv/reports/{report_id}/exposure",
                          headers=_auth(token),
                          json={"context": "marketing", "measure": "vibes",
                                "value_text": "3"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "PV_BAD_EXPOSURE_MEASURE"


# ------------------------------------------------------------ the registers

def test_the_registers_feed_their_tables(app_client, store):
    token, product_id, report_id, _ids = store
    base = f"/api/v1/pv/products/{product_id}/registers"
    assert app_client.post(f"{base}/safety-concerns", headers=_auth(token), json={
        "concern_type": "important_identified_risk", "title": "Hepatotoxicity",
        "meddra_terms": ["Hepatic failure"]}).status_code == 201
    assert app_client.post(f"{base}/safety-actions", headers=_auth(token), json={
        "action_type": "label_change", "region": "EU", "action_date": "2026-02-10",
        "description": "Hepatic warning added"}).status_code == 201
    assert app_client.post(f"{base}/studies", headers=_auth(token), json={
        "study_id": "TAB-301", "phase": "III", "status": "ongoing",
        "start_date": "2025-01-01"}).status_code == 201
    assert app_client.post(f"{base}/literature", headers=_auth(token), json={
        "citation": "Smith J et al. 2026", "search_date": "2026-03-01"}).status_code == 201

    concerns = _render(product_id, report_id, "safety_concern_table")
    assert concerns.rows[0][1] == "Hepatotoxicity"
    actions = _render(product_id, report_id, "action_table")
    assert actions.rows[0][0] == "2026-02-10"
    studies = _render(product_id, report_id, "study_inventory")
    assert studies.rows[0][0] == "TAB-301"
    literature = _render(product_id, report_id, "literature_table")
    assert literature.rows[0][0] == "Smith J et al. 2026"


def test_an_action_outside_the_interval_is_not_in_the_table(app_client, store):
    token, product_id, report_id, _ids = store
    base = f"/api/v1/pv/products/{product_id}/registers"
    app_client.post(f"{base}/safety-actions", headers=_auth(token), json={
        "action_type": "dhpc", "action_date": "2025-11-01", "description": "old"})
    with pytest.raises(tab.TableUnavailable):
        _render(product_id, report_id, "action_table")


def test_a_register_refuses_a_value_outside_its_vocabulary(app_client, store):
    token, product_id, _report_id, _ids = store
    res = app_client.post(
        f"/api/v1/pv/products/{product_id}/registers/safety-concerns",
        headers=_auth(token), json={"concern_type": "vague_worry", "title": "x"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "PV_REGISTER_BAD_CHOICE"


def test_a_register_refuses_a_missing_required_field(app_client, store):
    token, product_id, _report_id, _ids = store
    res = app_client.post(f"/api/v1/pv/products/{product_id}/registers/studies",
                          headers=_auth(token), json={"phase": "II"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "PV_REGISTER_FIELD_REQUIRED"


def test_an_unknown_register_is_a_404(app_client, store):
    token, product_id, _report_id, _ids = store
    res = app_client.get(f"/api/v1/pv/products/{product_id}/registers/horoscopes",
                         headers=_auth(token))
    assert res.status_code == 404


def test_every_table_key_the_trees_name_has_a_builder():
    """A section declaring a table nothing builds would block its own export
    forever."""
    from app.safety import trees

    for doc_type, mapping in trees.TABLE_KEYS.items():
        for key in mapping.values():
            assert key in tab.BUILDERS, f"{doc_type} names {key!r}"
