"""What the compiler is doing, while it is doing it.

Compiling a template is between two seconds and three minutes of silence. The
fast path is deterministic -- read the OOXML, match the family grammar, compile
the rules -- and costs nothing; the slow path sends the whole document to a
model and costs real money. From the outside both are the same spinner, so a
reviewer cannot tell a template that compiled for free from one that spent two
minutes and a model call, and cannot tell either from a request that has hung.

This records the stages as they happen. Each carries a `kind`, because the
distinction an operator wants is not how far along it is but what sort of work
is running:

    deterministic  parsing, rule compilation, reconciliation -- free, repeatable
    retrieval      similarity search over this tenant's indexed columns
    model          a language model reading the template. The expensive one.
    embedding      writing vectors back, so the next template binds itself

Storage is a `GenerationJob` row whose id is a token the client chose before it
made the request. That is what makes the progress readable *during* the compile
rather than after it: the client cannot learn a server-generated id until the
response arrives, and by then there is nothing left to watch. It also means the
existing `GET /jobs/{id}` serves this with no new endpoint and no new polling
code -- folded by `public_stages` into three plain steps, because the stage list
itself describes the pipeline and is ours to read, not the customer's.

Every write commits on its own session. The request doing the compiling holds a
transaction that will not commit until it finishes, so anything written on that
session is invisible to the poller for exactly as long as it is interesting.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import GenerationJob
from app.tenancy import set_current_org

log = logging.getLogger(__name__)

STAGE_FAILED_MESSAGE = "This step could not be completed. Please try again."

DETERMINISTIC = "deterministic"
RETRIEVAL = "retrieval"
MODEL = "model"
EMBEDDING = "embedding"

RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: A client-chosen token has to be safe to use as a primary key and impossible
#: to confuse with somebody else's job. A uuid satisfies both; anything else is
#: refused rather than sanitised, because a caller passing "1" would otherwise
#: silently share a row with every other caller passing "1".
TOKEN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def valid_token(token: str | None) -> bool:
    return bool(token and TOKEN_RE.match(token))


# ---- what the poller is shown ----
#
# The stages above are the engine's own record: which kind of work ran (model,
# retrieval, embedding), how the template was split, how many review rounds it
# took. That is how the product works, so it stays on the row for us and the
# poller gets three plain steps folded out of it. An unknown key lands in the
# middle step: a new internal stage must never appear on its own.

PENDING = "pending"

PUBLIC_STEPS = (
    ("read", "Reading your template"),
    ("analyse", "Understanding the structure"),
    ("finish", "Finishing up"),
)
_PUBLIC_LABEL = dict(PUBLIC_STEPS)

_STEP_OF = {
    "parse": "read", "scan": "read", "chunk": "read", "rules": "read",
    "retrieval": "analyse", "agentic": "analyse", "write": "analyse",
    "reconcile": "analyse", "review": "analyse", "model": "analyse",
    "embedding": "finish",
}


def public_step(key: str) -> str:
    return _STEP_OF.get(key, "analyse")


def _parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def public_stages(stages: list | None, *, job_status: str | None) -> list[dict]:
    """The internal stage list collapsed to the three public steps.

    Each step is `{key, label, status, elapsed_seconds}` -- no kind, no detail,
    no per-round rows. A finished job reports every step done: the internal
    stages it skipped (a single-part template has no reconcile) or shrugged off
    (indexing is best effort) are not the reader's concern.
    """
    grouped: dict[str, list] = {key: [] for key, _ in PUBLIC_STEPS}
    for stage in stages or []:
        grouped[public_step(stage.get("key", ""))].append(stage)

    now = datetime.now(timezone.utc)
    out = []
    for key, label in PUBLIC_STEPS:
        members = grouped[key]
        statuses = {m.get("status") for m in members}
        if job_status == DONE:
            status = DONE
        elif FAILED in statuses:
            status = FAILED
        elif RUNNING in statuses:
            status = RUNNING
        elif members and statuses <= {DONE}:
            status = DONE
        else:
            status = PENDING

        started = [t for t in (_parse_time(m.get("started_at")) for m in members) if t]
        finished = [t for t in (_parse_time(m.get("finished_at")) for m in members) if t]
        elapsed = None
        if started:
            end = max(finished) if status in (DONE, FAILED) and finished else now
            elapsed = round(max(0.0, (end - min(started)).total_seconds()), 1)
        out.append({"key": key, "label": label, "status": status, "elapsed_seconds": elapsed})

    # A later step that has begun means every earlier one is over.
    for index in range(len(out) - 1, 0, -1):
        if out[index]["status"] in (RUNNING, DONE, FAILED):
            for earlier in out[:index]:
                if earlier["status"] in (PENDING, RUNNING):
                    earlier["status"] = DONE
    return out


class CompileProgress:
    """Publishes stage events for one compile, or does nothing at all.

    A caller that passed no token gets an instance whose methods are no-ops, so
    the compile path reads the same either way and nothing has to branch on
    whether anybody is watching.
    """

    def __init__(self, token: str | None, *, org_id: str, project_id: str, user_id: str,
                 template_file_id: str | None = None):
        self.token = token if valid_token(token) else None
        self._org_id = org_id
        self._project_id = project_id
        self._user_id = user_id
        # Carried on the row so a reading can be found by its template -- the
        # templates list re-attaches a reload to it, and a second reading of the
        # same file is refused while one is running.
        self._template_file_id = template_file_id
        self._stages: list[dict] = []
        # Neutral counts the reader can check against their own document, each
        # recorded only once it is actually known. Never estimated.
        self._facts: dict = {}
        self._result: dict | None = None
        self._error_written = False

    # ------------------------------------------------------------------ writes

    def _write(self, *, status: str, error: str | None = None) -> None:
        if not self.token:
            return
        # Its own session, committed immediately. The request that is compiling
        # holds a transaction open for the duration; a write on that session
        # would become visible only once the compile finished, which is the one
        # moment this is no longer worth reading.
        # Opened inside the guard, not before it. `SessionLocal()` itself can
        # raise -- a pool exhausted, a database gone -- and a telemetry call that
        # takes the compile down with it is worse than no telemetry at all,
        # which is the one thing this method promises not to do.
        db = None
        try:
            db = SessionLocal()
            set_current_org(db, self._org_id)
            job = db.get(GenerationJob, self.token)
            if job is None:
                job = GenerationJob(
                    id=self.token, org_id=self._org_id, project_id=self._project_id,
                    status=status, model_profile="template-compile",
                    created_by=self._user_id, started_at=datetime.now(timezone.utc),
                )
                db.add(job)
            job.status = status
            progress = {"stages": list(self._stages), "facts": dict(self._facts)}
            if self._template_file_id:
                progress["template_file_id"] = self._template_file_id
            if self._result is not None:
                progress["result"] = dict(self._result)
            job.progress = progress
            if error:
                job.error = error
                self._error_written = True
            if status in (DONE, FAILED):
                job.finished_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            # Never let telemetry break a compile. A reviewer would rather have
            # the manifest and no progress bar than neither.
            if db is not None:
                with contextlib.suppress(Exception):
                    db.rollback()
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    @contextlib.contextmanager
    def stage(self, key: str, label: str, kind: str = DETERMINISTIC):
        """Mark a stage running, then done -- or failed, and re-raise.

        The failure is recorded before the exception continues on its way,
        because "which stage was it in when it died" is the whole question a
        reviewer asks about a compile that returned an error.
        """
        entry = {
            "key": key, "label": label, "kind": kind, "status": RUNNING,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None, "detail": None,
        }
        self._stages.append(entry)
        self._write(status=RUNNING)
        try:
            yield entry
        except Exception as exc:
            entry["status"] = FAILED
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            # Which stage failed is the reviewer's question; the exception text
            # is ours. The progress row is polled by the client, so it gets a
            # sentence and the log gets the rest.
            log.error("Compile stage %r failed", key, exc_info=exc)
            entry["detail"] = STAGE_FAILED_MESSAGE
            # Named by the public step, not the internal stage: `error` is
            # returned to the poller as it stands.
            self._write(status=FAILED,
                        error=f"{_PUBLIC_LABEL[public_step(key)]} failed. {STAGE_FAILED_MESSAGE}")
            raise
        entry["status"] = DONE
        entry["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._write(status=RUNNING)

    def note(self, detail: str) -> None:
        """Attach a line to the stage currently running.

        Used for the things a reviewer wants to know mid-flight and cannot infer
        from the label -- which family matched, how many columns the retrieval
        found, whether the rules were enough.
        """
        if self._stages:
            self._stages[-1]["detail"] = detail[:200]
            self._write(status=RUNNING)

    def finish(self) -> None:
        self._write(status=DONE)

    # ------------------------------------------------ background-mode additions

    def start(self) -> None:
        """Write the row, running, before anything has happened.

        A background reading answers 202 before its first stage begins; without
        this the poller's first few ticks are 404s and the templates list has
        nothing to re-attach a reload to.
        """
        self._write(status=RUNNING)

    def fact(self, **values) -> None:
        """Record counts that are now known. `None` means not known: skipped."""
        known = {k: v for k, v in values.items()
                 if isinstance(v, int) and not isinstance(v, bool)}
        if known:
            self._facts.update(known)
            self._write(status=RUNNING)

    def set_result(self, result: dict) -> None:
        """What the reading produced, in the reader's terms. Written with the
        terminal status by `finish` or `fail`."""
        self._result = dict(result)

    def fail(self, message: str, *, keep_stage_error: bool = True) -> None:
        """End the run as failed. A stage that already failed wrote its own
        sentence naming the step; by default that one is kept rather than
        overwritten with something vaguer."""
        keep = keep_stage_error and self._error_written
        self._write(status=FAILED, error=None if keep else message)


# ---- finding a reading by its template ----

#: A reading that has said nothing for this long is not running: the process
#: that owned it has gone (a restart mid-read). Without a horizon it would hold
#: the template's "Reading" badge, and refuse every retry, forever.
STALE_AFTER_SECONDS = 30 * 60

COMPILE_PROFILE = "template-compile"


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def running_readings(db, *, project_id: str) -> dict[str, GenerationJob]:
    """The live reading per template in one project, keyed by template id.

    Filtered in Python on the JSON: the column is plain JSON on both SQLite and
    PostgreSQL, and a project has a handful of these rows, not thousands.
    """
    from sqlalchemy import select

    rows = db.scalars(
        select(GenerationJob).where(
            GenerationJob.project_id == project_id,
            GenerationJob.model_profile == COMPILE_PROFILE,
            GenerationJob.status == RUNNING,
        )
    ).all()
    horizon = datetime.now(timezone.utc).timestamp() - STALE_AFTER_SECONDS
    out: dict[str, GenerationJob] = {}
    for job in rows:
        template_id = (job.progress or {}).get("template_file_id")
        started = _aware(job.started_at) or _aware(job.created_at)
        if not template_id or (started and started.timestamp() < horizon):
            continue
        # The newest wins when two exist (a sync read racing a background one).
        current = out.get(template_id)
        if current is None or (_aware(current.started_at) or started) < started:
            out[template_id] = job
    return out


def reading_state(job: GenerationJob) -> dict:
    """What the templates list says about a running reading: enough to find
    the job again and draw 'step n of 3', and nothing about how it is done."""
    stages = public_stages((job.progress or {}).get("stages"), job_status=job.status)
    step_index = next((i for i, s in enumerate(stages) if s["status"] != DONE), len(stages) - 1)
    return {"progress_token": job.id, "status": job.status, "step_index": step_index}


def public_job_extras(job: GenerationJob) -> dict:
    """The compile-only fields `GET /jobs/{id}` adds: the three steps, the
    counts known so far and, once finished, the result. Allow-listed key by
    key, so something new written to the row is not published by default."""
    progress = job.progress if isinstance(job.progress, dict) else {}
    facts = {k: v for k, v in (progress.get("facts") or {}).items() if k in PUBLIC_FACTS}
    result = progress.get("result")
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if k in PUBLIC_RESULT}
    else:
        result = None
    # The whole run's wall time, so a client re-attaching after a reload shows
    # the real elapsed time rather than restarting its clock at zero.
    started = _aware(job.started_at)
    end = _aware(job.finished_at) or datetime.now(timezone.utc)
    elapsed = round(max(0.0, (end - started).total_seconds()), 1) if started else None
    return {
        "stages": public_stages(progress.get("stages"), job_status=job.status),
        "elapsed_seconds": elapsed,
        "facts": facts,
        "result": result,
        "template_file_id": progress.get("template_file_id"),
    }


PUBLIC_FACTS = frozenset({
    "placeholders_found", "instructions_found", "word_fields_found",
    "values_found", "optional_sections_found",
})
PUBLIC_RESULT = frozenset({
    "manifest_id", "status", "document_summary", "field_count", "condition_count",
    "warning_count", "approval_blocked_reason", "failure_reason",
})
