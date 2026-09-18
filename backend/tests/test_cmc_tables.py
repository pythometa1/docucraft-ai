"""What a rendered CMC table is allowed to say.

One question runs through the whole file: does the string in the cell match the
string in the store, character for character? A certificate of analysis that
reported 0.050 % must produce a dossier that prints 0.050 %, through every
builder, through the flattened rows, through the blueprint blocks and out the
other side of a real `.docx`. The second question is the one a reviewer asks:
where the data is not there, does the table say so out loud, or does it close
the gap up and read as complete?
"""

from decimal import Decimal

import pytest

from app.cmc import tables


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cmc_project(client, token: str, name: str) -> str:
    portal = client.post("/api/v1/projects", headers=_auth(token), json={
        "name": name, "function": "Quality-CMC", "document_type": "CMC Section",
        "region": "Global", "language": "English"}).json()
    return client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Tabletron 10 mg"}).json()["id"]


class _Data:
    """The project the whole file renders, and the ids a test needs to name."""


@pytest.fixture(scope="module")
def data(app_client, two_orgs):
    """One dossier's worth of structured data, written straight into the store.

    Rows are inserted deliberately out of order -- batch 002 before 001, the
    12-month stability column before the 0-month one -- because a renderer that
    happens to agree with insertion order proves nothing about its ordering.
    """
    from app.db import SessionLocal
    from app.models import (
        CmcBatch, CmcBatchFormula, CmcMaterial, CmcProject, CmcResult, CmcSite,
        CmcTest, User,
    )

    token_a, _pa, token_b, _pb = two_orgs
    project_id = _cmc_project(app_client, token_a, "Tables dossier")
    empty_id = _cmc_project(app_client, token_a, "Tables dossier with no data")

    db = SessionLocal()
    d = _Data()
    d.db = db
    d.token = token_a
    d.cmc_project_id = project_id
    d.empty_project_id = empty_id

    cmc = db.get(CmcProject, project_id)
    d.org_id = cmc.org_id
    user = db.query(User).filter(User.org_id == cmc.org_id).first()
    d.other_org_id = db.query(User).filter(User.org_id != cmc.org_id).first().org_id
    verifier = user.id

    def add(row):
        db.add(row)
        db.flush()
        return row

    scope = {"org_id": cmc.org_id, "cmc_project_id": project_id}

    substance = add(CmcMaterial(kind="drug_substance", name="Substanzol", **scope))
    excipient = add(CmcMaterial(kind="excipient", name="Lactose monohydrate", **scope))
    d.substance_id = substance.id
    d.excipient_id = excipient.id

    def add_test(name, method, criterion, order, material=None):
        return add(CmcTest(material_id=(material or substance).id, test_name=name,
                           method_id=method, acceptance_criterion_text=criterion,
                           stage="release", sort_order=order, **scope))

    d.description = add_test("Description", "VIS-001", "White to off-white powder", 0)
    d.assay = add_test("Assay", "HPLC-001", "98.0 - 102.0 %", 1)
    d.related = add_test("Related substance A", "HPLC-002", "NMT 0.10 %", 2)
    d.water = add_test("Water content", "KF-001", "NMT 0.5 %", 3)
    d.total = add_test("Total impurities", "HPLC-002", "NMT 0.5 %", 4)
    d.identification = add_test("Identification", "IR-001", "Complies", 0, excipient)

    second = add(CmcBatch(material_id=substance.id, batch_number="B-2026-002",
                          purpose="registration", **scope))
    first = add(CmcBatch(material_id=substance.id, batch_number="B-2026-001",
                         purpose="registration", **scope))
    d.first_batch, d.second_batch = first, second

    def result(batch, test, text, number, *, verified=True, condition=None, timepoint=None):
        return add(CmcResult(
            batch_id=batch.id, test_id=test.id, value_text=text,
            value_numeric=(Decimal(number) if number is not None else None),
            storage_condition=condition, timepoint_months=timepoint,
            extraction_confidence=0.9, verified_by=(verifier if verified else None),
            **scope))

    # -- release results. Water content on batch 002 is the deliberate hole,
    #    and the assay on batch 002 is the one nobody has verified.
    result(first, d.description, "White powder", None)
    result(first, d.assay, "99.2 %", "99.2")
    result(first, d.related, "0.050 %", "0.050")
    result(first, d.water, "0.21 %", "0.21")
    result(first, d.total, "0.15 %", "0.15")
    result(second, d.description, "White powder", None)
    result(second, d.assay, "101.0 %", "101.0", verified=False)
    result(second, d.related, "0.12 %", "0.12")
    result(second, d.total, "0.31 %", "0.31", verified=False)
    # Two results for one cell: a conflict somebody has to settle, and the
    # renderer must not settle it quietly.
    result(first, d.description, "Off-white powder", None, verified=False)

    # -- stability, written 12 months first so a lexical sort is visible.
    long_term = {0: ("99.2 %", "0.050 %"), 3: ("99.0 %", "0.060 %"),
                 6: ("98.7 %", "0.080 %"), 12: ("98.1 %", "0.11 %")}
    for month in (12, 3, 0, 6):
        assay_text, related_text = long_term[month]
        result(first, d.assay, assay_text, assay_text.split()[0],
               condition="25C/60RH", timepoint=float(month))
        result(first, d.related, related_text, related_text.split()[0],
               condition="25C/60RH", timepoint=float(month))
    accelerated = {0: ("99.2 %", "0.050 %"), 3: ("98.5 %", "0.090 %"),
                   6: ("97.9 %", "0.14 %")}
    for month in (6, 0, 3):
        assay_text, related_text = accelerated[month]
        result(first, d.assay, assay_text, assay_text.split()[0],
               condition="40C/75RH", timepoint=float(month))
        result(first, d.related, related_text, related_text.split()[0],
               condition="40C/75RH", timepoint=float(month))

    def component(name, function, per_unit, percent, per_batch, reference, order,
                  *, verified=True):
        return add(CmcBatchFormula(
            component_name=name, function=function, quantity_per_unit=per_unit, unit="mg",
            percent_ww=percent, quantity_per_batch=per_batch, reference_to_standard=reference,
            sort_order=order, verified_by=(verifier if verified else None), **scope))

    component("Substanzol", "Active ingredient", "10.0", "8.0", "1.00",
              "In-house specification", 0)
    component("Lactose monohydrate", "Diluent", "100.0", "80.0", "10.00", "Ph.Eur.", 1)
    component("Magnesium stearate", "Lubricant", "1.5", "1.2", "0.15", "Ph.Eur.", 2,
              verified=False)

    add(CmcSite(name="Alpha Pharma Ltd", address="1 Industrial Road, Springfield",
                identifier="FEI 3001234567",
                activities=["DS manufacture", "testing"], **scope))
    add(CmcSite(name="Beta Analytical", address=None, identifier="DUNS 987654321",
                activities=["testing"], **scope))

    db.commit()
    try:
        yield d
    finally:
        db.close()


@pytest.fixture(scope="module")
def unfiled(app_client, data):
    """A second project, holding stability results nobody filed under a storage
    condition -- which is what a certificate transcribed without its caption
    leaves behind."""
    from app.models import CmcBatch, CmcMaterial, CmcResult, CmcTest

    project_id = _cmc_project(app_client, data.token, "Tables dossier with a loose result")
    db = data.db
    scope = {"org_id": data.org_id, "cmc_project_id": project_id}

    material = CmcMaterial(kind="drug_substance", name="Substanzol", **scope)
    db.add(material)
    db.flush()
    test = CmcTest(material_id=material.id, test_name="Assay", method_id="HPLC-001",
                   acceptance_criterion_text="98.0 - 102.0 %", sort_order=0, **scope)
    batch = CmcBatch(material_id=material.id, batch_number="B-2026-009", **scope)
    db.add_all([test, batch])
    db.flush()
    for month, text in ((0, "99.4 %"), (3, "99.1 %")):
        db.add(CmcResult(batch_id=batch.id, test_id=test.id, value_text=text,
                         value_numeric=Decimal(text.split()[0]), storage_condition=None,
                         timepoint_months=float(month), **scope))
    db.commit()
    return project_id


@pytest.fixture(scope="module")
def timeless(app_client, data):
    """Two projects holding results that name a storage condition and record no
    timepoint -- what a stability table transcribed without its timepoint header
    leaves behind. One project also holds a proper study, so its table renders;
    the other holds nothing else, so it cannot.

    Both projects are created over HTTP before anything is written, because a
    write left open on this session locks the SQLite file the request handler
    would have to write through.
    """
    from app.models import CmcBatch, CmcMaterial, CmcResult, CmcTest

    ids = [_cmc_project(app_client, data.token, "Tables dossier with a loose condition"),
           _cmc_project(app_client, data.token, "Tables dossier of loose conditions only")]
    db = data.db
    for project_id, with_study in zip(ids, (True, False)):
        scope = {"org_id": data.org_id, "cmc_project_id": project_id}
        material = CmcMaterial(kind="drug_substance", name="Substanzol", **scope)
        db.add(material)
        db.flush()
        test = CmcTest(material_id=material.id, test_name="Assay", method_id="HPLC-001",
                       acceptance_criterion_text="98.0 - 102.0 %", sort_order=0, **scope)
        batch = CmcBatch(material_id=material.id, batch_number="B-2026-011", **scope)
        db.add_all([test, batch])
        db.flush()
        if with_study:
            for month, text in ((0, "99.4 %"), (3, "99.1 %")):
                db.add(CmcResult(batch_id=batch.id, test_id=test.id, value_text=text,
                                 value_numeric=Decimal(text.split()[0]),
                                 storage_condition="25C/60RH",
                                 timepoint_months=float(month), **scope))
        db.add(CmcResult(batch_id=batch.id, test_id=test.id, value_text="97.7 %",
                         value_numeric=Decimal("97.7"), storage_condition="40C/75RH",
                         timepoint_months=None, **scope))
    db.commit()
    return tuple(ids)


def _render(data, key, **kwargs):
    return tables.render_table(
        data.db, cmc_project_id=data.cmc_project_id, org_id=data.org_id,
        table_key=key, **kwargs)


def _cell_at(rendered, row_label: str, column: str) -> str:
    row = next(r for r in rendered.rows if r[0] == row_label)
    return row[rendered.columns.index(column)]


# ------------------------------------------------------------------ spec_table

def test_a_specification_prints_the_criterion_it_was_given(data):
    """Byte for byte. A criterion that came back "98.0-102.0%" would be a limit
    nobody set, and it is the kind of difference nobody reads closely enough to
    catch in a 300-page dossier."""
    rendered = _render(data, "spec_table", material_id=data.substance_id)

    assert rendered.columns == ["Test", "Method", "Acceptance criterion"]
    assert [row[0] for row in rendered.rows] == [
        "Description", "Assay", "Related substance A", "Water content", "Total impurities"]
    for test in (data.description, data.assay, data.related, data.water, data.total):
        assert _cell_at(rendered, test.test_name, "Acceptance criterion") == (
            test.acceptance_criterion_text)
        assert _cell_at(rendered, test.test_name, "Method") == test.method_id


def test_a_material_scope_keeps_another_materials_tests_out(data):
    """The excipient's identification test belongs in the excipient's
    specification. Rendered into the substance's, it is a test the substance is
    not controlled by."""
    substance = _render(data, "spec_table", material_id=data.substance_id)
    excipient = _render(data, "spec_table", material_id=data.excipient_id)

    assert "Identification" not in [row[0] for row in substance.rows]
    assert [row[0] for row in excipient.rows] == ["Identification"]
    assert excipient.title.endswith("Lactose monohydrate")


# --------------------------------------------------------------- batch_analyses

def test_a_batch_analysis_shows_the_cell_nobody_recorded(data):
    """The hole is the point. Water content was never run on batch 002, so the
    cell is a dash and `.missing` names it -- a table that quietly dropped the
    row would read as a complete analysis of both batches."""
    rendered = _render(data, "batch_analyses", material_id=data.substance_id)

    assert rendered.columns == ["Test", "Acceptance criterion", "B-2026-001", "B-2026-002"]
    assert _cell_at(rendered, "Water content", "B-2026-002") == tables.HOLE
    assert _cell_at(rendered, "Water content", "B-2026-001") == "0.21 %"
    assert any("Water content" in entry and "B-2026-002" in entry
               for entry in rendered.missing), rendered.missing


def test_batches_are_columns_and_the_criterion_comes_first(data):
    rendered = _render(data, "batch_analyses", material_id=data.substance_id)

    assert rendered.columns[1] == "Acceptance criterion"
    assert _cell_at(rendered, "Assay", "Acceptance criterion") == "98.0 - 102.0 %"
    assert _cell_at(rendered, "Assay", "B-2026-001") == "99.2 %"


def test_an_unverified_value_is_shown_counted_and_named(data):
    """Included, because whether unverified data may be exported is the export
    gate's decision and not the renderer's -- and counted, so the gate can make
    it."""
    rendered = _render(data, "batch_analyses", material_id=data.substance_id)

    assert _cell_at(rendered, "Assay", "B-2026-002") == "101.0 %"
    assert _cell_at(rendered, "Total impurities", "B-2026-002") == "0.31 %"
    assert rendered.unverified == 2
    assert any("101.0 %" in note for note in rendered.notes), rendered.notes
    assert any("0.31 %" in note for note in rendered.notes), rendered.notes


def test_two_results_for_one_cell_are_not_silently_resolved(data):
    """Something has to be printed, so the verified row is; what must not
    happen is a table that resolves a conflict on a reviewer's behalf and says
    nothing about it."""
    rendered = _render(data, "batch_analyses", material_id=data.substance_id)

    assert _cell_at(rendered, "Description", "B-2026-001") == "White powder"
    assert any("Description" in note and "2 results" in note
               for note in rendered.notes), rendered.notes


def test_the_strict_rendering_turns_an_unverified_value_into_a_hole(data):
    rendered = _render(data, "batch_analyses", material_id=data.substance_id,
                       include_unverified=False)

    assert _cell_at(rendered, "Assay", "B-2026-002") == tables.HOLE
    assert rendered.unverified == 0
    assert any("B-2026-002" in entry and "verified" in entry
               for entry in rendered.missing), rendered.missing
    # The verified values around it are untouched.
    assert _cell_at(rendered, "Assay", "B-2026-001") == "99.2 %"


# ------------------------------------------------------------ stability tables

def test_stability_timepoints_are_ordered_by_number_not_by_spelling(data):
    """Sorted as strings, 12 lands between 0 and 3 and the study reads as though
    it ran backwards."""
    rendered = _render(data, "stability_summary", material_id=data.substance_id)

    assert rendered.columns == ["Batch", "Storage condition", "Test",
                                "0 months", "3 months", "6 months", "12 months"]
    assert rendered.columns[3:] != sorted(rendered.columns[3:])


def test_a_stability_cell_is_the_string_the_study_reported(data):
    rendered = _render(data, "stability_summary", material_id=data.substance_id)

    long_term = [r for r in rendered.rows
                 if r[1] == "25C/60RH" and r[2] == "Related substance A"][0]
    assert long_term[3:] == ["0.050 %", "0.060 %", "0.080 %", "0.11 %"]
    assert [r[1] for r in rendered.rows][:1] == ["25C/60RH"]


def test_a_condition_pulled_no_further_shows_a_hole_at_the_last_timepoint(data):
    """Every block carries the same columns, so the accelerated arm that stops
    at 6 months shows the 12-month gap instead of a short row nobody notices."""
    rendered = _render(data, "stability_summary", material_id=data.substance_id)

    accelerated = [r for r in rendered.rows if r[1] == "40C/75RH" and r[2] == "Assay"][0]
    assert accelerated[-1] == tables.HOLE
    assert any("40C/75RH" in entry and "12 months" in entry
               for entry in rendered.missing), rendered.missing


def test_a_stability_result_with_no_condition_is_kept_and_reported(data, unfiled):
    """Dropping it would shrink a completeness view silently, which is the one
    thing a completeness view must never do."""
    rendered = tables.render_table(data.db, cmc_project_id=unfiled, org_id=data.org_id,
                                   table_key="stability_summary")

    assert rendered.columns == ["Batch", "Storage condition", "Test",
                                "0 months", "3 months"]
    assert rendered.rows == [["B-2026-009", tables.HOLE, "Assay", "99.4 %", "99.1 %"]]
    # One entry per timepoint, not one per result: a study with twelve tests
    # must not bury the rest of `.missing`.
    assert [entry for entry in rendered.missing if "no storage condition" in entry] == [
        "batch B-2026-009: results at 0 months record no storage condition",
        "batch B-2026-009: results at 3 months record no storage condition"]


def test_a_stability_result_with_no_timepoint_is_named_rather_than_dropped(data, timeless):
    """It belongs in no column -- a result with no timepoint cannot be placed on
    a schedule -- and a value the store holds, QC raises and no table mentions
    is a silent drop."""
    mixed, _only = timeless
    rendered = tables.render_table(data.db, cmc_project_id=mixed, org_id=data.org_id,
                                   table_key="stability_summary")

    assert rendered.columns == ["Batch", "Storage condition", "Test",
                                "0 months", "3 months"]
    assert "97.7 %" not in [cell for row in rendered.rows for cell in row]
    assert ("batch B-2026-011 / 40C/75RH: 1 result(s) record no timepoint, so no column "
            "can hold them") in rendered.missing


def test_a_study_that_is_only_loose_conditions_is_refused_by_what_is_wrong(data, timeless):
    """"No stability results" sends a reader to re-extract a study that is
    already in the store; what the store is missing is its timepoint column."""
    _mixed, only = timeless
    with pytest.raises(tables.TableUnavailable) as raised:
        tables.render_table(data.db, cmc_project_id=only, org_id=data.org_id,
                            table_key="stability_matrix")

    assert "record no timepoint" in str(raised.value)


def test_the_completeness_matrix_counts_what_is_there(data):
    """A count, not a value -- which is why it says so in `.notes` and why a
    count of nothing is a hole rather than a confident zero."""
    rendered = _render(data, "stability_matrix", material_id=data.substance_id)

    assert rendered.columns == ["Batch", "Storage condition",
                                "0 months", "3 months", "6 months", "12 months"]
    long_term = next(r for r in rendered.rows if r[1] == "25C/60RH")
    accelerated = next(r for r in rendered.rows if r[1] == "40C/75RH")
    assert long_term[2:] == ["2", "2", "2", "2"]
    assert accelerated[2:] == ["2", "2", "2", tables.HOLE]
    assert any("counts" in note for note in rendered.notes), rendered.notes


# ------------------------------------------------------- formula, sites, impurities

def test_a_batch_formula_keeps_its_quantities_and_its_units(data):
    rendered = _render(data, "batch_formula")

    assert rendered.columns == ["Component", "Function", "Quantity per unit", "% w/w",
                                "Quantity per batch", "Reference to standard"]
    assert [row[0] for row in rendered.rows] == [
        "Substanzol", "Lactose monohydrate", "Magnesium stearate"]
    assert _cell_at(rendered, "Substanzol", "Quantity per unit") == "10.0 mg"
    assert _cell_at(rendered, "Lactose monohydrate", "% w/w") == "80.0"
    assert rendered.unverified == 1


def test_the_composition_is_the_formula_without_the_batch_columns(data):
    """Both tables come from the same rows on purpose: a composition that
    disagreed with the batch formula in the same dossier is the defect a
    deterministic renderer exists to make impossible."""
    formula = _render(data, "batch_formula")
    composition = _render(data, "composition_table")

    assert composition.columns == ["Component", "Function", "Quantity per unit", "% w/w"]
    assert [row[:4] for row in formula.rows] == composition.rows


def test_an_unverified_component_is_withheld_by_name(data):
    """The component and its function stay so a reader can see which row is
    being held back; the quantities -- the data -- are the part that goes."""
    rendered = _render(data, "batch_formula", include_unverified=False)

    row = next(r for r in rendered.rows if r[0] == "Magnesium stearate")
    assert row[:2] == ["Magnesium stearate", "Lubricant"]
    assert row[2:] == [tables.HOLE, tables.HOLE, tables.HOLE, "Ph.Eur."]
    assert any("Magnesium stearate" in entry for entry in rendered.missing)


def test_a_site_without_an_address_is_named_as_a_gap(data):
    rendered = _render(data, "site_list")

    assert rendered.columns == ["Site", "Address", "Identifier", "Activities"]
    assert _cell_at(rendered, "Alpha Pharma Ltd", "Activities") == "DS manufacture, testing"
    assert _cell_at(rendered, "Beta Analytical", "Address") == tables.HOLE
    assert "Beta Analytical: no address" in rendered.missing


def test_an_observed_range_is_two_reported_strings(data):
    """0.050 and 0.12 are the batches' own strings. A range recomputed from
    `value_numeric` would print 0.05 and drop a significant figure the method
    claimed."""
    rendered = _render(data, "impurity_table", material_id=data.substance_id)

    assert [row[0] for row in rendered.rows] == ["Related substance A", "Total impurities"]
    observed = _cell_at(rendered, "Related substance A", "Observed range across batches")
    assert observed == "0.050 % - 0.12 %"
    assert _cell_at(rendered, "Total impurities", "Observed range across batches") == (
        "0.15 % - 0.31 %")
    assert "Assay" not in [row[0] for row in rendered.rows]
    assert any("test name" in note for note in rendered.notes), rendered.notes


def test_the_strict_rendering_leaves_an_unverified_result_out_of_the_range(data):
    """Total impurities was 0.31 % on a batch nobody has verified. Held back,
    the range is what the one verified batch reported -- one string, not a
    range invented to look like one."""
    rendered = _render(data, "impurity_table", material_id=data.substance_id,
                       include_unverified=False)

    assert _cell_at(rendered, "Total impurities", "Observed range across batches") == "0.15 %"
    assert any("Total impurities" in entry and "not verified" in entry
               for entry in rendered.missing), rendered.missing
    # The verified impurity beside it is untouched.
    assert _cell_at(rendered, "Related substance A",
                    "Observed range across batches") == "0.050 % - 0.12 %"
    assert rendered.unverified == 0


# ------------------------------------------------------- the guarantee itself

def _stored_strings(data) -> set:
    """Every string any builder is permitted to print.

    The derived forms are enumerated here rather than waved through, so this
    set is also the list of cells that are not a stored string verbatim: a
    timepoint header, a count in the completeness matrix, an observed range
    (two stored strings), a quantity joined to its unit, and a site's
    activities joined with commas. Anything else in a cell came from nowhere.
    """
    from app.models import CmcBatch, CmcBatchFormula, CmcResult, CmcSite, CmcTest

    def rows(model):
        return data.db.query(model).filter(
            model.cmc_project_id == data.cmc_project_id).all()

    values = [r.value_text for r in rows(CmcResult)]
    allowed = {tables.HOLE}
    allowed.update(values)
    for test in rows(CmcTest):
        allowed.update({test.test_name, test.method_id, test.acceptance_criterion_text})
    for batch in rows(CmcBatch):
        allowed.add(batch.batch_number)
    for result in rows(CmcResult):
        allowed.add(result.storage_condition)
        allowed.add(f"{tables._timepoint_label(Decimal(str(result.timepoint_months)))}"
                    if result.timepoint_months is not None else None)
    for component in rows(CmcBatchFormula):
        allowed.update({component.component_name, component.function,
                        component.percent_ww, component.quantity_per_batch,
                        component.reference_to_standard,
                        f"{component.quantity_per_unit} {component.unit}"})
    for site in rows(CmcSite):
        allowed.update({site.name, site.address, site.identifier,
                        ", ".join(site.activities or ())})
    allowed.update(str(n) for n in range(1, 20))          # completeness counts
    allowed.update(f"{low} - {high}" for low in values for high in values)
    return {value for value in allowed if value}


def test_no_cell_says_anything_the_store_does_not(data):
    """The whole module in one assertion, over every builder it has.

    A cell holding "0.05 %" where the store holds "0.050 %" fails here, and so
    does a cell holding a number no source reported.
    """
    allowed = _stored_strings(data)

    for key in tables.BUILDERS:
        rendered = _render(data, key, material_id=(
            data.substance_id if key not in ("batch_formula", "composition_table", "site_list")
            else None))
        for row in rendered.rows:
            for cell in row:
                assert cell in allowed, f"{key}: {cell!r} is in no row of the store"
        assert rendered.columns[0]


def test_three_significant_figures_survive_every_builder(data):
    """`value_format.format_value` would render 0.050 as 0.05, through a float,
    rounded at three decimal places. Nothing here may reach it."""
    for key in ("batch_analyses", "stability_summary", "impurity_table"):
        rendered = _render(data, key, material_id=data.substance_id)
        printed = {cell for row in rendered.rows for cell in row}
        assert any("0.050 %" in cell for cell in printed), f"{key}: {sorted(printed)}"
        assert "0.05 %" not in printed, key


def test_rendering_twice_gives_the_same_table(data):
    """A diff between two exports has to mean the data changed."""
    for key in tables.BUILDERS:
        first = _render(data, key, material_id=(
            data.substance_id if key not in ("batch_formula", "composition_table", "site_list")
            else None))
        second = _render(data, key, material_id=(
            data.substance_id if key not in ("batch_formula", "composition_table", "site_list")
            else None))
        assert first.columns == second.columns, key
        assert first.rows == second.rows, key
        assert first.missing == second.missing, key
        assert first.notes == second.notes, key
        assert first.blocks == second.blocks, key


# ------------------------------------------------------------------- emission

def test_the_written_document_says_what_the_store_says(data, tmp_path):
    """Read back out of a real `.docx`, with python-docx, off the disk."""
    import docx

    rendered = _render(data, "batch_analyses", material_id=data.substance_id)
    path = tables.render_to_docx(rendered, str(tmp_path / "batch-analyses.docx"))

    document = docx.Document(path)
    assert len(document.tables) == 1
    table = document.tables[0]
    assert table.style.name == tables.TABLE_STYLE

    read_back = [[cell.text for cell in row.cells] for row in table.rows]
    assert read_back[0] == rendered.columns
    assert read_back[1:] == rendered.rows
    assert "0.050 %" in read_back[3]
    assert tables.HOLE in read_back[4]


def test_every_builder_survives_the_emitter(data, tmp_path):
    """`emit` verifies its own output by pre-scanning it, so a builder that
    produced a ragged grid or an empty cell fails here rather than in an
    export."""
    import docx

    for key in tables.BUILDERS:
        rendered = _render(data, key, material_id=(
            data.substance_id if key not in ("batch_formula", "composition_table", "site_list")
            else None))
        path = tables.render_to_docx(rendered, str(tmp_path / f"{key}.docx"))
        document = docx.Document(path)
        assert document.tables, key
        assert all(t.style.name == tables.TABLE_STYLE for t in document.tables), key


def test_the_same_table_written_twice_is_the_same_file(data, tmp_path):
    rendered = _render(data, "spec_table", material_id=data.substance_id)
    one = tmp_path / "one.docx"
    two = tmp_path / "two.docx"
    tables.render_to_docx(rendered, str(one))
    tables.render_to_docx(rendered, str(two))

    assert one.read_bytes() == two.read_bytes()


# --------------------------------------------------------------- refusals

def test_a_project_with_no_data_is_refused_rather_than_rendered_empty(data):
    """A grid of dashes under a heading reads as a finding. An absence of data
    is the caller's to report."""
    for key in tables.BUILDERS:
        with pytest.raises(tables.TableUnavailable):
            tables.render_table(data.db, cmc_project_id=data.empty_project_id,
                                org_id=data.org_id, table_key=key)


def test_another_org_cannot_render_this_projects_data(data):
    """The scope is org AND project, in SQL, before a row is seen."""
    for key in tables.BUILDERS:
        with pytest.raises(tables.TableUnavailable):
            tables.render_table(data.db, cmc_project_id=data.cmc_project_id,
                                org_id=data.other_org_id, table_key=key)


def test_a_material_that_is_not_in_this_project_is_refused_by_name(data):
    """Reported as the wrong material rather than as "this project records no
    tests", which sends somebody to look at data that is sitting right there."""
    with pytest.raises(tables.TableUnavailable) as raised:
        _render(data, "spec_table", material_id="no-such-material")

    assert "no-such-material" in str(raised.value)


def test_a_formula_row_with_no_deliverable_still_belongs_to_the_project(data):
    """Rows extracted before any deliverable existed carry no deliverable id.
    Excluding them empties the batch formula of every project whose data was
    loaded first and structured second."""
    rendered = tables.render_table(
        data.db, cmc_project_id=data.cmc_project_id, org_id=data.org_id,
        table_key="batch_formula", deliverable_id="a-deliverable-nothing-points-at")

    assert [row[0] for row in rendered.rows] == [
        "Substanzol", "Lactose monohydrate", "Magnesium stearate"]


def test_an_unknown_key_is_not_reported_as_missing_data(data):
    """`[TABLE: spec_tabel]` is a typo. Reporting it as "no data for this
    project" sends somebody to the review grid for a row that was never the
    problem."""
    with pytest.raises(tables.UnknownTable):
        _render(data, "spec_tabel")


def test_a_ragged_table_is_refused_before_the_emitter_sees_it(data):
    """`emit` refuses a ragged grid only once a whole document is assembled,
    and by then its message names neither the table nor the row."""
    tally = tables._Tally(include_unverified=True)
    with pytest.raises(tables.RaggedTable):
        tables._rendered("spec_table", "Specification", ["Test", "Method"],
                         [["Assay", "HPLC-001"], ["Water content"]], tally)


def test_the_preview_shape_is_the_documents_shape(data):
    """`rows` is a flattened union for QC; `groups` is what the export writes.

    A screen drawn from `rows` would show a stability summary as one wide grid
    where the document holds one per batch and condition -- a preview of a
    document nobody produced. `groups` closes that by carrying the arrangement
    the blocks use.
    """
    for key in ("spec_table", "batch_analyses"):
        rendered = _render(data, key)
        # Every table but the stability summary is one grid, and the group is
        # that grid exactly.
        assert len(rendered.groups) == 1
        assert rendered.groups[0]["columns"] == rendered.columns
        assert rendered.groups[0]["rows"] == rendered.rows

    stability = _render(data, "stability_summary")
    # One group per batch and condition, each narrower than the flat union.
    assert len(stability.groups) >= 1
    for group in stability.groups:
        assert group["columns"][0] == "Test"
        assert len(group["columns"]) < len(stability.columns)
        assert all(len(row) == len(group["columns"]) for row in group["rows"])
    # And every value in the groups is a value in the union: the two views
    # disagree about shape and never about content.
    grouped_values = {cell for g in stability.groups for row in g["rows"] for cell in row}
    flat_values = {cell for row in stability.rows for cell in row}
    assert grouped_values <= flat_values


def test_a_removed_material_stops_being_rendered(data):
    """A material is soft-deleted and its tests and batches are not -- they
    carry no deleted_at of their own. Without a filter a specification table
    keeps printing limits for a substance the submission no longer claims."""
    from app.cmc.tables import TableUnavailable
    from app.models import CmcMaterial, now as model_now

    before = _render(data, "spec_table")
    assert before.rows

    materials = data.db.query(CmcMaterial).filter(
        CmcMaterial.cmc_project_id == data.cmc_project_id).all()
    for material in materials:
        material.deleted_at = model_now()
    data.db.flush()
    try:
        try:
            after = _render(data, "spec_table")
            assert after.rows == []
        except TableUnavailable:
            pass  # nothing left to render is the same answer, said louder
    finally:
        for material in materials:
            material.deleted_at = None
        data.db.flush()
