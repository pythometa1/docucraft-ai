"""A production process must not start with development settings.

Every check here guards a failure that is silent while it is happening and
unrecoverable once noticed. A signing key that sits in a public git history
mints valid tokens for anybody who reads it, and nothing in the logs looks
unusual. A wildcard CORS origin lets any page a signed-in user visits call this
API with their session. SQLite under a multi-worker deployment loses writes
under concurrency rather than raising.

So the posture is refuse-to-boot rather than warn. A warning in a log nobody
reads is not a control, and each of these is a one-line fix at deploy time --
the cost of being strict is small and lands on the right person at the right
moment.
"""

import pytest

from app.config import (
    DEV_JWT_SECRET, PRODUCTION_ENVS, InsecureConfiguration, Settings,
    verify_production_config,
)


def _config(**over) -> Settings:
    """A configuration that would be safe in production, minus whatever is overridden."""
    base = dict(
        env="production",
        jwt_secret="d3f2a1c09b8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928170",
        cors_origins=["https://app.documind.example"],
        database_url="postgresql+psycopg://documind:secret@db:5432/documind",
    )
    base.update(over)
    return Settings(**base)


def test_a_correctly_configured_production_process_starts():
    """The guard has to let real deployments through, or it gets disabled."""
    verify_production_config(_config())


@pytest.mark.parametrize("env", sorted(PRODUCTION_ENVS))
def test_every_production_environment_is_checked(env):
    """Staging counts. It holds real customer data often enough that the weaker
    posture is not worth the convenience."""
    with pytest.raises(InsecureConfiguration):
        verify_production_config(_config(env=env, jwt_secret=DEV_JWT_SECRET))


@pytest.mark.parametrize("env", ["development", "dev", "test", "local", ""])
def test_development_is_left_alone(env):
    """A developer must still be able to run the thing with defaults."""
    verify_production_config(_config(
        env=env, jwt_secret=DEV_JWT_SECRET, cors_origins=["*"],
        database_url="sqlite:///documind.db",
    ))


def test_the_development_signing_key_cannot_reach_production():
    with pytest.raises(InsecureConfiguration) as raised:
        verify_production_config(_config(jwt_secret=DEV_JWT_SECRET))
    assert "JWT_SECRET" in str(raised.value)
    assert "mint a valid token" in str(raised.value)


def test_a_short_signing_key_is_refused():
    """HS256 keys shorter than 32 characters are brute-forcible offline, and an
    attacker needs no access to this system to try."""
    with pytest.raises(InsecureConfiguration, match="32"):
        verify_production_config(_config(jwt_secret="short-but-not-the-default"))


def test_a_wildcard_cors_origin_is_refused():
    with pytest.raises(InsecureConfiguration) as raised:
        verify_production_config(_config(cors_origins=["*"]))
    assert "any origin" in str(raised.value)


def test_a_wildcard_hidden_among_real_origins_is_still_refused():
    """One entry is enough to open it; the list looking careful does not help."""
    with pytest.raises(InsecureConfiguration):
        verify_production_config(_config(
            cors_origins=["https://app.documind.example", "*"],
        ))


def test_sqlite_is_refused_in_production():
    """Row-level security, the pgvector column and the concurrency this service
    assumes all need PostgreSQL. On SQLite the RLS policies simply do not exist,
    so every tenant guard falls back to the handler remembering."""
    with pytest.raises(InsecureConfiguration, match="SQLite"):
        verify_production_config(_config(database_url="sqlite:///documind.db"))


def test_every_failure_is_reported_not_just_the_first():
    """A deploy that fixes one problem and fails again on the next wastes a
    cycle each time; the operator should see the whole list at once."""
    with pytest.raises(InsecureConfiguration) as raised:
        verify_production_config(_config(
            jwt_secret=DEV_JWT_SECRET, cors_origins=["*"],
            database_url="sqlite:///documind.db",
        ))

    message = str(raised.value)
    assert "JWT_SECRET" in message
    assert "CORS_ORIGINS" in message
    assert "SQLite" in message


def test_the_refusal_says_what_to_do_about_it():
    """A refusal that only says "insecure configuration" gets escalated and
    stalls; one that names the environment variable gets fixed."""
    with pytest.raises(InsecureConfiguration) as raised:
        verify_production_config(_config(jwt_secret="short"))
    assert "openssl rand" in str(raised.value)
