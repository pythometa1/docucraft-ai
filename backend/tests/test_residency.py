"""Residency and zero retention at the LLM boundary.

§16's boundary table asks for two things this build recorded nowhere:

    Zero retention | Provider configured for no training and no retention,
                     confirmed contractually rather than assumed from a
                     settings page.
    Residency      | EU, UK and India customer data pinned to in-region model
                     deployments; residency recorded per organisation.

The failure both prevent is the same one and it is unrecoverable. A prompt built
for a customer whose contract pins their employee data to the EU, sent to a
deployment in another region, cannot be recalled -- there is no version of that
incident where the response arriving quickly was worth anything. So the refusal
has to happen before the prompt exists, which is why it lives in
`prepare_context` next to redaction rather than in the router that calls it.
"""

from __future__ import annotations

import pytest

from app import tenancy
from app.llm.boundary import ResidencyViolation, prepare_context

EU_ONLY = tenancy.LLMDataPolicy(residency="EU")
UK_ONLY = tenancy.LLMDataPolicy(residency="UK")
GLOBAL_DEPLOYMENT = tenancy.ProviderBoundary(provider="anthropic", residency="GLOBAL")
EU_DEPLOYMENT = tenancy.ProviderBoundary(provider="anthropic", residency="EU")


# ------------------------------------------------------------- the vocabulary


def test_a_region_the_boundary_cannot_check_is_refused_when_it_is_declared():
    """An unrecognised region is a requirement that will silently never be
    enforced. Refusing at construction means the mistake surfaces where somebody
    typed it, not months later in an audit."""
    with pytest.raises(ValueError, match="not one of"):
        tenancy.LLMDataPolicy(residency="Ireland")
    with pytest.raises(ValueError, match="not one of"):
        tenancy.ProviderBoundary(provider="anthropic", residency="eu-west-1")


def test_the_uk_is_not_the_eu():
    """§16 lists EU, UK and India separately, and they are separate. Deciding at
    three in the morning that an Irish deployment covers a UK contract is
    exactly the judgement this rule exists to remove."""
    assert tenancy.satisfies_residency(required="UK", offered="EU") is False
    assert tenancy.satisfies_residency(required="EU", offered="UK") is False
    assert tenancy.satisfies_residency(required="UK", offered="UK") is True


def test_an_organisation_with_no_recorded_requirement_is_served_by_any_deployment():
    """GLOBAL is a recorded answer rather than a missing one. Most customers
    outside the named regions have no pinning requirement, and treating that as
    a violation would refuse every prompt in the product."""
    assert tenancy.satisfies_residency(required="GLOBAL", offered="EU") is True
    assert tenancy.satisfies_residency(required="GLOBAL", offered="GLOBAL") is True


# --------------------------------------------------------------- the refusal


def test_the_boundary_refuses_to_build_a_prompt_that_would_leave_the_region():
    """The §16 control, in one call. Not a warning, not a fallback model -- the
    prompt is never assembled."""
    with pytest.raises(ResidencyViolation) as raised:
        prepare_context(
            org_id="org-a", model="claude-sonnet-5",
            template_context="You will report to <New Reporting To>.",
            policy=EU_ONLY, provider_boundary=GLOBAL_DEPLOYMENT,
        )
    message = str(raised.value)
    assert "EU" in message and "GLOBAL" in message, (
        "a refusal that does not name both regions gets escalated and stalls; one that "
        f"does gets fixed. Got: {message}"
    )


def test_an_in_region_deployment_is_allowed_through():
    """The control has to permit the compliant case, or it is not a control, it
    is an outage."""
    prepared = prepare_context(
        org_id="org-a", model="claude-sonnet-5",
        template_context="You will report to <New Reporting To>.",
        policy=EU_ONLY, provider_boundary=EU_DEPLOYMENT,
    )
    assert prepared.record.residency == "EU"


def test_zero_retention_is_refused_when_the_deployment_cannot_evidence_it():
    """§16 wants zero retention "confirmed contractually rather than assumed
    from a settings page", so the configuration flag defaults to False and an
    organisation promised zero retention gets a refusal rather than a prompt."""
    policy = tenancy.LLMDataPolicy(zero_retention_required=True)
    unconfirmed = tenancy.ProviderBoundary(provider="anthropic", zero_retention=False)
    with pytest.raises(ResidencyViolation, match="zero retention"):
        prepare_context(
            org_id="org-a", model="claude-sonnet-5", policy=policy,
            provider_boundary=unconfirmed,
        )

    confirmed = tenancy.ProviderBoundary(provider="anthropic", zero_retention=True)
    prepared = prepare_context(
        org_id="org-a", model="claude-sonnet-5", policy=policy, provider_boundary=confirmed,
    )
    assert prepared.record.zero_retention is True


def test_the_check_runs_before_any_source_chunk_is_processed():
    """Ordering matters. A prompt built and then discarded has already had the
    tenant's context assembled in memory; the honest failure is to never
    construct it."""
    with pytest.raises(ResidencyViolation):
        prepare_context(
            org_id="org-a", model="claude-sonnet-5",
            # Deliberately unredactable: if the residency check ran second, this
            # would raise UnredactedValueError instead and the test would fail
            # on the wrong exception.
            context_chunks=[{"id": "c1", "text": "annual_salary: 118400"}],
            allow_redaction=False,
            policy=UK_ONLY, provider_boundary=EU_DEPLOYMENT,
        )


# ------------------------------------------------------------- what is recorded


def test_the_prompt_record_says_where_the_prompt_actually_went():
    """§16 asks for residency "recorded per organisation". Recording it only on
    the organisation says what was promised; the audit question is where the
    data went, and those two agree only until somebody edits the provider
    configuration."""
    prepared = prepare_context(
        org_id="org-a", model="claude-opus-5",
        policy=EU_ONLY, provider_boundary=EU_DEPLOYMENT,
    )
    recorded = prepared.record.as_dict()
    assert recorded["residency"] == "EU"
    assert recorded["zero_retention"] is False


def test_the_configured_deployment_is_read_from_settings():
    """The default configuration claims the weakest thing that is true: no
    region pinning, and no contractual zero retention. Anything stronger is a
    line somebody has to write down."""
    boundary = tenancy.configured_provider_boundary()
    assert boundary.residency == "GLOBAL"
    assert boundary.zero_retention is False


# ---------------------------------------------------- the per-organisation record


def test_an_organisation_policy_is_read_back_from_the_row_that_recorded_it(app_client, two_orgs):
    """The residency has to live with the tenant, not in a provider console. A
    requirement recorded somewhere the boundary cannot read is a requirement the
    boundary cannot enforce."""
    from app import retention
    from app.db import SessionLocal
    from app.models import Project

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        org_id = db.get(Project, project_a).org_id
        retention.set_policy(db, org_id, residency="IN", zero_retention_required=True)
        db.commit()

        policy = tenancy.llm_policy_for(db, org_id)
        assert policy.residency == "IN"
        assert policy.zero_retention_required is True

        # And the boundary then refuses on it, without anybody restating the
        # requirement at the call site.
        with pytest.raises(ResidencyViolation):
            prepare_context(org_id=org_id, model="claude-sonnet-5", policy=policy)
    finally:
        retention.set_policy(db, org_id, residency="GLOBAL", zero_retention_required=False)
        db.commit()
        db.close()


def test_an_organisation_that_has_recorded_nothing_reads_as_unconstrained(app_client, two_orgs):
    """Absence of a row is a real state -- a tenant that never asked for
    regional pinning has not been promised it, and GLOBAL is what we would
    truthfully tell them."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        policy = tenancy.llm_policy_for(db, "an-organisation-with-no-policy-row")
        assert policy == tenancy.LLMDataPolicy()
        assert policy.residency == "GLOBAL"
    finally:
        db.close()


def test_the_chat_path_resolves_the_policy_rather_than_assuming_one():
    """The one wired call site. A boundary that only refuses when the caller
    remembers to hand it a policy is a boundary that will be bypassed by the
    next caller."""
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "app" / "routers" / "chat.py").read_text(encoding="utf-8")
    assert "llm_policy_for(db, conv.org_id)" in source, (
        "app/routers/chat.py must resolve the organisation's recorded residency and pass it "
        "to prepare_context"
    )


# ------------------------------------------- the check reaches every call site

def test_asking_for_a_model_without_naming_a_tenant_is_refused():
    """The hole this closes: residency was enforced inside
    `llm.boundary.prepare_context`, whose only caller is the chat route. Five of
    the six paths that reach a model -- including every compile path -- never
    went near it, so an EU-pinned organisation's template could be compiled
    against a global deployment and nothing said so.

    `get_llm_provider` is the only way to obtain a provider, so requiring the
    policy there makes the check unskippable rather than remembered.
    """
    from app.llm.provider import ResidencyUnscoped, get_llm_provider

    with pytest.raises(ResidencyUnscoped) as raised:
        get_llm_provider("Manifest refinement")

    message = str(raised.value)
    assert "Manifest refinement" in message, "the refusal must name the caller that has to be fixed"
    assert "allow_unscoped" in message, "and how to declare a genuinely tenant-less call"


def test_a_tenantless_call_must_say_so_out_loud():
    """`allow_unscoped=True` is the escape hatch, and it is deliberately a
    visible token in the diff rather than a default."""
    from app.llm.provider import LLMNotConfiguredError, get_llm_provider

    # No provider is configured in the suite, so reaching the build step at all
    # proves the residency gate let it past.
    with pytest.raises(LLMNotConfiguredError):
        get_llm_provider("A CLI with no tenant", allow_unscoped=True)


def test_every_compile_path_takes_a_policy():
    """The three compile entry points must be able to carry the tenant down to
    the provider; a signature without it cannot enforce residency however
    carefully the router behaves."""
    import inspect

    from app.compiler.llm_compiler import compile_manifest_llm
    from app.compiler.mapping_agent import compile_agentic
    from app.compiler.rule_compiler import refine_with_llm

    for fn in (compile_manifest_llm, refine_with_llm, compile_agentic):
        assert "llm_policy" in inspect.signature(fn).parameters, (
            f"{fn.__module__}.{fn.__name__} cannot be handed the tenant's residency policy"
        )


def test_an_out_of_region_policy_stops_a_compile_before_a_prompt_exists():
    """§16: refuse to build the prompt, not refuse to send it. By the time a
    prompt exists the template context is already assembled in memory."""
    from app.llm.provider import get_llm_provider

    with pytest.raises(ResidencyViolation) as raised:
        get_llm_provider("Compiling a template with no colour coding", policy=EU_ONLY)

    assert "EU" in str(raised.value)
