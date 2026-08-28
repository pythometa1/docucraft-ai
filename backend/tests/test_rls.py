"""Row-level security: the backstop, and the one thing about it a SQLite suite
can actually prove.

§16 asks for "PostgreSQL row-level security as the backstop, so a forgotten
WHERE clause fails closed instead of leaking". The suite runs on SQLite, which
has no row-level security at all, so none of these tests can watch a policy
refuse a row. That is not a reason to test nothing -- it is a reason to be
precise about what is worth pinning.

What actually regresses is coverage. The policies were written once, against the
tables that existed that afternoon; the failure a year later is a new customer
table that nobody enabled RLS on, sitting outside the backstop while every
document and every reviewer assumes it is inside. So the test that matters is
`test_every_org_scoped_table_is_covered_by_a_policy`: it reads what the
migrations actually emit, compares it against every table the models say carries
`org_id`, and goes red the moment those two disagree.

The rest pin the details that make a policy fail closed rather than open -- the
GUC name matching the one the application writes, `FORCE` as well as `ENABLE`,
and the fact that the maintenance bypass is not reachable from a request.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import os

import pytest

def _on_postgres() -> bool:
    """Whether this run is against PostgreSQL rather than the SQLite default.

    Some tests here assert the *inert* half of a control -- what happens on a
    backend that cannot express row-level security. Those are real assertions
    worth keeping (the suite runs on SQLite by default, and a no-op that started
    raising would take it down), but they describe SQLite, so they are skipped
    when TEST_DATABASE_URL points the same suite at PostgreSQL.
    """
    return os.environ.get("DATABASE_URL", "").startswith("postgresql")

from app import tenancy
from app.db import Base, SessionLocal

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _migration_modules():
    """Every Alembic revision, imported so its constants can be read.

    Reading the modules rather than one hand-picked one is deliberate: RLS is
    expected to arrive with the table it protects, so a later revision that
    creates a customer table and enables RLS on it in the same breath is
    counted here without anybody editing this file.
    """
    modules = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_rls_probe_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def _rls_migrations():
    return [m for m in _migration_modules() if getattr(m, "RLS_TABLES", None)]


def _covered_tables() -> set:
    covered = set()
    for module in _rls_migrations():
        covered |= set(module.RLS_TABLES)
    return covered


def _exempt_tables() -> dict:
    exempt = {}
    for module in _migration_modules():
        exempt.update(getattr(module, "RLS_EXEMPT", {}) or {})
    return exempt


def _org_scoped_tables() -> set:
    return {name for name, table in Base.metadata.tables.items() if "org_id" in table.columns}


# ------------------------------------------------------------------ coverage


def test_every_org_scoped_table_is_covered_by_a_policy():
    """The guardrail this module exists for.

    A customer table added next year with `org_id` on it and no policy sits
    outside the backstop, and nothing else in the suite would notice -- the
    handlers filter correctly today, so every test still passes while the
    fail-closed guarantee quietly stops applying to one table.
    """
    uncovered = _org_scoped_tables() - _covered_tables() - set(_exempt_tables())
    assert not uncovered, (
        "tables carrying org_id with no row-level security policy:\n  "
        + "\n  ".join(sorted(uncovered))
        + "\n\nEnable RLS on them in the migration that creates them (export RLS_TABLES), "
          "or add them to that revision's RLS_EXEMPT with a reason."
    )


def test_no_policy_is_written_for_a_table_that_does_not_exist():
    """A policy naming a dropped table is DDL that fails on the next fresh
    database -- and it reads as coverage until someone runs the migrations from
    scratch, which is usually a new environment on a bad day."""
    unknown = _covered_tables() - set(Base.metadata.tables)
    assert not unknown, f"RLS_TABLES names tables that do not exist: {sorted(unknown)}"


def test_every_exemption_names_a_real_table_and_gives_a_reason():
    """A stale exemption is worse than none: it exempts nothing while reading as
    though it exempts something."""
    exempt = _exempt_tables()
    unknown = set(exempt) - set(Base.metadata.tables)
    assert not unknown, f"RLS_EXEMPT names tables that do not exist: {sorted(unknown)}"
    assert all(reason.strip() for reason in exempt.values()), (
        "an RLS_EXEMPT entry is a decision to leave a table outside the backstop; it needs "
        "a written reason"
    )


def test_users_is_the_only_table_left_outside_the_backstop():
    """Pinned by name. The exemption exists because authentication has to find
    the user before it knows the tenant; anything else appearing here is a
    convenience being dressed up as a chicken-and-egg problem."""
    assert set(_exempt_tables()) == {"users"}


# --------------------------------------------------------------- the policies


@pytest.fixture(scope="module")
def statements() -> list:
    emitted = []
    for module in _rls_migrations():
        emitted.extend(module.rls_statements())
    return emitted


def test_the_policy_reads_the_setting_the_application_actually_writes(statements):
    """A policy keyed on a GUC nobody sets fails closed on every query, which
    presents as a total outage with no error message rather than as a typo."""
    creates = [s for s in statements if s.startswith("CREATE POLICY org_isolation")]
    assert creates, "no org-isolation policies were emitted at all"
    for statement in creates:
        assert f"current_setting('{tenancy.ORG_GUC}', true)" in statement, statement
        assert "org_id = current_setting" in statement, statement


def test_the_setting_is_read_with_missing_ok_so_an_unscoped_session_sees_nothing(statements):
    """`current_setting(name, true)` returns NULL when unset; `org_id = NULL` is
    NULL, and NULL is not TRUE, so the rows are invisible. Read it without the
    second argument and an unscoped session raises instead -- which is louder,
    but turns every unauthenticated health check into a 500."""
    for statement in statements:
        if "current_setting(" in statement:
            assert ", true)" in statement, f"missing_ok dropped from: {statement}"


def test_every_covered_table_is_forced_not_merely_enabled(statements):
    """ENABLE without FORCE is a policy the table's owner ignores -- and this
    application connects as the schema owner. An inert policy is worse than none
    because it reads as protection in a compliance review."""
    enabled = {s.split()[2] for s in statements if s.endswith("ENABLE ROW LEVEL SECURITY")}
    forced = {s.split()[2] for s in statements if s.endswith("FORCE ROW LEVEL SECURITY")}
    assert enabled == forced, f"enabled but not forced: {sorted(enabled - forced)}"
    assert enabled == _covered_tables()


def test_writes_are_checked_as_well_as_reads(statements):
    """USING filters what a session can see; WITH CHECK is what stops it writing
    a row belonging to somebody else. A policy with only the first lets a
    mis-scoped INSERT plant data in another tenant that the planter cannot then
    see -- the hardest kind of leak to notice."""
    for statement in statements:
        if statement.startswith("CREATE POLICY"):
            assert "WITH CHECK" in statement, statement


def test_the_maintenance_bypass_is_keyed_on_its_own_setting(statements):
    """The escape hatch a data migration needs, and nothing else. Keyed on a
    second GUC so it cannot be reached by setting the tenant one, and named so
    it is one grep away from being audited."""
    bypasses = [s for s in statements if s.startswith("CREATE POLICY rls_maintenance")]
    assert len(bypasses) == len(_covered_tables())
    for statement in bypasses:
        assert f"current_setting('{tenancy.MAINTENANCE_GUC}', true) = 'on'" in statement


def test_the_downgrade_removes_exactly_what_the_upgrade_added():
    """A downgrade that leaves policies behind makes the next upgrade fail with
    "policy already exists" on a database nobody can now move in either
    direction."""
    for module in _rls_migrations():
        added = {s.split()[2] for s in module.rls_statements() if s.startswith("CREATE POLICY")}
        dropped = {
            s.split()[-1]
            for s in (module.drop_statements() if hasattr(module, "drop_statements")
                      else module.drop_rls_statements())
            if s.startswith("DROP POLICY")
        }
        assert dropped == set(module.RLS_TABLES)
        assert added == {"org_isolation", "rls_maintenance"}


# ------------------------------------------------------ the SQLite behaviour


def test_the_migration_chain_still_runs_on_sqlite(app_client):
    """The whole suite depends on it. RLS DDL emitted unconditionally would take
    every test down with a syntax error, so the migration checks the dialect and
    returns -- and this is the assertion that the check is still there."""
    from sqlalchemy import inspect

    tables = set(inspect(SessionLocal().bind).get_table_names())
    assert _org_scoped_tables() <= tables, (
        "tables the models declare are missing from the migrated SQLite schema: "
        f"{sorted(_org_scoped_tables() - tables)}"
    )


@pytest.mark.skipif(
    _on_postgres(), reason="asserts the SQLite no-op path; PostgreSQL runs the real thing"
)
def test_setting_a_tenant_is_an_inert_no_op_on_sqlite(app_client):
    """It has to be inert rather than absent: the call sites are unconditional,
    so a version that raised on SQLite would mean the tenant scope could only be
    wired in on a database the suite never runs against."""
    db = SessionLocal()
    try:
        assert tenancy.set_current_org(db, "org-a") is False
        assert tenancy.clear_current_org(db) is False
        assert tenancy.current_org(db) is None
        tenancy.release_org_scope(db)
    finally:
        db.close()


def test_scoping_a_session_to_nothing_is_refused(app_client):
    """An empty scope reads as "this tenant has no data", which is
    indistinguishable from a working request against an empty account -- so it
    is the one failure nobody would ever report."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="needs an org_id"):
            tenancy.set_current_org(db, "")
        with pytest.raises(ValueError, match="needs an org_id"):
            tenancy.set_current_org(db, "   ")
    finally:
        db.close()


def test_a_background_job_takes_its_tenant_from_the_user_it_runs_for(app_client, two_orgs):
    """A worker session has nobody to ask. `users` is outside RLS precisely so
    this lookup can still happen, and it is the only way a batch started an hour
    ago can name the tenant whose documents it is about to write."""
    from sqlalchemy import select

    from app.models import Project, User

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        user = db.scalar(select(User).where(User.org_id == org_id))
        assert tenancy.adopt_org_of_user(db, user.id) == org_id
    finally:
        db.close()


def test_a_job_that_cannot_name_its_tenant_refuses_to_run(app_client):
    """Failing loudly rather than reading nothing. An unscoped worker on
    PostgreSQL finds no rows, and a batch runner that finds no rows reports
    success on a batch it never ran."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="no such user"):
            tenancy.adopt_org_of_user(db, "nobody")
    finally:
        db.close()


@pytest.mark.skipif(
    _on_postgres(), reason="asserts the SQLite no-op path; PostgreSQL runs the real thing"
)
def test_the_org_scope_context_manager_releases_on_the_way_out(app_client, two_orgs):
    """Connections are pooled. A scope that outlives its block is handed to the
    next piece of work on that connection, which is a cross-tenant read produced
    by the mechanism meant to prevent one."""
    from app.models import Project

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        with tenancy.org_scope(db, org_id) as scoped:
            assert scoped is db
        assert tenancy.current_org(db) is None
    finally:
        db.close()


def test_the_maintenance_bypass_is_never_opened_by_application_code():
    """The escape hatch belongs to migrations. If a router or a service ever
    sets it, the backstop is off for whatever that code path does next, and
    nothing about the request would look unusual."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "tenancy.py":
            continue  # where the constant and its context manager are defined
        if tenancy.MAINTENANCE_GUC in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(app_dir)))
    assert not offenders, (
        "application code that opens the RLS maintenance bypass: " + ", ".join(sorted(offenders))
    )


def test_a_token_naming_an_organisation_its_user_is_not_in_is_refused(app_client, two_orgs):
    """`users` sits outside RLS so that authentication can find the user at all,
    which means nothing in the database stops a token claiming the wrong tenant
    -- and the claim is what scopes every query the request then makes. This
    check is the only thing between a stale or forged claim and a session
    reading another organisation's rows under its own policy."""
    from sqlalchemy import select

    from app.models import Project, User
    from app.security import create_access_token

    _token_a, project_a, _token_b, project_b = two_orgs
    db = SessionLocal()
    try:
        org_a = db.get(Project, project_a).org_id
        org_b = db.get(Project, project_b).org_id
        user_a = db.scalar(select(User).where(User.org_id == org_a))
    finally:
        db.close()

    mismatched = create_access_token(user_a.id, org_b)
    refused = app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {mismatched}"})
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"]["error"]["code"] == "TOKEN_INVALID"


def test_a_token_with_no_organisation_claim_is_refused(app_client, two_orgs):
    """A session that cannot name a tenant cannot be scoped to one, and an
    unscoped session on PostgreSQL reads zero rows -- which would present to the
    user as an empty account rather than as a broken token."""
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from app.config import settings
    from app.db import SessionLocal as _Session
    from app.models import Project, User
    from sqlalchemy import select as _select

    _token_a, project_a, *_ = two_orgs
    db = _Session()
    try:
        org_a = db.get(Project, project_a).org_id
        user_a = db.scalar(_select(User).where(User.org_id == org_a))
    finally:
        db.close()

    orgless = jwt.encode(
        {"sub": user_a.id, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )
    refused = app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {orgless}"})
    assert refused.status_code == 401
    assert refused.json()["detail"]["error"]["code"] == "TOKEN_INVALID"


# ------------------------------------------------- does RLS actually apply?

class _FakeBind:
    def __init__(self, name):
        self.dialect = type("D", (), {"name": name})()


class _FakeSession:
    """Enough of a Session to answer the one question `role_bypasses_rls` asks.

    A stub rather than a real connection because the branch under test is the
    PostgreSQL one, and the suite's default backend is SQLite -- so without this
    the code that decides whether tenant isolation is real would only ever be
    exercised on the runs that happen to point at PostgreSQL.
    """

    def __init__(self, dialect="postgresql", answer=False):
        self._bind = _FakeBind(dialect)
        self._answer = answer
        self.statements = []

    def get_bind(self):
        return self._bind

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return type("R", (), {"scalar": lambda _self: self._answer})()


def test_a_role_that_can_bypass_rls_is_refused():
    """The defect this exists for: policies present, FORCE on, `pg_policies`
    listing them, and the tables not filtering at all because the connecting
    role is a superuser. Everything looks correctly configured."""
    session = _FakeSession(answer=True)

    assert tenancy.role_bypasses_rls(session) is True
    with pytest.raises(tenancy.RlsBypassed) as raised:
        tenancy.verify_rls_enforced(session)

    message = str(raised.value)
    assert "BYPASSRLS" in message
    assert "NOSUPERUSER" in message, "the refusal has to say what to do about it"


def test_a_restricted_role_passes_the_check():
    session = _FakeSession(answer=False)
    assert tenancy.role_bypasses_rls(session) is False
    tenancy.verify_rls_enforced(session)


def test_the_check_asks_postgres_about_the_current_user():
    """Not about a configured username: what matters is the role the connection
    actually authenticated as, which pooling and `SET ROLE` can both change."""
    session = _FakeSession(answer=False)
    tenancy.role_bypasses_rls(session)
    asked = " ".join(session.statements)
    assert "rolsuper" in asked and "rolbypassrls" in asked
    assert "current_user" in asked


def test_a_backend_without_the_concept_answers_none_rather_than_false():
    """None and False are different answers. "This database has no such thing"
    must not read as "checked, and the role is restricted"."""
    session = _FakeSession(dialect="sqlite")
    assert tenancy.role_bypasses_rls(session) is None
    tenancy.verify_rls_enforced(session)  # inert, not a refusal


def test_the_real_connection_is_checked_when_running_on_postgres(app_client):
    """When the suite is pointed at PostgreSQL, the application role must be one
    that row-level security actually applies to -- otherwise every other RLS
    test in this file is asserting against a database that ignores policies."""
    if not _on_postgres():
        pytest.skip("only meaningful against a real PostgreSQL")

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert tenancy.role_bypasses_rls(db) is False, (
            "the suite is connected as a role that bypasses RLS, so the policies "
            "under test here are not being enforced at all"
        )
        tenancy.verify_rls_enforced(db)
    finally:
        db.close()


def test_the_tenant_scope_survives_a_commit(app_client, two_orgs):
    """The bug this pins made every create fail in the UI.

    `set_current_org` sets a PostgreSQL session variable, which lives on the
    *connection*. A Session does not keep one: `commit()` returns it to the
    pool and the next statement checks out whichever is free. So a handler that
    inserted a row, committed, then refreshed it was reading through a
    connection that had never declared a tenant -- row-level security hid the
    row it had just written, and the create surfaced as "Could not refresh
    instance". Everything looked correct; the write had in fact succeeded.

    Nothing in the SQLite suite could see it: there are no policies there, so
    the unscoped read returns the row and the handler works perfectly.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Project, User

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "user-a@tenant.test"))
        tenancy.set_current_org(db, user.org_id)

        project = Project(
            org_id=user.org_id, display_id=93117, name="Commit probe",
            region="Europe", function="Human Resources", document_type="Offer Letter",
            language="English", status="pending", created_by=user.id,
        )
        db.add(project)
        db.commit()  # the connection goes back to the pool here

        # The read that used to come back empty.
        db.refresh(project)
        assert project.name == "Commit probe"

        reread = db.scalar(select(Project).where(Project.id == project.id))
        assert reread is not None, (
            "the row is invisible after commit: the tenant scope did not follow the "
            "session onto its next connection"
        )

        db.delete(project)
        db.commit()
    finally:
        db.close()


def test_the_session_carries_the_tenant_not_just_the_context(app_client, two_orgs):
    """The session is the source of truth, and it has to be.

    The first fix for the post-commit blindness kept the tenant in a ContextVar.
    That held for some endpoints and not others: FastAPI runs each dependency in
    a worker thread with a *copied* context, so a value set inside
    `get_current_user` is not reliably visible later in the request. Project
    creation worked; template upload -- which commits more than once on its way
    through -- did not.

    `Session.info` has exactly the right lifetime: created with the session,
    discarded with it, and handed to `after_begin` at the one moment the scope
    needs applying.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "user-a@tenant.test"))
        tenancy.set_current_org(db, user.org_id)
        assert db.info[tenancy.ORG_SESSION_KEY] == user.org_id

        # Survives a commit, which is where the connection can change underneath.
        db.commit()
        assert db.info[tenancy.ORG_SESSION_KEY] == user.org_id

        tenancy.release_org_scope(db)
        assert tenancy.ORG_SESSION_KEY not in db.info
    finally:
        db.close()


def test_two_sessions_do_not_share_a_tenant(app_client, two_orgs):
    """Two requests in flight at once must not read each other's scope -- which
    a single process-wide variable cannot guarantee."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User

    first, second = SessionLocal(), SessionLocal()
    try:
        users = {u.email: u for u in first.scalars(select(User)).all()}
        a = users.get("user-a@tenant.test")
        b = users.get("user-b@tenant.test")
        tenancy.set_current_org(first, a.org_id)
        tenancy.set_current_org(second, b.org_id)

        assert first.info[tenancy.ORG_SESSION_KEY] == a.org_id
        assert second.info[tenancy.ORG_SESSION_KEY] == b.org_id
        assert a.org_id != b.org_id
    finally:
        first.close()
        second.close()


def test_the_scope_follows_the_request_not_the_connection():
    """What makes the fix work: the tenant is held per request and re-applied at
    the start of every transaction, rather than set once on one socket."""
    tenancy._CURRENT_ORG.set(None)
    assert tenancy.scoped_org() is None

    class _Bind:
        dialect = type("D", (), {"name": "sqlite"})()

    class _Session:
        def get_bind(self):
            return _Bind()

        def execute(self, *_a, **_k):
            raise AssertionError("SQLite has no session setting to apply")

        # `release_org_scope` rolls back and commits before clearing, because on
        # PostgreSQL a `SET` is undone by a rollback. Both are no-ops here.
        def rollback(self):
            pass

        def commit(self):
            pass

    # Recorded even where there is nothing to apply it to, so a code path that
    # reads `scoped_org()` behaves the same on both backends.
    tenancy.set_current_org(_Session(), "org-request-scoped")
    assert tenancy.scoped_org() == "org-request-scoped"

    tenancy.release_org_scope(_Session())
    assert tenancy.scoped_org() is None
