"""Which bucket a stored result belongs to, and what a range across batches
may claim.

Three defects live here, and all three share a shape: the store held the right
value and something downstream disagreed with something else about what to do
with it. None of them produces an error, a blank cell or a warning -- each one
produces a document that reads as finished.

* `app.cmc.qc` decided "this is a stability result" by reading
  `storage_condition`, while `app.cmc.tables` decided it by reading
  `timepoint_months`. A result with a timepoint and no condition was filed
  under release by one and stability by the other.
* `_observed_range` ordered results by `value_numeric` alone, so a test whose
  batches were reported in different units produced an inverted range across
  two dimensions.
* `_observed_range` put a lone result through `tally.cell` twice -- once as the
  bottom of its own range and once as the top -- counting one unverified value
  as two.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.cmc import tables
from app.cmc.tables import (
    ON_STABILITY, RELEASE_RESULT, STABILITY_RESULT, TIMELESS_RESULT,
    classify_result,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _result(*, condition=None, timepoint=None):
    return SimpleNamespace(storage_condition=condition, timepoint_months=timepoint)


# ------------------------------------------------- one definition, not two

def test_the_four_combinations_each_have_exactly_one_bucket():
    assert classify_result(_result()) == RELEASE_RESULT
    assert classify_result(_result(timepoint=6.0)) == STABILITY_RESULT
    assert classify_result(_result(condition="25C/60RH", timepoint=6.0)) == STABILITY_RESULT
    assert classify_result(_result(condition="25C/60RH")) == TIMELESS_RESULT


def test_a_zero_timepoint_is_a_timepoint():
    """`Initial` parses to 0.0, and `if result.timepoint_months` would read
    that as absent -- filing every initial stability reading under release."""
    assert classify_result(_result(timepoint=0.0)) == STABILITY_RESULT
    assert classify_result(_result(condition="25C/60RH", timepoint=0.0)) == STABILITY_RESULT


def test_a_timepoint_with_no_condition_is_on_stability():
    """The exact row the two definitions disagreed about."""
    assert classify_result(_result(timepoint=6.0)) in ON_STABILITY
    assert classify_result(_result()) not in ON_STABILITY


def test_qc_buckets_a_result_the_way_the_table_builders_do():
    """The regression proper: `qc` must not restate the rule.

    Read as source rather than executed, because what failed before was not an
    arithmetic error -- it was a second copy of a definition, and the only
    durable assertion is that the second copy is gone.
    """
    import inspect

    from app.cmc import qc

    source = inspect.getsource(qc)
    bucketing = source[source.index("def _unverified_data"):]
    bucketing = bucketing[:bucketing.index("\ndef ")]
    # Comments are stripped: a comment explaining which rule was removed is
    # exactly the thing worth keeping, and it names the old expression.
    code = "\n".join(line for line in bucketing.splitlines()
                     if not line.strip().startswith("#"))
    assert "classify_result" in code
    assert "result.storage_condition" not in code


# ---------------------------------------------- the SQL and the predicate

@pytest.fixture
def store(app_client, two_orgs):
    """A project holding one result of every shape, written straight in."""
    from app.db import SessionLocal
    from app.models import CmcBatch, CmcMaterial, CmcProject, CmcResult, CmcTest

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Scope dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc_id = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Scopazole"}).json()["id"]

    db = SessionLocal()
    cmc = db.get(CmcProject, cmc_id)
    scope = {"org_id": cmc.org_id, "cmc_project_id": cmc_id}

    def add(row):
        db.add(row)
        db.flush()
        return row

    material = add(CmcMaterial(kind="drug_substance", name="Scopazole", **scope))
    test = add(CmcTest(material_id=material.id, test_name="Related substance A",
                       acceptance_criterion_text="NMT 0.10 %", sort_order=0, **scope))
    batch = add(CmcBatch(material_id=material.id, batch_number="B-1", **scope))

    shapes = {
        RELEASE_RESULT: dict(storage_condition=None, timepoint_months=None),
        STABILITY_RESULT: dict(storage_condition=None, timepoint_months=6.0),
        TIMELESS_RESULT: dict(storage_condition="25C/60RH", timepoint_months=None),
    }
    ids = {}
    for kind, columns in shapes.items():
        row = add(CmcResult(batch_id=batch.id, test_id=test.id, value_text="0.05 %",
                            value_numeric=Decimal("0.05"), unit="%", **columns, **scope))
        ids[kind] = row.id
    db.commit()
    try:
        yield SimpleNamespace(db=db, org_id=cmc.org_id, cmc_project_id=cmc_id,
                              test_id=test.id, batch_id=batch.id, ids=ids,
                              token=token)
    finally:
        db.close()


def test_the_sql_queries_agree_with_the_predicate(store):
    """`_results` and `_timeless` ask the database the same question the
    predicate answers in Python. If they ever drift, a value is either counted
    twice or dropped from every table -- and dropping is silent."""
    scope = tables._Scope(
        db=store.db, org_id=store.org_id, cmc_project_id=store.cmc_project_id,
        material_id=None, deliverable_id=None, include_unverified=True)
    ids = dict(test_ids=[store.test_id], batch_ids=[store.batch_id])

    release = tables._results(scope, release=True, **ids)
    stability = tables._results(scope, release=False, **ids)
    timeless = tables._timeless(scope, **ids)

    assert [r.id for r in release] == [store.ids[RELEASE_RESULT]]
    assert [r.id for r in stability] == [store.ids[STABILITY_RESULT]]
    assert [r.id for r in timeless] == [store.ids[TIMELESS_RESULT]]

    # Every stored row is claimed by exactly one of the three -- no gaps, no
    # double counting.
    claimed = [r.id for r in release] + [r.id for r in stability] + [r.id for r in timeless]
    assert sorted(claimed) == sorted(store.ids.values())
    for kind, row_id in store.ids.items():
        row = next(r for r in release + stability + timeless if r.id == row_id)
        assert classify_result(row) == kind


# --------------------------------------------------- the range across batches

@pytest.fixture
def impurities(app_client, two_orgs):
    """One impurity test whose batches are reported in mixed units."""
    from app.db import SessionLocal
    from app.models import CmcBatch, CmcMaterial, CmcProject, CmcResult, CmcTest, User

    token, _pa, _tb, _pb = two_orgs
    portal = app_client.post("/api/v1/projects", headers=_auth(token), json={
        "name": "Impurity dossier", "function": "Quality-CMC",
        "document_type": "CMC Section", "region": "Global", "language": "English"}).json()
    cmc_id = app_client.post("/api/v1/cmc/projects", headers=_auth(token), json={
        "project_id": portal["id"], "product_name": "Impurazole"}).json()["id"]

    db = SessionLocal()
    cmc = db.get(CmcProject, cmc_id)
    user = db.query(User).filter(User.org_id == cmc.org_id).first()
    scope = {"org_id": cmc.org_id, "cmc_project_id": cmc_id}

    def add(row):
        db.add(row)
        db.flush()
        return row

    material = add(CmcMaterial(kind="drug_substance", name="Impurazole", **scope))
    state = SimpleNamespace(db=db, org_id=cmc.org_id, cmc_project_id=cmc_id,
                            material_id=material.id, verifier=user.id, add=add,
                            scope=scope)

    def impurity(name, criterion="NMT 0.50 %"):
        return add(CmcTest(material_id=material.id, test_name=name,
                           acceptance_criterion_text=criterion, sort_order=0, **scope))

    def result(test, batch_number, value_text, numeric, unit, *, verified=True):
        batch = db.query(CmcBatch).filter(
            CmcBatch.cmc_project_id == cmc_id,
            CmcBatch.batch_number == batch_number).first()
        if batch is None:
            batch = add(CmcBatch(material_id=material.id, batch_number=batch_number, **scope))
        return add(CmcResult(
            batch_id=batch.id, test_id=test.id, value_text=value_text,
            value_numeric=Decimal(numeric), unit=unit,
            verified_by=(user.id if verified else None), **scope))

    state.impurity = impurity
    state.result = result
    try:
        yield state
    finally:
        db.close()


def _render(state):
    return tables.render_table(state.db, cmc_project_id=state.cmc_project_id,
                               org_id=state.org_id, table_key="impurity_table",
                               material_id=state.material_id)


def test_a_range_across_two_units_is_refused_not_inverted(impurities):
    """500 ppm and 0.2 % are 500 and 2000 ppm. Ordered by bare magnitude the
    cell reads "0.2 % - 500 ppm", which is backwards, dimensionally mixed, and
    states a range no batch was ever in."""
    test = impurities.impurity("Impurity D")
    impurities.result(test, "B-1", "0.2 %", "0.2", "%")
    impurities.result(test, "B-2", "500 ppm", "500", "ppm")

    rendered = _render(impurities)
    cell = [r for r in rendered.rows if r[0] == "Impurity D"][0][2]
    # 500 ppm is 0.05 %, so the low end is the ppm batch and the high end the
    # percent one -- the opposite of what the bare magnitudes said.
    assert cell == "500 ppm - 0.2 %"
    assert any("more than one unit" in note for note in rendered.notes)


def test_units_with_no_exact_conversion_state_no_range(impurities):
    test = impurities.impurity("Impurity E")
    impurities.result(test, "B-1", "3 mg/mL", "3", "mg/mL")
    impurities.result(test, "B-2", "0.2 %", "0.2", "%")

    rendered = _render(impurities)
    cell = [r for r in rendered.rows if r[0] == "Impurity E"][0][2]
    assert cell == tables.HOLE
    assert any("no exact conversion" in gap for gap in rendered.missing)


def test_one_unverified_result_is_counted_once_not_twice(impurities):
    """A lone result is both ends of its own range. Sent through the cell
    method twice it was counted as two unverified values and its note printed
    twice -- so a table with one unverified number reported two, and the count
    a reviewer works through never reconciled with the rows on screen."""
    test = impurities.impurity("Impurity F")
    impurities.result(test, "B-9", "0.03 %", "0.03", "%", verified=False)

    rendered = tables.render_table(
        impurities.db, cmc_project_id=impurities.cmc_project_id,
        org_id=impurities.org_id, table_key="impurity_table",
        material_id=impurities.material_id, include_unverified=True)
    cell = [r for r in rendered.rows if r[0] == "Impurity F"][0][2]
    assert cell == "0.03 %"
    assert rendered.unverified == 1


def test_a_lone_unverified_result_is_withheld_once_when_excluded(impurities):
    """Same row, the other way round. Excluded from the table it produces two
    DIFFERENT statements -- it was withheld, and therefore no range can be
    stated -- which is right. What must not happen is the withholding itself
    being counted twice."""
    test = impurities.impurity("Impurity G")
    impurities.result(test, "B-8", "0.04 %", "0.04", "%", verified=False)

    rendered = tables.render_table(
        impurities.db, cmc_project_id=impurities.cmc_project_id,
        org_id=impurities.org_id, table_key="impurity_table",
        material_id=impurities.material_id, include_unverified=False)
    withheld = [g for g in rendered.missing
                if "Impurity G" in g and "not verified" in g]
    assert withheld == ["Impurity G: 1 result(s) are not verified and are not in the range"]


def test_matching_units_still_produce_a_plain_range(impurities):
    """The ordinary case stays ordinary: no conversion note, no refusal."""
    test = impurities.impurity("Impurity H")
    impurities.result(test, "B-1", "0.10 %", "0.10", "%")
    impurities.result(test, "B-2", "0.30 %", "0.30", "%")

    rendered = _render(impurities)
    cell = [r for r in rendered.rows if r[0] == "Impurity H"][0][2]
    assert cell == "0.10 % - 0.30 %"
    assert not any("more than one unit" in note for note in rendered.notes)


# ------------------------------------------------- paging the grid honestly

def test_the_grid_can_ask_for_one_scope_at_a_time(app_client, store):
    """Each tab of the Data Review grid pages through its own rows. Sharing
    one page between release and stability meant a page of a hundred rows
    might hold ninety of one and ten of the other, and "next page" moved both
    tabs at once."""
    token = store.token
    base = f"/api/v1/cmc/projects/{store.cmc_project_id}/data/results"

    everything = app_client.get(base, headers=_auth(token)).json()
    assert everything["total"] == 3

    release = app_client.get(base, headers=_auth(token),
                             params={"scope": "release"}).json()
    assert release["total"] == 1
    assert [r["id"] for r in release["items"]] == [store.ids[RELEASE_RESULT]]

    stability = app_client.get(base, headers=_auth(token),
                               params={"scope": "stability"}).json()
    assert stability["total"] == 2
    assert set(r["id"] for r in stability["items"]) == {
        store.ids[STABILITY_RESULT], store.ids[TIMELESS_RESULT]}

    # The two scopes partition the set: nothing counted twice, nothing lost.
    assert release["total"] + stability["total"] == everything["total"]


def test_an_unknown_scope_is_refused_rather_than_ignored(app_client, store):
    token = store.token
    res = app_client.get(
        f"/api/v1/cmc/projects/{store.cmc_project_id}/data/results",
        headers=_auth(token), params={"scope": "everything"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CMC_UNKNOWN_SCOPE"


def test_the_filter_searches_the_store_not_the_page(app_client, store):
    """A filter applied in the browser searches only what was already fetched,
    so on a programme larger than one page it answers "no matches" for values
    that are in the dossier."""
    token = store.token
    base = f"/api/v1/cmc/projects/{store.cmc_project_id}/data/results"

    by_test = app_client.get(base, headers=_auth(token),
                             params={"q": "Related substance"}).json()
    assert by_test["total"] == 3
    by_batch = app_client.get(base, headers=_auth(token),
                              params={"q": "B-1"}).json()
    assert by_batch["total"] == 3
    by_value = app_client.get(base, headers=_auth(token),
                              params={"q": "0.05"}).json()
    assert by_value["total"] == 3
    nothing = app_client.get(base, headers=_auth(token),
                             params={"q": "Dissolution"}).json()
    assert nothing["total"] == 0
    assert nothing["items"] == []


def test_the_summary_describes_the_filtered_set(app_client, store):
    """`total` and the verification counts have to agree with each other, or
    the gate banner reports a fraction of a set nobody is looking at."""
    token = store.token
    scoped = app_client.get(
        f"/api/v1/cmc/projects/{store.cmc_project_id}/data/results",
        headers=_auth(token), params={"scope": "release"}).json()
    summary = scoped["summary"]
    assert summary["total"] == scoped["total"] == 1
    assert summary["verified"] + summary["unverified"] == summary["total"]


def test_paging_returns_each_row_once(app_client, store):
    token = store.token
    base = f"/api/v1/cmc/projects/{store.cmc_project_id}/data/results"
    seen = []
    for offset in (0, 2):
        page = app_client.get(base, headers=_auth(token),
                              params={"limit": 2, "offset": offset}).json()
        assert page["total"] == 3, "the total is of the whole set, not the page"
        seen.extend(r["id"] for r in page["items"])
    assert sorted(seen) == sorted(store.ids.values())
