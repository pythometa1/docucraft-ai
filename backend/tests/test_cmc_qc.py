"""What has to be true before a dossier can be exported.

The guarantees under test are the ones a quality reviewer would ask for out
loud: a failing result is named down to the batch, test, condition and
timepoint; a comparison the system could not make reads as neither a pass nor
a failure; a specification stated two ways is refused; a batch formula that
does not add up is refused; and nothing unsigned reaches a table that prints
it.

The dossier is built row by row rather than over HTTP. QC reads the structured
store, so the store is what the tests set up -- and a check that only worked
when an endpoint had populated the rows would be a check the export gate could
not run.
"""

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from app.cmc.qc import (
    ABBREVIATIONS, BATCH_FORMULA_ARITHMETIC, BLOCKER, COMPENDIAL_CLAIM,
    CONFORMANCE_UNCHECKED, DATA_NEEDED, INFO, METHOD_UNTRACEABLE,
    NAME_INCONSISTENT, OUT_OF_SPECIFICATION, OUT_OF_TREND, SPEC_INCONSISTENT,
    STABILITY_INCOMPLETE, TABLE_UNRESOLVED, UNIT_DRIFT, UNVERIFIED_DATA,
    WARNING, run_qc,
)


class _Dossier:
    """One CMC project's rows, in its own organisation.

    A fresh org id per dossier is what makes the tenancy assertion meaningful
    and keeps two dossiers in the same session from seeing each other.
    """

    def __init__(self, db):
        from app.models import CmcDeliverable, CmcMaterial, CmcProject

        self.db = db
        self.org_id = f"org-{uuid4()}"
        self.cmc = self._add(CmcProject(
            org_id=self.org_id, project_id=f"portal-{uuid4()}",
            product_name="Drug X Tablets", created_by="tester"))
        self.deliverable = self._add(CmcDeliverable(
            org_id=self.org_id, cmc_project_id=self.cmc.id, doc_type_key="ctd_32p"))
        self.material = self._add(CmcMaterial(
            org_id=self.org_id, cmc_project_id=self.cmc.id, kind="drug_product",
            name="Drug X Tablets 10 mg"))

    def _add(self, row):
        self.db.add(row)
        # The session does not autoflush, and `run_qc` reads through SQL.
        self.db.flush()
        return row

    # -- structure ----------------------------------------------------------

    def section(self, section_code, *, title="A section", table_key=None,
                enabled=True, applicability="applicable"):
        from app.models import CmcSection

        return self._add(CmcSection(
            org_id=self.org_id, cmc_deliverable_id=self.deliverable.id,
            section_code=section_code, title=title, table_key=table_key,
            enabled=enabled, applicability=applicability))

    def draft(self, section, content, *, version=1):
        from app.models import CmcSectionDraft

        return self._add(CmcSectionDraft(
            org_id=self.org_id, cmc_section_id=section.id, version=version,
            content=content, created_by="ai"))

    # -- the structured store ----------------------------------------------

    def spec_test(self, test_name, *, criterion=None, unit=None, method_id=None,
                 method_type=None, limit_lower=None, limit_upper=None,
                 limit_operator=None, material=None):
        from app.models import CmcTest

        return self._add(CmcTest(
            org_id=self.org_id, cmc_project_id=self.cmc.id,
            material_id=(material or self.material).id, test_name=test_name,
            acceptance_criterion_text=criterion, unit=unit, method_id=method_id,
            method_type=method_type, limit_lower=limit_lower,
            limit_upper=limit_upper, limit_operator=limit_operator))

    def batch(self, batch_number, *, site=None):
        from app.models import CmcBatch

        return self._add(CmcBatch(
            org_id=self.org_id, cmc_project_id=self.cmc.id,
            material_id=self.material.id, batch_number=batch_number,
            site_id=site.id if site else None))

    def result(self, batch, test, value_text, *, unit=None, condition=None,
               timepoint=None, verified="reviewer", value_numeric="from_text"):
        from app.cmc.values import parse_value
        from app.models import CmcResult

        parsed = parse_value(value_text)
        return self._add(CmcResult(
            org_id=self.org_id, cmc_project_id=self.cmc.id, batch_id=batch.id,
            test_id=test.id, value_text=value_text,
            value_numeric=(parsed.number if value_numeric == "from_text" else value_numeric),
            operator=parsed.operator, unit=unit, storage_condition=condition,
            timepoint_months=timepoint, verified_by=verified,
            extraction_confidence=1.0 if verified else 0.95))

    def formula(self, component_name, *, percent_ww=None, quantity_per_unit=None,
                quantity_per_batch=None, unit=None, sort_order=0, verified="reviewer"):
        from app.models import CmcBatchFormula

        return self._add(CmcBatchFormula(
            org_id=self.org_id, cmc_project_id=self.cmc.id,
            cmc_deliverable_id=self.deliverable.id, component_name=component_name,
            percent_ww=percent_ww, quantity_per_unit=quantity_per_unit,
            quantity_per_batch=quantity_per_batch, unit=unit,
            sort_order=sort_order, verified_by=verified))

    def site(self, name):
        from app.models import CmcSite

        return self._add(CmcSite(org_id=self.org_id, cmc_project_id=self.cmc.id, name=name))

    def method_document(self, content, *, doc_type="method_sop"):
        from app.models import CmcChunk, CmcDocument

        document = self._add(CmcDocument(
            org_id=self.org_id, cmc_project_id=self.cmc.id, doc_type=doc_type,
            original_filename=f"{doc_type}.pdf", storage_path=f"cmc/{uuid4()}.pdf",
            uploaded_by="tester"))
        return self._add(CmcChunk(
            org_id=self.org_id, cmc_project_id=self.cmc.id, document_id=document.id,
            doc_type=doc_type, content=content))

    # -- the pass -----------------------------------------------------------

    def findings(self, **kwargs):
        return run_qc(self.db, cmc_project_id=self.cmc.id, org_id=self.org_id, **kwargs)


@pytest.fixture
def dossier(app_client):
    """A factory, because two of the checks are only meaningful as a pair."""
    from app.db import SessionLocal

    db = SessionLocal()
    made = []
    try:
        def _make():
            made.append(_Dossier(db))
            return made[-1]

        yield _make
    finally:
        db.rollback()
        db.close()


def _codes(findings):
    return [finding.code for finding in findings]


def _only(findings, code):
    matched = [finding for finding in findings if finding.code == code]
    assert len(matched) == 1, f"expected one {code}, got {_codes(findings)}"
    return matched[0]


def _blockers(findings):
    return [finding for finding in findings if finding.severity == BLOCKER]


# --------------------------------------------------------------- conformance


def test_an_out_of_specification_result_names_batch_test_condition_and_timepoint(dossier):
    """A reviewer must be able to open the offending cell from the message
    alone. A finding that says only "a result is out of specification" sends
    somebody hunting through a grid of thousands."""
    d = dossier()
    assay = d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%",
                       limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    batch = d.batch("B-2026-002")
    d.result(batch, assay, "94.1 %", unit="%", condition="40C/75RH", timepoint=6.0)

    finding = _only(d.findings(), OUT_OF_SPECIFICATION)
    assert finding.severity == BLOCKER
    for fact in ("B-2026-002", "Assay", "40C/75RH", "6"):
        assert fact in finding.message
    assert finding.detail["batch_number"] == "B-2026-002"
    assert finding.detail["test_name"] == "Assay"
    assert finding.detail["storage_condition"] == "40C/75RH"
    assert finding.detail["timepoint_months"] == 6.0
    # The reported string, not a re-rendering of the parsed number.
    assert finding.detail["value_text"] == "94.1 %"


def test_a_criterion_that_cannot_be_reduced_to_bounds_is_unchecked_not_a_failure(dossier):
    """The one output a quality reviewer must never be handed is a conformance
    claim the system could not actually make. "Report result" is not a limit,
    so the answer is that nothing was checked -- not a pass, and not an OOS
    that would have a person investigating a batch that never failed."""
    d = dossier()
    reported = d.spec_test("Related substance B", criterion="Report result", unit="%")
    batch = d.batch("B-2026-001")
    d.result(batch, reported, "0.12 %", unit="%")

    findings = d.findings()
    unchecked = _only(findings, CONFORMANCE_UNCHECKED)
    assert unchecked.severity == WARNING
    assert OUT_OF_SPECIFICATION not in _codes(findings)
    assert _blockers(findings) == []
    assert unchecked.detail["value_text"] == "0.12 %"
    assert unchecked.detail["acceptance_criterion_text"] == "Report result"


def test_conformance_is_judged_on_the_reported_string_not_the_numeric_column(dossier):
    """`value_numeric` exists to compare, but `value_text` is the value. If the
    two ever disagree the string is what the source reported, and a verdict
    taken from the other column is a verdict about a number nobody measured."""
    d = dossier()
    impurity = d.spec_test("Related substance A", criterion="NMT 0.10 %", unit="%",
                          limit_upper="0.10", limit_operator="nmt")
    batch = d.batch("B-2026-001")
    # The numeric column is deliberately wrong in both directions.
    d.result(batch, impurity, "0.050 %", unit="%", value_numeric=Decimal("9.9"))
    passing = d.findings()
    assert OUT_OF_SPECIFICATION not in _codes(passing)

    other = d.batch("B-2026-002")
    d.result(other, impurity, "0.15 %", unit="%", value_numeric=Decimal("0.01"))
    finding = _only(d.findings(), OUT_OF_SPECIFICATION)
    assert finding.detail["value_text"] == "0.15 %"


# ------------------------------------------------------------ specifications


def test_one_test_specified_two_ways_for_a_material_blocks_the_export(dossier):
    """Prose and table pull limits from the same store, so two rows that
    disagree put two different limits into one dossier depending on which
    section is being rendered."""
    d = dossier()
    d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%", method_id="HPLC-010",
               limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    d.spec_test("Assay", criterion="98.0 - 102.0 %", unit="% of label claim",
               method_id="HPLC-010", limit_lower="98.0", limit_upper="102.0",
               limit_operator="between")

    finding = _only(d.findings(), SPEC_INCONSISTENT)
    assert finding.severity == BLOCKER
    assert "95.0 to 105.0" in finding.message and "98.0 to 102.0" in finding.message
    assert set(finding.detail["differences"]) == {"limits", "unit"}
    assert len(finding.detail["test_ids"]) == 2


def test_a_test_repeated_with_identical_limits_is_not_an_inconsistency(dossier):
    """Otherwise the check reports every material that carries a release and a
    shelf-life row for the same test, which is most of them."""
    d = dossier()
    for _ in range(2):
        d.spec_test("Water content", criterion="NMT 3.0 %", unit="%", method_id="KF-001",
                   limit_upper="3.0", limit_operator="nmt")

    assert SPEC_INCONSISTENT not in _codes(d.findings())


def test_a_method_no_procedure_or_validation_report_mentions_blocks_the_export(dossier):
    """A dossier that names a method it cannot show anybody how to perform is
    one a reviewer will ask about. A compendial method is exempt: its procedure
    is the monograph, which this system does not hold -- COMPENDIAL_CLAIM
    covers that one instead."""
    d = dossier()
    d.method_document("Analytical procedure HPLC-010: assay by gradient HPLC.")
    d.spec_test("Assay", criterion="95.0 - 105.0 %", method_id="HPLC-010")
    d.spec_test("Related substance A", criterion="NMT 0.20 %", method_id="HPLC-011")
    d.spec_test("Dissolution", criterion="Q = 80 % at 30 min", method_id="USP <711>",
               method_type="compendial")

    finding = _only(d.findings(), METHOD_UNTRACEABLE)
    assert finding.severity == BLOCKER
    assert "HPLC-011" in finding.message
    assert finding.detail["method_id"] == "HPLC-011"
    assert finding.detail["test_names"] == ["Related substance A"]


def test_a_method_id_is_not_traced_by_a_longer_one_that_contains_it(dossier):
    """A substring search finds GC-1 inside GC-10 and reports a method as
    traceable to a procedure describing a different one."""
    d = dossier()
    d.method_document("Residual solvents are determined by GC-10.")
    d.spec_test("Residual solvents", criterion="NMT 500 ppm", method_id="GC-1")

    assert _only(d.findings(), METHOD_UNTRACEABLE).detail["method_id"] == "GC-1"


def test_a_compendial_criterion_is_flagged_for_a_person_to_verify(dossier):
    """The system holds no pharmacopoeia text. It has not opened USP <711> and
    must never let a dossier imply that it did."""
    d = dossier()
    d.spec_test("Dissolution", criterion="Complies with USP <711>", method_id="DISS-001")
    d.method_document("Dissolution is performed per DISS-001.")

    finding = _only(d.findings(), COMPENDIAL_CLAIM)
    assert finding.severity == WARNING
    assert "USP <711>" in finding.message
    assert "person must verify" in finding.message
    assert finding.detail["claims"][0]["field"] == "acceptance_criterion_text"


def test_an_in_house_method_id_is_not_read_as_a_pharmacopoeia_claim(dossier):
    """"EP-001" names a European Pharmacopoeia nothing. A warnings list padded
    with those is one a reviewer stops reading, which loses the real claims."""
    d = dossier()
    d.method_document("Identity is confirmed by EP-001 and IR-002.")
    d.spec_test("Identity", criterion="Conforms to reference", method_id="EP-001")

    assert COMPENDIAL_CLAIM not in _codes(d.findings())


# --------------------------------------------------------------- verification


def test_unverified_values_feeding_a_data_bearing_section_block_the_export(dossier):
    """Nothing unsigned reaches an export. The finding names how many values
    and which tests, because "some data is unverified" is not something a
    reviewer can act on."""
    d = dossier()
    section = d.section("P.5.4", title="Batch Analyses", table_key="batch_analyses")
    assay = d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%",
                       limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    water = d.spec_test("Water content", criterion="NMT 3.0 %", unit="%",
                       limit_upper="3.0", limit_operator="nmt")
    batch = d.batch("B-2026-001")
    d.result(batch, assay, "99.2 %", unit="%")
    unsigned = d.result(batch, water, "2.1 %", unit="%", verified=None)

    finding = _only(d.findings(), UNVERIFIED_DATA)
    assert finding.severity == BLOCKER
    assert finding.section_code == "P.5.4"
    assert finding.detail["count"] == 1
    assert finding.detail["names"] == ["Water content"]
    assert finding.detail["result_ids"] == [unsigned.id]

    unsigned.verified_by = "reviewer"
    d.db.flush()
    assert UNVERIFIED_DATA not in _codes(d.findings())


def test_unverified_values_with_nothing_printing_them_block_nothing(dossier):
    """A project whose data-bearing sections are all disabled or not applicable
    exports no table, and gating it on a value no document contains is a gate
    on a document that does not exist."""
    d = dossier()
    d.section("P.5.4", title="Batch Analyses", table_key="batch_analyses",
              applicability="not_applicable")
    assay = d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%",
                       limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    d.result(d.batch("B-2026-001"), assay, "99.2 %", unit="%", verified=None)

    assert UNVERIFIED_DATA not in _codes(d.findings())


# -------------------------------------------------------------- batch formula


def test_a_batch_formula_totalling_ninety_nine_percent_blocks_the_export(dossier):
    d = dossier()
    d.formula("Drug X", percent_ww="10.0", sort_order=0)
    d.formula("Lactose monohydrate", percent_ww="45.0", sort_order=1)
    d.formula("Microcrystalline cellulose", percent_ww="44.0", sort_order=2)

    finding = _only(d.findings(), BATCH_FORMULA_ARITHMETIC)
    assert finding.severity == BLOCKER
    assert "99.0" in finding.message and "100" in finding.message
    assert finding.detail["total"] == "99.0"
    assert len(finding.detail["components"]) == 3


def test_a_batch_formula_totalling_one_hundred_percent_blocks_nothing(dossier):
    d = dossier()
    d.formula("Drug X", percent_ww="10.0", sort_order=0)
    d.formula("Lactose monohydrate", percent_ww="45.0", sort_order=1)
    d.formula("Microcrystalline cellulose", percent_ww="45.0", sort_order=2)

    assert d.findings() == []


def test_a_batch_formula_within_the_rounding_tolerance_blocks_nothing(dossier):
    """Each line of a real formula is rounded, so the column does not land on
    exactly 100. Half a percent absorbs that; a percent and a half is a
    component somebody left out."""
    d = dossier()
    d.formula("Drug X", percent_ww="10.2", sort_order=0)
    d.formula("Lactose monohydrate", percent_ww="45.1", sort_order=1)
    d.formula("Microcrystalline cellulose", percent_ww="44.4", sort_order=2)

    assert d.findings() == []


def test_an_unreadable_quantity_is_reported_rather_than_dropped_from_the_total(dossier):
    """Skipping it would leave a total that looks arithmetically sound while
    missing a component -- the check passing because it stopped checking."""
    d = dossier()
    d.formula("Drug X", percent_ww="10.0", sort_order=0)
    d.formula("Lactose monohydrate", percent_ww="45.0", sort_order=1)
    d.formula("Purified water", percent_ww="q.s. to 100", sort_order=2)

    unreadable = [finding for finding in d.findings()
                  if finding.detail.get("reason") == "unparseable"]
    assert len(unreadable) == 1
    assert unreadable[0].code == BATCH_FORMULA_ARITHMETIC
    assert unreadable[0].severity == BLOCKER
    assert "q.s. to 100" in unreadable[0].message
    assert unreadable[0].detail["component_name"] == "Purified water"


def test_per_unit_and_per_batch_quantities_must_agree_on_one_batch_size(dossier):
    """The factor between the two columns IS the number of units in the batch.
    A formula stating two of them describes two different batches."""
    d = dossier()
    d.formula("Drug X", quantity_per_unit="10", quantity_per_batch="10000",
              unit="mg", sort_order=0)
    d.formula("Lactose monohydrate", quantity_per_unit="45", quantity_per_batch="45000",
              unit="mg", sort_order=1)
    d.formula("Magnesium stearate", quantity_per_unit="2", quantity_per_batch="1000",
              unit="mg", sort_order=2)

    finding = _only(d.findings(), BATCH_FORMULA_ARITHMETIC)
    assert finding.severity == BLOCKER
    assert finding.detail["units_per_batch"] == "1000"
    assert [outlier["component_name"] for outlier in finding.detail["outliers"]] == [
        "Magnesium stearate"]


# --------------------------------------------------------------- the drafts


def test_a_gap_a_draft_declares_blocks_the_export(dossier):
    """The generation engine writes [DATA NEEDED: ...] when the sources did not
    contain something. A document that exports with one prints the bracket to a
    regulator."""
    d = dossier()
    section = d.section("P.3.3", title="Description of Manufacturing Process")
    d.draft(section, "The process is described [S1, p.4]. "
                     "[DATA NEEDED: the coating pan load range]", version=1)

    finding = _only(d.findings(), DATA_NEEDED)
    assert finding.severity == BLOCKER
    assert finding.section_code == "P.3.3"
    assert finding.detail["payload"] == "the coating pan load range"


def test_only_the_latest_draft_of_a_section_is_checked(dossier):
    """A gap a writer has since filled is not a gap. Checking every version
    would block an export on text nobody will ever read again."""
    d = dossier()
    section = d.section("P.3.3", title="Description of Manufacturing Process")
    d.draft(section, "[DATA NEEDED: the coating pan load range]", version=1)
    d.draft(section, "The coating pan is loaded to 12 kg [S1, p.4].", version=2)

    assert DATA_NEEDED not in _codes(d.findings())


def test_a_table_marker_naming_no_builder_blocks_the_export(dossier):
    """An unresolvable marker reaches the document as literal text, which is
    the one place a table's absence is invisible until a regulator reads it."""
    d = dossier()
    section = d.section("P.5.1", title="Specification", table_key="spec_table")
    d.draft(section, "The specification is given below.\n\n[TABLE: not_a_real_table]\n")

    finding = _only(d.findings(), TABLE_UNRESOLVED)
    assert finding.severity == BLOCKER
    assert finding.section_code == "P.5.1"
    assert finding.detail["table_key"] == "not_a_real_table"


def test_a_table_whose_builder_has_no_data_to_render_blocks_the_export(dossier):
    """A registered key is not a rendered table. A specification table built
    from a project that records no tests would state acceptance criteria nobody
    set, so the builder refuses and the export is blocked rather than shipping
    an empty grid."""
    d = dossier()
    section = d.section("P.5.1", title="Specification", table_key="spec_table")
    d.draft(section, "The specification is given below.\n\n[TABLE: spec_table]\n")

    finding = _only(d.findings(), TABLE_UNRESOLVED)
    assert finding.severity == BLOCKER
    assert finding.detail["table_key"] == "spec_table"
    assert finding.detail["reason"].strip()


def test_a_marker_sharing_its_line_with_prose_blocks_the_export(dossier):
    """The export resolves only a marker alone on its line. One buried in a
    sentence is never replaced by a table: it survives into the document as the
    literal characters "[TABLE: spec_table]", under a heading a regulator
    reads. The key naming a real builder does not save it."""
    d = dossier()
    d.method_document("Assay is performed per HPLC-010.")
    d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%", method_id="HPLC-010",
               limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    section = d.section("P.5.1", title="Specification", table_key="spec_table")
    d.draft(section, "The specification [TABLE: spec_table] is given above.")

    finding = _only(d.findings(), TABLE_UNRESOLVED)
    assert finding.severity == BLOCKER
    assert finding.detail["table_key"] == "spec_table"
    assert "line" in finding.detail["reason"]


def test_a_builder_that_trips_over_the_store_is_reported_not_raised(dossier, monkeypatch):
    """One table that cannot be built must not take the other thirteen checks
    down with it. The export gate reports the same trip as an unresolved table,
    and a QC pass that raised instead would hand a reviewer an error page where
    the list of what to fix belongs."""
    from app.cmc import tables

    d = dossier()
    section = d.section("P.5.1", title="Specification", table_key="spec_table")
    d.draft(section, "The specification is given below.\n\n[TABLE: spec_table]\n")

    def _trips(scope):
        raise KeyError("material_id")

    monkeypatch.setitem(tables.BUILDERS, "spec_table", _trips)

    finding = _only(d.findings(), TABLE_UNRESOLVED)
    assert finding.severity == BLOCKER
    # A KeyError's text is a key out of our store, not a sentence for the
    # reviewer: the finding says what is wrong and the log has the key.
    assert finding.detail["reason"] == "a value this table needs is missing from the store"
    assert "material_id" not in finding.detail["reason"]


# ------------------------------------------------------------------ stability


def test_a_hole_in_the_stability_matrix_names_what_is_missing(dossier):
    """Measured against the study itself -- what the other batches at the same
    condition have -- because this module holds no protocol and must not invent
    a timepoint nobody scheduled."""
    d = dossier()
    assay = d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%",
                       limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    water = d.spec_test("Water content", criterion="NMT 3.0 %", unit="%",
                       limit_upper="3.0", limit_operator="nmt")
    complete, partial = d.batch("B-2026-001"), d.batch("B-2026-002")
    for months in (0.0, 3.0, 6.0):
        d.result(complete, assay, "99.5 %", unit="%", condition="25C/60RH", timepoint=months)
    d.result(complete, water, "2.1 %", unit="%", condition="25C/60RH", timepoint=0.0)
    for months in (0.0, 3.0):
        d.result(partial, assay, "99.4 %", unit="%", condition="25C/60RH", timepoint=months)

    finding = _only(d.findings(), STABILITY_INCOMPLETE)
    assert finding.severity == WARNING
    assert "B-2026-002" in finding.message
    assert "6 months" in finding.message
    assert "Water content" in finding.message
    assert finding.detail["missing_timepoints"] == [6.0]
    assert finding.detail["missing_tests"] == ["Water content"]


def test_a_series_heading_for_its_limit_is_flagged_for_a_statistician(dossier):
    """Flag only. A shelf-life conclusion drawn from three points by a QC pass
    is exactly the statistical claim this module is forbidden to make, so the
    message has to hand the decision to a person who can make it."""
    d = dossier()
    impurity = d.spec_test("Related substance A", criterion="NMT 0.20 %", unit="%",
                          limit_upper="0.20", limit_operator="nmt")
    batch = d.batch("B-2026-001")
    for months, value in ((0.0, "0.05 %"), (3.0, "0.09 %"), (6.0, "0.13 %")):
        d.result(batch, impurity, value, unit="%", condition="40C/75RH", timepoint=months)

    finding = _only(d.findings(), OUT_OF_TREND)
    assert finding.severity == WARNING
    assert "statistician" in finding.message
    assert OUT_OF_SPECIFICATION not in _codes(d.findings())
    assert finding.detail["direction"] == "rising"
    assert finding.detail["limit"] == "0.20"
    assert finding.detail["projected_crossing_months"] == "11.25"
    assert finding.detail["method"] == "straight line through the first and last timepoint"


def test_a_flat_stability_series_is_not_a_trend(dossier):
    d = dossier()
    impurity = d.spec_test("Related substance A", criterion="NMT 0.20 %", unit="%",
                          limit_upper="0.20", limit_operator="nmt")
    batch = d.batch("B-2026-001")
    for months in (0.0, 3.0, 6.0):
        d.result(batch, impurity, "0.05 %", unit="%", condition="40C/75RH", timepoint=months)

    assert OUT_OF_TREND not in _codes(d.findings())


# ------------------------------------------------------------- consistency


def test_a_result_reported_in_another_unit_than_its_specification_is_flagged(dossier):
    d = dossier()
    impurity = d.spec_test("Residual solvents", criterion="NMT 500 ppm", unit="ppm",
                          limit_upper="500", limit_operator="nmt")
    d.result(d.batch("B-2026-001"), impurity, "0.03 %", unit="%")

    finding = _only(d.findings(), UNIT_DRIFT)
    assert finding.severity == WARNING
    assert finding.detail["result_unit"] == "%"
    assert finding.detail["test_unit"] == "ppm"


def test_the_same_unit_typed_two_ways_is_not_drift(dossier):
    """Reporting "mg / mL" against "mg/mL" would teach reviewers to ignore the
    check that also catches mg against g."""
    d = dossier()
    assay = d.spec_test("Assay", criterion="9.0 - 11.0 mg/mL", unit="mg/mL",
                       limit_lower="9.0", limit_upper="11.0", limit_operator="between")
    d.result(d.batch("B-2026-001"), assay, "10.1 mg/mL", unit="mg / mL")

    assert UNIT_DRIFT not in _codes(d.findings())


def test_batch_numbers_differing_only_in_punctuation_are_flagged(dossier):
    d = dossier()
    d.batch("B-2026-001")
    d.batch("b 2026 001")
    d.batch("B-2026-002")

    finding = _only(d.findings(), NAME_INCONSISTENT)
    assert finding.severity == WARNING
    assert finding.detail["spellings"] == ["B-2026-001", "b 2026 001"]


def test_site_names_differing_only_in_spacing_are_flagged(dossier):
    d = dossier()
    d.site("Acme Pharma, Cork")
    d.site("Acme Pharma Cork")

    assert _only(d.findings(), NAME_INCONSISTENT).detail["kind"] == "name"


def test_gaps_in_the_store_produce_findings_rather_than_an_exception(dossier):
    """The export gate runs against whatever the extractor left behind. A blank
    result, a test with no acceptance criterion, an empty formula line and a
    gap marker with no payload are all real states of a half-reviewed project,
    and a pass that raised on one would take the gate down instead of
    reporting the gap."""
    d = dossier()
    appearance = d.spec_test("Appearance")
    d.result(d.batch("B-2026-001"), appearance, "", verified=None)
    d.formula("Purified water")
    section = d.section("P.5.4", title="Batch Analyses", table_key="batch_analyses")
    d.draft(section, "[DATA NEEDED: ]")

    findings = d.findings()
    assert CONFORMANCE_UNCHECKED in _codes(findings)
    assert UNVERIFIED_DATA in _codes(findings)
    # An unstated gap is still a gap, and still blocks.
    assert _only(findings, DATA_NEEDED).detail["payload"] == "unspecified"


# ------------------------------------------------------------ the whole pass


def test_a_clean_project_has_no_blockers(dossier):
    """The dossier this module exists to let through: verified results inside
    their limits, methods traceable to a procedure, a complete section with no
    declared gaps and no unresolved tables."""
    d = dossier()
    d.method_document("Assay is performed per HPLC-010. Water content per KF-001.")
    section = d.section("P.5.4", title="Batch Analyses", table_key="batch_analyses")
    d.draft(section, "Batch analysis results for three registration batches are "
                     "presented [S1, p.2]. The results meet the specification.")
    assay = d.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%", method_id="HPLC-010",
                       limit_lower="95.0", limit_upper="105.0", limit_operator="between")
    water = d.spec_test("Water content", criterion="NMT 3.0 %", unit="%", method_id="KF-001",
                       limit_upper="3.0", limit_operator="nmt")
    for number, assay_value in (("B-2026-001", "99.2 %"), ("B-2026-002", "100.4 %")):
        batch = d.batch(number)
        d.result(batch, assay, assay_value, unit="%")
        d.result(batch, water, "2.1 %", unit="%")
    d.formula("Drug X", percent_ww="10.0", quantity_per_unit="10",
              quantity_per_batch="10000", unit="mg", sort_order=0)
    d.formula("Lactose monohydrate", percent_ww="90.0", quantity_per_unit="90",
              quantity_per_batch="90000", unit="mg", sort_order=1)

    findings = d.findings()
    assert _blockers(findings) == [], [finding.message for finding in findings]


def test_findings_are_json_safe_and_ordered_blockers_first(dossier):
    """`as_dict` crosses the wire to the Issues panel and the export gate, and
    the order it arrives in is the order a reviewer works. A Decimal left in a
    detail is a 500 rather than a finding."""
    d = dossier()
    section = d.section("P.5.4", title="Batch Analyses", table_key="batch_analyses")
    d.draft(section, "The AE rate and the CQA list were reviewed. "
                     "[DATA NEEDED: the release date of batch B-2026-003]")
    impurity = d.spec_test("Related substance A", criterion="NMT 0.10 %", unit="ppm",
                          limit_upper="0.10", limit_operator="nmt")
    d.result(d.batch("B-2026-001"), impurity, "0.15 %", unit="%", verified=None)

    findings = d.findings()
    severities = [finding.severity for finding in findings]
    assert severities == sorted(severities, key=[BLOCKER, WARNING, INFO].index)
    assert {UNVERIFIED_DATA, OUT_OF_SPECIFICATION, DATA_NEEDED} <= set(_codes(findings))
    assert UNIT_DRIFT in _codes(findings)
    assert ABBREVIATIONS in _codes(findings)
    # Serialises as it stands, with no encoder of its own.
    json.dumps([finding.as_dict() for finding in findings])


def test_another_organisation_and_another_project_stay_out_of_the_findings(dossier):
    """Every check scopes on both. A QC pass that read one row from a
    neighbouring tenant would put another sponsor's batch number in this
    sponsor's findings list."""
    theirs = dossier()
    assay = theirs.spec_test("Assay", criterion="95.0 - 105.0 %", unit="%",
                            limit_lower="95.0", limit_upper="105.0",
                            limit_operator="between")
    theirs.result(theirs.batch("THEIRS-001"), assay, "94.1 %", unit="%")
    assert _only(theirs.findings(), OUT_OF_SPECIFICATION)

    ours = dossier()
    assert ours.findings() == []
    # Their project id with our org id, and ours with theirs: neither reads.
    assert run_qc(ours.db, cmc_project_id=theirs.cmc.id, org_id=ours.org_id) == []
    assert run_qc(ours.db, cmc_project_id=ours.cmc.id, org_id=theirs.org_id) == []
