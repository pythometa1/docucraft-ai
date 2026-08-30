"""What the estate produced, how long it took, and what it cost.

Separate from `routers/admin.py` for the reason `metrics.py` is separate from
`routers/metrics.py`: a query is testable without a request, and a router that
also computes is a router nobody can test without one.

Four things were wrong with what this replaces, and each was invisible from the
screen:

* **The token figure was structurally zero.** It summed
  `generation_jobs.token_usage`, a column nothing in the repository ever wrote.
* **`range` was accepted and ignored.** Every endpoint answered "all time" or a
  hardcoded fourteen days, whichever it felt like, while the page showed four
  range buttons that did nothing.
* **Top templates was a cartesian fan-out.** It joined template → project →
  document with no link between a template and a document, so a project with
  three templates and ten documents reported ten uses for *each* of them.
* **"Average time to draft" averaged three unrelated things** -- template
  compiles measured in minutes, batches measured in seconds, and token-path jobs
  whose `started_at` and `finished_at` are set to the same instant and therefore
  contribute an exact zero.

The measurements mostly existed. `OperationTiming` has been recording compile,
parse and render durations on live paths all along, and nothing in the frontend
has ever called the endpoint that exposes them. So this module computes very
little itself: it reuses `metrics.slo_report`, `metrics._batch_projections` and
`metrics.Metric`, and adds the two things that genuinely did not exist -- tokens
and money.

**"No data" is never rendered as zero.** That rule is `metrics.py`'s, enforced by
`Metric` refusing to hold both a value and a reason, and it matters more for
money than for anything else: a spend of `$0.00` reads as "we are not spending
anything" and a blank reads as "nobody has measured this yet".
"""

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from app import metrics
from app.llm.pricing import usd
from app.metrics import AGENTIC_COMPILE, Metric
from app.models import (
    DocumentVersion, GeneratedDocument, LlmCall, ManifestGeneration, Project,
    TemplateFile, TemplateManifest,
)

#: The four windows the page offers. A range outside this is refused rather than
#: rounded to the nearest one -- answering a different question than the one
#: asked is how a chart gets quoted in a meeting.
RANGES = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}

DEFAULT_RANGE = "30d"

#: Past this many days a daily bar chart is unreadable, so buckets become weeks.
WEEKLY_ABOVE_DAYS = 90


class InvalidRange(ValueError):
    pass


def window(range_: str | None) -> tuple:
    """`(since, until, days)` for a range name.

    `since` is midnight, not this time of day N days ago. The trend buckets by
    calendar day, so an unfloored bound made the *oldest* bar cover only the tail
    of its day while being labelled with the whole date and drawn the same width
    as its neighbours -- at 17:00 the first bar of a 7-day chart showed about a
    quarter of that day's output and read as a collapse that had not happened.

    Flooring also makes the count come out right: midnight of `days - 1` ago
    through now is exactly `days` buckets. Today's is partial because today is
    partial, which is the one case a reader already expects.
    """
    key = (range_ or DEFAULT_RANGE).strip().lower()
    if key not in RANGES:
        raise InvalidRange(
            f"{range_!r} is not a range this reports on; use one of {', '.join(RANGES)}.")
    days = RANGES[key]
    until = datetime.now(timezone.utc)
    since = (until - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return since, until, days


def _naive(moment: datetime) -> datetime:
    """SQLite hands timestamps back without a timezone, and comparing an aware
    bound against a naive column raises. Every timestamp this system writes is
    UTC, so dropping the tzinfo compares the same instants."""
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


# ---- tokens and money ----

def _usage_rows(db, *, org_id: str, since: datetime, until: datetime):
    return db.execute(
        select(
            func.count(LlmCall.id),
            func.sum(LlmCall.input_tokens),
            func.sum(LlmCall.output_tokens),
            func.sum(LlmCall.cost_micro_usd),
            func.sum(case((LlmCall.cost_micro_usd.is_(None), 1), else_=0)),
        ).where(
            LlmCall.org_id == org_id,
            LlmCall.created_at >= _naive(since),
            LlmCall.created_at < _naive(until),
        )
    ).one()


def cost_summary(db, *, org_id: str, since: datetime, until: datetime) -> dict:
    """Spend for the window, and how much of it is known.

    `unpriced_calls` is reported beside the total rather than folded into it. A
    total that silently excludes the calls it could not price is a total that
    understates the bill and says nothing about it.
    """
    calls, tokens_in, tokens_out, cost, unpriced = _usage_rows(
        db, org_id=org_id, since=since, until=until)
    unpriced_models = []
    if unpriced:
        unpriced_models = [
            row[0] for row in db.execute(
                select(LlmCall.model).where(
                    LlmCall.org_id == org_id,
                    LlmCall.cost_micro_usd.is_(None),
                    LlmCall.created_at >= _naive(since),
                    LlmCall.created_at < _naive(until),
                ).group_by(LlmCall.model)
            ).all()
        ]
    return {
        "calls": int(calls or 0),
        "input_tokens": int(tokens_in or 0),
        "output_tokens": int(tokens_out or 0),
        "total_tokens": int(tokens_in or 0) + int(tokens_out or 0),
        "cost_usd": usd(int(cost)) if cost is not None else None,
        "unpriced_calls": int(unpriced or 0),
        "unpriced_models": sorted(unpriced_models),
    }


def _grouped_cost(db, *, org_id: str, since: datetime, until: datetime, column) -> list:
    rows = db.execute(
        select(
            column,
            func.count(LlmCall.id),
            func.sum(LlmCall.input_tokens + LlmCall.output_tokens),
            func.sum(LlmCall.cost_micro_usd),
        ).where(
            LlmCall.org_id == org_id,
            LlmCall.created_at >= _naive(since),
            LlmCall.created_at < _naive(until),
        ).group_by(column).order_by(func.sum(LlmCall.cost_micro_usd).desc().nullslast())
    ).all()
    return [
        {"key": key or "unknown", "calls": int(calls or 0),
         "tokens": int(tokens or 0),
         "cost_usd": usd(int(cost)) if cost is not None else None}
        for key, calls, tokens, cost in rows
    ]


def cost_by_model(db, *, org_id, since, until) -> list:
    return _grouped_cost(db, org_id=org_id, since=since, until=until, column=LlmCall.model)


def cost_by_operation(db, *, org_id, since, until) -> list:
    return _grouped_cost(db, org_id=org_id, since=since, until=until, column=LlmCall.operation)


def cost_by_template(db, *, org_id: str, since: datetime, until: datetime, limit: int = 10) -> list:
    """Spend attributed to the template that incurred it.

    This is the thing per-call detail buys and a pre-rolled daily ledger cannot
    answer. `subject_id` is set by `llm_policy_for` at the call site, so a
    compile's cost lands on the template it compiled.
    """
    rows = db.execute(
        select(
            LlmCall.subject_id,
            func.count(LlmCall.id),
            func.sum(LlmCall.input_tokens + LlmCall.output_tokens),
            func.sum(LlmCall.cost_micro_usd),
        ).where(
            LlmCall.org_id == org_id,
            LlmCall.subject_type == "template_file",
            LlmCall.created_at >= _naive(since),
            LlmCall.created_at < _naive(until),
        ).group_by(LlmCall.subject_id)
        .order_by(func.sum(LlmCall.cost_micro_usd).desc().nullslast()).limit(limit)
    ).all()
    if not rows:
        return []

    names = dict(db.execute(
        select(TemplateFile.id, TemplateFile.name).where(
            TemplateFile.id.in_([r[0] for r in rows if r[0]]))
    ).all())
    return [
        {"template_file_id": subject, "name": names.get(subject, "(deleted template)"),
         "calls": int(calls or 0), "tokens": int(tokens or 0),
         "cost_usd": usd(int(cost)) if cost is not None else None}
        for subject, calls, tokens, cost in rows
    ]


def cost_trend(db, *, org_id: str, since: datetime, until: datetime, days: int) -> dict:
    """Spend per bucket, split by model, gap-filled.

    Gap-filling is not a contradiction of "no data is not zero": the denominator
    here is a calendar day, which is known, so a day on which no model was called
    genuinely cost nothing. The distinction that matters is between a day with no
    calls (zero) and a call whose model has no rate (unknown), and the second is
    reported separately by `cost_summary`.
    """
    rows = db.execute(
        select(LlmCall.created_at, LlmCall.model, LlmCall.cost_micro_usd).where(
            LlmCall.org_id == org_id,
            LlmCall.created_at >= _naive(since),
            LlmCall.created_at < _naive(until),
        )
    ).all()

    weekly = days > WEEKLY_ABOVE_DAYS
    buckets = _bucket_labels(since, until, weekly=weekly)
    models = sorted({model for _at, model, _cost in rows if model})
    totals = {label: {model: 0 for model in models} for label in buckets}

    for at, model, cost in rows:
        label = _bucket_of(at, since, weekly=weekly)
        if label in totals and model:
            totals[label][model] += int(cost or 0)

    return {
        "granularity": "week" if weekly else "day",
        "models": models,
        "items": [
            {"bucket": label,
             **{model: usd(totals[label][model]) or 0.0 for model in models}}
            for label in buckets
        ],
    }


def _bucket_labels(since: datetime, until: datetime, *, weekly: bool) -> list:
    step = timedelta(days=7 if weekly else 1)
    labels, cursor = [], _naive(since).replace(hour=0, minute=0, second=0, microsecond=0)
    end = _naive(until)
    while cursor <= end:
        labels.append(cursor.strftime("%Y-%m-%d"))
        cursor += step
    return labels


def _bucket_of(moment: datetime, since: datetime, *, weekly: bool) -> str:
    start = _naive(since).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (_naive(moment) - start).days
    if weekly:
        delta -= delta % 7
    return (start + timedelta(days=max(0, delta))).strftime("%Y-%m-%d")


# ---- documents ----

def documents_trend(db, *, org_id: str, since: datetime, until: datetime, days: int) -> dict:
    """Documents produced per bucket, with the gaps filled in.

    The previous version returned only the days that had documents, so a bar
    chart drew them side by side and a fortnight's silence looked like a
    fortnight of steady output.
    """
    rows = db.scalars(
        select(GeneratedDocument.created_at).where(
            GeneratedDocument.org_id == org_id,
            GeneratedDocument.created_at >= _naive(since),
            GeneratedDocument.created_at < _naive(until),
        )
    ).all()

    weekly = days > WEEKLY_ABOVE_DAYS
    counts = {label: 0 for label in _bucket_labels(since, until, weekly=weekly)}
    for at in rows:
        label = _bucket_of(at, since, weekly=weekly)
        if label in counts:
            counts[label] += 1
    return {
        "granularity": "week" if weekly else "day",
        "items": [{"bucket": label, "count": count} for label, count in counts.items()],
    }


def documents_by_function(db, *, org_id: str, since: datetime, until: datetime) -> list:
    rows = db.execute(
        select(Project.function, func.count(GeneratedDocument.id))
        .join(GeneratedDocument, GeneratedDocument.project_id == Project.id)
        .where(
            Project.org_id == org_id,
            GeneratedDocument.created_at >= _naive(since),
            GeneratedDocument.created_at < _naive(until),
        )
        .group_by(Project.function)
        .order_by(func.count(GeneratedDocument.id).desc())
    ).all()
    return [{"function": function or "Unassigned", "count": int(count)} for function, count in rows]


def top_templates(db, *, org_id: str, since: datetime, until: datetime, limit: int = 5) -> dict:
    """Which templates actually produced documents.

    The query this replaces joined `TemplateFile -> Project -> GeneratedDocument`
    with **no link between a template and a document**, so every template in a
    project was credited with that project's entire output: three templates and
    ten documents reported ten uses each, thirty in total, from ten documents.

    The real path is `TemplateFile -> TemplateManifest -> ManifestGeneration`,
    and a `ManifestGeneration` is exactly one row per generated document.

    `covered` and `total` are returned alongside because documents produced by
    the token-library path have no `ManifestGeneration` at all, so the ranking is
    over a subset -- and a ranking that implies completeness it does not have is
    worse than one that says so.
    """
    uses = func.count(func.distinct(ManifestGeneration.id))
    rows = db.execute(
        select(TemplateFile.id, TemplateFile.name, uses)
        .join(TemplateManifest, TemplateManifest.template_file_id == TemplateFile.id)
        .join(ManifestGeneration, ManifestGeneration.manifest_id == TemplateManifest.id)
        .where(
            TemplateFile.org_id == org_id,
            ManifestGeneration.created_at >= _naive(since),
            ManifestGeneration.created_at < _naive(until),
        )
        .group_by(TemplateFile.id, TemplateFile.name)
        .order_by(uses.desc()).limit(limit)
    ).all()

    covered = db.scalar(
        select(func.count(ManifestGeneration.id)).where(
            ManifestGeneration.org_id == org_id,
            ManifestGeneration.created_at >= _naive(since),
            ManifestGeneration.created_at < _naive(until),
        )
    ) or 0
    total = db.scalar(
        select(func.count(GeneratedDocument.id)).where(
            GeneratedDocument.org_id == org_id,
            GeneratedDocument.created_at >= _naive(since),
            GeneratedDocument.created_at < _naive(until),
        )
    ) or 0

    return {
        "items": [{"template_file_id": tid, "name": name, "uses": int(count)}
                  for tid, name, count in rows],
        "covered_documents": int(covered),
        "total_documents": int(total),
    }


# ---- compiles ----

def compile_summary(db, *, org_id: str, since: datetime, until: datetime, days: int) -> dict:
    """How many templates were compiled, and how long a compile takes.

    The duration comes from `OperationTiming`, which has been recording it all
    along through `metrics.timed` and which nothing has ever displayed.
    """
    rows = db.execute(
        select(TemplateManifest.status, func.count(TemplateManifest.id)).where(
            TemplateManifest.org_id == org_id,
            TemplateManifest.created_at >= _naive(since),
            TemplateManifest.created_at < _naive(until),
        ).group_by(TemplateManifest.status)
    ).all()
    by_status = {status: int(count) for status, count in rows}
    failed = by_status.get("failed", 0)

    report = metrics.slo_report(db, org_id=org_id, window_days=days)
    timing = next(
        (row for row in report["operations"] if row["operation"] == AGENTIC_COMPILE), None)

    return {
        "compiled": sum(by_status.values()),
        "failed": failed,
        "by_status": by_status,
        "duration": timing,
    }


# ---- the tiles ----

def _tile(key, label, definition, unit, rank, *, value=None, reason=None,
          direction="up", sample=None) -> dict:
    return Metric(
        key=key, label=label, definition=definition, direction=direction, unit=unit,
        rank=rank, value=value, unavailable_reason=reason, sample=sample or {},
    ).as_dict()


def kpis(db, *, org_id: str, since: datetime, until: datetime, days: int) -> dict:
    """The headline numbers, each either a measurement or a stated absence.

    Built through `metrics.Metric` so the "exactly one of value and
    unavailable_reason" rule is enforced by the class that already enforces it
    for §22, and so one component on the frontend can render both pages.
    """
    documents = db.scalar(
        select(func.count(GeneratedDocument.id)).where(
            GeneratedDocument.org_id == org_id,
            GeneratedDocument.created_at >= _naive(since),
            GeneratedDocument.created_at < _naive(until),
        )
    ) or 0
    all_time = db.scalar(
        select(func.count(GeneratedDocument.id)).where(GeneratedDocument.org_id == org_id)
    ) or 0

    spend = cost_summary(db, org_id=org_id, since=since, until=until)

    versions = db.execute(
        select(DocumentVersion.status, func.count(DocumentVersion.id))
        .join(GeneratedDocument, GeneratedDocument.id == DocumentVersion.document_id)
        .where(
            GeneratedDocument.org_id == org_id,
            DocumentVersion.created_at >= _naive(since),
            DocumentVersion.created_at < _naive(until),
        ).group_by(DocumentVersion.status)
    ).all()
    version_counts = {status: int(count) for status, count in versions}
    total_versions = sum(version_counts.values())

    projections = metrics._batch_projections(db, org_id, _naive(since))
    compiles = compile_summary(db, org_id=org_id, since=since, until=until, days=days)
    measured = (compiles["duration"] or {}).get("measured")

    tiles = [
        _tile("documents_generated", "Documents generated",
              "Documents produced in the selected window.", "documents", 1,
              value=float(documents), sample={"all_time": int(all_time)}),
        _tile("tokens_consumed", "Tokens consumed",
              "Input plus output tokens across every model call in the window.",
              "tokens", 2,
              value=float(spend["total_tokens"]) if spend["calls"] else None,
              reason=None if spend["calls"] else
              "No model call has been recorded in this window.",
              sample={"input": spend["input_tokens"], "output": spend["output_tokens"],
                      "calls": spend["calls"]}),
        _tile("spend_usd", "Spend",
              "What those model calls cost, at the rate in force when each ran.",
              "USD", 3, direction="down",
              value=spend["cost_usd"],
              reason=None if spend["cost_usd"] is not None else (
                  "No priced model call in this window."
                  + (f" {spend['unpriced_calls']} call(s) used a model with no configured rate."
                     if spend["unpriced_calls"] else "")),
              sample={"unpriced_calls": spend["unpriced_calls"],
                      "unpriced_models": spend["unpriced_models"]}),
        _tile("approval_rate_pct", "Approval rate",
              "Share of document versions in the window that reached approved.",
              "%", 4,
              value=round(100 * version_counts.get("approved", 0) / total_versions, 1)
              if total_versions else None,
              reason=None if total_versions else
              "No document version was created in this window, and a rate over nothing "
              "is not zero per cent.",
              sample=version_counts),
        _tile("compile_p95_seconds", "Compile time (p95)",
              "How long reading a template into a manifest takes, at the 95th percentile.",
              "seconds", 5, direction="down",
              value=round(measured["p95_ms"] / 1000.0, 1) if measured else None,
              reason=None if measured else
              "No template compile has been timed in this window.",
              sample={"samples": measured["samples"]} if measured else {}),
        _tile("seconds_per_document", "Median time per document",
              "Batch wall clock divided by the rows it produced.", "seconds", 6,
              direction="down",
              # `_batch_projections` returns milliseconds projected onto a batch of
              # *one thousand* documents -- that is the §18 dimension it feeds, and
              # why its target is 600,000. Dividing by 1,000 gives seconds per
              # thousand while the label says per document, so a batch running a
              # comfortable 0.6 s each published "600.0 seconds" per letter. Two
              # factors of a thousand: one to seconds, one to a single document.
              value=round(statistics.median(projections) / 1_000_000.0, 2) if projections else None,
              reason=None if projections else
              "No completed batch in this window reported how many rows it ran.",
              sample={"batches": len(projections)}),
    ]
    return {
        "window_days": days,
        "tiles": tiles,
        "documents_generated_all_time": int(all_time),
        "templates_compiled": compiles["compiled"],
    }
