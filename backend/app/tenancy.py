"""Which organisation a database session is speaking for, and what that tenant
requires of the model provider.

Two §16 controls that both need the same fact -- who this request is for -- and
had nowhere to keep it.

**Row-level security needs a session to declare its tenant.** Migration
e5b26f0d71a4 puts a policy on every table carrying `org_id`, keyed on the
PostgreSQL session setting `app.current_org`. A policy is only as good as the
thing that sets that setting, and nothing did. This module is that thing:
`set_current_org` on the way in, `release_org_scope` on the way out, wired into
`app/db.py`'s session lifecycle so no handler has to remember it.

The direction of failure is the point. `current_setting('app.current_org', true)`
returns NULL when unset, `org_id = NULL` is NULL, and NULL is not TRUE -- so a
session that never declared a tenant reads zero rows. §16 asks for a backstop
where "a forgotten WHERE clause fails closed instead of leaking", and this is
the half of it that lives in Python.

Clearing is not tidiness. Connections are pooled, and a session-level setting
outlives the request that set it. Without `release_org_scope`, the next request
to be handed that connection would inherit the previous tenant's scope -- which
is a cross-tenant read produced by the mechanism meant to prevent one. And
because PostgreSQL reverts a `SET` when its transaction rolls back, the clear
has to be committed rather than merely issued, or closing the session would put
the old tenant straight back.

On SQLite every one of these is an inert no-op that returns False. The test
suite runs on SQLite, so the alternative -- raising, or emitting Postgres
syntax -- would take the suite down for a control SQLite cannot express.

**The LLM boundary needs to know what the tenant was promised.** §16's boundary
table asks for residency ("EU, UK and India customer data pinned to in-region
model deployments; residency recorded per organisation") and zero retention
("provider configured for no training and no retention, confirmed contractually
rather than assumed from a settings page"). `LLMDataPolicy` is what an
organisation requires; `ProviderBoundary` is what the configured deployment can
actually offer; `enforce_llm_policy` refuses when the first exceeds the second.
`app/llm/boundary.py` calls it before a prompt exists, because a residency
breach that is discovered after the request has left is not a breach that can be
undone.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field as dc_field

from sqlalchemy import text

from app.config import settings

# ------------------------------------------------------------ session settings

#: The PostgreSQL session setting the RLS policies read. Must stay in step with
#: the migration's `ORG_GUC`; `tests/test_rls.py` asserts that it does.
ORG_GUC = "app.current_org"

#: The maintenance escape hatch, set only by data migrations that legitimately
#: need to touch every tenant's rows. Never set on a request path -- and
#: `tests/test_rls.py` pins that too.
MAINTENANCE_GUC = "app.rls_bypass"

#: What "no tenant" looks like on the wire. `set_config` cannot store NULL, and
#: an empty string matches no organisation id, so clearing to '' fails closed in
#: exactly the same way an unset GUC does.
NO_ORG = ""


def _dialect_name(target) -> str:
    """The dialect behind a Session or a Connection."""
    bind = target.get_bind() if hasattr(target, "get_bind") else target
    return bind.dialect.name


def is_postgres(target) -> bool:
    return _dialect_name(target) == "postgresql"


class RlsBypassed(RuntimeError):
    """The database role this process connects as can ignore row-level security."""


def role_bypasses_rls(session) -> bool | None:
    """Whether this connection's role can see through every policy.

    None on a database that has no such concept (SQLite), so the caller can tell
    "cannot bypass" apart from "cannot be asked".

    This is the question that decides whether §16's backstop is a control or an
    ornament. A superuser, or any role with BYPASSRLS, reads every tenant's rows
    no matter how many policies exist -- and everything else about the setup
    looks identical: the policies are there, FORCE is on, `pg_policies` lists
    them. The tables simply do not filter. It is invisible to a test suite on
    SQLite, and invisible to an operator who checks that the policies exist,
    which is exactly how a deployment ends up with tenant isolation that has
    never once worked.
    """
    if not is_postgres(session):
        return None
    row = session.execute(text(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )).scalar()
    return bool(row)


def verify_rls_enforced(session) -> None:
    """Raise unless row-level security actually applies to this connection.

    Called at startup in production. Refusing to serve is right: the alternative
    is a system that reports itself correctly configured, passes every policy
    check an auditor runs, and returns one customer's letters to another.
    """
    if role_bypasses_rls(session):
        raise RlsBypassed(
            "this process connects to PostgreSQL as a role with SUPERUSER or BYPASSRLS, so "
            "every row-level security policy is ignored and tenant isolation depends entirely "
            "on each handler remembering its WHERE clause. Connect as a dedicated application "
            "role created with NOSUPERUSER NOBYPASSRLS."
        )


#: Key under which a Session records the tenant it speaks for.
#:
#: `Session.info` is the authoritative home: it is created with the session, it
#: lives exactly as long as the request, and SQLAlchemy hands the session to the
#: `after_begin` hook -- so the scope is readable at the one moment it needs to
#: be applied, on whatever connection the session has just picked up.
#:
#: The ContextVar below mirrors it for callers that have no session to hand, but
#: it cannot be the source of truth: FastAPI runs each dependency in a worker
#: thread with a *copied* context, so a value set inside `get_current_user` is
#: not necessarily visible to the endpoint that follows it.
ORG_SESSION_KEY = "documind_org_id"

#: The tenant this request speaks for, independent of which pooled connection
#: happens to be serving it at any moment.
#:
#: A ContextVar rather than a Session attribute because the two have different
#: lifetimes, and the difference is a live bug: `Session.commit()` returns its
#: connection to the pool, so the very next statement -- a `refresh()` after a
#: create, say -- can run on a *different* connection that has never had
#: `app.current_org` set. Row-level security then hides the row that was just
#: written, and the write appears to have failed. Keeping the tenant here and
#: re-applying it at every transaction start is what makes the scope follow the
#: request instead of the socket.
_CURRENT_ORG: ContextVar[str | None] = ContextVar("documind_current_org", default=None)


def scoped_org() -> str | None:
    """The tenant this request declared, if any."""
    return _CURRENT_ORG.get()


def apply_scope_to_session(session, connection) -> None:
    """Re-assert the session's tenant on the connection it has just begun on.

    Reads `Session.info` rather than the ContextVar, because the session is the
    thing whose lifetime actually matches the request.
    """
    org_id = session.info.get(ORG_SESSION_KEY) or _CURRENT_ORG.get()
    if not org_id or connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(
        f"SELECT set_config('{ORG_GUC}', %s, false)", (str(org_id),)
    )


def apply_scope_to_connection(connection) -> None:
    """Re-assert the request's tenant on whatever connection is in play.

    Wired to SQLAlchemy's `after_begin`, so it runs at the start of every
    transaction on every connection the session touches -- including the fresh
    one it picks up after a commit.

    Takes the Connection the event hands over, not the Session. Going through
    the Session here would ask it to begin a transaction from inside the hook
    that fires when a transaction begins, and SQLAlchemy refuses that
    re-entrancy outright ("this session is provisioning a new connection").
    """
    org_id = _CURRENT_ORG.get()
    if not org_id or connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(
        f"SELECT set_config('{ORG_GUC}', %s, false)", (str(org_id),)
    )


def set_current_org(session, org_id: str) -> bool:
    """Tell this session which tenant it speaks for. True if it was applied.

    Refuses an empty organisation rather than setting an empty scope. Silently
    scoping a session to nothing would read as "this tenant has no data", which
    is indistinguishable from a working request against an empty account and is
    therefore the one failure that would not get reported.
    """
    if not org_id or not str(org_id).strip():
        raise ValueError(
            "set_current_org needs an org_id: a session scoped to nothing reads as an "
            "empty tenant rather than as the mistake it is."
        )
    # Recorded before the dialect check so the scope is known even on SQLite,
    # where there is nothing to apply it to -- otherwise a code path that reads
    # `scoped_org()` would behave differently on the two backends.
    _CURRENT_ORG.set(str(org_id))
    # The session is the source of truth; the ContextVar is a convenience for
    # callers that have no session in hand.
    try:
        session.info[ORG_SESSION_KEY] = str(org_id)
    except AttributeError:  # a bind or connection rather than a Session
        pass
    if not is_postgres(session):
        return False
    # The setting name is a module constant, never caller input; the value is
    # bound, because it is.
    session.execute(
        text(f"SELECT set_config('{ORG_GUC}', :org, false)"), {"org": str(org_id)}
    )
    return True


def current_org(session) -> str | None:
    """Which tenant this session is scoped to, or None."""
    if not is_postgres(session):
        return None
    value = session.execute(text(f"SELECT current_setting('{ORG_GUC}', true)")).scalar()
    return value or None


def clear_current_org(session) -> bool:
    """Forget the tenant. True if a statement was actually issued."""
    if not is_postgres(session):
        return False
    session.execute(text(f"SELECT set_config('{ORG_GUC}', :none, false)"), {"none": NO_ORG})
    return True


def release_org_scope(session) -> None:
    _CURRENT_ORG.set(None)
    try:
        session.info.pop(ORG_SESSION_KEY, None)
    except AttributeError:
        pass
    """Session teardown: leave nothing behind for the next request on this connection.

    The rollback first, because a session handed back mid-transaction cannot
    execute anything and because none of the request's own work is pending by
    the time this runs -- handlers commit their own writes. The commit after,
    because PostgreSQL undoes a `SET` when its transaction rolls back, and
    `close()` rolls back: without it the clear would be reverted and the pooled
    connection would go back to the pool still carrying the last tenant.
    """
    session.rollback()
    if clear_current_org(session):
        session.commit()


@contextmanager
def org_scope(session, org_id: str):
    """Run a block with the session scoped to one tenant, then release it.

    For the paths that own their session rather than receiving one from the
    request lifecycle -- background workers, one-off scripts.
    """
    set_current_org(session, org_id)
    try:
        yield session
    finally:
        release_org_scope(session)


def adopt_org_of_user(session, user_id: str) -> str:
    """Scope a session to the organisation of the user it is acting for.

    Background jobs are handed a `user_id` and nothing else, and the job row
    they need to read is itself behind the policy -- so the tenant has to come
    from `users`, which is the one org-bearing table deliberately left outside
    RLS (see the migration's `RLS_EXEMPT`).
    """
    from app.models import User  # local: app.db imports this module

    user = session.get(User, user_id)
    if user is None:
        raise ValueError(
            f"cannot scope a session to user {user_id!r}: no such user. A background job "
            "that cannot name its tenant would read nothing and report success."
        )
    set_current_org(session, user.org_id)
    return user.org_id


@contextmanager
def maintenance_bypass(connection):
    """Let a data migration touch every tenant's rows, visibly.

    RLS is FORCEd, which applies to migrations as well as requests, so a
    backfill without this updates zero rows and looks like it worked. The
    bypass is transaction-local (`SET LOCAL`) so it cannot outlive the statement
    it was opened for, and it is one grep away from being audited.
    """
    if _dialect_name(connection) != "postgresql":
        yield connection
        return
    connection.execute(text(f"SET LOCAL {MAINTENANCE_GUC} = 'on'"))
    try:
        yield connection
    finally:
        connection.execute(text(f"SET LOCAL {MAINTENANCE_GUC} = 'off'"))


# ------------------------------------------------------- the model-provider boundary

#: No residency constraint recorded. Not the absence of a policy -- an explicit
#: statement that this tenant's data may be processed anywhere the deployment
#: runs, which is what most customers outside the named regions actually want.
GLOBAL = "GLOBAL"

#: §16 names EU, UK and India. They are listed separately and treated
#: separately: the UK is not in the EU, and an EU deployment does not satisfy a
#: UK residency requirement just because the map looks close.
RESIDENCIES = frozenset({GLOBAL, "EU", "UK", "IN"})


class ResidencyViolation(RuntimeError):
    """Raised when a prompt would leave the region the tenant was promised."""


@dataclass(frozen=True)
class LLMDataPolicy:
    """What an organisation requires of whatever model processes its data."""

    residency: str = GLOBAL
    zero_retention_required: bool = False
    #: Where to record what this tenant's model calls cost, or None to record
    #: nothing. Carried here rather than passed alongside because eight of the
    #: thirteen call sites that reach a model live inside the compiler, which has
    #: no session and no request by design -- and this policy is the only object
    #: that already travels all the way down to them.
    #:
    #: `compare=False` on purpose: equality is about what the tenant *requires*,
    #: and a meter is not one of their requirements. Two policies with the same
    #: residency are the same policy.
    meter: object | None = dc_field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.residency not in RESIDENCIES:
            raise ValueError(
                f"residency={self.residency!r} is not one of {sorted(RESIDENCIES)}. "
                "An unrecognised region cannot be checked against a deployment, and a "
                "requirement that cannot be checked is not a requirement."
            )


@dataclass(frozen=True)
class ProviderBoundary:
    """What the configured deployment can actually offer.

    `zero_retention` is an assertion about a signed agreement, which is why §16
    words it as "confirmed contractually rather than assumed from a settings
    page" and why the default in `app/config.py` is False. Claiming it costs
    one line of configuration; the claim being wrong costs a customer.
    """

    provider: str
    residency: str = GLOBAL
    zero_retention: bool = False

    def __post_init__(self) -> None:
        if self.residency not in RESIDENCIES:
            raise ValueError(
                f"llm_residency={self.residency!r} is not one of {sorted(RESIDENCIES)}"
            )


def configured_provider_boundary() -> ProviderBoundary:
    """The deployment the current configuration actually points at."""
    return ProviderBoundary(
        provider=settings.llm_provider,
        residency=(settings.llm_residency or GLOBAL).strip().upper(),
        zero_retention=bool(settings.llm_zero_retention),
    )


def satisfies_residency(*, required: str, offered: str) -> bool:
    """Whether a deployment in `offered` may process data pinned to `required`.

    Exact match, with GLOBAL as the only wildcard and only on the requiring
    side. There is deliberately no notion of one region being "close enough" to
    another: the failure this prevents is somebody deciding at three in the
    morning that Ireland covers a UK contract.
    """
    if required == GLOBAL:
        return True
    return required == offered


def enforce_llm_policy(policy: LLMDataPolicy, boundary: ProviderBoundary) -> None:
    """Refuse before a prompt exists. Raises `ResidencyViolation`.

    Both halves fail closed, and both name the two values involved -- a refusal
    that says "residency violation" gets escalated to whoever configured the
    provider and stalls there; one that says "org requires EU, anthropic
    deployment is GLOBAL" gets fixed.
    """
    if not satisfies_residency(required=policy.residency, offered=boundary.residency):
        raise ResidencyViolation(
            f"this organisation's data is pinned to {policy.residency}, and the configured "
            f"{boundary.provider} deployment is {boundary.residency}. §16 requires an "
            "in-region model deployment; refusing to build the prompt."
        )
    if policy.zero_retention_required and not boundary.zero_retention:
        raise ResidencyViolation(
            f"this organisation requires zero retention, and the configured "
            f"{boundary.provider} deployment is not recorded as zero-retention. §16 wants "
            "that confirmed contractually rather than assumed; refusing to build the prompt."
        )


def llm_policy_for(
    db,
    org_id: str,
    *,
    project_id: str | None = None,
    user_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    meter: bool = True,
) -> LLMDataPolicy:
    """The recorded requirements for one organisation, and where to bill it.

    No row means no recorded constraint, which is a real state rather than a
    silent default: a tenant that has never asked for regional pinning has not
    been promised it, and GLOBAL is what we would truthfully tell them.

    The optional attribution arguments name what the model is being asked about
    -- a template, a blueprint, a conversation -- so a cost can later be
    attributed to the thing that incurred it rather than only to the tenant.
    Every caller already has these values in scope; passing them is what makes
    "which template cost the most" answerable.

    `meter=False` is for a caller with a session it does not intend to commit.
    """
    from app.llm.metering import UsageMeter  # local: same cycle as below
    from app.models import OrgDataPolicy  # local: app.db imports this module

    row = db.query(OrgDataPolicy).filter(OrgDataPolicy.org_id == org_id).one_or_none()
    sink = UsageMeter(
        db=db, org_id=org_id, project_id=project_id, user_id=user_id,
        subject_type=subject_type, subject_id=subject_id,
    ) if meter else None

    if row is None:
        return LLMDataPolicy(meter=sink)
    return LLMDataPolicy(
        residency=(row.residency or GLOBAL).strip().upper(),
        zero_retention_required=bool(row.zero_retention_required),
        meter=sink,
    )
