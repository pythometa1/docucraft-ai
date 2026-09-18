"""Reading numbers out of the tables a CMC source actually contains.

Three real shapes: a certificate of analysis (one batch, a result column), a
specification (tests and limits, no results), and a stability table (one batch
per caption, a column per timepoint). What is asserted throughout is that the
value stored is the value printed in the source -- same digits, same operator,
same significant figures -- and that anything the parser could not place is
visible rather than guessed.
"""

import pytest

from app.cmc.extraction_structured import (
    batch_from_header, condition_from_text, timepoint_from_text,
)
from app.docgen.extraction import ExtractedTable, Extraction


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------- header reading

def test_batch_condition_and_timepoint_are_told_apart():
    """A stability column headed 25C/60RH is a condition. Read as a batch --
    which a bare token pattern does -- every timepoint would be filed under a
    batch nobody manufactured."""
    assert batch_from_header("Batch B-2026-001") == "B-2026-001"
    assert batch_from_header("Lot 12345") == "12345"
    assert batch_from_header("B-001") == "B-001"
    assert batch_from_header("25C/60RH") is None
    assert batch_from_header("6 months") is None

    assert condition_from_text("25C/60RH") == "25C/60RH"
    assert condition_from_text("40 °C / 75 % RH") == "40C/75RH"
    assert condition_from_text("25 °C") == "25C"
    assert condition_from_text("Long term") == "long_term"
    assert condition_from_text("Batch B-001") is None

    assert timepoint_from_text("6 months") == 6.0
    assert timepoint_from_text("T=3") == 3.0
    assert timepoint_from_text("3M") == 3.0
    assert timepoint_from_text("Initial") == 0.0
    assert timepoint_from_text("Assay") is None


# ------------------------------------------------------- the extractor

@pytest.fixture
def project(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Extraction dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Drug X Tablets"}).json()
    return token, cmc["id"]


def _run(db, document, tables):
    from app.cmc.extraction_structured import extract_structured

    return extract_structured(db, document, Extraction(pages=[], tables=tables, page_count=0))


def _document(db, cmc_project_id, org_id, user_id, doc_type):
    from app.models import CmcDocument

    document = CmcDocument(
        org_id=org_id, cmc_project_id=cmc_project_id, doc_type=doc_type,
        original_filename=f"{doc_type}.csv", storage_path="cmc/none.csv",
        uploaded_by=user_id)
    db.add(document)
    db.flush()
    return document


@pytest.fixture
def store(project):
    """A session, a project and a user id -- the extractor works below HTTP."""
    from app.db import SessionLocal
    from app.models import CmcProject, User

    token, cmc_project_id = project
    db = SessionLocal()
    try:
        cmc = db.get(CmcProject, cmc_project_id)
        user = db.query(User).filter(User.org_id == cmc.org_id).first()
        yield db, cmc, user
    finally:
        db.close()


def test_a_certificate_of_analysis_becomes_results(store):
    db, cmc, user = store
    document = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    table = ExtractedTable(
        table_id=None, title="Certificate of Analysis - Batch B-2026-001", page=1,
        rows=[["Test", "Acceptance Criteria", "Method", "Result"],
              ["Description", "White to off-white powder", "Visual", "White powder"],
              ["Assay", "98.0 - 102.0 %", "HPLC-001", "99.2 %"],
              ["Related substance A", "NMT 0.10 %", "HPLC-002", "0.050 %"],
              ["Water content", "NMT 0.5 %", "KF-001", "0.21 %"],
              ["Residual solvents", "NMT 500 ppm", "GC-001", "ND"]])
    stored = _run(db, document, [table])
    assert stored == 5

    from app.models import CmcBatch, CmcResult, CmcTest

    batches = db.query(CmcBatch).filter(CmcBatch.cmc_project_id == cmc.id).all()
    assert [b.batch_number for b in batches] == ["B-2026-001"]

    tests = {t.test_name: t for t in db.query(CmcTest).filter(
        CmcTest.cmc_project_id == cmc.id).all()}
    assert set(tests) == {"Description", "Assay", "Related substance A",
                          "Water content", "Residual solvents"}
    # The criterion is stored as written AND reduced to bounds where it can be.
    assert tests["Assay"].acceptance_criterion_text == "98.0 - 102.0 %"
    assert (tests["Assay"].limit_lower, tests["Assay"].limit_upper) == ("98.0", "102.0")
    assert tests["Assay"].limit_operator == "between"
    assert tests["Assay"].method_id == "HPLC-001"
    # An unreducible criterion keeps its text and invents no bound.
    assert tests["Description"].limit_lower is None
    assert tests["Description"].limit_upper is None

    results = {db.get(CmcTest, r.test_id).test_name: r
               for r in db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).all()}
    # The whole point: the source said 0.050 and the store says 0.050.
    assert results["Related substance A"].value_text == "0.050 %"
    assert str(results["Related substance A"].value_numeric) == "0.05000000"
    assert results["Assay"].value_text == "99.2 %"
    assert results["Residual solvents"].value_text == "ND"
    assert results["Residual solvents"].operator == "nd"
    assert results["Description"].value_text == "White powder"
    # Nothing is trusted yet.
    assert all(r.verified_by is None for r in results.values())
    assert all(r.extraction_confidence >= 0.9 for r in results.values())


def test_a_specification_defines_tests_without_inventing_results(store):
    db, cmc, user = store
    document = _document(db, cmc.id, cmc.org_id, user.id, "spec_dp")
    table = ExtractedTable(
        table_id="3.2.P.5.1", title="Drug Product Specification", page=1,
        rows=[["Test", "Acceptance Criteria", "Method"],
              ["Appearance", "White film-coated tablet", "Visual"],
              ["Assay", "95.0 - 105.0 % of label claim", "HPLC-010"],
              ["Dissolution", "NLT 80 % (Q) in 30 minutes", "USP <711>"],
              ["Uniformity of dosage units", "Complies", "USP <905>"]])
    stored = _run(db, document, [table])
    # A specification carries no measured values, so no results are written.
    assert stored == 0

    from app.models import CmcResult, CmcSpecification, CmcTest

    assert db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).count() == 0
    assert db.query(CmcSpecification).filter(
        CmcSpecification.cmc_project_id == cmc.id).count() == 1

    tests = {t.test_name: t for t in db.query(CmcTest).filter(
        CmcTest.cmc_project_id == cmc.id).all()}
    assert tests["Assay"].limit_lower == "95.0"
    assert tests["Dissolution"].limit_lower == "80"
    assert tests["Dissolution"].limit_operator == "nlt"
    # "USP <905>" is a monograph reference, never a limit of 905.
    assert tests["Uniformity of dosage units"].limit_lower is None
    assert tests["Uniformity of dosage units"].limit_upper is None
    assert tests["Uniformity of dosage units"].acceptance_criterion_text == "Complies"


def test_a_stability_table_files_every_value_under_its_condition_and_timepoint(store):
    db, cmc, user = store
    document = _document(db, cmc.id, cmc.org_id, user.id, "stability_data")
    table = ExtractedTable(
        table_id="14.1", title="Stability Data - Batch B-2026-001 - 25C/60RH", page=3,
        rows=[["Test", "Acceptance Criteria", "Initial", "3 months", "6 months"],
              ["Assay", "95.0 - 105.0 %", "99.8 %", "99.5 %", "99.1 %"],
              ["Total impurities", "NMT 1.0 %", "0.10 %", "0.15 %", "0.22 %"]])
    stored = _run(db, document, [table])
    assert stored == 6

    from app.models import CmcResult, CmcTest

    rows = db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).all()
    assay_id = db.query(CmcTest).filter(
        CmcTest.cmc_project_id == cmc.id, CmcTest.test_name == "Assay").one().id
    assay = {r.timepoint_months: r for r in rows if r.test_id == assay_id}
    assert sorted(assay) == [0.0, 3.0, 6.0]
    assert assay[0.0].value_text == "99.8 %"
    assert assay[6.0].value_text == "99.1 %"
    # The condition came from the caption and is canonical everywhere.
    assert all(r.storage_condition == "25C/60RH" for r in rows)


def test_two_sources_disagreeing_become_a_conflict_not_a_merge(store):
    db, cmc, user = store
    first = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    table = lambda value: ExtractedTable(
        table_id=None, title="CoA - Batch B-9001", page=1,
        rows=[["Test", "Acceptance Criteria", "Result"],
              ["Assay", "98.0 - 102.0 %", value]])
    assert _run(db, first, [table("99.2 %")]) == 1

    second = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    assert _run(db, second, [table("99.4 %")]) == 1

    from app.models import CmcResult

    rows = db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).all()
    assert len(rows) == 2
    assert {r.value_text for r in rows} == {"99.2 %", "99.4 %"}
    # Each points at the other: neither was silently kept.
    assert all(r.conflict_with_id for r in rows)

    # The same value from a second source is agreement, not a conflict.
    third = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    assert _run(db, third, [table("99.2 %")]) == 0


def test_an_unrecognised_table_yields_nothing_rather_than_guessing(store):
    db, cmc, user = store
    document = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    table = ExtractedTable(
        table_id=None, title="Shipping manifest", page=1,
        rows=[["Carrier", "Waybill", "Cartons"],
              ["FedEx", "7788990011", "12"]])
    assert _run(db, document, [table]) == 0

    from app.models import CmcResult

    assert db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).count() == 0


def test_a_release_result_never_acquires_a_storage_condition(store):
    """"...Batch B-2026-001 Certificate of Analysis" contains "01 C", and a
    loose temperature pattern read it as 34C/01C -- filing every release
    result under a storage condition that does not exist. The phantom column
    then broke conflict detection, because a second certificate's values
    landed in a different cell from the first's."""
    db, cmc, user = store
    document = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    table = ExtractedTable(
        table_id=None, title="Certificate of Analysis - Batch B-2026-001", page=1,
        rows=[["Certificate of Analysis - Batch B-2026-001", "", "", ""],
              ["Test", "Acceptance Criteria", "Method", "Result"],
              ["Assay", "95.0 - 105.0 %", "HPLC-010", "99.2 %"]])
    assert _run(db, document, [table]) == 1

    from app.models import CmcResult

    row = db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).one()
    assert row.storage_condition is None
    assert row.timepoint_months is None

    # And the second certificate lands in the SAME cell, so the disagreement
    # is seen rather than stored twice side by side.
    second = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    table.rows[2][3] = "99.4 %"
    assert _run(db, second, [table]) == 1
    rows = db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).all()
    assert len(rows) == 2
    assert all(r.conflict_with_id for r in rows)


def test_a_generated_storage_name_never_becomes_a_storage_condition():
    """Uploads land on disk under a generated id. One of them was
    "493b45e1-9fdc-43fe-82c4-5e8f71c77142", whose "-82c4-" read as a storage
    temperature of 82C -- so every stability value in that file was filed
    under a condition no chamber has ever held. Machine names must not reach a
    caption, and a temperature must not be found inside a hex string."""
    assert condition_from_text("493b45e1-9fdc-43fe-82c4-5e8f71c77142") is None
    assert condition_from_text("82c4") is None
    assert condition_from_text("a1b2c3d4") is None
    # The real conditions still read.
    assert condition_from_text("25C/60RH") == "25C/60RH"
    assert condition_from_text("Stability - 30 °C / 65 % RH") == "30C/65RH"
    assert condition_from_text(
        "493b45e1-9fdc-43fe-82c4-5e8f71c77142 Stability Data - 25C/60RH") == "25C/60RH"


def test_extraction_captions_use_the_uploaders_filename(tmp_path):
    """The caption feeds table identification, so it must be the name a person
    gave the file rather than the id storage gave it."""
    from app.docgen.extraction import extract

    stored = tmp_path / "493b45e1-9fdc-43fe-82c4-5e8f71c77142.csv"
    stored.write_text("Test,Result\nAssay,99.2 %\n")

    machine = extract(str(stored))
    assert machine.tables[0].title == "493b45e1-9fdc-43fe-82c4-5e8f71c77142"

    human = extract(str(stored), source_name="Stability Data - 25C-60RH")
    assert human.tables[0].title == "Stability Data - 25C-60RH"


# ------------------------------------------ the unit belongs to the result

def test_a_result_keeps_its_own_unit_not_the_specifications(store):
    """A certificate reporting ppm against a specification written in % must
    store ppm.

    The defect this was written against: `_record_result` parsed every cell
    with `unit_hint=test.unit`, so the row's own Unit column was read, handed
    to `_test_for` (which kept the specification's unit and dropped it), and
    never reached the value. The result was stored as "2500" stamped `%` -- a
    concentration ten thousand times its real one -- and because extraction
    had just made `result.unit` and `test.unit` identical by construction,
    `qc._unit_drift` could never fire. The only unit check in the module was
    blind to exactly the drift it exists for.
    """
    db, cmc, user = store
    # spec_dp, not spec_ds: a certificate of analysis is filed against the
    # drug product, and a specification for the substance governs a different
    # material entirely -- pairing those two would be comparing a result with
    # a limit nobody set for it.
    spec = _document(db, cmc.id, cmc.org_id, user.id, "spec_dp")
    _run(db, spec, [ExtractedTable(
        table_id=None, title="Specification", page=1,
        rows=[["Test", "Unit", "Acceptance Criteria"],
              ["Residual solvent - Methanol", "%", "NMT 0.3 %"]])])

    coa = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    _run(db, coa, [ExtractedTable(
        table_id=None, title="Certificate of Analysis - Batch B-2026-009", page=1,
        rows=[["Test", "Unit", "Result"],
              ["Residual solvent - Methanol", "ppm", "2500"]])])

    from app.models import CmcResult, CmcTest

    test = db.query(CmcTest).filter(
        CmcTest.cmc_project_id == cmc.id,
        CmcTest.test_name == "Residual solvent - Methanol").one()
    result = db.query(CmcResult).filter(
        CmcResult.cmc_project_id == cmc.id, CmcResult.test_id == test.id).one()

    # The specification still owns the test's unit...
    assert test.unit == "%"
    # ...and the certificate owns its own result's.
    assert result.unit == "ppm"
    assert result.value_text == "2500"

    # And with both units present the comparison is now the real one:
    # 2500 ppm is 0.25 %, inside a limit of NMT 0.3 %.
    from app.cmc.limits import PASS, evaluate_row

    verdict = evaluate_row(value_text=result.value_text,
                           acceptance_criterion_text=test.acceptance_criterion_text,
                           unit=result.unit)
    assert verdict.outcome == PASS


def test_a_cell_that_carries_its_own_unit_outranks_the_column(store):
    """Most specific wins: a unit written in the cell beats the Unit column,
    which beats the test's."""
    db, cmc, user = store
    coa = _document(db, cmc.id, cmc.org_id, user.id, "coa")
    _run(db, coa, [ExtractedTable(
        table_id=None, title="Certificate of Analysis - Batch B-2026-010", page=1,
        rows=[["Test", "Unit", "Acceptance Criteria", "Result"],
              ["Related substance B", "%", "NMT 0.5 %", "300 ppm"]])])

    from app.models import CmcResult

    result = db.query(CmcResult).filter(CmcResult.cmc_project_id == cmc.id).one()
    assert result.value_text == "300 ppm"
    assert result.unit == "ppm"
