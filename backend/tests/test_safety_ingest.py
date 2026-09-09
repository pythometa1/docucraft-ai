"""The ingestion pipeline, and where it deliberately stops.

The most important assertion in this file is a negative one: after ingesting a
narrative full of patient and investigator names, there is no `pv_chunks` row,
no embedding, and no model call. §2's fourth principle makes masking a stage
that runs BEFORE indexing, and M3 is where that stage is built -- so M2 parses,
stores, and halts at `awaiting_deid`.

A pipeline that indexed now and masked at M3 would mean every case ingested in
between had its identifiers embedded into a vector store, which is a much
harder thing to undo than never having done it.
"""

import io
import time

import pytest

from app.safety import ingest as ingest_mod
from tests.test_safety_e2b import R2


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


LISTING = (
    "Case Number,Initial Receipt Date,Country,Serious,Preferred Term,Suspect Drug\n"
    "GB-100,2026-03-04,GB,Yes,Headache,Vigilazine\n"
    "GB-100,2026-03-04,GB,Yes,Nausea,Vigilazine\n"
    "GB-101,2026-04-11,DE,No,Rash,Vigilazine\n"
)

MAPPING = {
    "Case Number": "worldwide_case_id",
    "Initial Receipt Date": "initial_receipt_date",
    "Country": "country_of_occurrence",
    "Serious": "is_serious",
    "Preferred Term": "meddra_pt",
    "Suspect Drug": "drug_name",
}


@pytest.fixture
def product(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Ingest safety", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    created = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Vigilazine",
        "inn": "vigilazine", "ibd": "2020-03-01", "dibd": "2016-09-01"}).json()
    return token, created["id"]


def _upload(client, token, product_id, files):
    payload = [("files", (name, io.BytesIO(data), mime)) for name, data, mime, _i in files]
    payload += [("doc_types", (None, "other")) for _f in files]
    payload += [("input_types", (None, input_type)) for _n, _d, _m, input_type in files]
    res = client.post(f"/api/v1/pv/products/{product_id}/documents",
                      headers=_auth(token), files=payload)
    assert res.status_code == 201, res.text
    return res.json()["items"]


def _process(client, token, product_id, mappings=None):
    res = client.post(f"/api/v1/pv/products/{product_id}/process",
                      headers=_auth(token), json={"mappings": mappings or {}})
    assert res.status_code == 202, res.text
    for _ in range(200):
        status = client.get(f"/api/v1/pv/products/{product_id}/processing-status",
                            headers=_auth(token)).json()
        if not status["in_flight"]:
            return status
        time.sleep(0.05)
    raise AssertionError("processing never settled")


# ------------------------------------------------------------------ E2B in

@pytest.fixture
def ingested_icsr(app_client, product):
    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("icsr.xml", R2.encode(), "text/xml", "e2b_r3_xml")])
    status = _process(app_client, token, product_id)
    return token, product_id, documents[0], status


def test_an_icsr_becomes_a_case_with_its_events_and_drugs(app_client, ingested_icsr):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseDrug, PvCaseEvent

    token, product_id, _document, _status = ingested_icsr
    db = SessionLocal()
    try:
        case = db.query(PvCase).filter(
            PvCase.pv_product_id == product_id,
            PvCase.worldwide_case_id == "GB-ACME-2026001").one()
        assert case.is_serious is True
        assert case.seriousness_criteria == ["hospitalisation"]
        assert case.patient_sex == "female"
        assert case.imported_from == "e2b_r3_xml"
        events = db.query(PvCaseEvent).filter(PvCaseEvent.case_id == case.id).all()
        assert {e.meddra_pt for e in events} == {"Headache", "Nausea"}
        drugs = db.query(PvCaseDrug).filter(PvCaseDrug.case_id == case.id).all()
        assert any(d.is_company_product for d in drugs)
    finally:
        db.close()


def test_nothing_arrives_confirmed(app_client, ingested_icsr):
    """A confirmed field is a person's determination. Ingestion produces data;
    it produces no judgments, so nothing it writes counts anywhere yet."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent

    _token, product_id, _document, _status = ingested_icsr
    db = SessionLocal()
    try:
        cases = db.query(PvCase).filter(PvCase.pv_product_id == product_id).all()
        assert cases and all(c.confirmed_by is None for c in cases)
        events = db.query(PvCaseEvent).filter(
            PvCaseEvent.pv_product_id == product_id).all()
        assert all(e.confirmed_by is None for e in events)
        assert all(e.expectedness == "not_assessed" for e in events)
        assert all(e.causality_company is None for e in events)
        assert all(e.suggested_by_system_json == {} for e in events)
    finally:
        db.close()


def test_an_uncoded_event_is_flagged_for_coding(app_client, product):
    from app.db import SessionLocal
    from app.models import PvCaseEvent

    token, product_id = product
    minimal = ("""<?xml version="1.0"?><ichicsr><safetyreport>
      <safetyreportid>UNCODED-1</safetyreportid><receiptdate>20260301</receiptdate>
      <patient><reaction><primarysourcereaction>funny turn</primarysourcereaction>
      </reaction></patient></safetyreport></ichicsr>""")
    _upload(app_client, token, product_id,
            [("u.xml", minimal.encode(), "text/xml", "e2b_r3_xml")])
    _process(app_client, token, product_id)

    db = SessionLocal()
    try:
        event = db.query(PvCaseEvent).filter(
            PvCaseEvent.pv_product_id == product_id,
            PvCaseEvent.verbatim_term == "funny turn").one()
        assert event.meddra_pt is None
        assert event.coding_required is True
    finally:
        db.close()


# ---------------------------------------------------------- the deid gate

def test_a_parsed_source_stops_at_the_deidentification_gate(app_client, ingested_icsr):
    _token, _product_id, document, status = ingested_icsr
    parsed = next(d for d in status["items"] if d["id"] == document["id"])
    assert parsed["processing_status"] == ingest_mod.AWAITING_DEID
    assert parsed["case_count"] == 1
    assert status["deid_gate"]["cleared"] is False
    assert status["deid_gate"]["documents_waiting"] == 1


def test_nothing_is_chunked_or_embedded_before_masking(app_client, ingested_icsr):
    """The assertion this milestone exists around.

    The ingested narrative contains a patient name and a hospital name. Neither
    may reach a chunk, an embedding, or a model, because nothing has masked
    them -- and an identifier embedded into a vector store is far harder to
    remove than one that was never put there.
    """
    from app.db import SessionLocal
    from app.models import PvChunk

    _token, product_id, _document, _status = ingested_icsr
    db = SessionLocal()
    try:
        assert db.query(PvChunk).filter(
            PvChunk.pv_product_id == product_id).count() == 0
    finally:
        db.close()


def test_the_narrative_goes_to_the_store_nothing_reads(app_client, ingested_icsr):
    """`pv_case_originals`, not `pv_case_narratives`. That table's column is
    called `raw_text_redacted`, and there is nothing yet that redacts."""
    from app.db import SessionLocal
    from app.models import PvCaseNarrative, PvCaseOriginal

    _token, product_id, _document, _status = ingested_icsr
    db = SessionLocal()
    try:
        originals = db.query(PvCaseOriginal).filter(
            PvCaseOriginal.pv_product_id == product_id).all()
        assert len(originals) == 1
        assert "Jane Smith" in originals[0].content
        assert originals[0].kind == "narrative"
        # And the masked working copy does not exist, because nothing masked it.
        assert db.query(PvCaseNarrative).filter(
            PvCaseNarrative.pv_product_id == product_id).count() == 0
    finally:
        db.close()


def test_every_case_is_pending_de_identification(app_client, ingested_icsr):
    from app.db import SessionLocal
    from app.models import PvCase

    _token, product_id, _document, _status = ingested_icsr
    db = SessionLocal()
    try:
        cases = db.query(PvCase).filter(PvCase.pv_product_id == product_id).all()
        assert all(c.deidentification_status == "pending" for c in cases)
    finally:
        db.close()


def test_the_status_says_what_has_not_been_built_yet(app_client, ingested_icsr):
    """Reporting a parsed source as "done" would tell somebody their sources
    were ready when what is ready is half a pipeline."""
    _token, _product_id, _document, status = ingested_icsr
    note = status["deid_gate"]["note"]
    assert "not built yet" in note
    assert "indexed" in note and "embedded" in note


# --------------------------------------------------------- the line listing

def test_a_line_listing_needs_a_mapping_before_it_is_read(app_client, product):
    """Refused at the endpoint rather than in the worker, so the answer arrives
    while somebody is still looking at the screen that would fix it."""
    token, product_id = product
    _upload(app_client, token, product_id,
            [("listing.csv", LISTING.encode(), "text/csv", "line_listing")])
    refused = app_client.post(f"/api/v1/pv/products/{product_id}/process",
                              headers=_auth(token), json={"mappings": {}})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_MAPPING_REQUIRED"


def test_a_mapped_line_listing_becomes_cases(app_client, product):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent

    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("listing.csv", LISTING.encode(), "text/csv", "line_listing")])
    status = _process(app_client, token, product_id,
                      mappings={documents[0]["id"]: MAPPING})
    parsed = status["items"][0]
    assert parsed["processing_status"] == ingest_mod.AWAITING_DEID, parsed
    assert parsed["case_count"] == 2, "three rows, two cases"

    db = SessionLocal()
    try:
        case = db.query(PvCase).filter(
            PvCase.worldwide_case_id == "GB-100",
            PvCase.pv_product_id == product_id).one()
        events = db.query(PvCaseEvent).filter(PvCaseEvent.case_id == case.id).all()
        assert len(events) == 2
        assert case.is_serious is True
    finally:
        db.close()


def test_the_columns_endpoint_suggests_a_mapping(app_client, product):
    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("listing.csv", LISTING.encode(), "text/csv", "line_listing")])
    columns = app_client.get(f"/api/v1/pv/documents/{documents[0]['id']}/columns",
                             headers=_auth(token))
    assert columns.status_code == 200, columns.text
    body = columns.json()
    assert body["headers"][0] == "Case Number"
    assert body["row_count"] == 3
    suggested = {s["column"]: s["field"] for s in body["suggestions"]}
    assert suggested["Case Number"] == "worldwide_case_id"
    assert "worldwide_case_id" in body["fields"]


def test_asking_for_columns_of_something_that_is_not_a_listing_is_refused(
        app_client, product):
    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("icsr.xml", R2.encode(), "text/xml", "e2b_r3_xml")])
    refused = app_client.get(f"/api/v1/pv/documents/{documents[0]['id']}/columns",
                             headers=_auth(token))
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_NOT_A_LINE_LISTING"


def test_rows_that_could_not_be_read_are_reported_on_the_document(app_client, product):
    """Half a listing loaded in silence is the failure this reports around."""
    token, product_id = product
    ragged = LISTING + "\n,2026-05-05,FR,Yes,Fever,Vigilazine\n"
    documents = _upload(app_client, token, product_id,
                        [("ragged.csv", ragged.encode(), "text/csv", "line_listing")])
    status = _process(app_client, token, product_id,
                      mappings={documents[0]["id"]: MAPPING})
    parsed = status["items"][0]
    assert "were not read" in (parsed["error_message"] or "")
    assert "case identifier" in parsed["error_message"]


# ------------------------------------------------------------- follow-ups

def test_a_follow_up_updates_the_case_rather_than_adding_a_second(
        app_client, product):
    """Two rows for one case is that case counted twice in every tabulation."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent

    token, product_id = product
    _upload(app_client, token, product_id,
            [("v1.xml", R2.encode(), "text/xml", "e2b_r3_xml")])
    _process(app_client, token, product_id)

    follow_up = R2.replace("<safetyreportversion>2</safetyreportversion>",
                           "<safetyreportversion>3</safetyreportversion>") \
                  .replace("<receiptdate>20260318</receiptdate>",
                           "<receiptdate>20260420</receiptdate>")
    _upload(app_client, token, product_id,
            [("v3.xml", follow_up.encode(), "text/xml", "e2b_r3_xml")])
    _process(app_client, token, product_id)

    db = SessionLocal()
    try:
        cases = db.query(PvCase).filter(
            PvCase.pv_product_id == product_id,
            PvCase.worldwide_case_id == "GB-ACME-2026001").all()
        assert len(cases) == 1
        assert cases[0].case_version == 3
        assert str(cases[0].latest_receipt_date) == "2026-04-20"
        # Events were replaced, not appended: a follow-up restates the case.
        assert db.query(PvCaseEvent).filter(
            PvCaseEvent.case_id == cases[0].id).count() == 2
    finally:
        db.close()


def test_an_older_version_does_not_roll_the_case_backwards(app_client, product):
    from app.db import SessionLocal
    from app.models import PvCase

    token, product_id = product
    _upload(app_client, token, product_id,
            [("v2.xml", R2.encode(), "text/xml", "e2b_r3_xml")])
    _process(app_client, token, product_id)

    older = R2.replace("<safetyreportversion>2</safetyreportversion>",
                       "<safetyreportversion>1</safetyreportversion>") \
              .replace("<receiptdate>20260318</receiptdate>",
                       "<receiptdate>20260210</receiptdate>")
    _upload(app_client, token, product_id,
            [("v1.xml", older.encode(), "text/xml", "e2b_r3_xml")])
    _process(app_client, token, product_id)

    db = SessionLocal()
    try:
        case = db.query(PvCase).filter(
            PvCase.pv_product_id == product_id,
            PvCase.worldwide_case_id == "GB-ACME-2026001").one()
        assert case.case_version == 2
        assert str(case.latest_receipt_date) == "2026-03-18"
    finally:
        db.close()


# -------------------------------------------------------------- the refusals

def test_an_input_type_that_cannot_be_that_file_is_refused_at_upload(
        app_client, product):
    """An E2B export is XML and a line listing is a spreadsheet. Saying so at
    upload is cheaper than a parser failure twenty files later, and the message
    can name what was expected."""
    token, product_id = product
    res = app_client.post(
        f"/api/v1/pv/products/{product_id}/documents", headers=_auth(token),
        files=[("files", ("listing.csv", io.BytesIO(LISTING.encode()), "text/csv")),
               ("doc_types", (None, "other")),
               ("input_types", (None, "e2b_r3_xml"))])
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "PV_INPUT_TYPE_MISMATCH"


def test_an_unreadable_icsr_fails_its_own_row_and_no_others(app_client, product):
    token, product_id = product
    _upload(app_client, token, product_id, [
        ("good.xml", R2.encode(), "text/xml", "e2b_r3_xml"),
        ("bad.xml", b"<ichicsr><safetyreport>", "text/xml", "e2b_r3_xml"),
    ])
    status = _process(app_client, token, product_id)
    by_name = {d["filename"]: d for d in status["items"]}
    assert by_name["good.xml"]["processing_status"] == ingest_mod.AWAITING_DEID
    assert by_name["bad.xml"]["processing_status"] == ingest_mod.FAILED
    assert "well-formed" in by_name["bad.xml"]["error_message"]


def test_retagging_a_source_sends_it_back_to_the_queue(app_client, ingested_icsr):
    """Retagging changes which pipeline the file goes through, so whatever the
    last one produced is no longer what this file says."""
    token, _product_id, document, _status = ingested_icsr
    retagged = app_client.patch(f"/api/v1/pv/documents/{document['id']}",
                                headers=_auth(token), json={"doc_type": "rsi_doc"})
    assert retagged.status_code == 200
    assert retagged.json()["processing_status"] == ingest_mod.QUEUED


def test_deleting_a_source_takes_its_cases_with_it(app_client, ingested_icsr):
    """An untraceable case in a safety report is worse than no case, because it
    will still be counted."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvCaseOriginal

    token, product_id, document, _status = ingested_icsr
    deleted = app_client.delete(f"/api/v1/pv/documents/{document['id']}",
                                headers=_auth(token))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["purged"]["cases"] == 1

    db = SessionLocal()
    try:
        assert db.query(PvCase).filter(
            PvCase.pv_product_id == product_id).count() == 0
        assert db.query(PvCaseEvent).filter(
            PvCaseEvent.pv_product_id == product_id).count() == 0
        assert db.query(PvCaseOriginal).filter(
            PvCaseOriginal.pv_product_id == product_id).count() == 0
    finally:
        db.close()


# ------------------------------------------------------- mapping profiles

def test_a_mapping_profile_is_validated_before_it_is_stored(app_client, product):
    """A profile saved broken is a profile that fails a quarter later, on
    somebody else's shift."""
    token, product_id = product
    refused = app_client.post(
        f"/api/v1/pv/products/{product_id}/mapping-profiles", headers=_auth(token),
        json={"name": "Argus", "column_map": {"Term": "meddra_pt"}})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_BAD_MAPPING"
    assert any("cannot be grouped" in p
               for p in refused.json()["detail"]["error"]["details"]["problems"])


def test_a_saved_profile_comes_back_for_the_next_cycle(app_client, product):
    token, product_id = product
    saved = app_client.post(
        f"/api/v1/pv/products/{product_id}/mapping-profiles", headers=_auth(token),
        json={"name": "Argus export", "source_system": "Argus",
              "column_map": MAPPING})
    assert saved.status_code == 201, saved.text
    listed = app_client.get(f"/api/v1/pv/products/{product_id}/mapping-profiles",
                            headers=_auth(token)).json()
    profile = next(p for p in listed["items"] if p["name"] == "Argus export")
    assert profile["column_map"]["Case Number"] == "worldwide_case_id"
    assert profile["shared"] is False


def test_the_case_listing_badges_each_case_against_a_report(app_client, product):
    """The badge comes from the same scope layer the figures do."""
    token, product_id = product
    _upload(app_client, token, product_id,
            [("listing.csv", LISTING.encode(), "text/csv", "line_listing")])
    documents = app_client.get(f"/api/v1/pv/products/{product_id}/documents",
                               headers=_auth(token)).json()["items"]
    _process(app_client, token, product_id,
             mappings={documents[0]["id"]: MAPPING})

    # Locked 15 April: the March case is in the interval, and the April one is
    # outside the period but inside the cumulative window.
    late_lock = app_client.post(
        f"/api/v1/pv/products/{product_id}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-03-31", "data_lock_point": "2026-04-15"}).json()
    cases = app_client.get(f"/api/v1/pv/products/{product_id}/cases",
                           headers=_auth(token),
                           params={"report_instance_id": late_lock["id"]}).json()
    badges = {c["worldwide_case_id"]: c["scope"] for c in cases["items"]}
    assert badges["GB-100"] == "interval"      # received 4 March, inside the period
    assert badges["GB-101"] == "cumulative"    # 11 April: after the period, before the lock
    assert cases["total"] == 2

    # The same two cases against a report locked on 1 April: the April case is
    # now on the far side of the lock and contributes to nothing.
    early_lock = app_client.post(
        f"/api/v1/pv/products/{product_id}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-03-31", "data_lock_point": "2026-04-01"}).json()
    cases = app_client.get(f"/api/v1/pv/products/{product_id}/cases",
                           headers=_auth(token),
                           params={"report_instance_id": early_lock["id"]}).json()
    badges = {c["worldwide_case_id"]: c["scope"] for c in cases["items"]}
    assert badges["GB-100"] == "interval"
    assert badges["GB-101"] == "after_lock"


# ------------------------------------------------- the other two input types

def test_a_narrative_document_becomes_a_case_with_its_text_held_apart(
        app_client, product):
    """A CIOMS form or a narrative document has no structured fields to read.
    Its text goes to the access-controlled store, and the case it creates says
    out loud that its identifier has to be entered by hand -- rather than being
    given a generated one that would look like a real case number."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseOriginal

    token, product_id = product
    text = (b"CIOMS I\nPatient: Jane Smith, 34F.\n"
            b"Reporter: Dr Alan Reed, St Mary's Hospital.\n"
            b"Severe headache following the second dose.\n")
    documents = _upload(app_client, token, product_id,
                        [("cioms.txt", text, "text/plain", "cioms_form")])
    status = _process(app_client, token, product_id)
    parsed = next(d for d in status["items"] if d["id"] == documents[0]["id"])
    assert parsed["processing_status"] == ingest_mod.AWAITING_DEID
    assert parsed["case_count"] == 1

    db = SessionLocal()
    try:
        case = db.query(PvCase).filter(
            PvCase.pv_product_id == product_id,
            PvCase.imported_from == "cioms_form").one()
        assert case.worldwide_case_id is None
        original = db.query(PvCaseOriginal).filter(
            PvCaseOriginal.case_id == case.id).one()
        assert original.kind == "document_text"
        assert "Jane Smith" in original.content
    finally:
        db.close()


def test_a_supporting_document_is_read_for_its_shape_and_then_stops(
        app_client, product):
    """A previous report or an RSI has no cases in it. Its text is read now so
    that an unreadable file is known now rather than at M6 -- and then it stops
    at the same gate, because §6 puts investigator and site names in exactly
    these files."""
    from app.db import SessionLocal
    from app.models import PvCase, PvChunk

    token, product_id = product
    documents = _upload(
        app_client, token, product_id,
        [("previous.txt", b"Previous DSUR, period 2025.\nSection 1 Introduction.\n",
          "text/plain", "document")])
    status = _process(app_client, token, product_id)
    parsed = next(d for d in status["items"] if d["id"] == documents[0]["id"])
    assert parsed["processing_status"] == ingest_mod.AWAITING_DEID
    assert parsed["case_count"] == 0, "a supporting document holds no cases"

    db = SessionLocal()
    try:
        assert db.query(PvCase).filter(
            PvCase.pv_product_id == product_id).count() == 0
        assert db.query(PvChunk).filter(
            PvChunk.pv_product_id == product_id).count() == 0
    finally:
        db.close()


def test_a_document_with_no_readable_text_fails_with_a_reason(app_client, product):
    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("blank.txt", b"   \n  \n", "text/plain", "document")])
    status = _process(app_client, token, product_id)
    parsed = next(d for d in status["items"] if d["id"] == documents[0]["id"])
    assert parsed["processing_status"] == ingest_mod.FAILED
    assert "no readable text" in parsed["error_message"]


def test_retrying_a_failed_source_needs_its_mapping_too(app_client, product):
    token, product_id = product
    documents = _upload(app_client, token, product_id,
                        [("listing.csv", LISTING.encode(), "text/csv", "line_listing")])
    refused = app_client.post(f"/api/v1/pv/documents/{documents[0]['id']}/retry",
                              headers=_auth(token), json={"mappings": {}})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "PV_MAPPING_REQUIRED"

    accepted = app_client.post(f"/api/v1/pv/documents/{documents[0]['id']}/retry",
                               headers=_auth(token),
                               json={"mappings": {documents[0]["id"]: MAPPING}})
    assert accepted.status_code == 202


def test_processing_with_nothing_queued_is_not_an_error(app_client, ingested_icsr):
    token, product_id, _document, _status = ingested_icsr
    again = app_client.post(f"/api/v1/pv/products/{product_id}/process",
                            headers=_auth(token), json={"mappings": {}})
    assert again.status_code == 202
    assert again.json()["queued"] == 0
