"""Stage events for a compile, readable while it is still running.

Compiling is between two seconds and three minutes and from the outside both
look like one spinner. The distinction worth publishing is not how far along it
is but what sort of work is running: a template the colour rules can read costs
nothing, one handed to a model costs money and minutes, and a request that has
hung looks exactly like either.

The token is chosen by the client before it makes the request. That is the whole
mechanism -- a server-generated id arrives only with the response, by which
point there is nothing left to watch.
"""

import uuid

import pytest
from sqlalchemy import select

from app.compile_progress import (
    DETERMINISTIC,
    DONE,
    FAILED,
    MODEL,
    RUNNING,
    CompileProgress,
    valid_token,
)
from app.db import SessionLocal
from app.models import GenerationJob, Organization, Project, User
from app.security import hash_password


_DISPLAY_IDS = iter(range(93001, 94000))


@pytest.fixture
def org(app_client):
    """A fresh organisation per test.

    `display_id` is unique across the table, so a fixed number works once and
    then collides -- which reads as a bug in the code under test rather than in
    the fixture, and costs the next person half an hour.
    """
    db = SessionLocal()
    try:
        o = Organization(name="ProgressOrg")
        db.add(o)
        db.flush()
        u = User(org_id=o.id, email=f"p-{o.id[:8]}@t.test", full_name="P",
                 password_hash=hash_password("pw"), role_key="org_admin")
        db.add(u)
        db.flush()
        p = Project(org_id=o.id, display_id=next(_DISPLAY_IDS), name="Progress", region="Europe",
                    function="Human Resources", document_type="Offer Letter",
                    language="English", status="pending", created_by=u.id)
        db.add(p)
        db.flush()
        ids = (o.id, p.id, u.id)
        db.commit()
        return ids
    finally:
        db.close()


def _job(token):
    db = SessionLocal()
    try:
        return db.get(GenerationJob, token)
    finally:
        db.close()


@pytest.mark.parametrize("token,ok", [
    (str(uuid.uuid4()), True),
    ("1", False),
    ("", False),
    (None, False),
    ("../../etc/passwd", False),
    ("not-a-uuid-at-all", False),
])
def test_only_a_uuid_is_accepted_as_a_token(token, ok):
    """A caller passing "1" would otherwise share a row with every other caller
    passing "1", and read somebody else's compile."""
    assert valid_token(token) is ok


def test_stages_are_visible_before_the_compile_finishes(org):
    """The property the whole design exists for. Each write commits on its own
    session, so a poller sees the stage while it is still running rather than
    when the compile is over."""
    org_id, project_id, user_id = org
    token = str(uuid.uuid4())
    progress = CompileProgress(token, org_id=org_id, project_id=project_id, user_id=user_id)

    with progress.stage("parse", "Reading the document", DETERMINISTIC):
        job = _job(token)
        assert job is not None, "nothing was readable while the stage was running"
        [stage] = job.progress["stages"]
        assert stage["status"] == RUNNING
        assert stage["kind"] == DETERMINISTIC

    assert _job(token).progress["stages"][0]["status"] == DONE


def test_the_kind_distinguishes_the_expensive_stage(org):
    """"AI model" is the stage that costs money and minutes. A reviewer watching
    should be able to tell it from the free ones without reading labels."""
    org_id, project_id, user_id = org
    token = str(uuid.uuid4())
    progress = CompileProgress(token, org_id=org_id, project_id=project_id, user_id=user_id)
    with progress.stage("rules", "Reading the colour convention", DETERMINISTIC):
        pass
    with progress.stage("model", "Handing the template to a model", MODEL):
        pass
    kinds = [s["kind"] for s in _job(token).progress["stages"]]
    assert kinds == [DETERMINISTIC, MODEL]


def test_a_failing_stage_is_recorded_and_the_error_still_raises(org):
    """"Which stage was it in when it died" is the question asked of a compile
    that errored, and it cannot be answered after the exception has unwound."""
    org_id, project_id, user_id = org
    token = str(uuid.uuid4())
    progress = CompileProgress(token, org_id=org_id, project_id=project_id, user_id=user_id)

    with pytest.raises(RuntimeError, match="model refused"):
        with progress.stage("model", "Handing the template to a model", MODEL):
            raise RuntimeError("model refused")

    job = _job(token)
    assert job.status == FAILED
    assert job.progress["stages"][0]["status"] == FAILED
    assert "model refused" in job.progress["stages"][0]["detail"]


def test_a_note_lands_on_the_running_stage(org):
    org_id, project_id, user_id = org
    token = str(uuid.uuid4())
    progress = CompileProgress(token, org_id=org_id, project_id=project_id, user_id=user_id)
    with progress.stage("retrieval", "Looking up column names", DETERMINISTIC):
        progress.note("16 column(s) found to compare against")
    assert _job(token).progress["stages"][0]["detail"] == "16 column(s) found to compare against"


def test_without_a_token_nothing_is_written_and_nothing_breaks(org):
    """A caller that did not ask to be watched must still compile. The stage
    context manager is used unconditionally in the compile path, so it has to be
    inert rather than optional."""
    org_id, project_id, user_id = org
    progress = CompileProgress(None, org_id=org_id, project_id=project_id, user_id=user_id)
    with progress.stage("parse", "Reading the document", DETERMINISTIC):
        progress.note("no watcher")
    progress.finish()

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(GenerationJob).where(GenerationJob.model_profile == "template-compile")
        ).all()
        assert all(r.org_id != org_id or r.id != "None" for r in rows)
    finally:
        db.close()


def test_telemetry_failure_never_breaks_the_compile(org, monkeypatch):
    """A reviewer would rather have the manifest and no progress bar than
    neither, so a write that throws is swallowed."""
    from app import compile_progress as cp

    org_id, project_id, user_id = org
    progress = CompileProgress(str(uuid.uuid4()), org_id=org_id, project_id=project_id, user_id=user_id)

    def explode():
        raise RuntimeError("database gone")

    monkeypatch.setattr(cp, "SessionLocal", explode)
    with progress.stage("parse", "Reading the document", DETERMINISTIC):
        pass
    progress.finish()
