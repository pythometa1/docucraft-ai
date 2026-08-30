"""What a model call costs, and the three rules that keep the number honest.

There was no pricing in this codebase at all before this: not a table, not a
constant, not a column. So every test here is about a number that did not exist
being right rather than a number that existed being wrong -- and the ways a cost
figure goes wrong are all quiet ones.
"""

import pytest

from app.config import settings
from app.llm import pricing
from app.llm.pricing import (
    DEFAULT_RATES, ModelRate, canonical_model, cost_micro_usd, rate_for, rate_source, usd,
)


# ---- the rates themselves ----

def test_every_model_this_build_can_be_configured_with_has_a_rate():
    """The guardrail. Adding a model to `config.py` without pricing it would
    make every call through it silently uncosted, and the analytics would report
    a smaller bill rather than an incomplete one."""
    configured = {
        settings.llm_model, settings.llm_compile_model,
        settings.gemini_model, settings.gemini_compile_model,
        settings.openai_model, settings.openai_compile_model,
    }
    missing = {m for m in configured if m and canonical_model(m) not in DEFAULT_RATES}
    assert not missing, (
        f"{sorted(missing)} can be configured but has no rate, so calls through it would "
        "report no cost. Add it to DEFAULT_RATES with a vendor URL and the date you read it."
    )


def test_every_rate_records_where_it_came_from_and_when():
    """A price carried forward from memory is a plausible number attached to
    somebody's invoice. The source is how a figure gets re-checked."""
    for model, rate in DEFAULT_RATES.items():
        assert rate.source.startswith("https://"), f"{model} has no vendor URL"
        assert pricing.RATES_READ_ON in rate.source, f"{model}'s source records no date"


def test_output_is_never_cheaper_than_input():
    """True of every vendor's published pricing and a cheap way to catch a
    transposed pair, which is the transcription error that actually happens."""
    for model, rate in DEFAULT_RATES.items():
        assert rate.output_micro_usd_per_ktok >= rate.input_micro_usd_per_ktok, model


def test_the_published_figures_are_what_was_read():
    """Pinned so a well-meant edit cannot quietly move a rate. If a vendor
    changes their price, this test is where you find out you have to update the
    date too."""
    assert (DEFAULT_RATES["claude-opus-5"].input_usd_per_mtok,
            DEFAULT_RATES["claude-opus-5"].output_usd_per_mtok) == (5.0, 25.0)
    assert (DEFAULT_RATES["claude-sonnet-5"].input_usd_per_mtok,
            DEFAULT_RATES["claude-sonnet-5"].output_usd_per_mtok) == (2.0, 10.0)
    assert (DEFAULT_RATES["gpt-5"].input_usd_per_mtok,
            DEFAULT_RATES["gpt-5"].output_usd_per_mtok) == (1.25, 10.0)
    assert (DEFAULT_RATES["gpt-5-mini"].input_usd_per_mtok,
            DEFAULT_RATES["gpt-5-mini"].output_usd_per_mtok) == (0.25, 2.0)
    assert (DEFAULT_RATES["gemini-2.5-pro"].input_usd_per_mtok,
            DEFAULT_RATES["gemini-2.5-pro"].output_usd_per_mtok) == (1.25, 10.0)
    assert (DEFAULT_RATES["gemini-3.6-flash"].input_usd_per_mtok,
            DEFAULT_RATES["gemini-3.6-flash"].output_usd_per_mtok) == (0.75, 3.75)


def test_a_promotional_rate_says_when_it_expires():
    """Gemini 3.6 Flash is promotional through 2026-12-31 and then doubles. A
    rate that changes on a date nobody wrote down is a bill that silently halves
    against reality."""
    assert "2026-12-31" in DEFAULT_RATES["gemini-3.6-flash"].note


# ---- the arithmetic ----

def test_the_cost_matches_the_vendors_own_worked_example():
    """Anthropic's pricing page works through 50,000 input and 15,000 output
    tokens on Opus 5 and arrives at $0.625. So does this."""
    cost = cost_micro_usd(DEFAULT_RATES["claude-opus-5"],
                          input_tokens=50_000, output_tokens=15_000)
    assert usd(cost) == 0.625


def test_money_is_integer_micro_dollars_so_a_thousand_calls_sum_exactly():
    """Floats do not. Neither does `Numeric` on SQLite, which is the backend the
    suite runs on -- and a money column that does not sum exactly is worse than
    no money column."""
    one = cost_micro_usd(DEFAULT_RATES["claude-sonnet-5"], input_tokens=333, output_tokens=777)
    assert isinstance(one, int)
    assert sum(one for _ in range(1000)) == one * 1000


def test_an_unpriced_model_costs_nothing_known_rather_than_nothing():
    """The distinction the whole feature turns on. A call that looks free is a
    call nobody investigates."""
    assert rate_for.__doc__  # the function exists
    assert cost_micro_usd(None, input_tokens=1_000_000, output_tokens=1_000_000) is None
    assert usd(None) is None


def test_negative_or_missing_token_counts_do_not_produce_a_negative_bill():
    rate = DEFAULT_RATES["gpt-5"]
    assert cost_micro_usd(rate, input_tokens=-5, output_tokens=None) == 0


# ---- naming ----

@pytest.mark.parametrize("raw,expected", [
    ("claude-sonnet-5", "claude-sonnet-5"),
    ("Claude-Sonnet-5", "claude-sonnet-5"),
    ("claude-sonnet-5-20260214", "claude-sonnet-5"),
    ("gpt-5-2026-03-01", "gpt-5"),
    ("anthropic/claude-opus-5", "claude-opus-5"),
    ("  gemini-2.5-pro  ", "gemini-2.5-pro"),
])
def test_a_dated_snapshot_prices_as_the_model_it_is_a_snapshot_of(raw, expected):
    """Vendors answer with dated aliases. Without this every real response would
    be an unpriced model and the cost column would be entirely NULL."""
    assert canonical_model(raw) == expected


def test_a_name_nobody_recognises_is_returned_rather_than_guessed_at():
    """So an org can price it themselves under the name their vendor uses."""
    assert canonical_model("llama-9-enormous") == "llama-9-enormous"
    assert canonical_model(None) == ""


# ---- per-organisation overrides ----

@pytest.fixture
def org(two_orgs):
    from app.db import SessionLocal
    from app.models import Project

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        yield db, db.get(Project, project_a).org_id
    finally:
        db.rollback()
        db.close()


def test_an_organisation_rate_overrides_the_shipped_default(org):
    """A tenant on a negotiated price should see their numbers, not ours."""
    from app.models import OrgModelRate

    db, org_id = org
    db.add(OrgModelRate(org_id=org_id, model="claude-sonnet-5",
                        input_micro_usd_per_ktok=1000, output_micro_usd_per_ktok=5000,
                        note="negotiated", updated_by="u1"))
    db.flush()

    rate = rate_for(db, org_id=org_id, model="claude-sonnet-5")
    assert rate.input_micro_usd_per_ktok == 1000
    assert rate_source(rate).startswith("org:")


def test_an_organisation_can_price_a_model_this_build_has_never_heard_of(org):
    from app.models import OrgModelRate

    db, org_id = org
    assert rate_for(db, org_id=org_id, model="llama-9-enormous") is None

    db.add(OrgModelRate(org_id=org_id, model="llama-9-enormous",
                        input_micro_usd_per_ktok=100, output_micro_usd_per_ktok=200,
                        updated_by="u1"))
    db.flush()
    assert rate_for(db, org_id=org_id, model="llama-9-enormous") is not None


def test_one_organisations_rate_does_not_price_anothers_calls(org, two_orgs):
    from app.models import OrgModelRate, Project

    db, org_id = org
    _ta, _pa, _tb, project_b = two_orgs
    other_org = db.get(Project, project_b).org_id

    db.add(OrgModelRate(org_id=org_id, model="gpt-5",
                        input_micro_usd_per_ktok=1, output_micro_usd_per_ktok=1,
                        updated_by="u1"))
    db.flush()

    assert rate_for(db, org_id=org_id, model="gpt-5").input_micro_usd_per_ktok == 1
    assert rate_for(db, org_id=other_org, model="gpt-5") == DEFAULT_RATES["gpt-5"]


def test_a_cost_already_recorded_is_not_restated_when_a_rate_changes(org):
    """The reason cost is frozen onto the row. Changing a price must never
    silently rewrite last quarter's spend."""
    from app.llm.metering import UsageMeter
    from app.models import LlmCall, OrgModelRate

    db, org_id = org
    meter = UsageMeter(db=db, org_id=org_id)
    meter.record(capability="Chat", purpose="generate", model="claude-sonnet-5",
                 input_tokens=1_000_000, output_tokens=0, outcome="ok")
    db.flush()
    before = db.query(LlmCall).filter(LlmCall.org_id == org_id).one().cost_micro_usd
    assert before == 2_000_000  # $2.00 for a million input tokens

    db.add(OrgModelRate(org_id=org_id, model="claude-sonnet-5",
                        input_micro_usd_per_ktok=99_000, output_micro_usd_per_ktok=99_000,
                        updated_by="u1"))
    db.flush()

    after = db.query(LlmCall).filter(LlmCall.org_id == org_id).one().cost_micro_usd
    assert after == before, "history moved when a rate changed"

    # And the next call uses the new rate.
    fresh = UsageMeter(db=db, org_id=org_id)
    fresh.record(capability="Chat", purpose="generate", model="claude-sonnet-5",
                 input_tokens=1000, output_tokens=0, outcome="ok")
    db.flush()
    newest = db.query(LlmCall).filter(LlmCall.org_id == org_id).order_by(
        LlmCall.id.desc()).first()
    assert newest.cost_micro_usd == 99_000


# ---- the API boundary ----

def test_a_rate_renders_for_the_admin_screen_in_the_units_a_vendor_publishes():
    """Stored per thousand tokens in micro-dollars because that sums exactly;
    shown per million in dollars because that is what a vendor's page says and
    what somebody will compare it against."""
    shown = DEFAULT_RATES["claude-opus-5"].as_dict()
    assert shown["model"] == "claude-opus-5"
    assert shown["input_usd_per_mtok"] == 5.0
    assert shown["output_usd_per_mtok"] == 25.0
    assert shown["source"].startswith("https://")


def test_asking_the_price_of_nothing_is_not_an_error(org):
    """A call whose model came back empty -- a provider error before the
    response -- must not raise inside the meter."""
    db, org_id = org
    assert rate_for(db, org_id=org_id, model="") is None
    assert rate_for(db, org_id=org_id, model=None) is None


def test_the_priced_models_can_be_listed_for_the_admin_screen():
    assert set(pricing.priced_models()) == set(DEFAULT_RATES)


def test_an_unpriced_call_records_no_rate_source_either():
    """Symmetry with the NULL cost: if there was no rate, there is no rate to
    name, and writing "default" would claim a lookup that never happened."""
    assert rate_source(None) is None
