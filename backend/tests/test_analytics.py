"""What the estate produced, how long it took, and what it cost.

The page these serve rendered real SQL over instrumentation nothing wrote to, so
these tests are mostly about numbers that were structurally wrong rather than
occasionally wrong: a token count that could only ever be zero, a range control
that changed nothing, and a template ranking that multiplied.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import analytics
from app.llm.metering import UsageMeter


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org(two_orgs):
    from app.db import SessionLocal
    from app.models import Project

    token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        yield db, token_a, project.org_id, project.id
    finally:
        db.rollback()
        db.close()


def _ago(days: float) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


def _document(db, *, org_id, project_id, when, status="approved"):
    from app.models import DocumentVersion, GeneratedDocument

    doc = GeneratedDocument(org_id=org_id, project_id=project_id, display_id=0,
                            language="en", status=status, created_at=when, updated_at=when)
    db.add(doc)
    db.flush()
    version = DocumentVersion(document_id=doc.id, org_id=org_id, version_no=1,
                              blob_path=f"generated/{doc.id}.docx", status=status,
                              created_by="u1", created_at=when)
    db.add(version)
    db.flush()
    return doc, version


# ---- the range control ----

def test_a_range_the_report_does_not_cover_is_refused_rather_than_rounded():
    """Answering a different question than the one asked is how a chart gets
    quoted in a meeting."""
    with pytest.raises(analytics.InvalidRange):
        analytics.window("all-time")


def test_the_range_actually_narrows_the_window(org):
    db, _token, org_id, project_id = org
    _document(db, org_id=org_id, project_id=project_id, when=_ago(3))
    _document(db, org_id=org_id, project_id=project_id, when=_ago(40))
    db.flush()

    week = analytics.window("7d")
    month = analytics.window("30d")
    quarter = analytics.window("90d")

    def count(w):
        since, until, days = w
        return analytics.kpis(db, org_id=org_id, since=since, until=until,
                              days=days)["tiles"][0]["value"]

    assert count(week) == 1
    assert count(month) == 1
    assert count(quarter) == 2


def test_the_endpoint_refuses_an_unknown_range(app_client, org):
    _db, token, _org_id, _project = org
    res = app_client.get("/api/v1/analytics/kpis", headers=_auth(token),
                         params={"range": "forever"})
    assert res.status_code == 400
    assert res.json()["detail"]["error"]["code"] == "INVALID_RANGE"


# ---- the trend, and its gaps ----

def test_a_day_with_no_documents_appears_as_zero_rather_than_being_omitted(org):
    """The previous version returned only the days that had documents, so a bar
    chart drew them side by side and a fortnight's silence looked like a
    fortnight of steady output."""
    db, _token, org_id, project_id = org
    _document(db, org_id=org_id, project_id=project_id, when=_ago(1))
    _document(db, org_id=org_id, project_id=project_id, when=_ago(5))
    db.flush()

    since, until, days = analytics.window("7d")
    trend = analytics.documents_trend(db, org_id=org_id, since=since, until=until, days=days)

    assert trend["granularity"] == "day"
    assert len(trend["items"]) >= 7, "the axis must be continuous"
    assert sum(item["count"] for item in trend["items"]) == 2
    assert any(item["count"] == 0 for item in trend["items"])


def test_a_year_is_bucketed_by_week_because_365_bars_is_not_a_chart(org):
    db, _token, org_id, _project = org
    since, until, days = analytics.window("1y")
    trend = analytics.documents_trend(db, org_id=org_id, since=since, until=until, days=days)
    assert trend["granularity"] == "week"
    assert len(trend["items"]) < 60


# ---- the fan-out ----

def test_top_templates_counts_documents_not_the_projects_cross_product(org):
    """The bug this replaces. The old query joined TemplateFile -> Project ->
    GeneratedDocument with no link between a template and a document, so a
    project with three templates and ten documents reported **ten uses for each
    of them** -- thirty uses from ten documents.

    The real path is TemplateFile -> TemplateManifest -> ManifestGeneration, and
    a ManifestGeneration is exactly one row per generated document.
    """
    from app.models import ManifestGeneration, TemplateFile, TemplateManifest, TemplateVersion

    db, _token, org_id, project_id = org
    templates = []
    for name in ("offer.docx", "contract.docx", "report.docx"):
        tf = TemplateFile(org_id=org_id, project_id=project_id, name=name,
                          status="ready", created_by="u1")
        db.add(tf)
        db.flush()
        tv = TemplateVersion(template_file_id=tf.id, org_id=org_id, version_no=1,
                             blob_path=f"t/{tf.id}.docx", created_by="u1")
        db.add(tv)
        db.flush()
        manifest = TemplateManifest(
            org_id=org_id, template_file_id=tf.id, template_version_id=tv.id, version_no=1,
            status="approved", fields=[], conditions=[], blocks=[], delete_always=[],
            confidence=1.0, compiled_by="rule_based", prescan_summary={}, created_by="u1")
        db.add(manifest)
        db.flush()
        templates.append((tf, manifest))

    # Ten documents, but only the first template actually produced any.
    for i in range(10):
        doc, version = _document(db, org_id=org_id, project_id=project_id, when=_ago(1))
        db.add(ManifestGeneration(
            org_id=org_id, manifest_id=templates[0][1].id,
            document_version_id=version.id, source_record={}, field_lineage=[],
            condition_lineage=[], qa_passed=True, qa_notes=[], created_by="u1",
            created_at=_ago(1)))
    db.flush()

    since, until, _days = analytics.window("7d")
    ranked = analytics.top_templates(db, org_id=org_id, since=since, until=until)

    assert [(r["name"], r["uses"]) for r in ranked["items"]] == [("offer.docx", 10)], (
        "the other two templates produced nothing and must not be credited")
    assert sum(r["uses"] for r in ranked["items"]) == 10, "uses must not exceed documents"
    assert ranked["covered_documents"] == 10
    assert ranked["total_documents"] == 10


def test_the_ranking_says_how_much_of_the_estate_it_covers(org):
    """Documents from the token-library path have no ManifestGeneration, so the
    ranking is over a subset -- and one that implies completeness it does not
    have is worse than one that says so."""
    db, _token, org_id, project_id = org
    for _ in range(4):
        _document(db, org_id=org_id, project_id=project_id, when=_ago(1))
    db.flush()

    since, until, _days = analytics.window("7d")
    ranked = analytics.top_templates(db, org_id=org_id, since=since, until=until)
    assert ranked["items"] == []
    assert (ranked["covered_documents"], ranked["total_documents"]) == (0, 4)


# ---- tokens and money ----

def _record(db, org_id, *, model="claude-sonnet-5", input_tokens=1000, output_tokens=500,
            operation_capability="Chat", project_id=None, subject=None, when=None):
    meter = UsageMeter(db=db, org_id=org_id, project_id=project_id,
                       subject_type="template_file" if subject else None, subject_id=subject)
    row = meter.record(capability=operation_capability, purpose="generate", model=model,
                       input_tokens=input_tokens, output_tokens=output_tokens, outcome="ok")
    if when is not None and row is not None:
        row.created_at = when
    db.flush()
    return row


def test_tokens_are_summed_from_the_calls_that_were_actually_made(org):
    """They used to be summed from `generation_jobs.token_usage`, which nothing
    in the repository ever wrote -- so the number was structurally always 0.
    The figure is the operator's: token counts describe how the product is
    built, so the customer tiles do not carry it."""
    db, _token, org_id, _project = org
    _record(db, org_id, input_tokens=1000, output_tokens=500, when=_ago(1))
    _record(db, org_id, input_tokens=2000, output_tokens=250, when=_ago(2))

    since, until, days = analytics.window("7d")
    summary = analytics.cost_summary(db, org_id=org_id, since=since, until=until)
    assert summary["total_tokens"] == 3750
    assert summary["calls"] == 2

    kpis = analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)
    assert "tokens_consumed" not in {t["key"] for t in kpis["tiles"]}
    assert "token" not in json.dumps(kpis)


def test_spend_with_no_calls_is_unavailable_rather_than_zero(org):
    """A spend of $0.00 reads as "we are not spending anything". A blank reads
    as "nobody has measured this yet". They are different statements."""
    db, _token, org_id, _project = org
    since, until, days = analytics.window("7d")
    tiles = {t["key"]: t for t in
             analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)["tiles"]}

    assert tiles["spend_usd"]["value"] is None
    assert tiles["spend_usd"]["available"] is False
    assert tiles["spend_usd"]["unavailable_reason"]


def test_an_unpriced_call_is_counted_and_named_rather_than_folded_into_the_total(org):
    db, _token, org_id, _project = org
    _record(db, org_id, model="claude-sonnet-5", when=_ago(1))
    _record(db, org_id, model="llama-9-enormous", when=_ago(1))

    since, until, _days = analytics.window("7d")
    summary = analytics.cost_summary(db, org_id=org_id, since=since, until=until)

    assert summary["calls"] == 2
    assert summary["unpriced_calls"] == 1
    assert summary["unpriced_models"] == ["llama-9-enormous"]
    assert summary["cost_usd"] is not None, "the priced call still contributes"


def test_spend_is_broken_down_by_model_and_by_what_it_was_for(org):
    db, _token, org_id, _project = org
    _record(db, org_id, model="claude-opus-5", operation_capability="Compiling a template",
            when=_ago(1))
    _record(db, org_id, model="claude-sonnet-5", operation_capability="Chat", when=_ago(1))

    since, until, _days = analytics.window("7d")
    by_model = {r["key"]: r for r in
                analytics.cost_by_model(db, org_id=org_id, since=since, until=until)}
    by_op = {r["key"]: r for r in
             analytics.cost_by_operation(db, org_id=org_id, since=since, until=until)}

    assert set(by_model) == {"claude-opus-5", "claude-sonnet-5"}
    assert set(by_op) == {"compile", "chat"}


def test_spend_is_attributed_to_the_template_that_incurred_it(org):
    """The thing per-call detail buys, and a pre-rolled daily ledger cannot
    answer."""
    from app.models import TemplateFile

    db, _token, org_id, project_id = org
    tf = TemplateFile(org_id=org_id, project_id=project_id, name="offer.docx",
                      status="ready", created_by="u1")
    db.add(tf)
    db.flush()

    _record(db, org_id, model="claude-opus-5", operation_capability="Compiling a template",
            project_id=project_id, subject=tf.id, when=_ago(1))

    since, until, _days = analytics.window("7d")
    rows = analytics.cost_by_template(db, org_id=org_id, since=since, until=until)
    assert [(r["name"], r["calls"]) for r in rows] == [("offer.docx", 1)]
    assert rows[0]["cost_usd"] > 0


def test_the_cost_trend_is_gap_filled_and_split_by_model(org):
    db, _token, org_id, _project = org
    _record(db, org_id, model="claude-sonnet-5", when=_ago(1))
    _record(db, org_id, model="claude-opus-5", when=_ago(3))

    since, until, days = analytics.window("7d")
    trend = analytics.cost_trend(db, org_id=org_id, since=since, until=until, days=days)

    assert set(trend["models"]) == {"claude-sonnet-5", "claude-opus-5"}
    assert len(trend["items"]) >= 7
    assert all("claude-opus-5" in item for item in trend["items"]), "a stacked series needs every key on every bucket"


def test_one_tenants_spend_never_includes_anothers(org, two_orgs):
    from app.models import Project

    db, _token, org_id, _project = org
    _ta, _pa, _tb, project_b = two_orgs
    other = db.get(Project, project_b).org_id

    _record(db, org_id, when=_ago(1))
    _record(db, other, when=_ago(1))

    since, until, _days = analytics.window("7d")
    assert analytics.cost_summary(db, org_id=org_id, since=since, until=until)["calls"] == 1


# ---- compiles ----

def test_templates_compiled_is_counted_and_failures_are_separated(org):
    from app.models import TemplateFile, TemplateManifest, TemplateVersion

    db, _token, org_id, project_id = org
    tf = TemplateFile(org_id=org_id, project_id=project_id, name="t.docx",
                      status="ready", created_by="u1")
    db.add(tf)
    db.flush()
    tv = TemplateVersion(template_file_id=tf.id, org_id=org_id, version_no=1,
                         blob_path="t.docx", created_by="u1")
    db.add(tv)
    db.flush()
    for status in ("draft", "approved", "failed"):
        db.add(TemplateManifest(
            org_id=org_id, template_file_id=tf.id, template_version_id=tv.id, version_no=1,
            status=status, fields=[], conditions=[], blocks=[], delete_always=[],
            confidence=1.0, compiled_by="rule_based", prescan_summary={},
            created_by="u1", created_at=_ago(1)))
    db.flush()

    since, until, days = analytics.window("7d")
    summary = analytics.compile_summary(db, org_id=org_id, since=since, until=until, days=days)
    assert summary["compiled"] == 3
    assert summary["failed"] == 1


def test_compile_duration_is_unmeasured_rather_than_zero_when_nothing_was_timed(org):
    db, _token, org_id, _project = org
    since, until, days = analytics.window("7d")
    summary = analytics.compile_summary(db, org_id=org_id, since=since, until=until, days=days)
    assert summary["duration"]["status"] == "unmeasured"

    tiles = {t["key"]: t for t in
             analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)["tiles"]}
    assert tiles["compile_p95_seconds"]["value"] is None
    assert tiles["compile_p95_seconds"]["available"] is False


def test_compile_duration_is_reported_once_something_has_been_timed(org):
    from app.metrics import AGENTIC_COMPILE, record_timing

    db, _token, org_id, _project = org
    for ms in (1000.0, 2000.0, 3000.0):
        record_timing(db, org_id=org_id, operation=AGENTIC_COMPILE, duration_ms=ms)
    db.flush()

    since, until, days = analytics.window("7d")
    tiles = {t["key"]: t for t in
             analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)["tiles"]}
    assert tiles["compile_p95_seconds"]["value"] == 3.0


# ---- the tiles as a whole ----

def test_every_tile_states_either_a_value_or_why_it_has_none(org):
    """`metrics.Metric` refuses to hold both or neither, which is what stops a
    dashboard printing the number and dropping the caveat."""
    db, _token, org_id, _project = org
    since, until, days = analytics.window("30d")
    for tile in analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)["tiles"]:
        assert (tile["value"] is None) != (tile["unavailable_reason"] is None), tile["key"]


def test_approval_rate_over_no_documents_is_not_zero_per_cent(org):
    db, _token, org_id, _project = org
    since, until, days = analytics.window("7d")
    tiles = {t["key"]: t for t in
             analytics.kpis(db, org_id=org_id, since=since, until=until, days=days)["tiles"]}
    assert tiles["approval_rate_pct"]["value"] is None


def test_the_endpoints_answer(app_client, org):
    _db, token, _org_id, _project = org
    for path in ("kpis", "trend", "by-function", "top-templates", "cost", "compiles"):
        res = app_client.get(f"/api/v1/analytics/{path}", headers=_auth(token),
                             params={"range": "7d"})
        assert res.status_code == 200, (path, res.text)


def test_the_oldest_trend_bucket_is_a_whole_day(org):
    """It was a fraction of one, drawn full width and labelled with the date.

    `window` used to return `now - timedelta(days=N)`, which carries the current
    time of day, while `_bucket_labels` floored to midnight for the label. So the
    first bar covered only the tail of that calendar day. Two days with identical
    output rendered as a collapse followed by a recovery, and the chart's leftmost
    point -- the one a reader anchors the trend on -- was the wrong one.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import DocumentVersion, GeneratedDocument, User

    db, token, org_id, project_id = org
    user_id = db.query(User).filter(User.org_id == org_id).first().id
    since, until, days = analytics.window("7d")
    assert since.hour == 0 and since.minute == 0 and since.second == 0
    # `days - 1` back plus today: exactly the number the range name promises.
    assert len(analytics._bucket_labels(since, until, weekly=False)) == days

    # Two identical days, one of them the oldest bucket. They must report equal.
    made = []
    try:
        for offset in (days - 1, days - 2):
            midnight = (datetime.now(timezone.utc) - timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            for hour in (1, 9, 17, 23):
                doc = GeneratedDocument(
                    org_id=org_id, project_id=project_id, display_id=93000 + len(made),
                    language="en", status="draft",
                    created_at=midnight + timedelta(hours=hour))
                db.add(doc)
                db.flush()
                version = DocumentVersion(
                    document_id=doc.id, org_id=org_id, version_no=1, blob_path="x.docx",
                    status="draft", created_by=user_id,
                    created_at=midnight + timedelta(hours=hour))
                db.add(version)
                made.append(doc.id)
        db.commit()

        since, until, days = analytics.window("7d")
        trend = analytics.documents_trend(
            db, org_id=org_id, since=since, until=until, days=days)
        counts = {item["bucket"]: item["count"] for item in trend["items"]}
        oldest, next_along = trend["items"][0]["bucket"], trend["items"][1]["bucket"]
        assert counts[oldest] == counts[next_along] == 4, (
            f"identical days reported {counts[oldest]} and {counts[next_along]}")
    finally:
        for doc_id in made:
            db.query(DocumentVersion).filter(DocumentVersion.document_id == doc_id).delete()
            db.query(GeneratedDocument).filter(GeneratedDocument.id == doc_id).delete()
        db.commit()


def test_time_per_document_is_per_document_and_not_per_thousand(org):
    """`_batch_projections` returns milliseconds projected onto a batch of one
    *thousand* documents -- that is the §18 dimension it feeds, and why its
    target is 600,000. Dividing by 1,000 published seconds-per-thousand under a
    label that said per document, so a batch running a comfortable 0.6 s each
    reported "600.0 seconds" per letter.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import GenerationJob, User

    db, token, org_id, project_id = org
    started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
    job = GenerationJob(
        org_id=org_id, project_id=project_id, status="completed", languages=["en"],
        started_at=started, finished_at=started + timedelta(seconds=1000),
        progress={"rows_total": 1000},
        created_by=db.query(User).filter(User.org_id == org_id).first().id)
    db.add(job)
    db.commit()
    try:
        since, until, days = analytics.window("7d")
        tiles = {t["key"]: t for t in analytics.kpis(
            db, org_id=org_id, since=since, until=until, days=days)["tiles"]}
        # 1000 seconds of wall clock over 1000 documents is 1.0 s each.
        assert tiles["seconds_per_document"]["value"] == 1.0
    finally:
        db.query(GenerationJob).filter(GenerationJob.id == job.id).delete()
        db.commit()
