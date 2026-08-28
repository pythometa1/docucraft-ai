"""The canary gate: a batch proves itself on a sample before it runs to the end.

One wrong mapping inside an approved manifest produces thousands of wrong
documents before a human sees the first one. So a spread handful of rows is
rendered and fully QA'd first, and the remainder runs only if they come back
clean. These tests drive `run_batch` against a real template and a real CSV,
because the property under test is "the other rows did not get generated".
"""

import os

import docx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    GeneratedDocument, GenerationJob, Project, TemplateFile, TemplateManifest,
    TemplateVersion, User,
)
from app.generation.batch_runner import run_batch
from app.storage import abs_path


ROWS = 9  # enough that the canary set (3) is a strict subset


@pytest.fixture()
def batch_fixture(app_client, two_orgs):
    """An approved manifest over a one-field template, plus a 9-row CSV.

    Returns a callable that runs a batch for a given CSV body and yields the job.
    """
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))

        template_rel = f"templates/{project.id}/canary.docx"
        os.makedirs(str(abs_path(template_rel).parent), exist_ok=True)
        document = docx.Document()
        document.add_paragraph("Dear <full_name>,")
        document.add_paragraph("Welcome aboard.")
        document.save(str(abs_path(template_rel)))

        template_file = TemplateFile(
            org_id=project.org_id, project_id=project.id, name="canary.docx",
            status="parsed", created_by=user.id,
        )
        db.add(template_file)
        db.flush()
        template_version = TemplateVersion(
            template_file_id=template_file.id, org_id=project.org_id, version_no=1, blob_path=template_rel,
            created_by=user.id,
        )
        db.add(template_version)
        db.flush()
        template_file.current_version_id = template_version.id

        manifest = TemplateManifest(
            org_id=project.org_id, template_file_id=template_file.id,
            template_version_id=template_version.id, version_no=1, status="approved",
            fields=[{
                "id": "full_name", "type": "text", "required": True,
                "slots": [{"paragraph_index": 0, "span_index": 0, "text": "<full_name>", "field_id": "full_name"}],
            }],
            conditions=[], blocks=[], delete_always=[], created_by=user.id,
        )
        db.add(manifest)
        db.commit()
        ids = (project.org_id, project.id, manifest.id, user.id)
    finally:
        db.close()

    org_id, project_id, manifest_id, user_id = ids

    def _run(csv_body: str) -> GenerationJob:
        source_rel = f"sources/{project_id}/canary.csv"
        os.makedirs(str(abs_path(source_rel).parent), exist_ok=True)
        abs_path(source_rel).write_text(csv_body)

        db = SessionLocal()
        try:
            job = GenerationJob(
                org_id=org_id, project_id=project_id, status="queued",
                languages=["en"], created_by=user_id,
            )
            db.add(job)
            db.commit()
            job_id = job.id
        finally:
            db.close()

        run_batch(
            job_id=job_id, manifest_id=manifest_id, source_blob_path=source_rel,
            source_file_type="csv", sheet=None, field_bindings={"full_name": "full_name"},
            value_map={},
            row_indices=None, language="en", locale_override=None, user_id=user_id,
        )

        db = SessionLocal()
        try:
            db.expire_all()
            return db.get(GenerationJob, job_id), org_id
        finally:
            db.close()

    return _run


@pytest.fixture()
def document_count(two_orgs):
    """How many documents this org holds right now.

    A delta, not an absolute: `two_orgs` is session-scoped, so documents from
    earlier tests are still there and an absolute count would couple these tests
    to the order they run in.
    """
    _token_a, project_a, *_ = two_orgs

    def _count() -> int:
        db = SessionLocal()
        try:
            project = db.get(Project, project_a)
            rows = db.scalars(
                select(GeneratedDocument).where(GeneratedDocument.org_id == project.org_id)
            ).all()
            return len(rows)
        finally:
            db.close()

    return _count


def test_a_clean_batch_runs_every_row(batch_fixture):
    body = "full_name\n" + "\n".join(f"Person {i}" for i in range(ROWS))
    job, _org = batch_fixture(body)

    assert job.status == "completed", job.error
    assert job.progress["rows_total"] == ROWS
    assert job.progress["rows_done"] == ROWS
    assert job.progress["generated"] == ROWS
    assert job.progress["canary_size"] == 3


def test_a_failing_canary_stops_the_batch(batch_fixture, document_count):
    """The CSV has no `full_name` column, so the required field is missing on
    every row. The first canary fails and the other six must never be rendered.
    """
    body = "other_column\n" + "\n".join(f"Person {i}" for i in range(ROWS))
    before = document_count()
    job, _org = batch_fixture(body)

    assert job.status == "blocked", f"expected blocked, got {job.status}: {job.error}"
    assert "Canary check failed" in (job.error or "")
    # Only the canary rows were attempted.
    assert job.progress["rows_done"] == 3
    assert len(job.progress["rows"]) == 3
    assert all(r["is_canary"] for r in job.progress["rows"])
    assert document_count() - before == 3, "rows beyond the canary set were generated"


def test_a_qa_failure_after_the_canary_marks_the_row_blocked(batch_fixture):
    """One row lacks the required value while the canary rows have it, so the
    batch is allowed to run -- and the bad row must not be counted as generated
    or stored as an ordinary draft."""
    # A second column keeps row 7 a real row: a lone empty cell would make the
    # CSV line blank, and a blank line is dropped during extraction rather than
    # reaching the fill engine as a record with a missing value.
    rows = [f"Person {i},Ops" for i in range(ROWS)]
    rows[7] = ",Ops"  # row 7 is not a canary position (0, 3, 6)
    body = "full_name,dept\n" + "\n".join(rows)

    job, _org = batch_fixture(body)

    assert job.status == "completed_with_errors", job.error
    assert job.progress["blocked"] == 1
    assert job.progress["generated"] == ROWS - 1
    blocked = [r for r in job.progress["rows"] if r["status"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["qa_passed"] is False
    assert not blocked[0]["is_canary"]


def test_canary_rows_are_spread_not_the_first_three(batch_fixture):
    body = "full_name\n" + "\n".join(f"Person {i}" for i in range(ROWS))
    job, _org = batch_fixture(body)

    canary_rows = sorted(r["row_index"] for r in job.progress["rows"] if r["is_canary"])
    assert len(canary_rows) == 3

    # Positions 0, 3 and 6 of nine rows. Spread matters: a spreadsheet arrives
    # sorted, so the first three rows are usually the same department, the same
    # country and the same branch of every condition in the manifest -- the
    # sample least likely to exercise the mapping that is wrong.
    first, middle, last = canary_rows
    assert middle - first == 3 and last - middle == 3, (
        f"canary rows {canary_rows} are not evenly spread across the batch"
    )
    assert last - first == 6
