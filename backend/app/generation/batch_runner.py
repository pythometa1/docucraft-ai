"""Run a manifest over many source rows.

One spreadsheet row becomes one document, so a 500-row workbook is 500 letters
from a single extraction pass. The loop itself is pure CPU -- no LLM call
happens on the deterministic path -- which is what makes batch generation cheap
enough to be the normal way to use this.

Runs under FastAPI's BackgroundTasks and writes progress to the GenerationJob
row as it goes. That finally makes the job model real: until now every job was
written already-finished and never polled.

A row that needs human input completes as `pending_review` rather than failing,
and never blocks the rest of the batch.
"""

import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.tenancy import adopt_org_of_user, release_org_scope
from app.metrics import SINGLE_DOCX_RENDER, record_qa_findings, timed
from app.models import (
    Counter, DocumentVersion, GeneratedDocument, GenerationJob, ManifestGeneration,
    Project, ReviewTask, TemplateManifest, TemplateVersion,
)
from app.storage import abs_path
from app.generation.source_resolver import apply_binding
from app.generation.docx_renderer import fill_template
from app.generation.source_ingestion import extract_records
from app.generation.renderers import OOXML_FILL
from app.generation.resolution_engine import CyclicDependencyError, resolve_manifest
from app.generation.value_format import resolve_locale, resolved_locale


@dataclass
class RowOutcome:
    row_index: int
    status: str  # generated | pending_review | failed
    document_id: str | None = None
    document_version_id: str | None = None
    filename: str | None = None
    qa_passed: bool = False
    qa_notes: list = field(default_factory=list)
    open_tasks: int = 0
    error: str | None = None
    is_canary: bool = False


#: How many rows are rendered and checked before the rest of a batch is allowed
#: to run. Small enough to be cheap on a 5,000-row workbook, more than one so a
#: single unrepresentative row cannot clear the gate on its own.
CANARY_SIZE = 3


def _canary_positions(count: int, size: int = CANARY_SIZE) -> list[int]:
    """Evenly spaced positions across the batch.

    Spread rather than the first N: rows in a spreadsheet arrive sorted, so the
    first three are usually the same department, the same country and the same
    branch of every condition in the manifest -- which is precisely the sample
    least likely to exercise the mapping that is wrong.
    """
    if count <= 0:
        return []
    if count <= size:
        return list(range(count))
    step = count / size
    return sorted({min(count - 1, int(i * step)) for i in range(size)})


def _next_display_id(db: Session, name: str, start: int) -> int:
    counter = db.get(Counter, name)
    if counter is None:
        counter = Counter(name=name, value=start)
        db.add(counter)
    counter.value += 1
    db.flush()
    return counter.value


def run_row(
    db: Session,
    *,
    manifest: TemplateManifest,
    template_path: str,
    project: Project,
    record: dict,
    field_bindings: dict,
    value_map: dict,
    locale: str,
    language: str,
    user_id: str,
    persist: bool = True,
    run_key: str | None = None,
    source_version_id: str | None = None,
) -> RowOutcome:
    """Resolve and fill one row. Shared by batch runs and single-row preview,
    so what you preview is exactly what a batch would produce.

    `run_key` (the job id, in a batch) separates one run's output blobs from
    another's. Without it the path is a function of manifest and row alone, so
    re-running a batch -- or previewing a row while a batch is live -- writes
    over a document that may already have been downloaded and sent.
    """
    row_index = record.get("_row_index", 0)
    manifest_dict = {
        "fields": manifest.fields,
        "conditions": manifest.conditions,
        "blocks": manifest.blocks,
        "delete_always": manifest.delete_always,
    }
    resolved = apply_binding(record, field_bindings, value_map)

    try:
        resolution = resolve_manifest(manifest_dict, resolved)
    except CyclicDependencyError as exc:
        return RowOutcome(row_index, "failed", error=str(exc))

    # Computed values join the record so the fill engine can place them.
    filled_record = {**resolved, **resolution.values}

    out_dir = f"generated/{project.id}"
    os.makedirs(str(abs_path(out_dir)), exist_ok=True)
    scope = f"{run_key[:8]}-" if run_key else "preview-"
    out_rel = f"{out_dir}/manifest-{manifest.id[:8]}-{scope}row{row_index}.docx"

    try:
        # §18 puts a p95 of 1.5 s on this render. Timing it here rather than
        # around the batch means the figure is per document, which is what the
        # SLO is about, and it covers the preview path too -- previews are the
        # renders a person is actually waiting on.
        with timed(db, org_id=manifest.org_id, operation=SINGLE_DOCX_RENDER):
            fill = fill_template(
                template_path, str(abs_path(out_rel)), manifest_dict, filled_record,
                locale=locale, condition_verdicts=resolution.condition_verdicts,
            )
    except Exception as exc:
        return RowOutcome(row_index, "failed", error=f"{type(exc).__name__}: {exc}")

    if not persist:
        return RowOutcome(
            row_index, "pending_review" if resolution.needs_review else "generated",
            qa_passed=fill.qa_passed, qa_notes=fill.qa_notes,
            open_tasks=len(resolution.open_tasks), filename=out_rel,
        )

    display_id = _next_display_id(db, "generated_doc_display_id", 50000)
    filename = f"{project.name}_{project.display_id}_{display_id}_{language}.docx"
    # A QA failure outranks a review flag: `pending_review` means a person has
    # a decision to make, `blocked` means the document is defective and cannot
    # be approved until it is regenerated.
    if not fill.qa_passed:
        status = "blocked"
    elif resolution.needs_review:
        status = "pending_review"
    else:
        status = "draft"

    gen_doc = GeneratedDocument(
        org_id=manifest.org_id, project_id=project.id, draft_id=None,
        display_id=display_id, language=language, status=status,
    )
    db.add(gen_doc)
    db.flush()
    version = DocumentVersion(
        document_id=gen_doc.id, org_id=gen_doc.org_id, version_no=1, blob_path=out_rel, renderer=OOXML_FILL,
        change_summary=f"Generated from manifest v{manifest.version_no}, row {row_index}",
        status=status, created_by=user_id,
    )
    db.add(version)
    db.flush()
    gen_doc.current_version_id = version.id

    generation = ManifestGeneration(
        org_id=manifest.org_id, manifest_id=manifest.id, source_record=resolved,
        source_version_id=source_version_id, source_record_key=str(row_index),
        # Unit lineage is the compliance artifact: what each value resolved to,
        # by which strategy, and whether a human touched it.
        field_lineage=fill.field_lineage + resolution.lineage(),
        condition_lineage=fill.condition_lineage,
        qa_passed=fill.qa_passed and not resolution.needs_review,
        qa_notes=fill.qa_notes, blob_path=out_rel, created_by=user_id,
        # The lineage's link to its document, as a real column. Reaching it via
        # `blob_path` was never reliable: `apply_version_text` writes a different
        # path, so an edited document lost its lineage, and previews share paths
        # -- which meant `retention.delete_generated_document`, which deletes by
        # path, could destroy another document's record.
        document_version_id=version.id,
    )
    db.add(generation)
    db.flush()

    # §22: every QA failure, with the check that fired and the object it fired
    # on. The notes were already on the generation record as prose; this is the
    # same failures in a shape that can be counted.
    record_qa_findings(
        db, org_id=manifest.org_id, findings=fill.qa_findings, manifest_id=manifest.id,
        generation_id=generation.id, document_version_id=version.id,
    )

    for task in resolution.open_tasks:
        db.add(ReviewTask(
            org_id=manifest.org_id, project_id=project.id, generation_id=generation.id,
            manifest_id=manifest.id, unit_id=task["unit_id"], kind=task["kind"],
            question=task["question"], context=task["context"],
            proposed_value=task.get("proposed_value"),
            # Which letter this question is about, so answering it can move the
            # document, and so the queue can show the reader what they are
            # deciding for. `created_by` stays null: the machine raised this one.
            document_version_id=version.id,
        ))

    return RowOutcome(
        # Mirrors the document's own status, so a batch summary that says
        # "9 generated" cannot include a row that failed QA.
        row_index, "generated" if status == "draft" else status,
        document_id=gen_doc.id, document_version_id=version.id, filename=filename,
        qa_passed=fill.qa_passed, qa_notes=fill.qa_notes,
        open_tasks=len(resolution.open_tasks),
    )


def run_batch(
    job_id: str,
    manifest_id: str,
    source_blob_path: str,
    source_file_type: str,
    sheet: str | None,
    field_bindings: dict,
    value_map: dict,
    row_indices: list[int] | None,
    language: str,
    locale_override: str | None,
    user_id: str,
    source_version_id: str | None = None,
) -> None:
    """Background entry point. Owns its own session because the request that
    scheduled it has already returned and closed its own."""
    db = SessionLocal()
    try:
        # §16 row-level security. Nothing has told this session which tenant it
        # speaks for -- the request that scheduled the batch has gone -- and on
        # PostgreSQL every read below would come back empty, which a batch
        # runner would report as "no rows to generate" rather than as a fault.
        # `users` is the one org-bearing table outside RLS, so it is where the
        # tenant can still be looked up from. Inert on SQLite.
        adopt_org_of_user(db, user_id)

        job = db.get(GenerationJob, job_id)
        manifest = db.get(TemplateManifest, manifest_id)
        project = db.get(Project, job.project_id)
        template_version = db.get(TemplateVersion, manifest.template_version_id)
        template_path = str(abs_path(template_version.blob_path))
        locale, locale_source = (
            (locale_override, "request") if locale_override
            else resolved_locale(region=project.region, project_locale=project.locale)
        )

        _columns, records = extract_records(str(abs_path(source_blob_path)), source_file_type, sheet)
        if row_indices is not None:
            wanted = set(row_indices)
            records = [r for r in records if r.get("_row_index") in wanted]

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        # Carried on the job so the formatting decision is visible where the
        # documents are. Without it a reviewer reading "May 9, 2024" on an
        # Australian letter cannot tell a configured choice from a default
        # nobody made -- and the default is what every project got, because the
        # region vocabulary is continental and maps to no locale.
        job.progress = {
            "rows_total": len(records), "rows_done": 0, "generated": 0,
            "pending_review": 0, "failed": 0,
            "locale": locale, "locale_source": locale_source,
        }
        db.commit()

        # ---- canary set first ----
        # One wrong mapping inside an approved manifest produces thousands of
        # wrong documents before a human sees the first one. So a representative
        # handful is rendered and fully QA'd up front, and the rest of the batch
        # runs only if they come back clean.
        canary = set(_canary_positions(len(records)))
        results_by_position: dict[int, RowOutcome] = {}

        def _ordered() -> list[RowOutcome]:
            return [results_by_position[p] for p in sorted(results_by_position)]

        def _publish(done: int) -> None:
            rows = _ordered()
            job.progress = {
                "rows_total": len(records),
                "rows_done": done,
                "generated": sum(1 for r in rows if r.status == "generated"),
                "pending_review": sum(1 for r in rows if r.status == "pending_review"),
                "blocked": sum(1 for r in rows if r.status == "blocked"),
                "failed": sum(1 for r in rows if r.status == "failed"),
                "canary_size": len(canary),
                # Re-stated on every publish: this dict is replaced wholesale,
                # so anything set once at the start is lost on the first row.
                "locale": locale,
                "locale_source": locale_source,
                "rows": [r.__dict__ for r in rows],
            }
            # Commit per row so progress is observable while the batch runs and
            # one bad row cannot roll back the documents already produced.
            db.commit()

        def _run(position: int) -> RowOutcome:
            outcome = run_row(
                db, manifest=manifest, template_path=template_path, project=project,
                record=records[position], field_bindings=field_bindings, value_map=value_map,
                locale=locale, language=language, user_id=user_id, run_key=job.id,
                source_version_id=source_version_id,
            )
            outcome.is_canary = position in canary
            results_by_position[position] = outcome
            return outcome

        for done, position in enumerate(sorted(canary), start=1):
            _run(position)
            _publish(done)

        failed_canaries = [
            r for r in _ordered() if r.is_canary and (r.status == "failed" or not r.qa_passed)
        ]
        if failed_canaries:
            job.status = "blocked"
            job.error = (
                f"Canary check failed on {len(failed_canaries)} of {len(canary)} sample rows; "
                f"the remaining {len(records) - len(canary)} rows were not generated. "
                "Fix the manifest or the source data and run the batch again."
            )
            job.finished_at = datetime.now(timezone.utc)
            _publish(len(canary))
            return

        remaining = [p for p in range(len(records)) if p not in canary]
        for offset, position in enumerate(remaining, start=len(canary) + 1):
            _run(position)
            _publish(offset)

        rows = _ordered()
        # A blocked row is a defective document, not a clean run with a note.
        # Rolling it into "completed" is how a batch reports success while some
        # of its letters cannot be issued.
        failures = sum(1 for r in rows if r.status in ("failed", "blocked"))
        job.status = "completed_with_errors" if failures else "completed"
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        # PostgreSQL reverts a session SET when its transaction aborts, so the
        # rollback above has just thrown away the tenant scope. Without
        # re-adopting it the failure below would be invisible: the job row would
        # read as missing and the batch would end without ever recording why.
        adopt_org_of_user(db, user_id)
        job = db.get(GenerationJob, job_id)
        if job:
            job.status = "failed"
            job.error = traceback.format_exc(limit=4)
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        release_org_scope(db)
        db.close()
