"""M9: the signal log, the screening statistic, case-bound narratives and
regional appendices.

The two rules the signal work is held to:

* a screen never creates a signal -- it returns figures, a person raises a
  candidate, and a reviewer decides whether the candidate is a signal;
* the statistic is labelled as a screening statistic every time it appears.

The arithmetic is checked against numbers worked by hand, because a PRR that
is quietly an ROR (or the other way round) looks entirely plausible.
"""

import math
from datetime import date

import pytest

from app.safety import signals as sig
from app.safety import trees


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _product(app_client, token, name, *, ibd="2020-01-01", qualify=True):
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": f"{name} portal", "function": "Safety", "document_type": "PBRER",
        "region": "Global", "language": "English"}).json()
    body = {"project_id": portal["id"], "product_name": name, "dibd": "2016-01-01"}
    if ibd:
        body["ibd"] = ibd
    product = app_client.post("/api/v1/pv/products", headers=_auth(token),
                              json=body).json()
    if qualify:
        _grant(app_client, token, product["id"], "qualified_person")
    return product["id"]


def _grant(app_client, token, product_id, role):
    members = app_client.get(f"/api/v1/pv/products/{product_id}/members",
                             headers=_auth(token)).json()
    app_client.post(f"/api/v1/pv/products/{product_id}/members", headers=_auth(token),
                    json={"user_id": members["items"][0]["user_id"], "pv_role": role})


def _set_role(product_id, role):
    from app.models import PvMember

    db = _db()
    for member in db.query(PvMember).filter(PvMember.pv_product_id == product_id):
        member.pv_role = role
    db.commit()
    db.close()


def _cases(product_id, terms_per_case, *, received=date(2026, 3, 1), confirmed=True):
    """One case per entry; each entry is the terms that case reports."""
    from app.models import PvCase, PvCaseEvent, PvProduct

    db = _db()
    org = db.get(PvProduct, product_id).org_id
    ids = []
    for index, terms in enumerate(terms_per_case):
        case = PvCase(org_id=org, pv_product_id=product_id,
                      worldwide_case_id=f"{product_id[:4]}-{index}",
                      initial_receipt_date=received, latest_receipt_date=received,
                      is_serious=False, deidentification_status="clear")
        db.add(case)
        db.flush()
        for term in terms:
            db.add(PvCaseEvent(org_id=org, pv_product_id=product_id, case_id=case.id,
                               meddra_pt=term, meddra_soc=f"SOC of {term}",
                               meddra_version="27.0",
                               confirmed_by="qp" if confirmed else None))
        ids.append(case.id)
    db.commit()
    db.close()
    return ids


def _report(app_client, token, product_id, *, doc_type="pbrer", regions=(),
            case_ids=None, period=("2026-01-01", "2026-06-30", "2026-07-15")):
    body = {"doc_type_key": doc_type, "period_start": period[0], "period_end": period[1],
            "data_lock_point": period[2], "regions": list(regions)}
    if case_ids is not None:
        body["case_ids"] = case_ids
    return app_client.post(f"/api/v1/pv/products/{product_id}/reports",
                           headers=_auth(token), json=body)


@pytest.fixture
def org(app_client, two_orgs):
    """A fresh organisation per test. The disproportionality background is
    every other product in the organisation, so a shared one would compare
    against whatever earlier tests left behind."""
    import uuid

    from app.models import Organization, User
    from app.security import create_access_token, hash_password

    tag = uuid.uuid4().hex[:8]
    db = _db()
    try:
        made = Organization(name=f"SignalOrg-{tag}")
        db.add(made)
        db.flush()
        user = User(org_id=made.id, email=f"signals-{tag}@tenant.test",
                    full_name="Signal user", password_hash=hash_password("pw"),
                    role_key="org_admin")
        db.add(user)
        db.commit()
        return {"token": create_access_token(user.id, made.id)}
    finally:
        db.close()


# ------------------------------------------------------------- the arithmetic

def test_prr_and_ror_against_numbers_worked_by_hand():
    s = sig.screen("Headache", 10, 90, 20, 980)
    assert s.prr == pytest.approx((10 / 100) / (20 / 1000))            # 5.0
    assert s.ror == pytest.approx((10 * 980) / (90 * 20))              # 5.444
    se = math.sqrt(1 / 10 - 1 / 100 + 1 / 20 - 1 / 1000)
    assert s.prr_ci[0] == pytest.approx(math.exp(math.log(5.0) - 1.96 * se), rel=1e-3)
    n = 1100
    chi2 = n * (abs(10 * 980 - 90 * 20) - n / 2) ** 2 / (100 * 1000 * 30 * 1070)
    assert s.chi2 == pytest.approx(chi2)
    assert s.meets_evans and s.ror_lower_above_one
    out = s.as_dict()
    assert out["prr"] == 5.0 and out["screening_flag"] is True
    assert out["a"] == 10 and out["d"] == 980


def test_an_undefined_statistic_is_absent_and_says_which_cell():
    no_drug = sig.screen("X", 0, 10, 3, 30)
    assert no_drug.prr is None and "a = 0" in no_drug.notes[0]
    no_background = sig.screen("X", 3, 10, 0, 30)
    assert no_background.prr is None and no_background.ror is None
    assert "c = 0" in no_background.notes[0]
    every_case = sig.screen("X", 3, 0, 2, 30)
    assert every_case.prr is not None and every_case.ror is None
    assert any("b = 0" in n for n in every_case.notes)
    every_background = sig.screen("X", 3, 5, 4, 0)
    assert every_background.ror is None
    assert any("d = 0" in n for n in every_background.notes)
    with pytest.raises(ValueError):
        sig.screen("X", -1, 1, 1, 1)


def test_two_cases_never_meet_the_threshold_however_large_the_ratio():
    few = sig.screen("X", 2, 0, 1, 1000)
    assert few.prr > 100 and not few.meets_evans and not few.ror_lower_above_one


def test_screen_all_puts_flagged_terms_first_and_honours_min_cases():
    rows = sig.screen_all({"Rare": 1, "Common": 20, "Flagged": 10}, 100,
                          {"Common": 200, "Flagged": 20}, 1000, min_cases=2)
    assert [r.term for r in rows] == ["Flagged", "Common"]
    assert rows[0].meets_evans


# --------------------------------------------------------------- the lifecycle

def test_the_signal_lifecycle():
    sig.check_transition("candidate", "new")
    sig.check_transition("new", "new")
    with pytest.raises(sig.SignalError, match="can become"):
        sig.check_transition("candidate", "closed")
    with pytest.raises(sig.SignalError, match="must be one of"):
        sig.check_transition("new", "archived")
    assert sig.needs_reviewer("candidate", "new")
    assert not sig.needs_reviewer("new", "ongoing")
    assert sig.closing_problems(conclusion=" ", action_taken=None) == [
        "a conclusion", "the action taken (or 'none')"]


def test_a_writer_raises_candidates_and_a_reviewer_validates_them(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Signalazine")
    _set_role(product, "writer")
    url = f"/api/v1/pv/products/{product}/signals"
    made = app_client.post(url, headers=_auth(token), json={"meddra_terms": ["Rash"]})
    assert made.status_code == 201, made.text
    signal = made.json()
    assert signal["status"] == "candidate" and signal["detection_date"]
    refused = app_client.post(url, headers=_auth(token),
                              json={"meddra_terms": ["Rash"], "status": "new"})
    assert refused.status_code == 403
    patch = f"/api/v1/pv/signals/{signal['id']}"
    assert app_client.patch(patch, headers=_auth(token),
                            json={"status": "new"}).status_code == 403
    _set_role(product, "reviewer")
    validated = app_client.patch(patch, headers=_auth(token), json={"status": "new"})
    assert validated.status_code == 200 and validated.json()["status"] == "new"


def test_a_screen_raises_only_candidates(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Screenazine")
    url = f"/api/v1/pv/products/{product}/signals"
    refused = app_client.post(url, headers=_auth(token), json={
        "meddra_terms": ["Rash"], "status": "new",
        "detection_source": "disproportionality"})
    assert refused.json()["detail"]["error"]["code"] == "PV_SCREEN_RAISES_CANDIDATES"
    basis = {"term": "Rash", "a": 4, "prr": 16.0}
    made = app_client.post(url, headers=_auth(token), json={
        "meddra_terms": ["Rash"], "detection_source": "disproportionality",
        "detection_basis": basis}).json()
    assert made["status"] == "candidate" and made["detection_basis"] == basis


@pytest.mark.parametrize("body, code", [
    ({"meddra_terms": []}, "PV_SIGNAL_EMPTY"),
    ({"meddra_terms": ["X"], "status": "closed"}, "PV_BAD_SIGNAL_STATUS"),
    ({"meddra_terms": ["X"], "detection_source": "rumour"}, "PV_BAD_DETECTION_SOURCE"),
    ({"meddra_terms": ["X"], "priority": "urgent"}, "PV_BAD_PRIORITY"),
    ({"meddra_terms": ["X"], "linked_case_ids": ["nope"]}, "PV_UNKNOWN_CASE"),
])
def test_a_malformed_signal_is_refused(app_client, org, body, code):
    product = _product(app_client, org["token"], "Refusazine")
    response = app_client.post(f"/api/v1/pv/products/{product}/signals",
                               headers=_auth(org["token"]), json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == code


def test_closing_needs_a_conclusion_and_an_action(app_client, org):
    from app.models import AuditLog

    token = org["token"]
    product = _product(app_client, token, "Closazine")
    case = _cases(product, [["Rash"]])[0]
    signal = app_client.post(f"/api/v1/pv/products/{product}/signals",
                             headers=_auth(token),
                             json={"meddra_terms": ["Rash"], "status": "new",
                                   "linked_case_ids": [case]}).json()
    url = f"/api/v1/pv/signals/{signal['id']}"
    assert app_client.patch(url, headers=_auth(token),
                            json={"status": "ongoing"}).status_code == 200
    refused = app_client.patch(url, headers=_auth(token), json={"status": "closed"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["details"]["missing"] == [
        "a conclusion", "the action taken (or 'none')"]
    closed = app_client.patch(url, headers=_auth(token), json={
        "status": "closed", "conclusion": "Not confirmed.", "action_taken": "None"})
    assert closed.status_code == 200
    assert closed.json()["closure_date"] == date.today().isoformat()
    reopened = app_client.patch(url, headers=_auth(token), json={"status": "ongoing"})
    assert reopened.json()["closure_date"] is None
    backwards = app_client.patch(url, headers=_auth(token), json={"status": "candidate"})
    assert backwards.json()["detail"]["error"]["code"] == "PV_BAD_SIGNAL_TRANSITION"
    # A null never blanks the status or the terms.
    kept = app_client.patch(url, headers=_auth(token),
                            json={"status": None, "meddra_terms": None}).json()
    assert kept["status"] == "ongoing" and kept["meddra_terms"] == ["Rash"]
    db = _db()
    targets = [a.target for a in db.query(AuditLog).filter(
        AuditLog.entity_id == signal["id"]).order_by(AuditLog.id)]
    db.close()
    assert any("ongoing -> closed" in (t or "") for t in targets)


def test_refuting_a_candidate_needs_the_reason(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Refutazine")
    signal = app_client.post(f"/api/v1/pv/products/{product}/signals",
                             headers=_auth(token), json={"meddra_terms": ["Rash"]}).json()
    url = f"/api/v1/pv/signals/{signal['id']}"
    assert app_client.patch(url, headers=_auth(token),
                            json={"status": "refuted"}).status_code == 409
    refuted = app_client.patch(url, headers=_auth(token), json={
        "status": "refuted", "conclusion": "Reporting artefact of a campaign."}).json()
    assert refuted["status"] == "refuted" and refuted["closure_date"]
    listed = app_client.get(f"/api/v1/pv/products/{product}/signals",
                            headers=_auth(token), params={"status": "refuted"}).json()
    assert [s["id"] for s in listed["items"]] == [signal["id"]]
    assert listed["disclaimer"] == sig.DISCLAIMER


# -------------------------------------------------------- the report overview

def test_the_overview_shows_signals_not_candidates(app_client, org):
    from app.models import PvProduct, PvReportInstance, PvSignal
    from app.safety import scope as scope_mod
    from app.safety import tabulations as tab

    token = org["token"]
    product = _product(app_client, token, "Overviewazine")
    report = _report(app_client, token, product).json()
    db = _db()
    org_id = db.get(PvProduct, product).org_id

    def add(ref, status, detected, closed=None):
        db.add(PvSignal(org_id=org_id, pv_product_id=product, signal_reference=ref,
                        status=status, detection_date=detected, closure_date=closed,
                        meddra_terms=["Rash"]))

    add("CAND", "candidate", date(2026, 2, 1))
    add("OPEN", "new", date(2026, 2, 1))
    add("LATE", "new", date(2026, 8, 1))                        # after the lock
    add("REFUTED-IN", "refuted", date(2026, 1, 5), date(2026, 3, 1))
    add("CLOSED-BEFORE", "closed", date(2025, 1, 5), date(2025, 6, 1))
    db.commit()
    row = db.get(PvReportInstance, report["id"])
    shown = tab.signals_in_report(db, row, scope_mod.scope_for(
        db.get(PvProduct, product), row))
    table = tab.render(db, report=row, product=db.get(PvProduct, product),
                       table_key="signal_overview")
    db.close()
    assert sorted(s.signal_reference for s in shown) == ["OPEN", "REFUTED-IN"]
    assert sorted(r[0] for r in table.rows) == ["OPEN", "REFUTED-IN"]
    # Refuted with no conclusion recorded: the table says what is missing.
    assert any("REFUTED-IN" in gap for gap in table.missing)
    assert table.totals == {"signals_shown": 2}


def test_untriaged_candidates_are_a_warning(app_client, org):
    from app.models import PvProduct, PvReportInstance
    from app.safety import qc

    token = org["token"]
    product = _product(app_client, token, "Triagazine")
    report = _report(app_client, token, product).json()
    app_client.post(f"/api/v1/pv/products/{product}/signals", headers=_auth(token),
                    json={"meddra_terms": ["Rash"], "detection_date": "2026-02-01"})
    db = _db()
    findings = qc.run_qc(db, report=db.get(PvReportInstance, report["id"]),
                         product=db.get(PvProduct, product))
    db.close()
    found = [f for f in findings if f.code == "SIGNAL_CANDIDATES_UNTRIAGED"]
    assert found and found[0].severity == qc.WARNING


# ---------------------------------------------------------- the screen itself

def test_the_screen_compares_against_the_organisations_other_products(app_client, org):
    from app.models import PvSignal

    token = org["token"]
    ours = _product(app_client, token, "Screenedazine")
    theirs = _product(app_client, token, "Backgroundazine")
    _cases(ours, [["Headache"]] * 4 + [["Nausea"]])
    _cases(theirs, [["Headache"]] + [["Nausea"]] * 19)
    # Received after the lock: counted on neither side.
    _cases(theirs, [["Headache"]] * 50, received=date(2026, 8, 1))
    report = _report(app_client, token, ours).json()
    db = _db()
    before = db.query(PvSignal).count()
    db.close()

    result = app_client.post(f"/api/v1/pv/reports/{report['id']}/disproportionality",
                             headers=_auth(token), json={"window": "cumulative"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["disclaimer"] == sig.DISCLAIMER
    assert body["product_cases"] == 5 and body["background_cases"] == 20
    headache = next(r for r in body["rows"] if r["term"] == "Headache")
    assert (headache["a"], headache["b"], headache["c"], headache["d"]) == (4, 1, 1, 19)
    assert headache["prr"] == pytest.approx((4 / 5) / (1 / 20))
    assert headache["meets_evans"] is True
    assert body["rows"][0]["term"] == "Headache"
    db = _db()
    assert db.query(PvSignal).count() == before      # figures only, never a signal
    db.close()

    by_soc = app_client.post(f"/api/v1/pv/reports/{report['id']}/disproportionality",
                             headers=_auth(token),
                             json={"level": "soc", "window": "interval",
                                   "min_cases": 2}).json()
    assert [r["term"] for r in by_soc["rows"]] == ["SOC of Headache"]


def test_a_screen_with_nothing_to_compare_with_says_so(app_client, org):
    token = org["token"]
    alone = _product(app_client, token, "Aloneazine")
    _cases(alone, [["Headache"]])
    report = _report(app_client, token, alone).json()
    url = f"/api/v1/pv/reports/{report['id']}/disproportionality"
    refused = app_client.post(url, headers=_auth(token), json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "PV_NO_BACKGROUND"

    provided = app_client.post(url, headers=_auth(token), json={
        "background": "provided",
        "provided": [{"term": "Headache", "cases_with_term": 30, "total_cases": 10000,
                      "source_note": "National database extract, 2026-07"}]}).json()
    row = provided["rows"][0]
    assert (row["a"], row["c"], row["d"]) == (1, 30, 9970)
    assert "National database extract" in provided["background_basis"]


@pytest.mark.parametrize("body, code, status", [
    ({"window": "forever"}, "PV_BAD_WINDOW", 422),
    ({"level": "llt"}, "PV_BAD_LEVEL", 422),
    ({"background": "internet"}, "PV_BAD_BACKGROUND", 422),
    ({"min_cases": 0}, "PV_BAD_MIN_CASES", 422),
    ({"background": "provided"}, "PV_NO_BACKGROUND", 422),
    ({"background": "provided", "provided": [
        {"term": "A", "cases_with_term": 1, "total_cases": 10, "source_note": "x"},
        {"term": "B", "cases_with_term": 1, "total_cases": 11, "source_note": "x"}]},
     "PV_BACKGROUND_INCONSISTENT", 422),
    ({"background": "provided", "provided": [
        {"term": "A", "cases_with_term": 12, "total_cases": 10, "source_note": "x"}]},
     "PV_BACKGROUND_INCONSISTENT", 422),
])
def test_a_malformed_screen_is_refused(app_client, org, body, code, status):
    token = org["token"]
    product = _product(app_client, token, "Malformazine")
    report = _report(app_client, token, product).json()
    response = app_client.post(f"/api/v1/pv/reports/{report['id']}/disproportionality",
                               headers=_auth(token), json=body)
    assert response.status_code == status
    assert response.json()["detail"]["error"]["code"] == code


def test_a_cumulative_screen_needs_a_birth_date(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Unbornazine", ibd=None)
    report = _report(app_client, token, product).json()
    response = app_client.post(f"/api/v1/pv/reports/{report['id']}/disproportionality",
                               headers=_auth(token), json={})
    assert response.json()["detail"]["error"]["code"] == "PV_NO_CUMULATIVE"


def test_the_background_window_uses_the_same_dates(app_client, org):
    from app.models import PvProduct, PvReportInstance
    from app.safety import scope as scope_mod

    token = org["token"]
    ours = _product(app_client, token, "Windowazine", ibd=None)
    theirs = _product(app_client, token, "Otherazine")
    _cases(theirs, [["A"]], received=date(2026, 2, 1))       # in the interval
    _cases(theirs, [["A"]], received=date(2025, 2, 1))       # before it
    report = _report(app_client, token, ours).json()
    db = _db()
    row = db.get(PvReportInstance, report["id"])
    scope = scope_mod.scope_for(db.get(PvProduct, ours), row)
    terms, total = scope_mod.term_case_counts(
        db, scope, scope_mod.background(scope, "interval"))
    with pytest.raises(scope_mod.CumulativeUnavailable):
        scope_mod.background(scope, "cumulative")
    db.close()
    assert terms == {"A": 1} and total == 1


# ------------------------------------------------------ case-bound narratives

def test_only_a_narrative_report_is_bound_to_cases(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Boundazine")
    case = _cases(product, [["Rash"]])[0]
    wrong_type = _report(app_client, token, product, doc_type="pbrer", case_ids=[case])
    assert wrong_type.json()["detail"]["error"]["code"] == "PV_CASES_NOT_APPLICABLE"
    unknown = _report(app_client, token, product, doc_type="icsr_narrative",
                      case_ids=["nope"])
    assert unknown.json()["detail"]["error"]["code"] == "PV_UNKNOWN_CASE"
    made = _report(app_client, token, product, doc_type="icsr_narrative",
                   case_ids=[case, case])
    assert made.status_code == 201 and made.json()["case_ids"] == [case]


def test_a_narrative_report_checks_its_cases(app_client, org):
    from app.models import PvCase, PvProduct, PvReportInstance
    from app.safety import qc

    token = org["token"]
    product = _product(app_client, token, "Checkazine")
    good, late = _cases(product, [["Rash"], ["Rash"]])
    unconfirmed = _cases(product, [["Itch"]], confirmed=False)[0]
    db = _db()
    db.get(PvCase, late).latest_receipt_date = date(2026, 9, 1)
    db.commit()
    db.close()
    empty = _report(app_client, token, product, doc_type="icsr_narrative").json()
    bound = _report(app_client, token, product, doc_type="icsr_narrative",
                    case_ids=[good, late, unconfirmed]).json()

    def codes(report_id):
        db = _db()
        try:
            return {f.code for f in qc.run_qc(
                db, report=db.get(PvReportInstance, report_id),
                product=db.get(PvProduct, product)) if f.severity == qc.BLOCKER}
        finally:
            db.close()

    assert "NO_CASES_BOUND" in codes(empty["id"])
    found = codes(bound["id"])
    assert {"BOUND_CASE_OUTSIDE_LOCK", "UNCONFIRMED_DATA"} <= found
    from app.db import delete_in_order
    from app.models import PvCaseEvent

    db = _db()
    delete_in_order(db, db.query(PvCaseEvent).filter(PvCaseEvent.case_id == good).all(),
                    [db.get(PvCase, good)])
    db.commit()
    db.close()
    assert "BOUND_CASE_MISSING" in codes(bound["id"])
    # A patch re-binds, and is validated the same way.
    rebound = app_client.patch(f"/api/v1/pv/reports/{bound['id']}", headers=_auth(token),
                               json={"case_ids": [late]})
    assert rebound.status_code == 200 and rebound.json()["case_ids"] == [late]


def test_a_narrative_section_is_given_its_cases_structured_data(app_client, org):
    from app.models import PvCaseDrug, PvCaseLab, PvProduct, PvReportInstance, PvSection
    from app.safety import router as pv_router

    token = org["token"]
    product = _product(app_client, token, "Narrazine")
    case = _cases(product, [["Rash"]])[0]
    report = _report(app_client, token, product, doc_type="icsr_narrative",
                     case_ids=[case]).json()
    db = _db()
    org_id = db.get(PvProduct, product).org_id
    db.add(PvCaseDrug(org_id=org_id, pv_product_id=product, case_id=case,
                      drug_name="Narrazine", role="suspect", dose="10", dose_unit="mg"))
    db.add(PvCaseLab(org_id=org_id, pv_product_id=product, case_id=case,
                     test_name="ALT", result="80", unit="U/L", test_date=date(2026, 3, 2)))
    db.commit()
    row = db.get(PvReportInstance, report["id"])
    section = db.query(PvSection).filter(PvSection.report_instance_id == row.id).first()
    data = pv_router._confirmed_data(db, db.get(PvProduct, product), row, section)
    db.close()
    assert len(data["cases"]) == 1
    narrated = data["cases"][0]
    assert narrated["events"][0]["term"] == "Rash"
    assert narrated["drugs"][0]["name"] == "Narrazine"
    assert narrated["labs"][0]["date"] == date(2026, 3, 2)


# ------------------------------------------------------- regional appendices

def test_regional_appendices_are_seeded_for_the_regions_named():
    base = trees.seed_sections("pbrer")
    both = trees.seed_sections("pbrer", regions=["US", "EU", "JP"])
    codes = [s["section_code"] for s in both]
    assert codes[:len(base)] == [s["section_code"] for s in base]
    assert codes[len(base):] == trees.regional_codes("pbrer", "EU") + \
        trees.regional_codes("pbrer", "US")
    by_code = {s["section_code"]: s for s in both}
    assert by_code["EU"]["is_container"] and by_code["EU.3"]["table_key"] == \
        "safety_concern_table"
    assert "rmp_doc" in by_code["EU.2"]["source_types"]
    assert trees.table_key_for("pbrer", "US.1") == "line_listing_sar"
    assert trees.table_key_for("pbrer", "6.3") == "summary_tab_soc_pt"
    assert trees.region_of("EU.3") == "EU" and trees.region_of("16.1") is None
    # A report type with no regional structure is unaffected by its regions.
    assert trees.seed_sections("dsur", regions=["EU"]) == trees.seed_sections("dsur")


def test_changing_a_reports_regions_adds_and_disables_appendices(app_client, org):
    token = org["token"]
    product = _product(app_client, token, "Regionazine")
    report = _report(app_client, token, product, regions=["US"]).json()

    def sections():
        return app_client.get(f"/api/v1/pv/reports/{report['id']}/sections",
                              headers=_auth(token)).json()["items"]

    assert "US.1" in {s["section_code"] for s in sections()}
    app_client.patch(f"/api/v1/pv/reports/{report['id']}", headers=_auth(token),
                     json={"regions": ["US", "EU"]})
    ordered = [s["section_code"] for s in sections()]
    # EU sorts before US whatever order the regions were added in.
    assert ordered.index("EU.5") < ordered.index("US")
    app_client.patch(f"/api/v1/pv/reports/{report['id']}", headers=_auth(token),
                     json={"regions": ["EU"]})
    enabled = {s["section_code"]: s["enabled"] for s in sections()}
    assert enabled["US.1"] is False and enabled["EU.1"] is True
    app_client.patch(f"/api/v1/pv/reports/{report['id']}", headers=_auth(token),
                     json={"regions": ["EU", "US"]})
    codes = [s["section_code"] for s in sections()]
    assert codes.count("US.1") == 1                     # re-enabled, not re-seeded
    assert {s["section_code"]: s["enabled"] for s in sections()}["US.1"] is True


# ------------------------------------------------------------------ merging

def test_merging_a_case_with_labs_and_other_pairs(app_client, org):
    """Found by running the suite on PostgreSQL: a merged case's laboratory
    results were left behind (a foreign-key failure on any database that
    enforces keys), and other candidate pairs naming it pointed at nothing."""
    from app.models import PvCase, PvCaseLab, PvDuplicateCandidate, PvProduct

    token = org["token"]
    product = _product(app_client, token, "Mergazine")
    keep, drop, third = _cases(product, [["Rash"], ["Rash"], ["Rash"]])
    db = _db()
    org_id = db.get(PvProduct, product).org_id
    db.add(PvCaseLab(org_id=org_id, pv_product_id=product, case_id=drop,
                     test_name="ALT", result="80"))
    pair = PvDuplicateCandidate(org_id=org_id, pv_product_id=product, case_id=keep,
                                other_case_id=drop, score=0.9)
    other = PvDuplicateCandidate(org_id=org_id, pv_product_id=product, case_id=drop,
                                 other_case_id=third, score=0.7)
    db.add_all([pair, other])
    db.commit()
    pair_id, other_id = pair.id, other.id
    db.close()

    merged = app_client.post(f"/api/v1/pv/duplicates/{pair_id}/resolve",
                             headers=_auth(token),
                             json={"action": "merged", "keep_case_id": keep})
    assert merged.status_code == 200, merged.text
    assert merged.json() == {"resolved": "merged"}
    db = _db()
    try:
        assert db.get(PvCase, drop) is None
        assert db.query(PvCaseLab).filter(PvCaseLab.case_id == drop).count() == 0
        assert db.get(PvDuplicateCandidate, pair_id) is None
        moved = db.get(PvDuplicateCandidate, other_id)
        assert {moved.case_id, moved.other_case_id} == {keep, third}
        assert moved.status == "pending"
    finally:
        db.close()
