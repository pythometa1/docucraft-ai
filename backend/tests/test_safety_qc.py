"""§11's checks, one at a time, and the two properties of the engine.

The engine properties first, because they are what the CMC module got wrong:

* `run_qc` is the only gate -- approval and sign-off are findings in it, so the
  dashboard and the export ask one question;
* a check that raises becomes a blocker naming itself, and the other checks
  still run.

Then each blocker is made to fire on the smallest report that should trip it,
and the clean report is shown to trip none of them.
"""

from datetime import date

import pytest

from app.safety import qc


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def report(app_client, two_orgs):
    """A DSUR with one confirmed, serious, related, unlisted interval case."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "QC safety", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Qcazine",
        "ibd": "2020-01-01", "dibd": "2016-01-01"}).json()
    rsi = app_client.post(f"/api/v1/pv/products/{product['id']}/rsi-versions",
                          headers=_auth(token),
                          json={"rsi_type": "ib", "version_label": "7"}).json()
    made = app_client.post(
        f"/api/v1/pv/products/{product['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "signal_eval", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15",
              "rsi_version_id": rsi["id"], "meddra_version": "27.0"}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    case = PvCase(org_id=org_id, pv_product_id=product["id"], worldwide_case_id="Q-1",
                  initial_receipt_date=date(2026, 3, 4),
                  latest_receipt_date=date(2026, 3, 4), is_serious=True,
                  deidentification_status="clear")
    db.add(case)
    db.flush()
    event = PvCaseEvent(org_id=org_id, pv_product_id=product["id"], case_id=case.id,
                        meddra_pt="Headache", meddra_soc="Nervous system disorders",
                        meddra_version="27.0", is_serious=True, expectedness="unlisted",
                        expectedness_rsi_version_id=rsi["id"],
                        causality_company="related", confirmed_by="qp")
    db.add(event)
    db.commit()
    ids = {"case": case.id, "event": event.id, "org": org_id}
    db.close()
    sections = app_client.get(f"/api/v1/pv/reports/{made['id']}/sections",
                              headers=_auth(token)).json()["items"]
    return {"token": token, "product_id": product["id"], "report_id": made["id"],
            "rsi_id": rsi["id"], "sections": sections, "ids": ids}


def _run(r):
    from app.db import SessionLocal
    from app.models import PvProduct, PvReportInstance

    db = SessionLocal()
    try:
        return qc.run_qc(db, report=db.get(PvReportInstance, r["report_id"]),
                         product=db.get(PvProduct, r["product_id"]))
    finally:
        db.close()


def _codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


def _write(r, code, content, *, origin="edited", status=None):
    """A section's text, written straight into the store."""
    from app.db import SessionLocal
    from app.models import PvSection, PvSectionDraft

    db = SessionLocal()
    try:
        section = db.query(PvSection).filter(
            PvSection.report_instance_id == r["report_id"],
            PvSection.section_code == code).one()
        latest = db.query(PvSectionDraft).filter(
            PvSectionDraft.pv_section_id == section.id).count()
        db.add(PvSectionDraft(org_id=section.org_id, pv_section_id=section.id,
                              version=latest + 1, content=content, origin=origin,
                              created_by="t"))
        if status:
            section.status = status
        db.commit()
        return section.id
    finally:
        db.close()


# ----------------------------------------------------- the engine properties

def test_approval_and_sign_off_are_findings_so_there_is_one_gate(report):
    """CMC had a QC dashboard and a separate export gate, and the export never
    called QC. Here approval and sign-off are in the list the export reads."""
    codes = _codes(_run(report), qc.BLOCKER)
    assert "SECTION_EMPTY" in codes
    assert "NOT_SIGNED_OFF" in codes


def test_a_check_that_crashes_fails_closed_and_the_others_still_run(
        report, monkeypatch):
    def broken(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(qc, "BLOCKER_CHECKS", (broken,) + qc.BLOCKER_CHECKS[1:])
    findings = _run(report)
    failed = [f for f in findings if f.code == "QC_CHECK_FAILED"]
    assert failed and failed[0].severity == qc.BLOCKER
    # Names the check, never the exception: the message is exported.
    assert failed[0].message.startswith("The broken check could not run.")
    assert failed[0].detail == {"check": "broken"}
    assert "boom" not in failed[0].message
    # The rest ran.
    assert "NOT_SIGNED_OFF" in _codes(findings)


def test_blockers_come_first():
    findings = [qc.PvFinding("B", qc.INFO, "i"), qc.PvFinding("A", qc.BLOCKER, "b"),
                qc.PvFinding("C", qc.WARNING, "w")]
    assert not qc.exportable(findings)
    assert qc.exportable([f for f in findings if f.severity != qc.BLOCKER])


# ------------------------------------------------------------ §11.1 identifiers

def test_an_open_deid_queue_blocks(report):
    from app.db import SessionLocal
    from app.models import PvDeidItem

    db = SessionLocal()
    db.add(PvDeidItem(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                      identifier_type="patient_name", detected_text="Jane Smith"))
    db.commit()
    db.close()
    assert "DEID_QUEUE_OPEN" in _codes(_run(report), qc.BLOCKER)


def test_an_identifier_in_a_draft_blocks(report):
    _write(report, "1", "1 Signal Identification\n\nReported by Dr Alan Reed.")
    found = [f for f in _run(report) if f.code == "PII_IN_DRAFT"]
    assert found and found[0].section_code == "1"


def test_an_identifier_in_the_index_blocks(report):
    from app.db import SessionLocal
    from app.models import PvChunk, PvDocument

    db = SessionLocal()
    document = PvDocument(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                          doc_type="other", original_filename="x.txt",
                          blob_path="pv/x", uploaded_by="u")
    db.add(document)
    db.flush()
    db.add(PvChunk(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                   document_id=document.id, doc_type="other",
                   content="Contact alan.reed@example.com"))
    db.commit()
    db.close()
    assert "PII_IN_INDEX" in _codes(_run(report), qc.BLOCKER)


# ------------------------------------------------------- §11.2 unconfirmed

def test_an_unconfirmed_event_behind_a_data_section_blocks(report):
    from app.db import SessionLocal
    from app.models import PvCaseEvent

    db = SessionLocal()
    db.add(PvCaseEvent(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                       case_id=report["ids"]["case"], meddra_pt="Nausea"))
    db.commit()
    db.close()
    found = [f for f in _run(report) if f.code == "UNCONFIRMED_DATA"]
    assert found and found[0].detail["unconfirmed_events"] == 1


def test_unconfirmed_exposure_blocks_when_a_section_prints_it(report):
    app_client_free = report  # signal_eval 10 carries exposure_table
    from app.db import SessionLocal
    from app.models import PvExposure

    db = SessionLocal()
    db.add(PvExposure(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                      report_instance_id=report["report_id"], context="marketing",
                      measure="patient_years", value_text="1,000"))
    db.commit()
    db.close()
    assert "UNCONFIRMED_EXPOSURE" in _codes(_run(app_client_free), qc.BLOCKER)


# ---------------------------------------------------- §11.3 reconciliation

def test_a_count_the_store_did_not_produce_blocks(report):
    """The model never counts. "17 serious cases" when the store holds one is a
    figure somebody typed."""
    _write(report, "4", "4 Case Series Review\n\nThere were 17 serious cases.")
    found = [f for f in _run(report) if f.code == "PROSE_FIGURE_UNSOURCED"]
    assert found and "17 serious cases" in found[0].message


def test_a_count_the_store_did_produce_passes(report):
    _write(report, "4", "4 Case Series Review\n\nThere was 1 serious case [S1].")
    assert "PROSE_FIGURE_UNSOURCED" not in _codes(_run(report))


def test_numbers_that_are_not_counts_are_not_checked(report):
    """"2 hours after the second dose" is not a count. A check that flagged it
    would teach people to ignore this one."""
    _write(report, "4", "4 Case Series Review\n\nOnset was 2 hours after dose 3.")
    assert "PROSE_FIGURE_UNSOURCED" not in _codes(_run(report))


def test_a_citation_marker_is_not_a_figure(report):
    _write(report, "4", "4 Case Series Review\n\nSee [S12, p.3] for 1 case.")
    assert "PROSE_FIGURE_UNSOURCED" not in _codes(_run(report))


def test_the_summary_and_the_listing_disagreeing_blocks(report, monkeypatch):
    """The summary's serious interval events must all be accounted for by the
    listing: SAR rows, unassessed, or assessed unrelated. A difference means
    one builder counted something the other did not."""
    monkeypatch.setattr(qc, "_serious_unrelated", lambda ctx: 5)
    found = [f for f in _run(report) if f.code == "COUNT_MISMATCH"]
    assert found and found[0].detail["listing_accounted"] > found[0].detail["summary_serious"]


# ----------------------------------------------------------- §11.4 windows

def _next_report(app_client, report, stated):
    """A second interval whose baseline is `report`, which stated `stated`
    at its sign-off."""
    from app.db import SessionLocal
    from app.models import PvReportInstance

    db = SessionLocal()
    first = db.get(PvReportInstance, report["report_id"])
    first.status = "approved"
    first.figures_at_signoff = stated
    db.commit()
    db.close()
    return app_client.post(
        f"/api/v1/pv/products/{report['product_id']}/reports",
        headers=_auth(report["token"]),
        json={"doc_type_key": "signal_eval", "period_start": "2026-07-01",
              "period_end": "2026-12-31", "data_lock_point": "2027-01-15",
              "rsi_version_id": report["rsi_id"],
              "baseline_report_id": report["report_id"]}).json()


def test_cumulative_below_the_previous_reports_blocks(app_client, report):
    """A cumulative count that fell between two reports is a data loss or a
    case updated after the lock -- either way somebody has to look. It is
    compared against what the previous report STATED: a recomputation of its
    window over today's store loses the same case and can never be larger."""
    from app.db import SessionLocal
    from app.models import PvCase

    nxt = _next_report(app_client, report, {"cumulative_cases": 1})
    # The only case is updated after the new lock, so the new report excludes it.
    db = SessionLocal()
    db.get(PvCase, report["ids"]["case"]).latest_receipt_date = date(2027, 3, 1)
    db.commit()
    db.close()
    found = [f for f in _run({**report, "report_id": nxt["id"]})
             if f.code == "CUMULATIVE_DECREASED"]
    assert found and found[0].severity == qc.BLOCKER
    assert found[0].detail == {"this": 0, "previous": 1}


def test_cumulative_equal_to_the_previous_reports_passes(app_client, report):
    nxt = _next_report(app_client, report, {"cumulative_cases": 1})
    assert "CUMULATIVE_DECREASED" not in _codes(_run({**report, "report_id": nxt["id"]}))


def test_a_baseline_with_no_recorded_figures_says_it_cannot_check(app_client, report):
    nxt = _next_report(app_client, report, None)
    findings = _run({**report, "report_id": nxt["id"]})
    assert "CUMULATIVE_DECREASED" not in _codes(findings)
    assert "BASELINE_FIGURES_UNRECORDED" in _codes(findings, qc.INFO)


def test_an_interval_that_starts_before_the_anchor_blocks(report):
    """A DSUR counts cumulatively from the DIBD. An interval that opens before
    it has interval cases the cumulative cannot contain."""
    from app.db import SessionLocal
    from app.models import PvProduct

    db = SessionLocal()
    product = db.get(PvProduct, report["product_id"])
    product.ibd = product.dibd = date(2026, 5, 1)
    db.commit()
    db.close()
    assert "CUMULATIVE_BELOW_INTERVAL" in _codes(_run(report), qc.BLOCKER)


def test_a_table_that_counts_a_case_after_the_lock_blocks(report, monkeypatch):
    """Structural -- the builders filter through the scope layer -- and checked
    anyway, because a builder that stopped doing so would otherwise pass."""
    from app.db import SessionLocal
    from app.models import PvCase
    from app.safety import tabulations as tab

    db = SessionLocal()
    late = PvCase(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                  worldwide_case_id="LATE-1", initial_receipt_date=date(2026, 3, 1),
                  latest_receipt_date=date(2026, 8, 1), deidentification_status="clear")
    db.add(late)
    db.commit()
    late_id = late.id
    db.close()
    real = tab.render

    def leaky(db, *, report, product, table_key):
        built = real(db, report=report, product=product, table_key=table_key)
        if table_key == "summary_tab_soc_pt":
            built.cells["r0c9"] = {"events": [], "cases": [late_id]}
        return built

    monkeypatch.setattr(tab, "render", leaky)
    found = [f for f in _run(report) if f.code == "CASE_AFTER_LOCK_COUNTED"]
    assert found and found[0].detail == {"table": "summary_tab_soc_pt", "cases": [late_id]}


def test_a_numbered_heading_is_not_a_count(report):
    """Every draft opens with "<code> <title>"; "4 Case Series Review" is not
    four cases, and "7.3 Cumulative Events" is not a count either."""
    _write(report, "4", "4 Case Series Review\n\n7.3 Cumulative Events\n\nNothing new.")
    assert "PROSE_FIGURE_UNSOURCED" not in _codes(_run(report))


# --------------------------------------------------------------- §11.5 RSI

def test_no_pinned_rsi_blocks(app_client, report):
    app_client.patch(f"/api/v1/pv/reports/{report['report_id']}",
                     headers=_auth(report["token"]), json={"rsi_version_id": None})
    assert "NO_RSI_PINNED" in _codes(_run(report), qc.BLOCKER)


def test_a_determination_against_another_version_blocks(app_client, report):
    newer = app_client.post(f"/api/v1/pv/products/{report['product_id']}/rsi-versions",
                            headers=_auth(report["token"]),
                            json={"rsi_type": "ib", "version_label": "8"}).json()
    app_client.patch(f"/api/v1/pv/reports/{report['report_id']}",
                     headers=_auth(report["token"]),
                     json={"rsi_version_id": newer["id"]})
    assert "STALE_EXPECTEDNESS" in _codes(_run(report), qc.BLOCKER)


# ------------------------------------------------------------ §11.6 MedDRA

def test_two_meddra_versions_block(report):
    from app.db import SessionLocal
    from app.models import PvCaseEvent

    db = SessionLocal()
    db.add(PvCaseEvent(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                       case_id=report["ids"]["case"], meddra_pt="Rash",
                       meddra_version="26.1", confirmed_by="qp"))
    db.commit()
    db.close()
    found = [f for f in _run(report) if f.code == "MEDDRA_VERSION_MIXED"]
    assert found and found[0].detail["versions"] == ["26.1", "27.0"]


# ------------------------------------------------------ §11.7 / §11.8 markers

def test_a_gap_and_an_open_judgment_each_block(report):
    _write(report, "11", "11 Assessment and Conclusion\n\n"
                         "[DATA NEEDED: the hepatic case count]\n"
                         "[ASSESSMENT REQUIRED: whether this is a signal]")
    codes = _codes(_run(report), qc.BLOCKER)
    assert "DATA_NEEDED" in codes
    assert "ASSESSMENT_REQUIRED" in codes


#: What a real PBRER draft came back with: the model quoting its brief.
LEAKED_DRAFT = (
    "4 Case Series Review\n\n"
    "In the interval, 3 cases were received [CONFIRMED SAFETY DATA: table_totals]. "
    "The product was authorised as stated [Product and period metadata]. "
    "Totals are in the report workspace [table_totals].\n"
    "[DATA NEEDED: the hepatic case count]\n"
)


def test_a_draft_quoting_its_brief_blocks_and_the_gap_markers_still_do(report):
    _write(report, "4", LEAKED_DRAFT)
    findings = _run(report)
    found = [f for f in findings if f.code == "PROMPT_TEXT_IN_DRAFT"]
    assert found and found[0].severity == qc.BLOCKER
    assert sorted(found[0].detail["found"]) == sorted([
        "[CONFIRMED SAFETY DATA: table_totals]", "[Product and period metadata]",
        "[table_totals]"])
    # The intended gap marker is not swallowed by the new check.
    assert "DATA_NEEDED" in _codes(findings, qc.BLOCKER)
    assert qc.exportable(findings) is False


def test_a_clean_draft_has_no_prompt_text(report):
    _write(report, "4", "4 Case Series Review\n\nThree cases [S1, p.2] and [S5; S1].\n")
    assert "PROMPT_TEXT_IN_DRAFT" not in _codes(_run(report))


def test_a_table_marker_nothing_builds_blocks(report):
    _write(report, "4", "4 Case Series Review\n\n[TABLE: summary_tab_everything]\n")
    found = [f for f in _run(report) if f.code == "TABLE_UNKNOWN"]
    assert found and found[0].severity == qc.BLOCKER
    assert found[0].detail == {"table_key": "summary_tab_everything"}


def test_a_marker_whose_table_cannot_be_built_says_why(report, monkeypatch):
    from app.safety import tabulations as tab

    def unavailable(db, *, report, product, table_key):
        raise tab.TableUnavailable("no exposure has been entered for this report")

    monkeypatch.setattr(tab, "render", unavailable)
    _write(report, "3", "3 Data Sources\n\n[TABLE: exposure_table]")
    found = [f for f in _run(report) if f.code == "TABLE_UNRESOLVED"
             and f.section_code == "3"]
    assert found and "no exposure has been entered" in found[0].message


def test_a_data_section_without_its_marker_blocks(report):
    _write(report, "5", "5 Disproportionality\n\nNo table here.")
    found = [f for f in _run(report) if f.code == "TABLE_MISSING"]
    assert found and found[0].section_code == "5"


# ------------------------------------------------------ §11.9 denominator

def test_a_rate_with_no_confirmed_exposure_blocks(report):
    _write(report, "10", "10 Exposure\n\nThe reporting rate was 3 per 100,000 "
                         "patient-years.")
    assert "RATE_WITHOUT_DENOMINATOR" in _codes(_run(report), qc.BLOCKER)


# --------------------------------------------------------------- warnings

def test_carried_forward_text_quoting_a_count_is_flagged(report):
    """Rule 6: an interval figure is never carried forward. A section carried
    word for word that says "12 cases" is quoting last interval's number."""
    from app.db import SessionLocal
    from app.models import PvSection

    section_id = _write(report, "4", "4 Case Series Review\n\n12 cases were reported.",
                        origin="carried_forward")
    db = SessionLocal()
    db.get(PvSection, section_id).delta_status = "carried_forward"
    db.commit()
    db.close()
    assert "BASELINE_FIGURE_CARRIED" in _codes(_run(report), qc.WARNING)


def test_a_fatal_case_without_a_narrative_is_flagged(report):
    from app.db import SessionLocal
    from app.models import PvCase

    db = SessionLocal()
    db.get(PvCase, report["ids"]["case"]).seriousness_criteria = ["death"]
    db.commit()
    db.close()
    found = [f for f in _run(report) if f.code == "NARRATIVE_MISSING"]
    assert found and found[0].detail["cases"] == ["Q-1"]


def test_uncoded_events_and_open_duplicates_are_warnings(report):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvDuplicateCandidate

    db = SessionLocal()
    other = PvCase(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                   worldwide_case_id="Q-2", initial_receipt_date=date(2026, 3, 5),
                   latest_receipt_date=date(2026, 3, 5))
    db.add(other)
    db.flush()
    db.add(PvCaseEvent(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                       case_id=other.id, verbatim_term="funny turn",
                       coding_required=True, confirmed_by="qp"))
    db.add(PvDuplicateCandidate(org_id=report["ids"]["org"],
                                pv_product_id=report["product_id"],
                                case_id=report["ids"]["case"], other_case_id=other.id))
    db.commit()
    db.close()
    codes = _codes(_run(report), qc.WARNING)
    assert {"CODING_REQUIRED", "DUPLICATES_UNRESOLVED"} <= codes


def test_a_closed_signal_without_a_conclusion_is_flagged(report):
    from app.db import SessionLocal
    from app.models import PvSignal

    db = SessionLocal()
    db.add(PvSignal(org_id=report["ids"]["org"], pv_product_id=report["product_id"],
                    signal_reference="SIG-1", status="closed"))
    db.commit()
    db.close()
    assert "SIGNAL_CLOSED_INCOMPLETE" in _codes(_run(report), qc.WARNING)


def test_a_label_change_with_no_rsi_after_it_is_flagged(app_client, report):
    app_client.post(f"/api/v1/pv/products/{report['product_id']}/registers/safety-actions",
                    headers=_auth(report["token"]),
                    json={"action_type": "label_change", "action_date": "2026-04-01"})
    assert "LABEL_CHANGE_NOT_IN_RSI" in _codes(_run(report), qc.WARNING)


def test_a_model_draft_with_no_citations_is_flagged(report):
    _write(report, "2", "2 Description of the Signal\n\n" + "word " * 60,
           origin="model")
    assert "NO_CITATIONS" in _codes(_run(report), qc.WARNING)


def test_a_region_with_no_due_date_is_flagged(app_client, report):
    app_client.patch(f"/api/v1/pv/reports/{report['report_id']}",
                     headers=_auth(report["token"]), json={"regions": ["EU"]})
    assert "REGION_NO_DUE_DATE" in _codes(_run(report), qc.WARNING)


# ------------------------------------------------------------------ info

def test_undefined_abbreviations_are_information(report):
    _write(report, "2", "2 Description\n\nThe DILI and ALT findings [S1].")
    found = [f for f in _run(report) if f.code == "ABBREVIATIONS_UNDEFINED"]
    assert found and found[0].severity == qc.INFO


# ------------------------------------------------------------ the endpoint

def test_the_endpoint_groups_and_says_whether_it_can_export(app_client, report):
    res = app_client.get(f"/api/v1/pv/reports/{report['report_id']}/qc",
                         headers=_auth(report["token"]))
    assert res.status_code == 200
    body = res.json()
    assert body["exportable"] is False
    assert all(f["severity"] == "blocker" for f in body["blockers"])
    assert len(body["findings"]) == (len(body["blockers"]) + len(body["warnings"])
                                     + len(body["info"]))
