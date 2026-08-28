"""What the compiler is doing, while it is doing it.

Compiling a template is between two seconds and three minutes of silence. The
fast path is deterministic -- read the OOXML, match the family grammar, compile
the rules -- and costs nothing; the slow path sends the whole document to a
model and costs real money. From the outside both are the same spinner, so a
reviewer cannot tell a template that compiled for free from one that spent two
minutes and a model call, and cannot tell either from a request that has hung.

This publishes the stages as they happen. Each carries a `kind`, because the
distinction people actually want is not how far along it is but what sort of
work is running:

    deterministic  parsing, rule compilation, reconciliation -- free, repeatable
    retrieval      similarity search over this tenant's indexed columns
    model          a language model reading the template. The expensive one.
    embedding      writing vectors back, so the next template binds itself

Storage is a `GenerationJob` row whose id is a token the client chose before it
made the request. That is what makes the progress readable *during* the compile
rather than after it: the client cannot learn a server-generated id until the
response arrives, and by then there is nothing left to watch. It also means the
existing `GET /jobs/{id}` serves this with no new endpoint and no new polling
code.

Every write commits on its own session. The request doing the compiling holds a
transaction that will not commit until it finishes, so anything written on that
session is invisible to the poller for exactly as long as it is interesting.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import GenerationJob
from app.tenancy import set_current_org

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


class CompileProgress:
    """Publishes stage events for one compile, or does nothing at all.

    A caller that passed no token gets an instance whose methods are no-ops, so
    the compile path reads the same either way and nothing has to branch on
    whether anybody is watching.
    """

    def __init__(self, token: str | None, *, org_id: str, project_id: str, user_id: str):
        self.token = token if valid_token(token) else None
        self._org_id = org_id
        self._project_id = project_id
        self._user_id = user_id
        self._stages: list[dict] = []

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
            job.progress = {"stages": list(self._stages)}
            if error:
                job.error = error
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
            entry["detail"] = str(exc)[:200]
            self._write(status=FAILED, error=str(exc)[:500])
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
