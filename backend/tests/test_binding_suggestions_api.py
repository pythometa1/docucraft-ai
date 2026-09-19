"""The binding screen's answer, seen the way a reviewer sees it.

`binding-suggestions` is where §13 stops being a design note and becomes
something a person clicks. The failure it has to prevent is a screen that shows
a number: a reviewer handed "0.95" against two hundred fields cannot tell an
approved-forty-two-times mapping from a lucky string match, so they either
accept everything or re-read everything -- and one of those ends with a letter
that states the wrong salary.

The response says what to do and why, in words: a status (matched / confirm /
review / unmatched), a short reason with no number in it, and the other columns
in ranked order. The score, band, vetoes and evidence behind it are the scoring
method, so they stay on the server -- in the suggestion log, for calibration.

So these tests check two things. The HTTP shape, which must carry no score,
band, weight or evidence. And the scoring itself, read off the plan the
endpoint built (captured on its way to the suggestion log), so every property
the old response-level assertions protected is still asserted.
"""

from __future__ import annotations

import json

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


@pytest.fixture
def plans(monkeypatch):
    """Every plan `binding-suggestions` builds, captured where it is logged.

    The response no longer carries the scores; the plan does, and it is exactly
    what the suggestion log records.
    """
    from app.routers import bindings

    captured = []
    original = bindings.record_binding_suggestions

    def spy(db, **kwargs):
        captured.append(kwargs["plan"])
        return original(db, **kwargs)

    monkeypatch.setattr(bindings, "record_binding_suggestions", spy)
    return captured


def _suggestions(app_client, token, manifest_id, source_version_id):
    response = app_client.get(
        f"{API}/template-manifests/{manifest_id}/binding-suggestions",
        params={"source_version_id": source_version_id}, headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _scored(app_client, token, manifest_id, source_version_id, plans):
    """The response, and the plan's suggestions by field id."""
    body = _suggestions(app_client, token, manifest_id, source_version_id)
    return body, {s.field_id: s for s in plans[-1].suggestions}


def _veto_codes(suggestion) -> list:
    return list(suggestion.vetoes)


def _keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def test_the_response_says_what_to_do_and_not_how_it_was_scored(app_client, binding_org):
    """Per field: a column, a status, a reason in words, and ranked alternatives
    as names. No score, band, weight, evidence or policy block anywhere."""
    token, ids = binding_org
    body = _suggestions(app_client, token, ids["manifest_a"], ids["source_version_id"])

    by_id = {s["field_id"]: s for s in body["suggestions"]}
    assert set(by_id) == {"colleague_first_name", "start_date", "annual_salary"}
    for suggestion in by_id.values():
        assert set(suggestion) == {
            "field_id", "column", "status", "reason", "alternatives", "origin", "type",
            "sample_value",
        }
        assert suggestion["status"] in {"matched", "confirm", "review", "unmatched"}
        assert suggestion["reason"] and not any(ch.isdigit() for ch in suggestion["reason"])
        assert all(isinstance(a, str) for a in suggestion["alternatives"])

    keys = _keys(body)
    for forbidden in ("score", "band", "weight", "weights", "evidence", "vetoes", "confidence",
                      "confidence_policy", "band_summary", "method", "rationale",
                      "max_attainable_score", "auto_accept_reachable", "signals_computed"):
        assert forbidden not in keys, forbidden
    text = json.dumps(body).lower()
    for vendor in ("gpt", "claude", "gemini", "openai", "anthropic", "llm:"):
        assert vendor not in text


def test_the_binding_screen_says_why_and_not_only_how_much(app_client, binding_org, plans):
    """Every suggestion is still scored, banded and evidenced -- on the server.
    The reason the reviewer reads is derived from that evidence."""
    token, ids = binding_org
    body, by_id = _scored(app_client, token, ids["manifest_a"], ids["source_version_id"], plans)

    for suggestion in by_id.values():
        assert suggestion.band in {b.value for b in cf.Band}
        assert suggestion.evidence, "a score with no evidence is the thing this replaced"

    name = by_id["colleague_first_name"]
    assert name.column == "Colleague First Name"
    assert name.confidence != 0.95, "the per-tier constant is gone"
    assert "exact_name_match" in name.evidence

    public = {s["field_id"]: s for s in body["suggestions"]}["colleague_first_name"]
    assert public["column"] == "Colleague First Name"
    assert public["reason"] == "Column name matches"


def test_a_type_mismatch_reaches_the_reviewer_with_the_rule_that_fired(app_client, binding_org, plans):
    """The salary column holds "to be confirmed". Every name signal says the
    mapping is right, which is exactly why the type gate exists -- and the
    reviewer is told what is wrong, in words, not by a code."""
    token, ids = binding_org
    body, by_id = _scored(app_client, token, ids["manifest_a"], ids["source_version_id"], plans)
    salary = by_id["annual_salary"]

    assert salary.column == "Annual Salary"
    assert salary.declared_type == "currency"
    assert salary.observed_type == "text"
    assert cf.VETO_TYPE_MISMATCH in _veto_codes(salary)
    # A veto sends a candidate to REVIEW, not BLOCK: §13's signal table says
    # "Vetoes -- force REVIEW regardless of computed score", and BLOCK is the
    # band for a score below 0.50. The distinction is load-bearing on a new
    # installation, where the "no precedent and no family" veto is true of
    # every mapping and BLOCK would make the product unusable on day one.
    assert salary.band == cf.Band.REVIEW.value
    assert salary.auto_applicable is False

    public = {s["field_id"]: s for s in body["suggestions"]}["annual_salary"]
    assert public["status"] == "review"
    assert public["reason"] == "Values don't look like this kind of field"


def test_a_cold_estate_sends_everything_to_review_and_says_so(app_client, binding_org, plans):
    """§13's day-one posture. Nothing has been approved before, so nothing is
    auto-applied and every field asks the reviewer to check it."""
    token, ids = binding_org
    body, by_id = _scored(app_client, token, ids["manifest_a"], ids["source_version_id"], plans)

    bands = [s.band for s in by_id.values() if s.column]
    assert bands.count(cf.Band.AUTO_ACCEPT.value) == 0
    assert bands.count(cf.Band.REVIEW.value) == 3
    assert all(s.auto_applicable is False for s in by_id.values())
    assert all(
        cf.VETO_NO_PRECEDENT in _veto_codes(s)
        for s in by_id.values() if s.field_id != "annual_salary"
    )
    assert [s["status"] for s in body["suggestions"]] == ["review"] * 3


def test_the_scorer_admits_which_signals_it_could_not_compute():
    """Three of §13's seven signals have nothing to read yet, so nothing can
    reach the auto-accept band today. Asserted on the resolver, where the facts
    live; the response no longer carries them."""
    from app.generation.source_resolver import (
        SIGNALS_COMPUTED, SIGNALS_NOT_COMPUTED, max_attainable_score,
    )

    assert cf.WEIGHTS_CALIBRATED is False
    assert set(SIGNALS_COMPUTED) | set(SIGNALS_NOT_COMPUTED) == set(cf.INITIAL_WEIGHTS)
    assert max_attainable_score() < cf.AUTO_ACCEPT_FLOOR
    assert all(reason for reason in SIGNALS_NOT_COMPUTED.values())


def test_a_mapping_confirmed_on_another_template_comes_back_as_precedent(app_client, binding_org, plans):
    """The whole point of mapping memory: the second template from a customer
    must not be reviewed as though it were the first. Saving the binding on one
    manifest lifts the same mapping on the next one out of REVIEW."""
    token, ids = binding_org
    _body, before = _scored(app_client, token, ids["manifest_b"], ids["source_version_id"], plans)
    name_before = before["colleague_first_name"]
    assert name_before.band == cf.Band.REVIEW.value

    saved = app_client.post(
        f"{API}/template-manifests/{ids['manifest_a']}/bindings",
        json={"source_version_id": ids["source_version_id"],
              "field_bindings": {"colleague_first_name": "Colleague First Name"}, "value_map": {}},
        headers=_auth(token),
    )
    assert saved.status_code == 201, saved.text

    body, after = _scored(app_client, token, ids["manifest_b"], ids["source_version_id"], plans)
    name_after = after["colleague_first_name"]

    assert any(e.startswith("historical_approvals:1") for e in name_after.evidence)
    assert name_after.confidence > name_before.confidence
    assert name_after.band == cf.Band.CONFIRM.value
    assert name_after.vetoes == []
    # Confirm still means a human clicks. §13 keeps auto-accept for 0.97, which
    # four of seven signals cannot reach.
    assert name_after.auto_applicable is False

    public = {s["field_id"]: s for s in body["suggestions"]}["colleague_first_name"]
    assert public["status"] == "confirm"


def test_a_manifests_own_saved_binding_is_not_precedent_for_itself(app_client, binding_org, plans):
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

    _body, by_id = _scored(app_client, token, ids["manifest_a"], ids["source_version_id"], plans)
    start = by_id["start_date"]
    assert "historical_approvals:0_approvals" in start.evidence
    assert cf.VETO_NO_PRECEDENT in _veto_codes(start)


def test_a_binding_that_names_fields_this_manifest_never_had_is_not_precedent(app_client, binding_org, plans):
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

    _body, by_id = _scored(app_client, token, ids["manifest_b"], ids["source_version_id"], plans)
    assert "retired_field_from_another_template" not in by_id
    assert "historical_approvals:0_approvals" in by_id["start_date"].evidence
    assert any(e.startswith("historical_approvals:1") for e in by_id["colleague_first_name"].evidence)


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


def test_name_match_reason_says_which_route_matched():
    """`exact_name_match` fires for a dictionary alias and a MERGEFIELD code as
    well as for a literal name match. Only the last may say the names match:
    `letter_date` <- "Offer Date" matched because a reviewer confirmed the pair
    before, and the screen must not claim otherwise."""
    from app.routers.bindings import _name_match_reason

    assert _name_match_reason("dictionary") == "Matched to this column before in your organisation"
    assert _name_match_reason("mergefield") == "Matches the field in your template"
    assert _name_match_reason("exact_slug") == "Column name matches"
    assert _name_match_reason(None) == "Column name matches"
