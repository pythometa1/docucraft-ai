"""§22's instrumentation: the calibration log, the QA log, and honest metrics.

Three failures are pinned here, and all three are failures of silence rather
than of computation.

The first is the one §22 says cannot be undone: a mapping suggestion shown to a
reviewer, decided, and never written down. The §13 weights are declared starting
values and stay that way until somebody fits them against real decisions, so a
binding session that leaves no trace destroys the only data that fit could ever
use.

The second is a metric that reads 0.0 when it means "nobody has measured this".
Escaped error rate is the case that matters -- an organisation with no way to
report a wrong value produces the same zero as an organisation that has never
had one, and only one of those is good news.

The third is §18's table being quoted as though it were a benchmark. Every
figure in it is a design target; a row nobody has exercised has to say so.
"""

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import metrics
from app.compiler import confidence
from app.db import SessionLocal
from app.generation.renderers import HTML_ASSEMBLY, OOXML_FILL
from app.models import (
    DocumentVersion, GeneratedDocument, GenerationJob, ManifestGeneration, OperationTiming,
    Organization, Project, QaFailureLog, SourceFile, SourceVersion, SuggestionLog, TemplateCluster,
    TemplateClusterMember, TemplateFile, TemplateManifest, TemplateVersion, User,
)
from app.qa.policy import BLOCKING, PLACEHOLDER_REMAINS, REQUIRED_VALUE_MISSING, WARNING
from app.security import create_access_token, hash_password
from app.storage import save_bytes

API = "/api/v1"

#: `projects.display_id` is unique across the whole database, so every fixture
#: org needs its own. A fixed number works until the second test asks for one.
_DISPLAY_IDS = itertools.count(70_000)

#: Hashing a password is deliberately slow. Four users per test times two dozen
#: tests is a minute of the suite spent proving bcrypt works.
_PASSWORD_HASH = hash_password("pw")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org(app_client):
    """A private organisation per test.

    The metrics are aggregates over a whole tenant, so a shared org would make
    every assertion depend on what the rest of the suite happened to insert.
    """
    db = SessionLocal()
    try:
        organisation = Organization(name=f"MetricsOrg-{datetime.now().timestamp()}")
        db.add(organisation)
        db.flush()
        people = {}
        for role in ("org_admin", "auditor", "approver", "generator"):
            user = User(
                org_id=organisation.id, email=f"{role}-{organisation.id}@metrics.test",
                full_name=role, password_hash=_PASSWORD_HASH, role_key=role,
            )
            db.add(user)
            db.flush()
            people[role] = (user.id, create_access_token(user.id, organisation.id))
        project = Project(
            org_id=organisation.id, display_id=next(_DISPLAY_IDS), name="Metrics", region="Europe",
            function="Human Resources", document_type="Offer Letter", language="English",
            status="pending", created_by=people["org_admin"][0],
        )
        db.add(project)
        db.flush()
        made = {"org_id": organisation.id, "project_id": project.id, "people": people}
        db.commit()
    finally:
        db.close()
    return made


def _template(db, org, *, name="t.docx", created_at=None):
    tf = TemplateFile(
        org_id=org["org_id"], project_id=org["project_id"], name=name, status="ready",
        created_by=org["people"]["org_admin"][0],
    )
    if created_at:
        tf.created_at = created_at
    db.add(tf)
    db.flush()
    tv = TemplateVersion(
        template_file_id=tf.id, org_id=org["org_id"], version_no=1, blob_path="x.docx",
        created_by=org["people"]["org_admin"][0],
    )
    db.add(tv)
    db.flush()
    tf.current_version_id = tv.id
    return tf, tv


def _manifest(db, org, tf, tv, *, fields=None, created_at=None, version_no=1):
    m = TemplateManifest(
        org_id=org["org_id"], template_file_id=tf.id, template_version_id=tv.id,
        version_no=version_no, status="draft", fields=fields or [], conditions=[], blocks=[],
        delete_always=[], confidence=1.0, compiled_by="rule_based", prescan_summary={},
        created_by=org["people"]["org_admin"][0],
    )
    if created_at:
        m.created_at = created_at
    db.add(m)
    db.flush()
    return m


def _document(db, org, *, blob_path="generated/x.docx", renderer=OOXML_FILL, versions=1, approved=False):
    doc = GeneratedDocument(
        org_id=org["org_id"], project_id=org["project_id"], display_id=50001,
        language="en", status="draft",
    )
    db.add(doc)
    db.flush()
    made = []
    for index in range(versions):
        dv = DocumentVersion(
            document_id=doc.id, org_id=org["org_id"], version_no=index + 1,
            blob_path=blob_path if index == 0 else f"{blob_path}.v{index + 1}",
            renderer=renderer, status="approved" if approved and index == 0 else "draft",
            created_by=org["people"]["org_admin"][0],
        )
        if approved and index == 0:
            dv.approved_at = metrics._utcnow()
            dv.approved_by = org["people"]["approver"][0]
        db.add(dv)
        db.flush()
        made.append(dv)
    doc.current_version_id = made[-1].id
    return doc, made


class _Suggestion:
    """The shape `suggest_bindings` hands back, reduced to what the log reads."""

    def __init__(self, field_id, column, method, **scored):
        self.field_id = field_id
        self.column = column
        self.method = method
        self.confidence = scored.get("confidence", 0.0)
        self.band = scored.get("band")
        self.vetoes = scored.get("vetoes", [])
        self.evidence = scored.get("evidence", [])


class _Plan:
    def __init__(self, suggestions):
        self.suggestions = suggestions


# ------------------------------------------------------- the §13 bridge

def test_a_fuzzy_string_ratio_is_not_treated_as_semantic_evidence():
    """§13's semantic signal is an embedding cosine with a 0.75 floor. Feeding a
    SequenceMatcher ratio through that door would inflate every score in the
    estate with evidence the record never described, and the mapping this
    product gets wrong is the plausible one."""
    scored = metrics.score_suggestion(object_id="start_date", column="Cmnc Dt", method="fuzzy")

    assert scored.band == confidence.Band.BLOCK.value
    assert scored.score < confidence.CONFIRM_FLOOR
    assert not any("semantic" in e for e in scored.evidence)


def test_a_model_proposal_carries_no_signal_of_its_own():
    """§13 excludes a model's stated confidence twice over. A mapping the model
    proposed and nothing else supports must not out-score one a human has
    approved forty times."""
    llm = metrics.score_suggestion(object_id="manager_name", column="Mgr", method="llm")
    precedent = metrics.score_suggestion(
        object_id="manager_name", column="Mgr", method="exact_slug", approvals=40
    )

    assert llm.score < precedent.score
    assert llm.band == confidence.Band.BLOCK.value


def test_a_name_match_that_did_not_happen_is_not_logged_as_evidence_that_it_did():
    """`Signal.as_evidence` prints the detail when there is one, so a
    zero-strength `exact_name_match:fuzzy` would read to a reviewer -- and to
    whoever fits the weights later -- as a name match that occurred."""
    scored = metrics.score_suggestion(object_id="salary", column="Base Pay", method="fuzzy")

    assert "exact_name_match:fuzzy" not in scored.evidence


def test_an_object_nothing_matched_is_still_scored_and_banded():
    """`confidence.decide` refuses an empty candidate list on purpose. The
    unmatched object still has to appear in the corpus, or the auto-map rate
    quietly loses its hardest denominator."""
    scored = metrics.score_suggestion(object_id="colleague_type", column=None, method="unmatched")

    assert scored.score == 0.0
    assert scored.band == confidence.Band.BLOCK.value
    assert scored.evidence == ()


def test_a_money_field_never_reaches_the_auto_accept_band():
    """§13: a money or identifier field is never auto-accepted however strong
    the evidence. That click is the only thing between a very confident wrong
    mapping and a letter stating the wrong salary."""
    scored = metrics.score_suggestion(
        object_id="annual_salary", column="Annual Salary", method="exact_slug",
        target_field={"id": "annual_salary", "type": "currency"},
        observed_type="number", approvals=200,
    )

    assert scored.band != confidence.Band.AUTO_ACCEPT.value


def test_a_column_of_numbers_is_typed_from_its_values_not_its_file_format():
    """Every cell in a CSV is text on disk. Reporting that to the type gate
    would veto every mapping in the estate; reporting the declared type would
    defeat the gate entirely."""
    assert metrics.observed_column_type(["85000", "92,500", "£70000"]) == "number"
    assert metrics.observed_column_type(["2026-03-01", "15/04/2026"]) == "date"
    assert metrics.observed_column_type(["Full time", "Part time"]) == "text"


def test_an_empty_column_is_unknown_rather_than_text():
    """UNKNOWN withholds both the §13 weight and the §13 veto, which is the
    honest answer for a column nobody probed. Calling it text would veto every
    date field bound to a column that happened to be blank in the sample."""
    assert metrics.observed_column_type([None, "", "  " and ""]) is None


# --------------------------------------------------- the calibration log

def _log_a_session(db, org, *, suggestions, manifest=None, source_version_id="sv-1"):
    tf, tv = _template(db, org)
    manifest = manifest or _manifest(db, org, tf, tv, fields=[{"id": "first_name", "type": "string"}])
    metrics.record_binding_suggestions(
        db, org_id=org["org_id"], manifest=manifest, plan=_Plan(suggestions),
        source_version_id=source_version_id, records=[{"First Name": "Ada"}], columns=["First Name"],
    )
    db.commit()
    return manifest


def test_every_suggestion_in_a_binding_session_is_logged_with_its_evidence(org):
    """§22: this is the only dataset that can ever calibrate §13's weights, and
    it cannot be reconstructed later. A session that leaves no row destroys
    training data that existed for the length of one HTTP response."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("first_name", "First Name", "exact_slug"),
            _Suggestion("colleague_type", None, "unmatched"),
        ])
        rows = db.scalars(
            select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id)
        ).all()
    finally:
        db.close()

    by_object = {r.object_id: r for r in rows}
    assert set(by_object) == {"first_name", "colleague_type"}
    assert by_object["first_name"].evidence, "the evidence a reviewer saw has to be in the row"
    assert by_object["first_name"].band in {b.value for b in confidence.Band}
    assert by_object["first_name"].reviewer_decision == metrics.PENDING
    # The object nothing matched is logged too. Dropping it would remove the
    # hardest cases from the denominator of the metric §22 already calls vain.
    assert by_object["colleague_type"].suggested_column is None


def test_re_opening_the_binding_screen_does_not_duplicate_the_corpus(org):
    """A reviewer who reloads the page has not made a second decision. Counting
    the same suggestion twice biases any fit towards whatever people look at
    most, which is not the same as whatever is hardest."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[_Suggestion("first_name", "First Name", "exact_slug")])
        _log_a_session(
            db, org, manifest=manifest,
            suggestions=[_Suggestion("first_name", "First Name", "exact_slug")],
        )
        rows = db.scalars(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id)).all()
    finally:
        db.close()

    assert len(rows) == 1


def test_saving_a_binding_records_what_the_reviewer_actually_did(org):
    """Accept, correct and reject are three different labels, and a fit needs
    all three. `final_column` is what makes a correction usable: knowing a
    reviewer said no teaches far less than knowing what they said instead."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("first_name", "First Name", "exact_slug"),
            _Suggestion("start_date", "Joining Dt", "fuzzy"),
            _Suggestion("manager", "Mgr", "fuzzy"),
            _Suggestion("colleague_type", None, "unmatched"),
        ])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={
                "first_name": "First Name",   # accepted as proposed
                "start_date": "Start Date",   # corrected
                "colleague_type": "Type",     # a human supplied what nothing matched
            },
            decided_by=org["people"]["org_admin"][0],
        )
        db.commit()
        rows = {
            r.object_id: r
            for r in db.scalars(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id)).all()
        }
    finally:
        db.close()

    assert rows["first_name"].reviewer_decision == metrics.ACCEPTED
    assert rows["start_date"].reviewer_decision == metrics.EDITED
    assert rows["start_date"].final_column == "Start Date"
    assert rows["manager"].reviewer_decision == metrics.REJECTED
    assert rows["colleague_type"].reviewer_decision == metrics.EDITED
    assert all(r.decided_at is not None and r.decided_by for r in rows.values())


def test_a_decided_row_is_never_rewritten_by_a_later_suggestion(org):
    """The evidence in a decided row is the evidence the reviewer saw. Restating
    it from today's precedent would calibrate the weights against a screen
    nobody was ever shown."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[_Suggestion("first_name", "First Name", "exact_slug")])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"first_name": "First Name"}, decided_by=org["people"]["org_admin"][0],
        )
        db.commit()
        _log_a_session(
            db, org, manifest=manifest,
            suggestions=[_Suggestion("first_name", "Different Column", "fuzzy")],
        )
        row = db.scalar(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id))
    finally:
        db.close()

    assert row.reviewer_decision == metrics.ACCEPTED
    assert row.suggested_column == "First Name"


def test_the_log_keeps_the_score_the_reviewer_was_actually_shown(org):
    """The resolver scores its own candidates now. Re-deriving the score here
    would log a number that was never on screen, and calibrate §13 against it."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion(
                "first_name", "First Name", "exact_slug",
                confidence=0.8123, band=confidence.Band.CONFIRM.value,
                vetoes=[confidence.VETO_NO_PRECEDENT], evidence=["exact_name_match:exact_slug"],
            ),
        ])
        row = db.scalar(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id))
    finally:
        db.close()

    assert row.score == pytest.approx(0.8123)
    assert row.band == confidence.Band.CONFIRM.value
    assert row.vetoes == [confidence.VETO_NO_PRECEDENT]


def test_every_logged_row_records_which_weight_table_scored_it(org):
    """A fit over rows scored under two different weight tables is a fit over
    nothing. The regime has to be on the row, not inferred from its date."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[_Suggestion("first_name", "First Name", "exact_slug")])
        row = db.scalar(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id))
    finally:
        db.close()

    assert row.weights_calibrated is confidence.WEIGHTS_CALIBRATED


def test_precedent_counts_approvals_of_the_pair_not_uses_of_the_field(org):
    """§13's historical signal is "n previous approvals of this exact mapping".
    A usage counter on the field would credit `salary -> Bonus` with every time
    salary was ever bound to anything."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[_Suggestion("salary", "Annual Salary", "exact_slug")])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"salary": "Annual Salary"}, decided_by=org["people"]["org_admin"][0],
        )
        db.commit()

        same = metrics._precedent_for(db, org_id=org["org_id"], object_id="salary", column="Annual Salary")
        other = metrics._precedent_for(db, org_id=org["org_id"], object_id="salary", column="Bonus")
    finally:
        db.close()

    assert same == 1
    assert other == 0


# ---------------------------------------------------------------- QA log

def test_a_qa_failure_is_logged_with_the_check_and_the_object(org):
    """`qa_notes` held these as prose, so "which check fired" could only be
    recovered by matching strings and "on which field" by reading a sentence.
    Neither survives contact with a dashboard."""
    db = SessionLocal()
    try:
        tf, tv = _template(db, org)
        manifest = _manifest(db, org, tf, tv)
        metrics.record_qa_findings(
            db, org_id=org["org_id"], manifest_id=manifest.id, generation_id="g-1",
            document_version_id="dv-1",
            findings=[
                {"check": REQUIRED_VALUE_MISSING, "object_id": "salary", "detail": "no value", "severity": BLOCKING},
                {"check": PLACEHOLDER_REMAINS, "object_id": None, "detail": "[NAME] left", "severity": WARNING},
            ],
        )
        db.commit()
        rows = db.scalars(
            select(QaFailureLog).where(QaFailureLog.document_version_id == "dv-1")
        ).all()
    finally:
        db.close()

    by_check = {r.check_name: r for r in rows}
    assert by_check[REQUIRED_VALUE_MISSING].object_id == "salary"
    assert by_check[REQUIRED_VALUE_MISSING].phase == metrics.PRE_APPROVAL
    assert by_check[PLACEHOLDER_REMAINS].severity == WARNING
    assert all(not r.overridden for r in rows)


def test_an_unknown_severity_is_refused_rather_than_stored(org):
    """A severity the policy vocabulary does not know would sit in the log
    looking like a decision somebody made."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            metrics.record_qa_findings(
                db, org_id=org["org_id"],
                findings=[{"check": PLACEHOLDER_REMAINS, "detail": "x", "severity": "advisory"}],
            )
    finally:
        db.rollback()
        db.close()


def test_approving_a_document_records_who_signed_under_its_warnings(app_client, org):
    """§22 asks whether a human overrode a QA failure. Without a name against
    the warning, "the check fired and the letter went out anyway" is a fact
    nobody owns."""
    db = SessionLocal()
    try:
        _doc, versions = _document(db, org)
        version_id = versions[0].id
        metrics.record_qa_findings(
            db, org_id=org["org_id"], document_version_id=version_id,
            findings=[{"check": PLACEHOLDER_REMAINS, "detail": "overflow estimated", "severity": WARNING}],
        )
        db.commit()
    finally:
        db.close()

    token = org["people"]["approver"][1]
    response = app_client.post(f"{API}/document-versions/{version_id}:approve", headers=_auth(token))
    assert response.status_code == 200

    db = SessionLocal()
    try:
        row = db.scalar(select(QaFailureLog).where(QaFailureLog.document_version_id == version_id))
    finally:
        db.close()
    assert row.overridden is True
    assert row.overridden_by == org["people"]["approver"][0]
    assert row.overridden_at is not None


def test_an_escaped_error_needs_a_description_of_what_was_wrong(org):
    """It is a compliance record before it is a metric. A count with no detail
    cannot be investigated, and an escaped error that cannot be investigated is
    a number on a slide."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            metrics.record_escaped_error(
                db, org_id=org["org_id"], document_version_id="dv-1",
                object_id="salary", detail="   ",
            )
        with pytest.raises(ValueError):
            metrics.record_escaped_error(
                db, org_id=org["org_id"], document_version_id="dv-1", object_id="salary",
                detail="wrong", check_name="not_a_registered_check",
            )
    finally:
        db.rollback()
        db.close()


# --------------------------------------------------------------- metrics

def _metrics_for(org, **kwargs):
    db = SessionLocal()
    try:
        return {m.key: m for m in metrics.compute_metrics(db, org_id=org["org_id"], **kwargs)}
    finally:
        db.close()


def test_a_metric_with_no_input_is_unavailable_rather_than_zero(org):
    """Zero reads as an achievement. On a brand-new organisation every one of
    these has no input at all, and printing 0.0 escaped errors per 10,000 fields
    for an estate that has generated nothing is the most flattering wrong number
    this system could produce."""
    computed = _metrics_for(org)

    assert set(computed) == set(metrics.METRIC_ORDER)
    for key, metric in computed.items():
        assert not metric.available, f"{key} claims to be computable on an empty org"
        assert metric.value is None
        assert metric.unavailable_reason and metric.unavailable_reason.strip()


def test_escaped_error_rate_outranks_the_vanity_metric_in_the_returned_order(org):
    """§22: "Auto-map rate is the vanity metric. Escaped error rate is the real
    one." A dict would leave that ranking to whoever renders the payload."""
    db = SessionLocal()
    try:
        ordered = metrics.compute_metrics(db, org_id=org["org_id"])
    finally:
        db.close()

    assert ordered[0].key == "escaped_error_rate"
    assert ordered[-1].key == "auto_map_rate"
    assert [m.rank for m in ordered] == list(range(1, len(ordered) + 1))
    assert "vanity" in ordered[-1].note


def test_escaped_error_rate_stays_unavailable_until_a_defect_has_ever_been_reported(org):
    """An organisation that has never reported a wrong value and one with no way
    to report one produce the same numerator. Only one of them is good news, and
    the metric must not claim to know which."""
    db = SessionLocal()
    try:
        _doc, versions = _document(db, org, approved=True)
        # A real manifest row, not an invented id: manifest_generations carries a
        # foreign key that PostgreSQL enforces and SQLite ignores by default.
        tf, tv = _template(db, org)
        manifest = _manifest(db, org, tf, tv)
        db.add(ManifestGeneration(
            org_id=org["org_id"], manifest_id=manifest.id, source_record={},
            field_lineage=[{"field_id": f"f{i}"} for i in range(20)],
            condition_lineage=[], qa_passed=True, qa_notes=[],
            blob_path=versions[0].blob_path, created_by=org["people"]["org_admin"][0],
        ))
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["escaped_error_rate"]
    assert not metric.available
    assert "reported" in metric.unavailable_reason
    assert metric.sample["fields_in_approved_outputs"] == 20


def test_escaped_error_rate_is_counted_per_ten_thousand_fields(org):
    """§22 asks for wrong values per 10,000 fields, not per document. A letter
    with forty fields and one with four are not comparable units of exposure."""
    db = SessionLocal()
    try:
        _doc, versions = _document(db, org, approved=True)
        # A real manifest row, not an invented id: manifest_generations carries a
        # foreign key that PostgreSQL enforces and SQLite ignores by default.
        tf, tv = _template(db, org)
        manifest = _manifest(db, org, tf, tv)
        db.add(ManifestGeneration(
            org_id=org["org_id"], manifest_id=manifest.id, source_record={},
            field_lineage=[{"field_id": f"f{i}"} for i in range(25)],
            condition_lineage=[], qa_passed=True, qa_notes=[],
            blob_path=versions[0].blob_path, created_by=org["people"]["org_admin"][0],
        ))
        metrics.record_escaped_error(
            db, org_id=org["org_id"], document_version_id=versions[0].id,
            object_id="annual_salary", detail="stated last year's salary",
        )
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["escaped_error_rate"]
    assert metric.available
    assert metric.value == pytest.approx(1 * 10_000 / 25)


def test_auto_map_rate_counts_the_objects_nothing_matched(org):
    """Excluding unmatched objects from the denominator inflates precisely the
    number §22 warns is already the easiest one to flatter."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("a", "A", "exact_slug", band=confidence.Band.AUTO_ACCEPT.value, confidence=0.99),
            _Suggestion("b", None, "unmatched", band=confidence.Band.BLOCK.value),
        ])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"a": "A"}, decided_by=org["people"]["org_admin"][0],
        )
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["auto_map_rate"]
    assert metric.sample["decided"] == 2
    assert metric.value == pytest.approx(0.5)


def test_reviewer_touches_ignores_the_mappings_nobody_had_to_look_at(org):
    """An auto-accepted mapping cost nobody anything. Counting it as a touch
    makes a better compiler look like a heavier review burden, which is the
    opposite of what §22 wants this number to drive."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("a", "A", "exact_slug", band=confidence.Band.AUTO_ACCEPT.value, confidence=0.99),
            _Suggestion("b", "B", "fuzzy", band=confidence.Band.REVIEW.value, confidence=0.6),
            _Suggestion("c", "C", "fuzzy", band=confidence.Band.BLOCK.value, confidence=0.1),
        ])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"a": "A", "b": "B2", "c": "C"}, decided_by=org["people"]["org_admin"][0],
        )
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["reviewer_touches_per_template"]
    assert metric.sample["auto_accepted"] == 1
    assert metric.value == pytest.approx(2.0)


def test_qa_block_rate_is_the_share_of_generations_a_check_stopped(org):
    """§17's gates only mean something if somebody can see how often they fire.
    A rising rate is the signal that a source schema has drifted."""
    db = SessionLocal()
    try:
        tf, tv = _template(db, org)
        manifest = _manifest(db, org, tf, tv)
        for passed in (True, True, False, True):
            db.add(ManifestGeneration(
                org_id=org["org_id"], manifest_id=manifest.id, source_record={}, field_lineage=[],
                condition_lineage=[], qa_passed=passed, qa_notes=[],
                created_by=org["people"]["org_admin"][0],
            ))
        metrics.record_qa_findings(
            db, org_id=org["org_id"],
            findings=[{"check": PLACEHOLDER_REMAINS, "detail": "x", "severity": BLOCKING}],
        )
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["qa_block_rate"]
    assert metric.value == pytest.approx(0.25)
    assert metric.sample["by_check"][PLACEHOLDER_REMAINS] == 1


def test_regeneration_rate_counts_documents_that_had_to_be_produced_twice(org):
    """§22 calls this the proxy for defects QA did not catch. A second version
    means the first output was not the one that was issued."""
    db = SessionLocal()
    try:
        _document(db, org, blob_path="generated/one.docx", versions=1)
        _document(db, org, blob_path="generated/two.docx", versions=3)
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["regeneration_rate"]
    assert metric.sample == {"documents": 2, "regenerated": 1}
    assert metric.value == pytest.approx(0.5)


def test_family_reuse_is_unavailable_when_nothing_went_through_matching(org):
    """A reuse rate of zero on templates that were never put through family
    matching describes the pipeline, not the estate -- and §18 calls family
    reuse the primary cost lever, so the difference is a budget decision."""
    db = SessionLocal()
    try:
        _template(db, org, name="alone.docx")
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["family_reuse_rate"]
    assert not metric.available
    assert "family matching" in metric.unavailable_reason


def test_family_reuse_counts_the_templates_that_took_a_manifest_from_a_sibling(org):
    """The representative is the one that was compiled from scratch; everything
    else in the cluster is what reuse means."""
    db = SessionLocal()
    try:
        rep, _ = _template(db, org, name="rep.docx")
        member, _ = _template(db, org, name="member.docx")
        cluster = TemplateCluster(
            org_id=org["org_id"], label="Offer letters", representative_template_file_id=rep.id,
        )
        db.add(cluster)
        db.flush()
        db.add_all([
            TemplateClusterMember(
                cluster_id=cluster.id, org_id=org["org_id"], template_file_id=rep.id,
                template_name="rep.docx", similarity_score=1.0, is_representative=True,
            ),
            TemplateClusterMember(
                cluster_id=cluster.id, org_id=org["org_id"], template_file_id=member.id,
                template_name="member.docx", similarity_score=0.94, is_representative=False,
            ),
        ])
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["family_reuse_rate"]
    assert metric.value == pytest.approx(0.5)
    assert metric.sample["templates_resolved_through_a_family"] == 1


def test_manifest_churn_is_projected_onto_a_quarter(org):
    """§22 asks for versions per template per quarter. Reporting a 30-day count
    as though it were three months of history understates churn by a factor of
    three, and churn is the signal that a template or its schema is unstable."""
    db = SessionLocal()
    try:
        tf, tv = _template(db, org)
        for version_no in (1, 2, 3):
            _manifest(db, org, tf, tv, version_no=version_no)
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org, window_days=30)["manifest_churn"]
    assert metric.value == pytest.approx(round(3 * metrics.QUARTER_DAYS / 30, 2))


def test_zero_llm_share_excludes_versions_with_no_renderer_recorded(org):
    """A row written before the renderer column existed cannot be classified
    either way. Assuming it took the cheap deterministic path is how a cost and
    hallucination-exposure metric flatters itself."""
    db = SessionLocal()
    try:
        _document(db, org, blob_path="generated/a.docx", renderer=OOXML_FILL)
        _document(db, org, blob_path="generated/b.docx", renderer=HTML_ASSEMBLY)
        _document(db, org, blob_path="generated/c.docx", renderer=None)
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["zero_llm_generation_share"]
    assert metric.sample["unclassified_no_renderer_recorded"] == 1
    assert metric.value == pytest.approx(0.5)


def test_time_to_first_correct_document_runs_from_upload_to_the_first_qa_pass(org):
    """§22's definition, exactly: upload of a new template to the first
    QA-passing generated output. It is the only onboarding number a buyer
    feels."""
    uploaded = metrics._utcnow() - timedelta(hours=2)
    db = SessionLocal()
    try:
        tf, tv = _template(db, org, created_at=uploaded)
        manifest = _manifest(db, org, tf, tv)
        blocked = ManifestGeneration(
            org_id=org["org_id"], manifest_id=manifest.id, source_record={}, field_lineage=[],
            condition_lineage=[], qa_passed=False, qa_notes=["blocked"],
            created_by=org["people"]["org_admin"][0],
        )
        blocked.created_at = uploaded + timedelta(minutes=10)
        clean = ManifestGeneration(
            org_id=org["org_id"], manifest_id=manifest.id, source_record={}, field_lineage=[],
            condition_lineage=[], qa_passed=True, qa_notes=[],
            created_by=org["people"]["org_admin"][0],
        )
        clean.created_at = uploaded + timedelta(minutes=45)
        db.add_all([blocked, clean])
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["time_to_first_correct_document"]
    # The blocked generation does not stop the clock. A document that failed QA
    # is not a correct document.
    assert metric.value == pytest.approx(45 * 60, abs=2)


def test_a_template_still_being_onboarded_is_not_a_time_of_zero(org):
    """The clock is still running. Reporting zero would make an estate nobody
    has finished onboarding look like the fastest one on record."""
    db = SessionLocal()
    try:
        _template(db, org, name="in-flight.docx")
        db.commit()
    finally:
        db.close()

    metric = _metrics_for(org)["time_to_first_correct_document"]
    assert not metric.available
    assert "still running" in metric.unavailable_reason


def test_an_impossible_window_is_refused_rather_than_clamped(org):
    """Clamping answers a question nobody asked, and the answer looks
    authoritative."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            metrics.compute_metrics(db, org_id=org["org_id"], window_days=0)
        with pytest.raises(ValueError):
            metrics.slo_report(db, org_id=org["org_id"], window_days=99_999)
    finally:
        db.close()


# ------------------------------------------------------------- §18 SLOs

def _slos_for(org, **kwargs):
    db = SessionLocal()
    try:
        report = metrics.slo_report(db, org_id=org["org_id"], **kwargs)
    finally:
        db.close()
    return {row["operation"]: row for row in report["operations"]}, report


def test_an_slo_nobody_has_exercised_says_unmeasured_rather_than_meeting_it(org):
    """§18 opens by warning that none of its figures is a benchmark result. A
    row that inherited its target and rendered green would be that warning being
    ignored in code -- and those are the numbers that end up on a slide."""
    rows, report = _slos_for(org)

    assert set(rows) == set(metrics.SLO_BY_OPERATION)
    for operation, row in rows.items():
        assert row["status"] == "unmeasured", operation
        assert row["measured"] is None
        assert row["target_ms"] > 0
        assert row["reason"]
    assert report["measured_operations"] == 0
    assert "benchmark" in report["caveat"]


def test_a_measured_slo_keeps_target_and_measurement_in_separate_fields(org):
    """The whole job of the endpoint is to stop a design target being quoted as
    a measurement. One field holding both is how that happens."""
    db = SessionLocal()
    try:
        for duration in (100.0, 120.0, 3_000.0):
            metrics.record_timing(
                db, org_id=org["org_id"], operation=metrics.SINGLE_DOCX_RENDER, duration_ms=duration,
            )
        db.commit()
    finally:
        db.close()

    rows, _report = _slos_for(org)
    row = rows[metrics.SINGLE_DOCX_RENDER]

    assert row["target_ms"] == 1_500
    assert row["measured"]["samples"] == 3
    assert row["measured"]["p95_ms"] == 3_000.0
    # p95 of that sample is over the 1.5 s target, so the row must say so rather
    # than reporting the median and looking healthy.
    assert row["status"] == "breaching"


def test_a_failed_run_is_timed_but_kept_out_of_the_percentile(org):
    """How long a crash took is not how long the operation takes. Keeping the
    row matters too: a rising error count next to a healthy p95 is a different
    story from a rising p95."""
    db = SessionLocal()
    try:
        metrics.record_timing(
            db, org_id=org["org_id"], operation=metrics.TEMPLATE_PARSE, duration_ms=50.0,
        )
        with pytest.raises(RuntimeError):
            with metrics.timed(db, org_id=org["org_id"], operation=metrics.TEMPLATE_PARSE):
                raise RuntimeError("parser blew up")
        db.commit()
        recorded = db.scalars(
            select(OperationTiming).where(OperationTiming.org_id == org["org_id"])
        ).all()
    finally:
        db.close()

    rows, _report = _slos_for(org)
    assert len(recorded) == 2
    assert rows[metrics.TEMPLATE_PARSE]["measured"]["samples"] == 1
    assert rows[metrics.TEMPLATE_PARSE]["failed_runs"] == 1


def test_batch_throughput_is_projected_onto_a_thousand_documents(org):
    """§18's target is a batch of 1,000. Comparing a 250-row run against it as
    though it were 1,000 would report a batch four times too fast, which is
    exactly the direction nobody would question."""
    started = metrics._utcnow() - timedelta(minutes=10)
    db = SessionLocal()
    try:
        job = GenerationJob(
            org_id=org["org_id"], project_id=org["project_id"], status="completed",
            languages=["en"], progress={"rows_total": 250, "rows_done": 250},
            created_by=org["people"]["org_admin"][0],
        )
        job.started_at = started
        job.finished_at = started + timedelta(minutes=2)  # 120 s for 250 documents
        db.add(job)
        db.commit()
    finally:
        db.close()

    rows, _report = _slos_for(org)
    row = rows[metrics.BATCH_THROUGHPUT]

    # 120 s / 250 x 1,000 = 480 s, comfortably inside the 10-minute target.
    assert row["measured"]["headline_ms"] == pytest.approx(480_000, rel=0.01)
    assert row["status"] == "meeting"


def test_timing_an_operation_that_is_not_in_the_slo_table_is_refused(org):
    """A timing under a name the SLO report never looks at is work nobody will
    ever read, recorded as though somebody would."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            metrics.record_timing(
                db, org_id=org["org_id"], operation="render_the_thing", duration_ms=1.0,
            )
    finally:
        db.rollback()
        db.close()


def test_a_real_generation_records_its_render_time(app_client, org, fixtures_dir):
    """§18's single-render target is unmeasurable unless the production path
    itself is timed. A benchmark run by hand measures the benchmark."""
    db = SessionLocal()
    try:
        before = db.scalars(
            select(OperationTiming).where(
                OperationTiming.org_id == org["org_id"],
                OperationTiming.operation == metrics.SINGLE_DOCX_RENDER,
            )
        ).all()
    finally:
        db.close()

    token = org["people"]["org_admin"][1]
    template = fixtures_dir / "templates" / "hospira_offer.docx"
    with template.open("rb") as handle:
        upload = app_client.post(
            f"{API}/projects/{org['project_id']}/templates",
            headers=_auth(token),
            files={"file": (
                "offer.docx", handle.read(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )},
        )
    assert upload.status_code in (200, 201), upload.text

    db = SessionLocal()
    try:
        after = db.scalars(
            select(OperationTiming).where(
                OperationTiming.org_id == org["org_id"],
                OperationTiming.operation == metrics.TEMPLATE_PARSE,
            )
        ).all()
    finally:
        db.close()
    assert len(after) > len(before), "uploading a template must record a §18 parse timing"


# -------------------------------------------------------------- endpoint

def test_the_metrics_endpoint_is_gated_on_read_audit(app_client, org):
    """These numbers are a summary of a customer's error rate and review
    behaviour. The roles that may read the audit trail are exactly the roles
    that may read this."""
    denied = app_client.get(f"{API}/metrics", headers=_auth(org["people"]["generator"][1]))
    allowed = app_client.get(f"{API}/metrics", headers=_auth(org["people"]["auditor"][1]))

    assert denied.status_code == 403
    assert denied.json()["detail"]["error"]["details"]["required_capability"] == "read_audit"
    assert allowed.status_code == 200


def test_the_endpoint_never_reports_a_missing_metric_as_a_number(app_client, org):
    """The payload is what a dashboard renders. If "no data" arrives as 0.0 with
    a note beside it, the note is what gets dropped."""
    response = app_client.get(f"{API}/metrics", headers=_auth(org["people"]["auditor"][1]))
    body = response.json()

    assert [m["key"] for m in body["metrics"]][0] == "escaped_error_rate"
    assert [m["key"] for m in body["metrics"]][-1] == "auto_map_rate"
    for metric in body["metrics"]:
        if not metric["available"]:
            assert metric["value"] is None
            assert metric["unavailable_reason"]
    assert set(body["unavailable"]) == {m["key"] for m in body["metrics"] if not m["available"]}
    assert body["calibration"]["weights_calibrated"] is confidence.WEIGHTS_CALIBRATED


def test_one_tenants_metrics_never_include_another_tenants_estate(app_client, org, two_orgs):
    """The aggregate is the leak nobody looks for: no id crosses the boundary,
    just a count that describes somebody else's business."""
    token_a, project_a, _token_b, _ = two_orgs
    db = SessionLocal()
    try:
        user_a = db.scalar(select(User).where(User.email == "user-a@tenant.test"))
        # A real manifest for tenant A. `manifest_generations.manifest_id` is a
        # foreign key that PostgreSQL enforces and SQLite ignores by default, so
        # an invented id passes on one backend and not the other.
        other = TemplateManifest(
            org_id=user_a.org_id, template_file_id=None, template_version_id="tv-x",
            version_no=1, status="draft", fields=[], conditions=[], blocks=[],
            delete_always=[], confidence=1.0, compiled_by="rule_based",
            prescan_summary={}, created_by=user_a.id,
        )
        db.add(other)
        db.flush()
        db.add(ManifestGeneration(
            org_id=user_a.org_id, manifest_id=other.id, source_record={}, field_lineage=[],
            condition_lineage=[], qa_passed=False, qa_notes=[], created_by=user_a.id,
        ))
        db.commit()
    finally:
        db.close()

    body = app_client.get(f"{API}/metrics", headers=_auth(org["people"]["auditor"][1])).json()
    qa = next(m for m in body["metrics"] if m["key"] == "qa_block_rate")

    assert not qa["available"], "another tenant's generation must not appear in this org's denominator"


def test_reporting_an_escaped_error_needs_the_capability_to_approve_documents(app_client, org):
    """Putting a document into production and admitting one that went out was
    wrong are the same responsibility. An auditor reads the record without
    writing to it."""
    db = SessionLocal()
    try:
        _doc, versions = _document(db, org, blob_path="generated/escaped.docx", approved=True)
        version_id = versions[0].id
        db.commit()
    finally:
        db.close()

    payload = {"document_version_id": version_id, "object_id": "salary", "detail": "wrong figure"}
    denied = app_client.post(
        f"{API}/metrics/escaped-errors", headers=_auth(org["people"]["auditor"][1]), json=payload
    )
    allowed = app_client.post(
        f"{API}/metrics/escaped-errors", headers=_auth(org["people"]["approver"][1]), json=payload
    )

    assert denied.status_code == 403
    assert allowed.status_code == 201
    assert allowed.json()["phase"] == metrics.POST_APPROVAL


def test_an_escaped_error_cannot_be_filed_against_another_tenants_document(app_client, org, two_orgs):
    """404 rather than 403: a cross-tenant probe must not learn that the
    document exists."""
    _token_a, _project_a, token_b, _ = two_orgs
    db = SessionLocal()
    try:
        _doc, versions = _document(db, org, blob_path="generated/private.docx", approved=True)
        version_id = versions[0].id
        db.commit()
    finally:
        db.close()

    response = app_client.post(
        f"{API}/metrics/escaped-errors", headers=_auth(token_b),
        json={"document_version_id": version_id, "object_id": "salary", "detail": "wrong"},
    )
    assert response.status_code == 404


def test_the_calibration_log_can_be_read_back_for_a_fit(app_client, org):
    """§22 calls this the only dataset that can calibrate §13. A dataset nobody
    can get out of the database is not a dataset."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("first_name", "First Name", "exact_slug"),
            _Suggestion("salary", "Annual Salary", "fuzzy"),
        ])
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"first_name": "First Name"}, decided_by=org["people"]["org_admin"][0],
        )
        db.commit()
    finally:
        db.close()

    token = org["people"]["auditor"][1]
    everything = app_client.get(f"{API}/metrics/calibration-log", headers=_auth(token)).json()
    accepted = app_client.get(
        f"{API}/metrics/calibration-log", headers=_auth(token), params={"decision": metrics.ACCEPTED}
    ).json()

    assert len(everything["items"]) == 2
    assert len(accepted["items"]) == 1
    row = accepted["items"][0]
    # The three things a fit needs, and the three that cannot be reconstructed.
    assert row["score"] is not None and row["evidence"] is not None
    assert row["reviewer_decision"] == metrics.ACCEPTED


def test_the_calibration_log_refuses_a_page_size_it_will_not_serve(app_client, org):
    """Silently clamping a page size makes a caller believe they have read the
    whole corpus when they have read the first slice of it."""
    token = org["people"]["auditor"][1]
    assert app_client.get(
        f"{API}/metrics/calibration-log", headers=_auth(token), params={"limit": 10_000}
    ).status_code == 400
    assert app_client.get(
        f"{API}/metrics/calibration-log", headers=_auth(token), params={"decision": "maybe"}
    ).status_code == 400


def test_an_invalid_window_is_a_400_rather_than_a_silently_different_answer(app_client, org):
    response = app_client.get(
        f"{API}/metrics", headers=_auth(org["people"]["auditor"][1]), params={"window_days": 0}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "INVALID_WINDOW"


def test_the_metric_dataclass_refuses_to_be_both_available_and_unavailable():
    """The failure this guards against is a metric that returns 0.0 with an
    explanatory note attached that no dashboard renders."""
    with pytest.raises(ValueError):
        metrics.Metric(
            key="auto_map_rate", label="x", definition="d", direction="Up", unit="u",
            rank=1, value=0.0, unavailable_reason="no data",
        )
    with pytest.raises(ValueError):
        metrics.Metric(key="auto_map_rate", label="x", definition="d", direction="Up", unit="u", rank=1)


def test_the_corpus_refuses_the_same_object_twice(org):
    """The writer dedupes, but two requests that both read before either wrote
    would each believe they were first. Without the constraint, a reviewer who
    opened the binding screen in two tabs appears in a calibration fit twice."""
    db = SessionLocal()
    try:
        for _ in range(2):
            db.add(SuggestionLog(
                org_id=org["org_id"], manifest_id="m-dup", source_version_id="sv-dup",
                object_id="first_name", suggested_column="First Name", method="exact_slug",
                score=0.5, band=confidence.Band.REVIEW.value, vetoes=[], evidence=[],
            ))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_a_real_binding_session_leaves_a_decided_corpus_behind(app_client, org):
    """End to end, through the endpoints a reviewer actually drives: the
    suggestion screen writes the evidence, saving the binding writes the
    decision. Neither can be reconstructed from `manifest_bindings`, which keeps
    the answer and throws away everything that led to it."""
    db = SessionLocal()
    try:
        tf, tv = _template(db, org, name="binding.docx")
        manifest = _manifest(db, org, tf, tv, fields=[
            {"id": "first_name", "type": "string", "slots": []},
            {"id": "annual_salary", "type": "currency", "slots": []},
        ])
        manifest_id = manifest.id
        blob = save_bytes(b"First Name,Annual Salary\nAda,85000\n", "sources", ".csv")
        source = SourceFile(
            org_id=org["org_id"], project_id=org["project_id"], name="people.csv",
            file_type="csv", status="ready", created_by=org["people"]["org_admin"][0],
        )
        db.add(source)
        db.flush()
        version = SourceVersion(
            source_file_id=source.id, org_id=org["org_id"], version_no=1, blob_path=blob,
            created_by=org["people"]["org_admin"][0],
        )
        db.add(version)
        db.flush()
        source.current_version_id = version.id
        source_version_id = version.id
        db.commit()
    finally:
        db.close()

    token = org["people"]["org_admin"][1]
    suggested = app_client.get(
        f"{API}/template-manifests/{manifest_id}/binding-suggestions",
        headers=_auth(token), params={"source_version_id": source_version_id},
    )
    assert suggested.status_code == 200, suggested.text

    db = SessionLocal()
    try:
        logged = db.scalars(
            select(SuggestionLog).where(SuggestionLog.manifest_id == manifest_id)
        ).all()
    finally:
        db.close()
    assert {r.object_id for r in logged} == {"first_name", "annual_salary"}
    assert all(r.reviewer_decision == metrics.PENDING for r in logged)
    assert all(r.band for r in logged), "a row with no band cannot be fitted against"

    saved = app_client.post(
        f"{API}/template-manifests/{manifest_id}/bindings", headers=_auth(token),
        json={
            "source_version_id": source_version_id,
            "field_bindings": {"first_name": "First Name", "annual_salary": "Annual Salary"},
            "value_map": {},
        },
    )
    assert saved.status_code == 201, saved.text

    db = SessionLocal()
    try:
        decided = db.scalars(
            select(SuggestionLog).where(SuggestionLog.manifest_id == manifest_id)
        ).all()
    finally:
        db.close()
    assert all(r.reviewer_decision in metrics.DECIDED for r in decided)
    assert all(r.decided_by == org["people"]["org_admin"][0] for r in decided)


def test_a_later_save_corrects_what_an_autosave_recorded(org):
    """The binding screen autosaves, so the first save catches the reviewer
    mid-session. If that first answer were final, a suggestion they had not
    reached yet would be stamped `rejected` and the accept they made a second
    later could never correct it -- and §13's weights would be fitted against
    the reviewer's typing speed."""
    db = SessionLocal()
    try:
        manifest = _log_a_session(db, org, suggestions=[
            _Suggestion("first_name", "First Name", "exact_slug"),
            _Suggestion("manager", "Mgr", "fuzzy"),
        ])
        decided_by = org["people"]["org_admin"][0]

        # Autosave: only the first field has been touched so far.
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"first_name": "First Name"}, decided_by=decided_by,
        )
        db.commit()
        interim = {
            r.object_id: r.reviewer_decision
            for r in db.scalars(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id)).all()
        }
        assert interim["manager"] == metrics.REJECTED  # nothing bound it yet

        # The reviewer then accepts the manager suggestion and saves again.
        metrics.record_binding_decisions(
            db, org_id=org["org_id"], manifest_id=manifest.id, source_version_id="sv-1",
            field_bindings={"first_name": "First Name", "manager": "Mgr"}, decided_by=decided_by,
        )
        db.commit()
        final = {
            r.object_id: r
            for r in db.scalars(select(SuggestionLog).where(SuggestionLog.manifest_id == manifest.id)).all()
        }
    finally:
        db.close()

    assert final["manager"].reviewer_decision == metrics.ACCEPTED
    assert final["manager"].final_column == "Mgr"
    assert final["first_name"].reviewer_decision == metrics.ACCEPTED
    # One row per suggestion throughout: re-deciding must not append duplicates,
    # which would double-count the same reviewer judgement in a fit.
    assert len(final) == 2
