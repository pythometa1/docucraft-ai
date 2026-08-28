"""Shared test fixtures.

Tests run against a throwaway SQLite database and a fake Redis, so they need
neither the developer's Postgres nor a running server. The env vars are set
before `app.config` is imported, because pydantic-settings reads them at class
definition time.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="documind-tests-")
# SQLite by default: the suite has to run on a laptop with nothing installed,
# and it is the only way most contributors will ever execute it.
#
# But SQLite cannot express row-level security, the pgvector column type, or
# PostgreSQL's transaction and locking behaviour, so a green suite here is not
# evidence about production. Setting TEST_DATABASE_URL runs the identical suite
# against a real PostgreSQL -- that is how the RLS-bypass defect was found, and
# it is what CI should do on the branch that ships.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", f"sqlite:///{_TMP}/test.db"
)
os.environ["STORAGE_DIR"] = f"{_TMP}/storage"
os.environ["JWT_SECRET"] = "test-secret"
# Every provider is deliberately left unconfigured. The suite must never reach a
# real model: model-backed paths are expected to refuse, and a test that quietly
# succeeds because it made a live billable call is not testing anything.
#
# All of these must be set, not just the key. pydantic-settings reads
# backend/.env, which is where a developer's working key lives -- os.environ
# takes precedence over that file, so setting them here is what actually
# isolates the run. Adding a provider without adding it here reintroduces the
# hole, which `test_no_provider_is_configured_during_tests` guards against.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["LLM_COMPILE_PROVIDER"] = ""


class _FakeRedis:
    """Enough of the redis client for auth revocation and the rate limiter.

    The rate-limit script is a Lua token bucket; tests only need it to allow
    every call, so `register_script` returns a callable that always grants.
    """

    def __init__(self):
        self._store: dict[str, str] = {}

    def setex(self, key, _ttl, value):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        # Download grants are single-use, and `redeem` deletes before serving.
        # Returning the real client's "how many keys went" makes a test that
        # asserts on the second redeem meaningful rather than incidental.
        return 1 if self._store.pop(key, None) is not None else 0

    def exists(self, key):
        return 1 if key in self._store else 0

    def ping(self):
        return True

    def register_script(self, _src):
        return lambda keys, args: 1


@pytest.fixture(autouse=True)
def _reset_tenant_scope():
    """Forget the request's tenant between tests.

    `set_current_org` records it in a ContextVar so the scope follows the
    request rather than whichever pooled connection happens to serve it. In the
    application `get_db` clears it on every teardown; a test that calls
    `set_current_org` directly has no such teardown, so the value leaks into the
    next test -- where the `after_begin` hook dutifully applies a stale tenant
    and row-level security blocks the seeding that test was doing.

    The symptom is the worst kind: every file passes alone and the suite fails
    when run together, differently depending on order.
    """
    from app import tenancy

    yield
    tenancy._CURRENT_ORG.set(None)


@pytest.fixture(scope="session", autouse=True)
def _seed_with_rls_bypass():
    """On PostgreSQL, let the suite's fixtures seed across tenants.

    Row-level security refuses an INSERT whose `org_id` does not match the
    session's tenant, which is exactly what it is for -- and exactly what stops
    a fixture from setting up two organisations before either has a request to
    scope a session with. The migration provides `app.rls_bypass` for the same
    reason a data migration needs it: seeding is not serving.

    This does mean the bulk of the suite is not, itself, evidence that RLS
    isolates anything. That evidence belongs in `tests/test_rls.py`, which turns
    the bypass off and proves the policies bite. What running the rest of the
    suite on PostgreSQL buys is the other half: real SQL dialect, real JSON
    operators, real transaction and locking behaviour, and the pgvector column
    compiled for real rather than falling back to JSON.

    Inert on SQLite, which has neither the setting nor the policies.
    """
    from sqlalchemy import event, text

    from app.db import engine

    if engine.dialect.name != "postgresql":
        yield
        return

    # Per transaction, not per connection. A connect-time hook misses any
    # connection already in the pool, and a Session moves between connections
    # across commits -- the same reason the tenant scope itself is applied in
    # `after_begin`. Applying both the same way is what keeps seeding working
    # regardless of which connection a fixture happens to land on.
    from app.db import SessionLocal

    @event.listens_for(SessionLocal, "after_begin")
    def _bypass_on_begin(_session, _transaction, connection):
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET app.rls_bypass = 'on'")

    @event.listens_for(engine, "connect")
    def _bypass_on_connect(dbapi_connection, _record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET app.rls_bypass = 'on'")

    engine.dispose()
    yield
    event.remove(SessionLocal, "after_begin", _bypass_on_begin)
    event.remove(engine, "connect", _bypass_on_connect)


@pytest.fixture(scope="session", autouse=True)
def _patch_redis():
    import app.redis_client as rc

    fake = _FakeRedis()
    rc.redis_client = fake
    # security.py and rate_limit.py bind the client at import time
    import app.rate_limit as rl
    import app.security as sec

    sec.redis_client = fake
    rl.redis_client = fake
    rl._script = fake.register_script("")
    yield


def _create_schema() -> None:
    """Build the test schema the way production builds it: through Alembic.

    `Base.metadata.create_all()` builds the schema the *models* describe, which
    is not necessarily the schema the migrations produce. Running the migrations
    means a broken or missing revision fails the suite instead of being
    discovered on a deploy, and it costs nothing -- the whole chain runs in well
    under a second on SQLite.
    """
    from alembic import command
    from alembic.config import Config

    # On SQLite every run gets a fresh temp file, so the suite is isolated for
    # free. A PostgreSQL test database persists between runs, and the leftovers
    # are not inert: unique constraints on emails and display ids start failing
    # on the second run, which reads as a broken test rather than as yesterday's
    # data. Dropping the schema first makes the two backends behave the same
    # way, which is the whole point of being able to run the suite on either.
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql"):
        from sqlalchemy import create_engine, text

        # Tables, not the schema. `DROP SCHEMA public CASCADE` would take the
        # pgvector extension with it, and re-creating an extension needs
        # superuser -- which the application role deliberately is not.
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text("""
                DO $$
                DECLARE r record;
                BEGIN
                  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                  LOOP
                    EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
                  END LOOP;
                END $$;
            """))
        admin.dispose()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def app_client(_patch_redis):
    from fastapi.testclient import TestClient

    from app.main import app

    _create_schema()
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def two_orgs(_patch_redis, app_client):
    """Two organisations with one user each -- the fixture tenancy tests need.

    Returns (token_a, org_a_project_id, token_b, org_b_project_id).
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Organization, Project, User
    from app.security import create_access_token, hash_password

    db = SessionLocal()
    made = {}
    try:
        for tag in ("a", "b"):
            org = db.scalar(select(Organization).where(Organization.name == f"TenantOrg{tag}"))
            if org is None:
                org = Organization(name=f"TenantOrg{tag}")
                db.add(org)
                db.flush()
                user = User(
                    org_id=org.id, email=f"user-{tag}@tenant.test", full_name=f"User {tag}",
                    password_hash=hash_password("pw"), role_key="org_admin",
                )
                db.add(user)
                db.flush()
                project = Project(
                    org_id=org.id, display_id=90000 + ord(tag), name=f"Project {tag}",
                    region="Europe", function="Human Resources", document_type="Offer Letter",
                    language="English", status="pending", created_by=user.id,
                )
                db.add(project)
                db.flush()
            else:
                user = db.scalar(select(User).where(User.org_id == org.id))
                project = db.scalar(select(Project).where(Project.org_id == org.id))
            made[tag] = (create_access_token(user.id, org.id), project.id)
        db.commit()
    finally:
        db.close()
    return made["a"][0], made["a"][1], made["b"][0], made["b"][1]


# ----------------------------------------------------------------- goldens
def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Regenerate golden .docx files instead of asserting against them. "
             "A golden change in a PR without an explanation is a red flag.",
    )


@pytest.fixture(scope="session")
def update_goldens(pytestconfig) -> bool:
    return bool(pytestconfig.getoption("--update-goldens"))


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def goldens_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "goldens"


# ---------------------------------------------------------------- customer fixtures
#
# Some golden fixtures are real client masters -- Hospira/Pfizer offer and ICC
# contract templates. They are excluded from the published repository because it
# is public and the documents are the customer's, not ours. They stay on the
# machines that have them, where the full suite runs unchanged.
#
# On a clone without them the tests that read them skip with a reason, rather
# than failing as though the code were broken. Skipping at module level is
# coarser than skipping per test, and deliberately so: the alternative is a
# guard at every one of the forty-odd call sites, which is more code to get
# wrong than the coverage it buys back on a machine that cannot run them anyway.
CUSTOMER_FIXTURES = (
    "templates/hospira_offer.docx",
    "templates/icc_ct036_template.docx",
    "templates/icc_ct040_template.docx",
)

CUSTOMER_FIXTURE_MODULES = frozenset({
    "test_binding_confidence",
    "test_compiler_snapshots",
    "test_conditions",
    "test_conventions_and_safety",
    "test_fill_goldens",
    "test_golden_hospira",
    "test_icc_templates",
    "test_metrics",
    "test_reproducibility",
    # Both assert against a client master by name: the widened instruction
    # vocabulary was measured on one, and the orphan gate's true-negative case
    # is the Hospira signature block.
    "test_instruction_shape_detection",
    "test_orphaned_fields",
})


def _missing_customer_fixtures() -> list[str]:
    root = Path(__file__).parent / "fixtures"
    return [name for name in CUSTOMER_FIXTURES if not (root / name).exists()]


def pytest_collection_modifyitems(config, items):
    missing = _missing_customer_fixtures()
    if not missing:
        return
    skip = pytest.mark.skip(
        reason=(
            "needs a client-owned template fixture that is not published with this "
            f"repository ({', '.join(missing)}). The suite runs in full where those files are present."
        )
    )
    for item in items:
        if item.module.__name__.rsplit(".", 1)[-1] in CUSTOMER_FIXTURE_MODULES:
            item.add_marker(skip)
