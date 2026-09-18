"""Which cases a report may count.

§14 makes this the highest-priority unit test in the module, and the reason is
that everything else in a periodic safety report is downstream of it. A wrong
count does not announce itself: it is a number in a tabulation, repeated in a
sentence above the tabulation, carried into the cumulative column, and compared
by a regulator against the same figure in the last report.

Four rules are pinned here, in the order they bite:

* the data lock point is absolute -- nothing learned after it counts;
* interval membership is the INITIAL receipt, so follow-up on an old case is
  not a new case this interval;
* cumulative counting starts at the birth date the REPORT TYPE names, and a
  DSUR's is not a PBRER's;
* a case that cannot be placed in time is counted nowhere and shown anyway.
"""

from datetime import date

import pytest

from app.safety import registry
from app.safety.scope import (
    CumulativeUnavailable, Scope, after_lock, count_cases, cumulative, interval,
    preview, scope_of_case, undated,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 6, 30)
DLP = date(2026, 7, 15)
IBD = date(2020, 3, 1)
DIBD = date(2016, 9, 1)


@pytest.fixture
def store(app_client, two_orgs):
    """One product and a case for every position in time that matters."""
    from app.db import SessionLocal
    from app.models import PvCase, PvCaseEvent, PvProduct

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Scope safety", "function": "Safety",
        "document_type": "PSUR", "region": "Global", "language": "English"}).json()
    product = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Scopazine",
        "ibd": IBD.isoformat(), "dibd": DIBD.isoformat()}).json()

    db = SessionLocal()
    org_id = db.get(PvProduct, product["id"]).org_id

    def case(label, *, initial, latest=None, events=1):
        row = PvCase(org_id=org_id, pv_product_id=product["id"],
                     worldwide_case_id=label, initial_receipt_date=initial,
                     latest_receipt_date=latest if latest is not None else initial)
        db.add(row)
        db.flush()
        for n in range(events):
            db.add(PvCaseEvent(org_id=org_id, pv_product_id=product["id"],
                               case_id=row.id, verbatim_term=f"{label} event {n}"))
        db.flush()
        return row

    cases = {
        # Squarely inside the reporting interval.
        "in_interval": case("IN-1", initial=date(2026, 3, 4), events=2),
        # Before this interval but after the development birth date: cumulative
        # for a DSUR, and also cumulative for a PBRER since it is after the IBD.
        "before_interval": case("OLD-1", initial=date(2022, 5, 9)),
        # Between the development birth date and the approval birth date: a
        # DSUR counts it, a PBRER does not.
        "pre_approval": case("DEV-1", initial=date(2017, 2, 2)),
        # Received during the interval, updated AFTER the lock. The row holds
        # post-lock data, so this report may not see it at all.
        "updated_after_lock": case("UPD-1", initial=date(2026, 4, 1),
                                   latest=date(2026, 8, 20)),
        # First received after the lock: acceptance criterion 4.
        "after_lock": case("NEW-1", initial=date(2026, 8, 1)),
        # In the store with no date at all.
        "undated": case("NIL-1", initial=None, latest=None),
        # Received after the interval ended but before the lock: in neither the
        # interval nor, for a first report, anything else this report counts --
        # but it IS within the cumulative window.
        "between_end_and_lock": case("TAIL-1", initial=date(2026, 7, 3)),
    }
    db.commit()
    try:
        yield {"db": db, "token": token, "org_id": org_id,
               "product_id": product["id"], "cases": cases}
    finally:
        db.close()


def _scope(store, doc_type_key="dsur"):
    anchor = registry.cumulative_anchor(doc_type_key)
    return Scope(pv_product_id=store["product_id"], org_id=store["org_id"],
                 period_start=PERIOD_START, period_end=PERIOD_END,
                 data_lock_point=DLP,
                 cumulative_from=DIBD if anchor == "dibd" else IBD,
                 anchor=anchor)


def _ids(store, db, predicate):
    from app.models import PvCase
    from sqlalchemy import select

    rows = db.scalars(select(PvCase).where(predicate)).all()
    return {r.worldwide_case_id for r in rows}


# ------------------------------------------------------- the data lock point

def test_a_case_received_after_the_lock_counts_nowhere(store):
    """Acceptance criterion 4: it is in the store and in no figure."""
    db, scope = store["db"], _scope(store)
    assert "NEW-1" not in _ids(store, db, interval(scope))
    assert "NEW-1" not in _ids(store, db, cumulative(scope))
    # And it is visible rather than lost.
    assert "NEW-1" in _ids(store, db, after_lock(scope))


def test_a_case_updated_after_the_lock_is_excluded_entirely(store):
    """The row holds the post-lock version of the data, and there is no way to
    reconstruct the version that existed on the lock date. Excluding it can
    make a cumulative count fall, which QC blocks on; including it would put
    post-lock information into the report, which nothing would catch."""
    db, scope = store["db"], _scope(store)
    assert "UPD-1" not in _ids(store, db, interval(scope))
    assert "UPD-1" not in _ids(store, db, cumulative(scope))
    assert "UPD-1" in _ids(store, db, after_lock(scope))


def test_moving_the_lock_forward_admits_the_updated_case(store):
    """The exclusion is about the lock, not about the case."""
    db = store["db"]
    later = Scope(**{**_scope(store).__dict__, "data_lock_point": date(2026, 9, 1)})
    assert "UPD-1" in _ids(store, db, interval(later))


# --------------------------------------------------- what the interval means

def test_the_interval_is_the_initial_receipt(store):
    db, scope = store["db"], _scope(store)
    assert _ids(store, db, interval(scope)) == {"IN-1"}


def test_a_case_between_the_period_end_and_the_lock_is_not_in_the_interval(store):
    """It arrived while the report was being prepared. It belongs to the next
    interval, and counting it here would double-count it there."""
    db, scope = store["db"], _scope(store)
    assert "TAIL-1" not in _ids(store, db, interval(scope))
    # It is inside the cumulative window, though: cumulative runs to the lock.
    assert "TAIL-1" in _ids(store, db, cumulative(scope))


# -------------------------------------------------------- the two birth dates

def test_a_dsur_counts_from_the_development_birth_date(store):
    db = store["db"]
    counted = _ids(store, db, cumulative(_scope(store, "dsur")))
    assert "DEV-1" in counted, "a subject exposed before first approval is in a DSUR"
    assert counted == {"IN-1", "OLD-1", "DEV-1", "TAIL-1"}


def test_a_pbrer_counts_from_the_approval_birth_date(store):
    db = store["db"]
    counted = _ids(store, db, cumulative(_scope(store, "pbrer")))
    assert "DEV-1" not in counted, "a pre-approval case is not in a PBRER's cumulative"
    assert counted == {"IN-1", "OLD-1", "TAIL-1"}


def test_the_anchor_comes_from_the_report_type_not_the_query(store):
    """The registry owns the choice. A query layer that picked one would change
    every cumulative figure in the other document with nothing looking wrong."""
    assert registry.cumulative_anchor("dsur") == "dibd"
    assert registry.cumulative_anchor("pbrer") == "ibd"
    assert registry.cumulative_anchor("pader") == "ibd"


def test_a_product_with_no_birth_date_refuses_a_cumulative_figure(store):
    """Counting from an unknown start would produce an interval figure wearing
    a cumulative label, which is worse than no figure."""
    scope = Scope(**{**_scope(store).__dict__, "cumulative_from": None})
    assert scope.has_cumulative is False
    with pytest.raises(CumulativeUnavailable):
        cumulative(scope)


# --------------------------------------------------- a case with no date at all

def test_an_undated_case_is_counted_nowhere_and_shown(store):
    db, scope = store["db"], _scope(store)
    assert "NIL-1" not in _ids(store, db, interval(scope))
    assert "NIL-1" not in _ids(store, db, cumulative(scope))
    assert _ids(store, db, undated(scope)) == {"NIL-1"}


# ------------------------------------------------------------- the preview

def test_the_preview_is_what_screen_s2_shows(store):
    db, scope = store["db"], _scope(store)
    out = preview(db, scope)
    assert out["interval_cases"] == 1
    assert out["interval_events"] == 2, "the interval case has two events"
    assert out["cumulative_cases"] == 4
    assert out["excluded_after_lock"] == 2, "one new after the lock, one updated after"
    assert out["undated"] == 1
    assert out["cumulative_anchor"] == "dibd"
    assert out["cumulative_from"] == DIBD.isoformat()


def test_the_preview_says_why_a_cumulative_figure_is_missing(store):
    db = store["db"]
    scope = Scope(**{**_scope(store).__dict__, "cumulative_from": None})
    out = preview(db, scope)
    assert out["cumulative_cases"] is None
    assert "DIBD" in out["cumulative_unavailable"]
    # The interval half is still answerable and still answered.
    assert out["interval_cases"] == 1


def test_new_since_the_baseline_counts_only_what_the_last_report_had_not_seen(store):
    from types import SimpleNamespace

    db, scope = store["db"], _scope(store)
    baseline = SimpleNamespace(id="baseline", data_lock_point=date(2026, 2, 1))
    out = preview(db, scope, baseline=baseline)
    # IN-1 arrived in March, after the previous report locked in February.
    assert out["new_since_baseline"] == 1
    later_baseline = SimpleNamespace(id="baseline", data_lock_point=date(2026, 5, 1))
    assert preview(db, scope, baseline=later_baseline)["new_since_baseline"] == 0


# ------------------------------------- the badge and the query cannot disagree

@pytest.mark.parametrize("label,expected", [
    ("IN-1", "interval"),
    ("OLD-1", "cumulative"),
    ("NEW-1", "after_lock"),
    ("UPD-1", "after_lock"),
    ("NIL-1", "undated"),
    ("TAIL-1", "cumulative"),
])
def test_the_row_badge_matches_the_sql(store, label, expected):
    """`scope_of_case` is what the grid puts on a row. If it disagreed with the
    predicates, a case would be badged as counted and be absent from the count
    -- and the only way to notice would be to add the column up by hand."""
    db, scope = store["db"], _scope(store)
    case = next(c for c in store["cases"].values() if c.worldwide_case_id == label)
    assert scope_of_case(scope, case) == expected

    in_interval = label in _ids(store, db, interval(scope))
    assert in_interval == (expected == "interval")


def test_a_pre_approval_case_is_outside_a_pbrer_and_inside_a_dsur(store):
    case = store["cases"]["pre_approval"]
    assert scope_of_case(_scope(store, "dsur"), case) == "cumulative"
    assert scope_of_case(_scope(store, "pbrer"), case) == "outside"


def test_counting_is_scoped_to_the_product(store, app_client, two_orgs):
    """Another product's cases can never reach this one's figures."""
    from app.db import SessionLocal
    from app.models import PvCase

    db, scope = store["db"], _scope(store)
    before = count_cases(db, scope, interval(scope))

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Other safety", "function": "Safety", "document_type": "PSUR",
        "region": "Global", "language": "English"}).json()
    other = app_client.post("/api/v1/pv/products", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Otherazine"}).json()
    writer = SessionLocal()
    writer.add(PvCase(org_id=store["org_id"], pv_product_id=other["id"],
                      worldwide_case_id="OTHER-1",
                      initial_receipt_date=date(2026, 3, 4),
                      latest_receipt_date=date(2026, 3, 4)))
    writer.commit()
    writer.close()

    assert count_cases(db, scope, interval(scope)) == before
