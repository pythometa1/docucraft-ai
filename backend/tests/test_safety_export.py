"""§12 and the M8 routes: sign-off, export, audit.

The properties, in the order a report meets them:

* a qualified person signs off only a report with nothing blocking it, and the
  figures they signed are frozen;
* anything that changes the report afterwards takes the signature away;
* the export's gate is `run_qc` -- the same list the Checks screen shows;
* the document carries its tables from the builders, its heading once, a
  CONFIDENTIAL header, page numbers and a contents field;
* a tracked-changes copy marks only what a person changed;
* the finished file is scanned, deleted text included, and a leak deletes it.
"""

import zipfile
from datetime import date, datetime

import pytest
from lxml import etree

from app.safety import export as export_mod
from app.safety import qc

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
BODY = "The review of this topic found nothing requiring action."


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _make_report(app_client, ws, *, period=("2026-01-01", "2026-06-30", "2026-07-15"),
                 baseline=None):
    body = {"doc_type_key": "signal_eval", "period_start": period[0],
            "period_end": period[1], "data_lock_point": period[2],
            "rsi_version_id": ws["rsi_id"], "meddra_version": "27.0",
            "regions": ["EU"]}
    if baseline:
        body["baseline_report_id"] = baseline
    made = app_client.post(f"/api/v1/pv/products/{ws['product_id']}/reports",
                           headers=_auth(ws["token"]), json=body)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _sections(app_client, ws, report_id):
    return app_client.get(f"/api/v1/pv/reports/{report_id}/sections",
                          headers=_auth(ws["token"])).json()["items"]


def _text(section, extra=""):
    table = {"5": "\n\n[TABLE: summary_tab_soc_pt]\n",
             "10": "\n\n[TABLE: exposure_table]\n"}.get(section["section_code"], "")
    return f"{section['section_code']} {section['title']}\n\n{BODY}{extra}{table}"


def _write(app_client, ws, section_id, content):
    response = app_client.put(f"/api/v1/pv/sections/{section_id}/draft",
                              headers=_auth(ws["token"]), json={"content": content})
    assert response.status_code == 201, response.text


def _approve(app_client, ws, section_id):
    response = app_client.patch(f"/api/v1/pv/sections/{section_id}/status",
                                headers=_auth(ws["token"]), json={"status": "approved"})
    assert response.status_code == 200, response.text


def _exposure(ws, report_id):
    from app.models import PvExposure

    db = _db()
    db.add(PvExposure(org_id=ws["org"], pv_product_id=ws["product_id"],
                      report_instance_id=report_id, context="marketing",
                      measure="patient_years", value_text="1,000", value_numeric=1000,
                      calculation_method_note="sales", confirmed_by="qp",
                      confirmed_at=datetime(2026, 7, 1)))
    db.commit()
    db.close()


def _fill_and_approve(app_client, ws, report_id, *, overrides=None):
    for section in _sections(app_client, ws, report_id):
        if section["is_container"] or not section["enabled"]:
            continue
        content = (overrides or {}).get(section["section_code"]) or _text(section)
        _write(app_client, ws, section["id"], content)
        _approve(app_client, ws, section["id"])


def _sign(app_client, ws, report_id, **body):
    return app_client.post(f"/api/v1/pv/reports/{report_id}/signoff",
                           headers=_auth(ws["token"]), json=body)


def _export(app_client, ws, report_id, **body):
    return app_client.post(f"/api/v1/pv/reports/{report_id}/export",
                           headers=_auth(ws["token"]), json=body)


@pytest.fixture
def ws(app_client, two_orgs):
    """A product with one confirmed, serious, related case, a qualified
    person, and a signal-evaluation report with every section written."""
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Export safety", "function": "Safety", "document_type": "Signal",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Exportazine",
        "ibd": "2020-01-01", "dibd": "2016-01-01"}).json()
    members = app_client.get(f"/api/v1/pv/products/{product['id']}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{product['id']}/members", headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"],
                          "pv_role": "qualified_person"})
    rsi = app_client.post(f"/api/v1/pv/products/{product['id']}/rsi-versions",
                          headers=_auth(token),
                          json={"rsi_type": "ib", "version_label": "7"}).json()
    app_client.post(f"/api/v1/pv/rsi-versions/{rsi['id']}/listed-terms",
                    headers=_auth(token),
                    json=[{"meddra_pt": "Nausea", "meddra_soc": "Gastrointestinal disorders"}])

    db = _db()
    org_id = db.get(PvProduct, product["id"]).org_id
    case = PvCase(org_id=org_id, pv_product_id=product["id"], worldwide_case_id="EX-1",
                  initial_receipt_date=date(2026, 3, 4),
                  latest_receipt_date=date(2026, 3, 4), is_serious=True,
                  deidentification_status="clear")
    db.add(case)
    db.flush()
    db.add(PvCaseEvent(org_id=org_id, pv_product_id=product["id"], case_id=case.id,
                       meddra_pt="Headache", meddra_soc="Nervous system disorders",
                       meddra_version="27.0", is_serious=True, expectedness="unlisted",
                       expectedness_rsi_version_id=rsi["id"],
                       causality_company="related", confirmed_by="qp"))
    db.commit()
    db.close()

    out = {"token": token, "product_id": product["id"], "rsi_id": rsi["id"],
           "org": org_id}
    out["report_id"] = _make_report(app_client, out)
    _exposure(out, out["report_id"])
    _fill_and_approve(app_client, out, out["report_id"])
    return out


def _report(ws, report_id=None):
    from app.models import PvReportInstance

    db = _db()
    try:
        row = db.get(PvReportInstance, report_id or ws["report_id"])
        db.expunge(row)
        return row
    finally:
        db.close()


def _docx_part(path, part="word/document.xml"):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read(part))


def _download(app_client, ws, export, index=0):
    response = app_client.get(f"/api/v1/pv/exports/{export['id']}/download",
                              headers=_auth(ws["token"]), params={"index": index})
    assert response.status_code == 200, response.text
    return response


def _saved(tmp_path, response, name="out.docx"):
    path = tmp_path / name
    path.write_bytes(response.content)
    return path


# ------------------------------------------------------------------ sign-off

def test_a_ready_report_is_blocked_only_by_its_missing_signature(app_client, ws):
    """The fixture is the smallest exportable report, less one signature."""
    blockers = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                              headers=_auth(ws["token"])).json()["blockers"]
    assert [b["code"] for b in blockers] == ["NOT_SIGNED_OFF"]


def test_sign_off_approves_the_report_and_freezes_its_figures(app_client, ws):
    signed = _sign(app_client, ws, ws["report_id"], statement="Reviewed in full.")
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["status"] == "approved" and body["qppv_signoff_by"]
    figures = body["figures_at_signoff"]
    assert figures["interval_cases"] == 1 and figures["cumulative_cases"] == 1
    assert "summary_tab_soc_pt" in figures["tables"]


def test_sign_off_is_refused_while_anything_else_blocks(app_client, ws):
    section = _sections(app_client, ws, ws["report_id"])[0]
    app_client.patch(f"/api/v1/pv/sections/{section['id']}/status",
                     headers=_auth(ws["token"]), json={"status": "in_review"})
    refused = _sign(app_client, ws, ws["report_id"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_CANNOT_SIGN_OFF"
    codes = {b["code"] for b in refused.json()["detail"]["error"]["details"]["blockers"]}
    assert codes == {"SECTION_NOT_APPROVED"}


def test_sign_off_needs_the_qualified_person_role(app_client, ws):
    from app.models import PvMember

    db = _db()
    for member in db.query(PvMember).filter(PvMember.pv_product_id == ws["product_id"]):
        member.pv_role = "reviewer"
    db.commit()
    db.close()
    assert _sign(app_client, ws, ws["report_id"]).status_code == 403


def test_signing_twice_is_refused(app_client, ws):
    assert _sign(app_client, ws, ws["report_id"]).status_code == 200
    again = _sign(app_client, ws, ws["report_id"])
    assert again.status_code == 409
    assert again.json()["detail"]["error"]["code"] == "PV_ALREADY_SIGNED_OFF"


def test_editing_a_signed_report_takes_the_signature_away(app_client, ws):
    """A signature that survived an edit would be on text its signatory
    never read."""
    _sign(app_client, ws, ws["report_id"])
    section = _sections(app_client, ws, ws["report_id"])[0]
    _write(app_client, ws, section["id"], _text(section, " Reworded."))
    report = _report(ws)
    assert report.qppv_signoff_by is None and report.figures_at_signoff is None
    assert report.status == "in_review"


def test_changing_the_reports_terms_takes_the_signature_away(app_client, ws):
    _sign(app_client, ws, ws["report_id"])
    app_client.patch(f"/api/v1/pv/reports/{ws['report_id']}", headers=_auth(ws["token"]),
                     json={"meddra_version": "27.1"})
    assert _report(ws).qppv_signoff_by is None


def test_withdrawing_a_section_approval_takes_the_signature_away(app_client, ws):
    _sign(app_client, ws, ws["report_id"])
    section = _sections(app_client, ws, ws["report_id"])[0]
    app_client.patch(f"/api/v1/pv/sections/{section['id']}/status",
                     headers=_auth(ws["token"]), json={"status": "in_review"})
    assert _report(ws).qppv_signoff_by is None


def test_a_signature_is_withdrawn_with_a_reason(app_client, ws):
    url = f"/api/v1/pv/reports/{ws['report_id']}/signoff:withdraw"
    assert app_client.post(url, headers=_auth(ws["token"]),
                           json={"reason": "x"}).status_code == 409   # not signed
    _sign(app_client, ws, ws["report_id"])
    assert app_client.post(url, headers=_auth(ws["token"]),
                           json={"reason": "  "}).status_code == 422
    withdrawn = app_client.post(url, headers=_auth(ws["token"]),
                                json={"reason": "late case"})
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "in_review"


def test_figures_that_moved_after_sign_off_block_export(app_client, ws):
    """The export resolves tables from the store as it is now. A case
    confirmed after the signature would print a number nobody signed for."""
    from app.models import PvCase, PvCaseEvent

    _sign(app_client, ws, ws["report_id"])
    db = _db()
    late = PvCase(org_id=ws["org"], pv_product_id=ws["product_id"],
                  worldwide_case_id="EX-2", initial_receipt_date=date(2026, 4, 1),
                  latest_receipt_date=date(2026, 4, 1), is_serious=True,
                  deidentification_status="clear")
    db.add(late)
    db.flush()
    db.add(PvCaseEvent(org_id=ws["org"], pv_product_id=ws["product_id"], case_id=late.id,
                       meddra_pt="Headache", meddra_soc="Nervous system disorders",
                       meddra_version="27.0", is_serious=True, expectedness="unlisted",
                       expectedness_rsi_version_id=ws["rsi_id"],
                       causality_company="related", confirmed_by="qp"))
    db.commit()
    db.close()
    refused = _export(app_client, ws, ws["report_id"])
    assert refused.status_code == 409
    codes = {b["code"] for b in refused.json()["detail"]["error"]["details"]["blockers"]}
    assert "FIGURES_CHANGED_SINCE_SIGNOFF" in codes


# ----------------------------------------------------------------- acceptance

def test_a_heuristic_blocker_can_be_accepted_with_a_reason(app_client, ws):
    """"A published series of 12 patients" is a count the store did not
    produce and is still correct."""
    section = _sections(app_client, ws, ws["report_id"])[1]
    _write(app_client, ws, section["id"],
           _text(section, " A published series described 12 patients."))
    found = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                           headers=_auth(ws["token"])).json()
    finding = next(f for f in found["blockers"] if f["code"] == "PROSE_FIGURE_UNSOURCED")
    assert finding["acceptable"] is True
    url = f"/api/v1/pv/reports/{ws['report_id']}/qc/accept"
    assert app_client.post(url, headers=_auth(ws["token"]),
                           json={"key": finding["key"], "reason": ""}).status_code == 422
    accepted = app_client.post(url, headers=_auth(ws["token"]),
                               json={"key": finding["key"], "reason": "Literature count."})
    assert accepted.status_code == 200, accepted.text

    after = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                           headers=_auth(ws["token"])).json()
    assert "PROSE_FIGURE_UNSOURCED" not in {b["code"] for b in after["blockers"]}
    kept = next(w for w in after["warnings"] if w["code"] == "PROSE_FIGURE_UNSOURCED")
    assert kept["detail"]["accepted_reason"] == "Literature count."
    again = app_client.post(url, headers=_auth(ws["token"]),
                            json={"key": finding["key"], "reason": "x"})
    assert again.json()["detail"]["error"]["code"] == "PV_FINDING_ALREADY_ACCEPTED"


def test_an_acceptance_does_not_survive_a_changed_figure(app_client, ws):
    section = _sections(app_client, ws, ws["report_id"])[1]
    _write(app_client, ws, section["id"], _text(section, " A series of 12 patients."))
    finding = next(f for f in app_client.get(
        f"/api/v1/pv/reports/{ws['report_id']}/qc", headers=_auth(ws["token"])
    ).json()["blockers"] if f["code"] == "PROSE_FIGURE_UNSOURCED")
    app_client.post(f"/api/v1/pv/reports/{ws['report_id']}/qc/accept",
                    headers=_auth(ws["token"]), json={"key": finding["key"], "reason": "ok"})
    _write(app_client, ws, section["id"], _text(section, " A series of 14 patients."))
    blockers = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                              headers=_auth(ws["token"])).json()["blockers"]
    assert "PROSE_FIGURE_UNSOURCED" in {b["code"] for b in blockers}


def test_a_state_is_fixed_not_accepted(app_client, ws):
    finding = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                             headers=_auth(ws["token"])).json()["blockers"][0]
    assert finding["code"] == "NOT_SIGNED_OFF" and finding["acceptable"] is False
    url = f"/api/v1/pv/reports/{ws['report_id']}/qc/accept"
    refused = app_client.post(url, headers=_auth(ws["token"]),
                              json={"key": finding["key"], "reason": "trust me"})
    assert refused.json()["detail"]["error"]["code"] == "PV_FINDING_NOT_ACCEPTABLE"
    missing = app_client.post(url, headers=_auth(ws["token"]),
                              json={"key": "0000", "reason": "x"})
    assert missing.status_code == 404


# -------------------------------------------------------------------- export

def test_export_is_refused_before_sign_off_and_the_refusal_is_audited(app_client, ws):
    from app.models import AuditLog

    refused = _export(app_client, ws, ws["report_id"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_EXPORT_BLOCKED"
    db = _db()
    events = [a.event for a in db.query(AuditLog).filter(
        AuditLog.entity_id == ws["report_id"])]
    db.close()
    assert "Refused a safety report export" in events


@pytest.mark.parametrize("body, code", [
    ({"appendices": ["everything"]}, "PV_BAD_APPENDIX"),
    ({"citations": "footnotes"}, "PV_BAD_CITATION_MODE"),
    ({"region": "JP"}, "PV_BAD_REGION"),
    ({"tracked_changes": True}, "PV_NO_BASELINE"),
])
def test_bad_options_are_refused_before_anything_is_written(app_client, ws, body, code):
    response = _export(app_client, ws, ws["report_id"], **body)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == code


def test_the_exported_report(app_client, ws, tmp_path):
    _sign(app_client, ws, ws["report_id"])
    made = _export(app_client, ws, ws["report_id"])
    assert made.status_code == 200, made.text
    export = made.json()
    assert [f["kind"] for f in export["files"]] == ["report"]
    assert export["options"]["leakage_scan"] == "passed"
    assert "blob_path" not in str(export)

    path = _saved(tmp_path, _download(app_client, ws, export))
    text = export_mod.document_text(str(path))
    # Each heading once: the draft's own copy is dropped, the export writes it.
    assert text.count("4 Case Series Review") == 1
    # The table came from the builder, not from anyone's typing.
    assert "Headache" in text and "[TABLE:" not in text
    # The header, the page numbers and the contents field.
    assert "CONFIDENTIAL" in text and "Exportazine" in text
    header = etree.tostring(_docx_part(path, "word/header1.xml")).decode()
    assert "CONFIDENTIAL" in header and "DRAFT" not in header
    footer = etree.tostring(_docx_part(path, "word/footer1.xml")).decode()
    assert "PAGE" in footer and "NUMPAGES" in footer
    body = _docx_part(path)
    assert any("TOC" in f.get(f"{W}instr") for f in body.iter(f"{W}fldSimple"))
    styles = [p.find(f"{W}pPr/{W}pStyle") for p in body.iter(f"{W}p")]
    assert any(s is not None and s.get(f"{W}val", "").startswith("Heading")
               for s in styles)
    settings = _docx_part(path, "word/settings.xml")
    assert settings.find(f"{W}updateFields") is not None


def test_citations_are_stripped_unless_asked_to_be_kept(app_client, ws, tmp_path):
    section = _sections(app_client, ws, ws["report_id"])[0]
    _write(app_client, ws, section["id"], _text(section, " See the source [S1]."))
    _approve(app_client, ws, section["id"])
    _sign(app_client, ws, ws["report_id"])
    stripped = _export(app_client, ws, ws["report_id"]).json()
    kept = _export(app_client, ws, ws["report_id"], citations="keep").json()
    assert "[S1]" not in export_mod.document_text(
        str(_saved(tmp_path, _download(app_client, ws, stripped), "a.docx")))
    assert "[S1]" in export_mod.document_text(
        str(_saved(tmp_path, _download(app_client, ws, kept), "b.docx")))


def test_a_draft_copy_carries_a_watermark_and_says_so(app_client, ws, tmp_path):
    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"], draft_watermark=True,
                     region="EU").json()
    path = _saved(tmp_path, _download(app_client, ws, export))
    header = etree.tostring(_docx_part(path, "word/header1.xml")).decode()
    assert 'string="DRAFT"' in header and "| DRAFT" in header
    assert "Regional copy: EU" in export_mod.document_text(str(path))
    assert export["files"][0]["filename"] == "signal_eval-2026-06-30-EU.docx"


def test_appendices_come_from_the_same_builders(app_client, ws, tmp_path):
    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"],
                     appendices=["rsi", "signal_log", "line_listings"]).json()
    text = export_mod.document_text(str(_saved(tmp_path, _download(app_client, ws, export))))
    assert "Appendix 1: Reference safety information" in text
    assert "Nausea" in text                     # the pinned RSI's listed term
    # An empty appendix says so rather than printing a heading over nothing.
    assert "No signal overview:" in text
    assert "Appendix 3: Line listings" in text and "EX-1" in text


def test_a_pdf_is_added_when_it_can_be_made(app_client, ws, monkeypatch):
    from app.generation import pdf_renderer

    def unavailable(docx_path, out_dir, language="en"):
        raise pdf_renderer.PreviewUnavailable("LibreOffice is not installed")

    monkeypatch.setattr(pdf_renderer, "render_pdf", unavailable)
    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"], pdf=True).json()
    assert [f["kind"] for f in export["files"]] == ["report"]
    assert "No PDF was produced" in export["options"]["notes"][0]

    def fake(docx_path, out_dir, language="en"):
        target = out_dir / "rendered.pdf"
        target.write_bytes(b"%PDF-1.4")
        return pdf_renderer.PreviewResult(pdf_path=str(target), renderer="fake")

    monkeypatch.setattr(pdf_renderer, "render_pdf", fake)
    export = _export(app_client, ws, ws["report_id"], pdf=True).json()
    assert [f["kind"] for f in export["files"]] == ["report", "pdf"]
    response = _download(app_client, ws, export, index=1)
    assert response.headers["content-type"] == "application/pdf"


def test_a_confirmed_identifier_in_a_table_deletes_the_export(app_client, ws):
    """Tables are built from structured fields QC does not read as prose. A
    name a person confirmed in the review queue, sitting in a study title, is
    caught in the finished file -- and the file is deleted, not offered."""
    from app.models import AuditLog, PvDeidItem, PvExport, PvStudy
    from app.storage import abs_path

    db = _db()
    db.add(PvStudy(org_id=ws["org"], pv_product_id=ws["product_id"], study_id="ST-1",
                   title="Okafor site extension", start_date=date(2025, 1, 1)))
    db.add(PvDeidItem(org_id=ws["org"], pv_product_id=ws["product_id"],
                      identifier_type="investigator_name", detected_text="Okafor",
                      status="masked"))
    db.commit()
    db.close()
    _sign(app_client, ws, ws["report_id"])
    folder = abs_path(f"pv-export/{ws['product_id']}")
    before = set(folder.glob("*")) if folder.exists() else set()

    refused = _export(app_client, ws, ws["report_id"], appendices=["study_inventory"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_EXPORT_LEAK"
    assert refused.json()["detail"]["error"]["details"]["found"][0]["identifier_type"] == \
        "confirmed_identifier"
    after = set(folder.glob("*")) if folder.exists() else set()
    assert after == before
    db = _db()
    assert db.query(PvExport).filter(
        PvExport.report_instance_id == ws["report_id"]).count() == 0
    targets = [a.target or "" for a in db.query(AuditLog).filter(
        AuditLog.entity_id == ws["report_id"])]
    db.close()
    # The audit names the kind of identifier and never the identifier.
    assert not any("Okafor" in t for t in targets)


def test_a_confirmed_identifier_in_a_draft_is_a_qc_blocker(app_client, ws):
    from app.models import PvDeidItem

    db = _db()
    db.add(PvDeidItem(org_id=ws["org"], pv_product_id=ws["product_id"],
                      identifier_type="patient_name", detected_text="Okafor",
                      status="masked"))
    db.commit()
    db.close()
    section = _sections(app_client, ws, ws["report_id"])[0]
    _write(app_client, ws, section["id"], _text(section, " Mrs Okafor recovered."))
    blockers = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/qc",
                              headers=_auth(ws["token"])).json()["blockers"]
    found = next(b for b in blockers if b["code"] == "PII_IN_DRAFT")
    assert found["detail"]["found"] == ["Okafor"]
    refused = app_client.patch(f"/api/v1/pv/sections/{section['id']}/status",
                               headers=_auth(ws["token"]), json={"status": "approved"})
    assert refused.json()["detail"]["error"]["code"] == "PV_CANNOT_APPROVE"


# ----------------------------------------------------------- tracked changes

def test_tracked_changes_mark_only_what_a_person_changed(app_client, ws, tmp_path):
    """§16.9: the diff shows only genuinely changed sections."""
    assert _sign(app_client, ws, ws["report_id"]).status_code == 200
    second = _make_report(app_client, ws, period=("2026-07-01", "2026-12-31",
                                                  "2027-01-15"),
                          baseline=ws["report_id"])
    _exposure(ws, second)
    changed = {"2": "2 Description of the Signal\n\nThe review of this topic found "
                    "one matter requiring action."}
    _fill_and_approve(app_client, ws, second, overrides=changed)
    assert _sign(app_client, ws, second).status_code == 200
    export = _export(app_client, ws, second, tracked_changes=True).json()
    assert [f["kind"] for f in export["files"]] == ["report", "tracked_changes"]

    clean = _docx_part(_saved(tmp_path, _download(app_client, ws, export, 0), "c.docx"))
    assert not list(clean.iter(f"{W}ins")) and not list(clean.iter(f"{W}del"))

    tracked = _docx_part(_saved(tmp_path, _download(app_client, ws, export, 1), "t.docx"))
    heading = None
    marked_in = set()
    for paragraph in tracked.iter(f"{W}p"):
        style = paragraph.find(f"{W}pPr/{W}pStyle")
        if style is not None and style.get(f"{W}val", "").startswith("Heading"):
            heading = "".join(t.text or "" for t in paragraph.iter(f"{W}t"))
        if list(paragraph.iter(f"{W}ins")) or list(paragraph.iter(f"{W}del")):
            marked_in.add(heading)
    assert marked_in == {"2 Description of the Signal"}
    deleted = "".join(t.text for t in tracked.iter(f"{W}delText"))
    inserted = "".join(t.text for i in tracked.iter(f"{W}ins") for t in i.iter(f"{W}t"))
    assert "nothing" in deleted and "one matter" in inserted
    author = next(tracked.iter(f"{W}ins")).get(f"{W}author")
    assert author == export_mod.REVISION_AUTHOR


# ------------------------------------------------------ downloads and audit

def test_downloads_are_per_file_and_audited(app_client, ws):
    from app.models import AuditLog

    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"]).json()
    response = _download(app_client, ws, export)
    assert "wordprocessingml" in response.headers["content-type"]
    missing = app_client.get(f"/api/v1/pv/exports/{export['id']}/download",
                             headers=_auth(ws["token"]), params={"index": 3})
    assert missing.json()["detail"]["error"]["code"] == "PV_EXPORT_NO_SUCH_FILE"
    listed = app_client.get(f"/api/v1/pv/reports/{ws['report_id']}/exports",
                            headers=_auth(ws["token"])).json()["items"]
    assert [e["id"] for e in listed] == [export["id"]]
    db = _db()
    events = [a.event for a in db.query(AuditLog).filter(AuditLog.entity_id == export["id"])]
    db.close()
    assert "Exported a periodic safety report" in events
    assert "Downloaded a safety report export" in events


def test_a_file_gone_from_storage_says_so(app_client, ws):
    from app.models import PvExport
    from app.storage import abs_path

    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"]).json()
    db = _db()
    abs_path(db.get(PvExport, export["id"]).blob_path).unlink()
    db.close()
    gone = app_client.get(f"/api/v1/pv/exports/{export['id']}/download",
                          headers=_auth(ws["token"]))
    assert gone.status_code == 410


def test_the_audit_trail_by_report_and_by_product(app_client, ws):
    _sign(app_client, ws, ws["report_id"])
    _export(app_client, ws, ws["report_id"])
    url = f"/api/v1/pv/reports/{ws['report_id']}/audit"
    report_trail = app_client.get(url, headers=_auth(ws["token"])).json()
    events = [e["event"] for e in report_trail["items"]]
    assert "Signed off a periodic safety report" in events
    assert "Exported a periodic safety report" in events
    assert "Set a safety section status" in events
    assert report_trail["total"] >= len(report_trail["items"])

    product_trail = app_client.get(url, headers=_auth(ws["token"]),
                                   params={"scope": "product"}).json()
    assert "Created a safety product" in [e["event"] for e in product_trail["items"]]
    page = app_client.get(url, headers=_auth(ws["token"]),
                          params={"limit": 1, "offset": 1}).json()
    assert len(page["items"]) == 1 and page["items"][0]["id"] == report_trail["items"][1]["id"]
    bad = app_client.get(url, headers=_auth(ws["token"]), params={"scope": "everything"})
    assert bad.status_code == 422


# ----------------------------------------------------------------- deletion

def test_deleting_the_product_removes_every_export_file(app_client, ws, monkeypatch):
    from app.generation import pdf_renderer
    from app.models import PvExport
    from app.storage import abs_path

    def fake(docx_path, out_dir, language="en"):
        target = out_dir / "rendered-purge.pdf"
        target.write_bytes(b"%PDF-1.4")
        return pdf_renderer.PreviewResult(pdf_path=str(target), renderer="fake")

    monkeypatch.setattr(pdf_renderer, "render_pdf", fake)
    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"], pdf=True).json()
    db = _db()
    paths = [abs_path(f["blob_path"])
             for f in db.get(PvExport, export["id"]).options["files"]]
    db.close()
    assert len(paths) == 2 and all(p.exists() for p in paths)
    deleted = app_client.delete(f"/api/v1/pv/products/{ws['product_id']}",
                                headers=_auth(ws["token"]))
    assert deleted.status_code == 200, deleted.text
    assert not any(p.exists() for p in paths)


def test_deleting_a_report_removes_its_exports(app_client, ws):
    from app.models import PvExport
    from app.storage import abs_path

    _sign(app_client, ws, ws["report_id"])
    export = _export(app_client, ws, ws["report_id"]).json()
    db = _db()
    path = abs_path(db.get(PvExport, export["id"]).blob_path)
    db.close()
    app_client.post(f"/api/v1/pv/reports/{ws['report_id']}/signoff:withdraw",
                    headers=_auth(ws["token"]), json={"reason": "redo"})
    deleted = app_client.delete(f"/api/v1/pv/reports/{ws['report_id']}",
                                headers=_auth(ws["token"]))
    assert deleted.status_code == 200, deleted.text
    assert not path.exists()
    db = _db()
    assert db.get(PvExport, export["id"]) is None
    db.close()


# ------------------------------------------------------------- the module

def _section(code="4", title="Case Series Review"):
    return export_mod.SectionText(code=code, title=title, is_container=False)


def test_the_drafts_own_heading_is_dropped_and_nothing_else():
    s = _section()
    assert export_mod.body_of("4 Case Series Review\n\nText.", s) == "Text."
    assert export_mod.body_of("\n## 4 Case Series Review\nText.", s) == "Text."
    # "4 cases were..." is a sentence, not the heading.
    assert export_mod.body_of("4 cases were reviewed.", s) == "4 cases were reviewed."


def test_citation_stripping_keeps_the_line_a_table_marker_is_on():
    s = _section()
    body = export_mod.body_of("Text [S1].\n[S2]\n[TABLE: line_listing_sar]\nMore.", s)
    assert export_mod.items_of(body)[-2] == ("table", "line_listing_sar")


def test_a_word_diff():
    runs = export_mod.word_diff("found nothing new", "found one thing new")
    assert ("nothing", "del") in runs and ("one thing", "ins") in runs
    assert "".join(t for t, k in runs if k != "del") == "found one thing new"
    assert "".join(t for t, k in runs if k != "ins") == "found nothing new"


def test_diffing_items():
    old = [("text", "Kept."), ("text", "Gone."), ("table", "t"), ("text", "Old words.")]
    new = [("text", "Kept."), ("table", "t"), ("text", "New  words."),
           ("text", "Added.")]
    marked = export_mod.diff_items(old, new)
    assert ("text", "Kept.", None) in marked
    assert ("text", "Gone.", "del") in marked
    assert ("table", "t", None) in marked
    assert ("text", "Added.", "ins") in marked
    changed = next(m for v, m in [(i[1], i[2]) for i in marked] if v == "New  words.")
    assert isinstance(changed, list)
    # Whitespace alone is not a change; a table is never marked.
    assert export_mod.diff_items([("text", "A  b.")], [("text", "A b.")]) == [
        ("text", "A b.", None)]
    assert export_mod.diff_items([], [("table", "t")]) == [("table", "t", None)]


def test_a_body_table_with_nothing_behind_it_is_refused():
    with pytest.raises(export_mod.ExportError):
        export_mod.assemble([export_mod.SectionText(
            code="5", title="Data", is_container=False, content="[TABLE: x]")],
            {"x": "no data"}, front=[], title="T")


def test_the_leak_scan_reads_deleted_text(tmp_path):
    """"Reject all" puts deleted text back on the page."""
    assembled = export_mod.Assembled()
    assembled.add(bp_paragraph("Contact alan.reed@example.com today."), "del")
    path = str(tmp_path / "leak.docx")
    export_mod.write(assembled, path, header="H")
    text = export_mod.document_text(path)
    assert "alan.reed@example.com" in text
    assert export_mod.leaks(text)
    assert export_mod.leaks("Seen by Dr REED.", ["Reed"])
    assert not export_mod.leaks("A bed of reeds.", ["Reed"])


def bp_paragraph(text):
    from app.templates import blueprint as bp

    return bp.paragraph([bp.segment("static", text)])


def test_qc_figures_are_json_safe(app_client, ws):
    from app.models import PvProduct, PvReportInstance

    db = _db()
    try:
        figures = qc.figures_for(db, report=db.get(PvReportInstance, ws["report_id"]),
                                 product=db.get(PvProduct, ws["product_id"]))
    finally:
        db.close()
    import json

    assert json.loads(json.dumps(figures)) == figures
