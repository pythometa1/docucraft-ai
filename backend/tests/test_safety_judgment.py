"""Coding, expectedness and duplicates: three things the machine proposes and
a person decides.

The rule running through all of it is §2's first principle. Seriousness,
expectedness and causality are regulatory determinations; the system may
suggest, with its reasoning, and a qualified person confirms. There is no code
path in the module that writes a confirmed field without one, and these tests
are mostly about proving that.
"""

from datetime import date

import pytest

from app.safety import duplicates as dup
from app.safety import expectedness as exp
from app.safety import meddra


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# =============================================================== the dictionary

def test_a_deployment_with_no_licence_codes_nothing():
    """MedDRA is licensed and this repository does not ship it. The important
    behaviour is the refusal: a guessed preferred term puts an event under the
    wrong System Organ Class inside a total a regulator compares against the
    last report, and nothing about that looks wrong."""
    result = meddra.code_term("severe headache", meddra.Dictionary.EMPTY)
    assert result.coded is False
    assert result.pt is None
    assert "no MedDRA dictionary is loaded" in result.reason


def test_a_loaded_dictionary_codes_an_exact_match():
    dictionary = meddra.load_csv(
        "llt,pt,hlt,soc\nHeadache severe,Headache,Headaches,Nervous system disorders\n",
        version="27.0")
    result = meddra.code_term("Headache severe", dictionary)
    assert result.coded is True
    assert result.pt == "Headache"
    assert result.soc == "Nervous system disorders"
    assert result.version == "27.0"


def test_lookup_ignores_case_and_spacing_and_nothing_else():
    dictionary = meddra.load_csv("llt,pt\nHeadache severe,Headache\n", version="27.0")
    assert meddra.code_term("  headache   SEVERE ", dictionary).coded is True
    # Not a fuzzy match. "headaches" is a different string and stays uncoded.
    assert meddra.code_term("headaches", dictionary).coded is False


def test_a_term_the_dictionary_does_not_hold_says_so_by_name():
    dictionary = meddra.load_csv("llt,pt\nHeadache,Headache\n", version="27.0")
    result = meddra.code_term("funny turn", dictionary)
    assert result.coded is False
    assert "funny turn" in result.reason and "27.0" in result.reason


def test_a_preferred_term_can_be_looked_up_directly():
    dictionary = meddra.load_csv("llt,pt\nCephalgia,Headache\n", version="27.0")
    assert meddra.code_term("Headache", dictionary).pt == "Headache"


def test_a_dictionary_without_the_two_required_columns_is_refused():
    with pytest.raises(ValueError) as raised:
        meddra.load_csv("term,code\nHeadache,10019211\n", version="27.0")
    assert "llt" in str(raised.value)


def test_absent_hierarchy_levels_stay_absent():
    """A missing HLT is not inferred from the PT. An invented hierarchy level
    is a grouping nobody assigned."""
    dictionary = meddra.load_csv("llt,pt\nHeadache severe,Headache\n", version="27.0")
    result = meddra.code_term("Headache severe", dictionary)
    assert result.hlt is None and result.soc is None


def test_the_version_travels_with_every_code():
    """MedDRA changes twice a year and a PT can move between SOCs. An event
    coded under one version and tabulated under another is filed in a hierarchy
    it was never assigned."""
    dictionary = meddra.load_csv("llt,pt\nHeadache,Headache\n", version="26.1")
    assert meddra.code_term("Headache", dictionary).version == "26.1"


# ============================================================== expectedness

class _Event:
    def __init__(self, pt=None):
        self.meddra_pt = pt


class _Rsi:
    id = "rsi-1"
    rsi_type = "ccds"
    version_label = "3.2"


class _Term:
    def __init__(self, pt, soc=None, condition=None):
        self.meddra_pt = pt
        self.meddra_soc = soc
        self.condition_text = condition


def test_a_term_in_the_rsi_is_suggested_listed_with_its_basis():
    terms = {"headache": [_Term("Headache", "Nervous system disorders")]}
    suggestion = exp.suggest(_Event("Headache"), terms=terms, rsi_version=_Rsi())
    assert suggestion.value == exp.LISTED
    assert "CCDS 3.2" in suggestion.basis
    assert suggestion.rsi_version_id == "rsi-1"


def test_a_term_not_in_the_rsi_is_suggested_unlisted():
    suggestion = exp.suggest(_Event("Dizziness"), terms={}, rsi_version=_Rsi())
    assert suggestion.value == exp.UNLISTED
    assert "no matching preferred term" in suggestion.basis


def test_a_qualified_listing_is_not_a_listing():
    """The RSI says "hepatic failure -- serious only". Whether this case meets
    the condition is a judgment; flattening it to a boolean is how an event
    that is unlisted in context reaches the listed column."""
    terms = {"hepatic failure": [_Term("Hepatic failure", condition="serious only")]}
    suggestion = exp.suggest(_Event("Hepatic failure"), terms=terms,
                             rsi_version=_Rsi())
    assert suggestion.value is None
    assert "serious only" in suggestion.basis
    assert "judgment" in suggestion.basis


def test_an_uncoded_event_gets_no_suggestion():
    """Listedness is decided on the preferred term. Matching a reporter's
    phrasing against a controlled vocabulary is not the same question."""
    suggestion = exp.suggest(_Event(None), terms={}, rsi_version=_Rsi())
    assert suggestion.value is None
    assert "no MedDRA preferred term" in suggestion.basis


def test_no_pinned_rsi_means_no_suggestion():
    suggestion = exp.suggest(_Event("Headache"), terms={}, rsi_version=None)
    assert suggestion.value is None
    assert "nothing for the event to be expected against" in suggestion.basis


def test_the_suggestion_json_keeps_its_reasoning():
    terms = {"headache": [_Term("Headache")]}
    stored = exp.suggest(_Event("Headache"), terms=terms,
                         rsi_version=_Rsi()).as_json()
    assert stored["expectedness"]["value"] == "listed"
    assert stored["expectedness"]["basis"]
    assert stored["expectedness"]["rsi_label"] == "CCDS 3.2"


def test_matching_is_case_and_space_insensitive():
    terms = {"headache": [_Term("Headache")]}
    assert exp.suggest(_Event("  HEADACHE "), terms=terms,
                       rsi_version=_Rsi()).value == exp.LISTED


# ================================================================ duplicates

class _Case:
    def __init__(self, id, **kwargs):
        self.id = id
        self.worldwide_case_id = kwargs.get("worldwide_case_id")
        self.local_case_ids = kwargs.get("local_case_ids", [])
        self.country_of_occurrence = kwargs.get("country")
        self.patient_sex = kwargs.get("sex")
        self.patient_age = kwargs.get("age")
        self.primary_reporter_qualification = kwargs.get("reporter")
        self.initial_receipt_date = kwargs.get("received")


class _Ev:
    def __init__(self, pt=None, onset=None):
        self.meddra_pt = pt
        self.verbatim_term = pt
        self.onset_date = onset


class _Drug:
    def __init__(self, name, ours=True):
        self.drug_name = name
        self.is_company_product = ours


def test_a_shared_country_alone_is_not_a_candidate():
    """A product with four thousand headache reports from one country would
    otherwise produce four thousand pairs and a queue nobody reads."""
    left = _Case("a", country="GB")
    right = _Case("b", country="GB")
    assert dup.compare(left, right) is None


def test_agreement_across_independent_fields_is_a_candidate():
    left = _Case("a", country="GB", sex="female", age=34.0,
                 received=date(2026, 3, 4))
    right = _Case("b", country="GB", sex="female", age=34.5,
                  received=date(2026, 3, 4))
    candidate = dup.compare(
        left, right,
        left_events=[_Ev("Headache", date(2026, 3, 1))],
        right_events=[_Ev("Headache", date(2026, 3, 1))])
    assert candidate is not None
    assert candidate.score >= dup.THRESHOLD


def test_the_evidence_is_named_not_scored():
    """A reviewer deciding whether two ICSRs are one case needs what agreed,
    not a number they cannot audit."""
    left = _Case("a", country="GB", sex="female", age=34.0)
    right = _Case("b", country="GB", sex="female", age=34.0)
    candidate = dup.compare(left, right,
                            left_events=[_Ev("Headache", date(2026, 3, 1))],
                            right_events=[_Ev("Headache", date(2026, 3, 1))])
    joined = " ".join(candidate.matched_on).lower()
    assert "gb" in joined
    assert "headache" in joined
    assert "onset" in joined


def test_a_shared_worldwide_identifier_is_nearly_decisive():
    left = _Case("a", worldwide_case_id="GB-1", country="GB")
    right = _Case("b", worldwide_case_id="GB-1", country="GB")
    assert dup.compare(left, right) is not None


def test_ages_a_year_apart_still_match_and_five_years_do_not():
    base = dict(country="GB", sex="female", received=date(2026, 3, 4))
    close = dup.compare(_Case("a", age=34.0, **base), _Case("b", age=34.8, **base),
                        left_events=[_Ev("Headache")], right_events=[_Ev("Headache")])
    assert close is not None
    assert any("within a year" in note for note in close.matched_on)

    far = dup.compare(_Case("a", age=34.0, **base), _Case("b", age=48.0, **base),
                      left_events=[_Ev("Headache")], right_events=[_Ev("Headache")])
    assert far is None or not any("within a year" in n for n in far.matched_on)


def test_only_the_company_product_counts_as_a_shared_drug():
    """Two cases both mentioning paracetamol is not evidence of anything."""
    base = dict(country="GB", sex="female")
    candidate = dup.compare(
        _Case("a", **base), _Case("b", **base),
        left_drugs=[_Drug("Paracetamol", ours=False)],
        right_drugs=[_Drug("Paracetamol", ours=False)])
    assert candidate is None


def test_comparison_is_symmetric():
    left = _Case("a", country="GB", sex="female", age=34.0)
    right = _Case("b", country="GB", sex="female", age=34.0)
    events = [_Ev("Headache", date(2026, 3, 1))]
    forward = dup.compare(left, right, left_events=events, right_events=events)
    backward = dup.compare(right, left, left_events=events, right_events=events)
    assert forward.score == backward.score
    assert sorted(forward.matched_on) == sorted(backward.matched_on)


# ============================================== the roles, through the endpoints

@pytest.fixture
def graded(app_client, two_orgs):
    """A product with two events and an RSI listing one of their terms."""
    from app.db import SessionLocal
    from app.models import (
        PvCase, PvCaseEvent, PvProduct, PvRsiListedTerm, PvRsiVersion,
    )

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Judgment safety", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Judgazine",
        "ibd": "2020-01-01", "dibd": "2016-01-01"}).json()
    rsi = app_client.post(f"/api/v1/pv/products/{product['id']}/rsi-versions",
                          headers=_auth(token),
                          json={"rsi_type": "ccds", "version_label": "3.2"}).json()
    report = app_client.post(
        f"/api/v1/pv/products/{product['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15",
              "rsi_version_id": rsi["id"]}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    db.add(PvRsiListedTerm(org_id=org_id, rsi_version_id=rsi["id"],
                           meddra_pt="Headache", meddra_soc="Nervous system"))
    case = PvCase(org_id=org_id, pv_product_id=product["id"],
                  worldwide_case_id="J-1", initial_receipt_date=date(2026, 3, 4),
                  latest_receipt_date=date(2026, 3, 4),
                  country_of_occurrence="GB", patient_sex="female",
                  patient_age=34.0)
    db.add(case)
    db.flush()
    for pt in ("Headache", "Dizziness"):
        db.add(PvCaseEvent(org_id=org_id, pv_product_id=product["id"],
                           case_id=case.id, verbatim_term=pt.lower(), meddra_pt=pt))
    db.commit()
    db.close()
    return token, product["id"], report["id"], rsi["id"]


def _make_qualified(client, token, product_id):
    members = client.get(f"/api/v1/pv/products/{product_id}/members",
                         headers=_auth(token)).json()
    client.post(f"/api/v1/pv/products/{product_id}/members", headers=_auth(token),
                json={"user_id": members["items"][0]["user_id"],
                      "pv_role": "qualified_person"})


def test_the_suggestion_never_touches_the_confirmed_column(app_client, graded):
    token, product_id, report_id, _rsi = graded
    res = app_client.post(f"/api/v1/pv/reports/{report_id}/suggest-expectedness",
                          headers=_auth(token))
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["counts"]["listed"] == 1      # Headache is in the RSI
    assert body["counts"]["unlisted"] == 1    # Dizziness is not
    assert "until a qualified person confirms" in body["note"]

    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()
    for event in events["items"]:
        assert event["expectedness"] == "not_assessed", "suggested, not confirmed"
        assert event["confirmed_by"] is None
        assert event["suggested"]["expectedness"]["value"] in ("listed", "unlisted")
        assert event["suggested"]["expectedness"]["basis"]
    assert events["summary"]["all_confirmed"] is False


def test_confirming_needs_the_qualified_person_role(app_client, graded):
    token, product_id, report_id, _rsi = graded
    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()["items"]
    refused = app_client.patch(
        f"/api/v1/pv/case-events/{events[0]['id']}/confirm",
        headers=_auth(token), params={"report_instance_id": report_id},
        json={"expectedness": "listed"})
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"]["code"] == "PV_ROLE_REQUIRED"


def test_a_confirmed_expectedness_records_the_version_it_was_made_against(
        app_client, graded):
    token, product_id, report_id, rsi_id = graded
    _make_qualified(app_client, token, product_id)
    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()["items"]
    done = app_client.patch(
        f"/api/v1/pv/case-events/{events[0]['id']}/confirm",
        headers=_auth(token), params={"report_instance_id": report_id},
        json={"expectedness": "listed"})
    assert done.status_code == 200, done.text
    assert done.json()["expectedness"] == "listed"
    assert done.json()["expectedness_rsi_version_id"] == rsi_id
    assert done.json()["confirmed_by"]


def test_expectedness_cannot_be_confirmed_with_no_version_to_be_expected_against(
        app_client, two_orgs):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "No RSI", "function": "Safety", "document_type": "DSUR",
        "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Norsiazine"}).json()
    report = app_client.post(
        f"/api/v1/pv/products/{product['id']}/reports", headers=_auth(token),
        json={"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-07-15"}).json()
    _make_qualified(app_client, token, product["id"])

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id
    case = PvCase(org_id=org_id, pv_product_id=product["id"], worldwide_case_id="N-1")
    db.add(case)
    db.flush()
    event = PvCaseEvent(org_id=org_id, pv_product_id=product["id"],
                        case_id=case.id, meddra_pt="Headache")
    db.add(event)
    db.commit()
    event_id = event.id
    db.close()

    refused = app_client.patch(
        f"/api/v1/pv/case-events/{event_id}/confirm", headers=_auth(token),
        params={"report_instance_id": report["id"]},
        json={"expectedness": "listed"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_NO_RSI_PINNED"


def test_confirming_is_audited_as_a_warning_with_what_changed(app_client, graded):
    from app.db import SessionLocal
    from app.models import AuditLog

    token, product_id, report_id, _rsi = graded
    _make_qualified(app_client, token, product_id)
    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()["items"]
    app_client.patch(f"/api/v1/pv/case-events/{events[0]['id']}/confirm",
                     headers=_auth(token),
                     params={"report_instance_id": report_id},
                     json={"expectedness": "listed", "is_serious": True})

    db = SessionLocal()
    try:
        entries = db.query(AuditLog).filter(
            AuditLog.entity_type == "pv_case_event",
            AuditLog.entity_id == events[0]["id"]).all()
    finally:
        db.close()
    assert entries and entries[0].severity == "warning"
    assert "expectedness" in entries[0].target
    assert "is_serious" in entries[0].target


def test_bulk_confirming_applies_one_determination_to_a_whole_term(
        app_client, graded):
    """Expectedness is a property of a TERM against an RSI version. Confirming
    forty ids is forty chances to be inconsistent."""
    token, product_id, report_id, _rsi = graded
    _make_qualified(app_client, token, product_id)
    res = app_client.post(
        f"/api/v1/pv/products/{product_id}/case-events:bulk-confirm",
        headers=_auth(token),
        json={"meddra_pt": "headache", "expectedness": "listed",
              "report_instance_id": report_id})
    assert res.status_code == 200, res.text
    assert res.json()["confirmed"] == 1

    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()["items"]
    headache = next(e for e in events if e["meddra_pt"] == "Headache")
    assert headache["expectedness"] == "listed"
    dizziness = next(e for e in events if e["meddra_pt"] == "Dizziness")
    assert dizziness["expectedness"] == "not_assessed", "only the named term"


def test_changing_the_pin_leaves_earlier_determinations_stale_and_visible(
        app_client, graded):
    """§11's fifth blocker. An expectedness confirmed against CCDS 3.2 was a
    judgment about 3.2, and re-pointing it at 3.3 would record a decision
    nobody made -- so it is flagged instead."""
    token, product_id, report_id, _rsi = graded
    _make_qualified(app_client, token, product_id)
    events = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token)).json()["items"]
    app_client.patch(f"/api/v1/pv/case-events/{events[0]['id']}/confirm",
                     headers=_auth(token),
                     params={"report_instance_id": report_id},
                     json={"expectedness": "listed"})

    newer = app_client.post(f"/api/v1/pv/products/{product_id}/rsi-versions",
                            headers=_auth(token),
                            json={"rsi_type": "ccds", "version_label": "3.3"}).json()
    app_client.patch(f"/api/v1/pv/reports/{report_id}", headers=_auth(token),
                     json={"rsi_version_id": newer["id"]})

    listed = app_client.get(f"/api/v1/pv/products/{product_id}/case-events",
                            headers=_auth(token),
                            params={"report_instance_id": report_id}).json()
    assert len(listed["stale_expectedness"]) == 1
    assert listed["stale_expectedness"][0]["meddra_pt"] == "Headache"


def test_coding_without_a_licence_reports_the_reason(app_client, graded):
    token, product_id, _report, _rsi = graded
    res = app_client.post(f"/api/v1/pv/products/{product_id}/code",
                          headers=_auth(token))
    assert res.status_code == 202
    body = res.json()
    assert body["dictionary_loaded"] is False


def test_duplicates_are_found_and_never_merged_automatically(app_client, graded):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, product_id, _report, _rsi = graded
    db = SessionLocal()
    org_id = db.get(PvProduct, product_id).org_id
    twin = PvCase(org_id=org_id, pv_product_id=product_id,
                  worldwide_case_id="J-1-DUP", country_of_occurrence="GB",
                  patient_sex="female", patient_age=34.0,
                  initial_receipt_date=date(2026, 3, 4),
                  latest_receipt_date=date(2026, 3, 4))
    db.add(twin)
    db.flush()
    db.add(PvCaseEvent(org_id=org_id, pv_product_id=product_id, case_id=twin.id,
                       verbatim_term="headache", meddra_pt="Headache"))
    db.commit()
    db.close()

    found = app_client.post(f"/api/v1/pv/products/{product_id}/duplicates:detect",
                            headers=_auth(token))
    assert found.status_code == 202, found.text
    assert "Nothing is merged without a person" in found.json()["note"]

    listed = app_client.get(f"/api/v1/pv/products/{product_id}/duplicates",
                            headers=_auth(token)).json()
    assert listed["items"], "the pair should be a candidate"
    assert all(item["status"] == "pending" for item in listed["items"])
    assert listed["items"][0]["matched_on"]

    # Both cases are still there. Detection decides nothing.
    cases = app_client.get(f"/api/v1/pv/products/{product_id}/cases",
                           headers=_auth(token)).json()
    assert cases["total"] == 2


def test_merging_needs_the_qualified_person_role_and_a_survivor(
        app_client, graded):
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, product_id, _report, _rsi = graded
    db = SessionLocal()
    org_id = db.get(PvProduct, product_id).org_id
    twin = PvCase(org_id=org_id, pv_product_id=product_id,
                  worldwide_case_id="J-1-DUP", country_of_occurrence="GB",
                  patient_sex="female", patient_age=34.0,
                  initial_receipt_date=date(2026, 3, 4),
                  latest_receipt_date=date(2026, 3, 4))
    db.add(twin)
    db.flush()
    db.add(PvCaseEvent(org_id=org_id, pv_product_id=product_id, case_id=twin.id,
                       verbatim_term="headache", meddra_pt="Headache"))
    db.commit()
    db.close()
    app_client.post(f"/api/v1/pv/products/{product_id}/duplicates:detect",
                    headers=_auth(token))
    pair = app_client.get(f"/api/v1/pv/products/{product_id}/duplicates",
                          headers=_auth(token)).json()["items"][0]

    refused = app_client.post(f"/api/v1/pv/duplicates/{pair['id']}/resolve",
                              headers=_auth(token),
                              json={"action": "merged",
                                    "keep_case_id": pair["case"]["id"]})
    assert refused.status_code == 403

    _make_qualified(app_client, token, product_id)
    no_survivor = app_client.post(f"/api/v1/pv/duplicates/{pair['id']}/resolve",
                                  headers=_auth(token), json={"action": "merged"})
    assert no_survivor.status_code == 422
    assert no_survivor.json()["detail"]["error"]["code"] == "PV_MERGE_NEEDS_SURVIVOR"

    done = app_client.post(f"/api/v1/pv/duplicates/{pair['id']}/resolve",
                           headers=_auth(token),
                           json={"action": "merged",
                                 "keep_case_id": pair["case"]["id"]})
    assert done.status_code == 200
    cases = app_client.get(f"/api/v1/pv/products/{product_id}/cases",
                           headers=_auth(token)).json()
    assert cases["total"] == 1
    # The merged case's identifier survives on the winner, so it can still be
    # found by the number the other source used.
    survivor = cases["items"][0]
    assert pair["other_case"]["worldwide_case_id"] in survivor["local_case_ids"]


def test_keeping_both_resolves_the_pair_without_touching_the_cases(
        app_client, graded):
    from app.db import SessionLocal
    from app.models import PvCase, PvProduct

    token, product_id, _report, _rsi = graded
    db = SessionLocal()
    org_id = db.get(PvProduct, product_id).org_id
    db.add(PvCase(org_id=org_id, pv_product_id=product_id,
                  worldwide_case_id="J-1", country_of_occurrence="GB",
                  patient_sex="female", patient_age=34.0,
                  initial_receipt_date=date(2026, 3, 4)))
    db.commit()
    db.close()
    app_client.post(f"/api/v1/pv/products/{product_id}/duplicates:detect",
                    headers=_auth(token))
    pairs = app_client.get(f"/api/v1/pv/products/{product_id}/duplicates",
                           headers=_auth(token)).json()["items"]
    if not pairs:
        pytest.skip("no candidate produced for this fixture")
    done = app_client.post(f"/api/v1/pv/duplicates/{pairs[0]['id']}/resolve",
                           headers=_auth(token), json={"action": "kept_both"})
    assert done.status_code == 200
    assert app_client.get(f"/api/v1/pv/products/{product_id}/cases",
                          headers=_auth(token)).json()["total"] == 2


def test_a_resolved_pair_is_not_raised_again(app_client, graded):
    from app.db import SessionLocal
    from app.models import PvCase, PvProduct

    token, product_id, _report, _rsi = graded
    db = SessionLocal()
    org_id = db.get(PvProduct, product_id).org_id
    db.add(PvCase(org_id=org_id, pv_product_id=product_id,
                  worldwide_case_id="J-1", country_of_occurrence="GB",
                  patient_sex="female", patient_age=34.0,
                  initial_receipt_date=date(2026, 3, 4)))
    db.commit()
    db.close()
    app_client.post(f"/api/v1/pv/products/{product_id}/duplicates:detect",
                    headers=_auth(token))
    first = app_client.get(f"/api/v1/pv/products/{product_id}/duplicates",
                           headers=_auth(token)).json()["items"]
    if not first:
        pytest.skip("no candidate produced for this fixture")
    app_client.post(f"/api/v1/pv/duplicates/{first[0]['id']}/resolve",
                    headers=_auth(token), json={"action": "kept_both"})
    again = app_client.post(f"/api/v1/pv/products/{product_id}/duplicates:detect",
                            headers=_auth(token)).json()
    assert again["new"] == 0, "a pair somebody has answered is not asked again"


# ---------------------------------------------------- the remaining branches

def test_an_empty_dictionary_reports_its_size_and_its_emptiness():
    assert len(meddra.Dictionary.EMPTY) == 0
    assert meddra.Dictionary.EMPTY.loaded is False
    dictionary = meddra.load_csv("llt,pt\nHeadache,Headache\n", version="27.0")
    assert len(dictionary) == 1 and dictionary.loaded is True


def test_an_event_with_no_reported_term_is_not_coded():
    dictionary = meddra.load_csv("llt,pt\nHeadache,Headache\n", version="27.0")
    result = meddra.code_term("   ", dictionary)
    assert result.coded is False
    assert "no reported term" in result.reason


def test_a_dictionary_row_missing_a_term_is_skipped_not_guessed():
    dictionary = meddra.load_csv(
        "llt,pt\nHeadache,Headache\n,Orphaned\nDizzy,\n", version="27.0")
    assert len(dictionary) == 1


def test_a_dictionary_file_with_no_header_is_refused():
    with pytest.raises(ValueError):
        meddra.load_csv("", version="27.0")


def test_the_dictionary_hook_defaults_to_empty():
    """A deployment holding a licence wires its own loading in. The default
    codes nothing, which is honest rather than confidently wrong."""
    assert meddra.dictionary_for(None, "org", None).loaded is False


def test_stale_determinations_are_empty_when_no_rsi_is_pinned():
    from types import SimpleNamespace

    report = SimpleNamespace(rsi_version_id=None, pv_product_id="p")
    assert exp.stale_determinations(None, report) == []


def test_a_shared_local_identifier_is_strong_evidence():
    left = _Case("a", local_case_ids=["ACME-77"], country="GB")
    right = _Case("b", local_case_ids=["ACME-77"], country="GB")
    candidate = dup.compare(left, right)
    assert candidate is not None
    assert any("local identifier" in note for note in candidate.matched_on)


def test_the_same_reporter_qualification_is_only_a_hint():
    left = _Case("a", reporter="physician")
    right = _Case("b", reporter="physician")
    assert dup.compare(left, right) is None


def test_a_shared_company_product_counts_but_does_not_carry_a_pair_alone():
    """Country, sex, age and the same product score 0.5 -- below the threshold,
    and rightly: a product with thousands of reports has many 34-year-old women
    in one country taking it. With a shared term as well, the pair is worth a
    person's time, and the product is named in the evidence."""
    base = dict(country="GB", sex="female", age=34.0)
    thin = dup.compare(
        _Case("a", **base), _Case("b", **base),
        left_drugs=[_Drug("Vigilazine")], right_drugs=[_Drug("Vigilazine")])
    assert thin is None

    candidate = dup.compare(
        _Case("a", **base), _Case("b", **base),
        left_events=[_Ev("Headache")], right_events=[_Ev("Headache")],
        left_drugs=[_Drug("Vigilazine")], right_drugs=[_Drug("Vigilazine")])
    assert candidate is not None
    assert any("suspect product" in note for note in candidate.matched_on)


def test_a_candidate_serialises_for_the_review_screen():
    left = _Case("a", country="GB", sex="female", age=34.0)
    right = _Case("b", country="GB", sex="female", age=34.0)
    candidate = dup.compare(left, right, left_events=[_Ev("Headache")],
                            right_events=[_Ev("Headache")])
    row = candidate.as_row()
    assert row["case_id"] == "a" and row["other_case_id"] == "b"
    assert isinstance(row["score"], float)
    assert row["matched_on"]


def test_finding_duplicates_in_a_store_of_one_case_is_empty(app_client, graded):
    from app.db import SessionLocal

    _token, product_id, _report, _rsi = graded
    db = SessionLocal()
    try:
        from app.models import PvProduct

        org_id = db.get(PvProduct, product_id).org_id
        assert dup.find(db, pv_product_id=product_id, org_id=org_id) == []
    finally:
        db.close()
