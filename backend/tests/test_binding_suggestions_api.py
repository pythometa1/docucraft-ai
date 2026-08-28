"""The binding screen's answer, seen the way a reviewer sees it.

`binding-suggestions` is where §13 stops being a design note and becomes
something a person clicks. The failure it has to prevent is a screen that shows
a number: a reviewer handed "0.95" against two hundred fields cannot tell an
approved-forty-two-times mapping from a lucky string match, so they either
accept everything or re-read everything -- and one of those ends with a letter
that states the wrong salary.

So these tests assert what the response *says*, not only what it scores: the
band, the vetoes with the rule each one fired on, the evidence behind the
number, the ranked alternatives, and the policy block that admits which of the
seven signals are actually being combined today.
"""

from __future__ import annotations

import pytest

from app.compiler import confidence as cf

API = "/api/v1"

CSV = (
    "Colleague First Name,Start Date,Annual Salary,Notes\n"
    "Olivia,01/08/2026,to be confirmed,Joining the Melbourne site\n"
    "Marcus,15/09/2026,as per band,Transferring from Dalian\n"
)

FIELDS = [
    {"id": "colleague_first_name", "type": "string",
     "slots": [{"kind": "blue_placeholder", "text": "<Colleague First Name>"}]},
    {"id": "start_date", "type": "date",
     "slots": [{"kind": "blue_placeholder", "text": "<Start Date>"}]},
    {"id": "annual_salary", "type": "currency",
     "slots": [{"kind": "blue_placeholder", "text": "<Annual Salary>"}]},
]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def binding_org(app_client):
    """One organisation with two manifests over the same source file.

    Two, because precedent is the thing under test and precedent means "this
    mapping was confirmed somewhere else in the estate". One manifest could
    only ever cite itself.
    """
    from app.db import SessionLocal
    from app.models import (
        Organization, Project, SourceFile, SourceVersion, TemplateFile,
        TemplateManifest, TemplateVersion, User,
    )
    from app.security import create_access_token, hash_password
    from app.storage import save_bytes

    db = SessionLocal()
    try:
        org = Organization(name="BindingConfidenceOrg")
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email="binder@confidence.test", full_name="Binder",
                    password_hash=hash_password("pw"), role_key="org_admin")
        db.add(user)
        db.flush()
        project = Project(org_id=org.id, display_id=91001, name="Binding Project",
                          region="Europe", function="Human Resources", document_type="Offer Letter",
                          language="English", status="pending", created_by=user.id)
        db.add(project)
        db.flush()  # the children below reference project.id, minted on flush

        template = TemplateFile(org_id=org.id, project_id=project.id, name="offer.docx",
                                status="ready", created_by=user.id)
        source = SourceFile(org_id=org.id, project_id=project.id, name="colleagues.csv",
                            file_type="csv", status="ready", created_by=user.id)
        db.add_all([template, source])
        db.flush()

        template_version = TemplateVersion(template_file_id=template.id, org_id=org.id, version_no=1,
                                           blob_path="templates/none.docx", created_by=user.id)
        source_version = SourceVersion(source_file_id=source.id, org_id=org.id, version_no=1,
                                       blob_path=save_bytes(CSV.encode(), "sources", ".csv"),
                                       created_by=user.id)
        db.add_all([template_version, source_version])
        db.flush()

        manifests = []
        for version_no in (1, 2):
            manifest = TemplateManifest(
                org_id=org.id, template_file_id=template.id, template_version_id=template_version.id,
                version_no=version_no, status="draft", fields=FIELDS, conditions=[], blocks=[],
                delete_always=[], confidence=1.0, compiled_by="rule_based", prescan_summary={},
                created_by=user.id,
            )
            db.add(manifest)
            db.flush()
            manifests.append(manifest.id)

        ids = {"source_version_id": source_version.id,
               "manifest_a": manifests[0], "manifest_b": manifests[1]}
        db.commit()
        return create_access_token(user.id, org.id), ids
    finally:
        db.close()


def _suggestions(app_client, token, manifest_id, source_version_id):
    response = app_client.get(
        f"{API}/template-manifests/{manifest_id}/binding-suggestions",
        params={"source_version_id": source_version_id}, headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_binding_screen_says_why_and_not_only_how_much(app_client, binding_org):
    """Every suggestion carries the score, the band, the vetoes and the evidence
    behind it. A reviewer asked to accept a bare number has no way to audit it,
    and the mapping this product gets wrong is always the plausible one."""
    token, ids = binding_org
    body = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])

    by_id = {s["field_id"]: s for s in body["suggestions"]}
    assert set(by_id) == {"colleague_first_name", "start_date", "annual_salary"}
    for suggestion in by_id.values():
        assert set(suggestion) >= {
            "score", "band", "vetoes", "evidence", "alternatives", "auto_apply",
            "declared_type", "observed_type", "rationale",
        }
        assert suggestion["score"] == suggestion["confidence"]
        assert suggestion["band"] in {b.value for b in cf.Band}
        assert suggestion["evidence"], "a score with no evidence is the thing this replaced"

    name = by_id["colleague_first_name"]
    assert name["column"] == "Colleague First Name"
    assert name["score"] != 0.95, "the per-tier constant is gone"
    assert "exact_name_match" in name["evidence"]


def test_a_type_mismatch_reaches_the_reviewer_with_the_rule_that_fired(app_client, binding_org):
    """The salary column holds "to be confirmed". Every name signal says the
    mapping is right, which is exactly why the type gate exists -- and the
    reviewer is told which rule blocked it, in words, not by a code."""
    token, ids = binding_org
    body = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])
    salary = next(s for s in body["suggestions"] if s["field_id"] == "annual_salary")

    assert salary["column"] == "Annual Salary"
    assert salary["declared_type"] == "currency"
    assert salary["observed_type"] == "text"
    codes = [v["code"] for v in salary["vetoes"]]
    assert cf.VETO_TYPE_MISMATCH in codes
    assert all(v["explanation"] for v in salary["vetoes"])
    # A veto sends a candidate to REVIEW, not BLOCK: §13's signal table says
    # "Vetoes -- force REVIEW regardless of computed score", and BLOCK is the
    # band for a score below 0.50. The distinction is load-bearing on a new
    # installation, where the "no precedent and no family" veto is true of
    # every mapping and BLOCK would make the product unusable on day one.
    assert salary["band"] == cf.Band.REVIEW.value
    assert salary["auto_apply"] is False


def test_a_cold_estate_sends_everything_to_review_and_says_so(app_client, binding_org):
    """§13's day-one posture. Nothing has been approved before, so nothing is
    auto-applied and the summary shows a reviewer exactly how much is waiting
    on them."""
    token, ids = binding_org
    body = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])

    assert body["band_summary"][cf.Band.AUTO_ACCEPT.value] == 0
    assert body["band_summary"][cf.Band.REVIEW.value] == 3
    assert all(s["auto_apply"] is False for s in body["suggestions"])
    assert all(
        cf.VETO_NO_PRECEDENT in [v["code"] for v in s["vetoes"]]
        for s in body["suggestions"] if s["field_id"] != "annual_salary"
    )


def test_the_response_admits_which_signals_it_could_not_compute(app_client, binding_org):
    """Three of §13's seven signals have nothing to read yet. Saying so on the
    response is the difference between a conservative score and a score that
    looks low for no stated reason -- and it is the honest way to explain why
    nothing auto-accepts."""
    token, ids = binding_org
    policy = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])["confidence_policy"]

    assert policy["weights_calibrated"] is False
    assert set(policy["signals_computed"]) | set(policy["signals_not_computed"]) == set(cf.INITIAL_WEIGHTS)
    assert policy["auto_accept_reachable"] is False
    assert policy["max_attainable_score"] < policy["bands"]["auto_accept"]
    assert all(reason for reason in policy["signals_not_computed"].values())


def test_a_mapping_confirmed_on_another_template_comes_back_as_precedent(app_client, binding_org):
    """The whole point of mapping memory: the second template from a customer
    must not be reviewed as though it were the first. Saving the binding on one
    manifest lifts the same mapping on the next one out of REVIEW."""
    token, ids = binding_org
    before = _suggestions(app_client, token, ids["manifest_b"], ids["source_version_id"])
    name_before = next(s for s in before["suggestions"] if s["field_id"] == "colleague_first_name")
    assert name_before["band"] == cf.Band.REVIEW.value

    saved = app_client.post(
        f"{API}/template-manifests/{ids['manifest_a']}/bindings",
        json={"source_version_id": ids["source_version_id"],
              "field_bindings": {"colleague_first_name": "Colleague First Name"}, "value_map": {}},
        headers=_auth(token),
    )
    assert saved.status_code == 201, saved.text

    after = _suggestions(app_client, token, ids["manifest_b"], ids["source_version_id"])
    name_after = next(s for s in after["suggestions"] if s["field_id"] == "colleague_first_name")

    assert any(e.startswith("historical_approvals:1") for e in name_after["evidence"])
    assert name_after["score"] > name_before["score"]
    assert name_after["band"] == cf.Band.CONFIRM.value
    assert name_after["vetoes"] == []
    # Confirm still means a human clicks. §13 keeps auto-accept for 0.97, which
    # four of seven signals cannot reach.
    assert name_after["auto_apply"] is False


def test_a_manifests_own_saved_binding_is_not_precedent_for_itself(app_client, binding_org):
    """A suggestion that cites the row a reviewer saved on this very screen is
    citing itself. It would climb a band on the next page load with no new
    evidence behind it, which is how a mapping approves itself."""
    token, ids = binding_org
    saved = app_client.post(
        f"{API}/template-manifests/{ids['manifest_a']}/bindings",
        json={"source_version_id": ids["source_version_id"],
              "field_bindings": {"start_date": "Start Date"}, "value_map": {}},
        headers=_auth(token),
    )
    assert saved.status_code == 201, saved.text

    body = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])
    start = next(s for s in body["suggestions"] if s["field_id"] == "start_date")
    assert "historical_approvals:0_approvals" in start["evidence"]
    assert cf.VETO_NO_PRECEDENT in [v["code"] for v in start["vetoes"]]


def test_a_binding_that_names_fields_this_manifest_never_had_is_not_precedent(app_client, binding_org):
    """Saved bindings belong to a (manifest, source) pair, and the estate's are
    full of field ids this template has never heard of and of columns a
    reviewer deliberately left blank. Neither is evidence about anything here,
    and reading them as precedent would credit a mapping nobody made."""
    token, ids = binding_org
    saved = app_client.post(
        f"{API}/template-manifests/{ids['manifest_a']}/bindings",
        json={"source_version_id": ids["source_version_id"],
              "field_bindings": {
                  "colleague_first_name": "Colleague First Name",
                  "retired_field_from_another_template": "Notes",
                  "start_date": "",
              },
              "value_map": {}},
        headers=_auth(token),
    )
    assert saved.status_code == 201, saved.text

    body = _suggestions(app_client, token, ids["manifest_b"], ids["source_version_id"])
    by_id = {s["field_id"]: s for s in body["suggestions"]}
    assert "retired_field_from_another_template" not in by_id
    assert "historical_approvals:0_approvals" in by_id["start_date"]["evidence"]
    assert any(e.startswith("historical_approvals:1") for e in by_id["colleague_first_name"]["evidence"])


def test_the_request_scoped_embedder_vectorises_each_text_once():
    """Mapping memory compares a query against every stored entry and embeds
    the entry's context each time, so a binding screen costs fields x history
    embeddings: the customer with a year of approvals behind them gets the
    slowest screen. A vector is a pure function of its text."""
    import numpy as np

    from app.routers.bindings import _MemoisedEmbedder

    class _CountingEmbedder:
        dimensions = 8
        name = "counting"

        def __init__(self):
            self.batches = 0

        def embed(self, texts):
            self.batches += 1
            return np.ones((len(texts), self.dimensions)) / np.sqrt(self.dimensions)

    inner = _CountingEmbedder()
    embedder = _MemoisedEmbedder(inner)
    first = embedder.embed(["start date", "annual salary"])
    second = embedder.embed(["start date", "annual salary"])

    assert inner.batches == 1, "the second call must be answered from the cache"
    assert np.array_equal(first, second)
    assert embedder.embed([]).shape == (0, 8)
    assert embedder.dimensions == 8 and embedder.name == "counting"
