"""What every model call cost, recorded without asking the caller to remember.

The bug this closes was not subtle and lasted the life of the project: all three
providers returned token counts, all thirteen call sites discarded them, and the
column the analytics page summed was never written by anything -- so the figure
on the screen was structurally always zero.

Most of this file is about the wrapper being *invisible*. A meter that changes
what a provider returns, or swallows an exception, or forgets to forward
`purpose`, would trade a wrong number on a chart for a wrong document.
"""

import pytest

from app import tenancy
from app.llm import metering, provider as llm
from app.llm.metering import MeteredProvider, UsageMeter, operation_for
from app.llm.pricing import DEFAULT_RATES
from app.models import LlmCall


class _Fake(llm.LLMProvider):
    """A provider that answers instantly and remembers how it was called."""

    def __init__(self, *, model="claude-sonnet-5", input_tokens=1000, output_tokens=500,
                 refused=False, error=None):
        self.model, self.calls = model, []
        self._in, self._out, self._refused, self._error = input_tokens, output_tokens, refused, error

    def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return llm.LLMResult(
            text="hello", blocks=[], input_tokens=self._in, output_tokens=self._out,
            model=self.model, refused=self._refused,
            refusal_reason="declined" if self._refused else None,
        )

    def structured(self, *, system, prompt, schema, purpose="generate"):
        self.calls.append(("structured", purpose))
        return llm.StructuredResult(
            data=None if self._error else {"ok": True}, model=self.model,
            input_tokens=self._in, output_tokens=self._out, error=self._error,
        )


@pytest.fixture
def org(two_orgs):
    from app.db import SessionLocal
    from app.models import Project

    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        yield db, project.org_id, project.id
    finally:
        db.rollback()
        db.close()


def _rows(db, org_id):
    return db.query(LlmCall).filter(LlmCall.org_id == org_id).all()


# ---- the wrapper is invisible ----

def test_the_result_the_caller_gets_is_the_one_the_provider_returned(org):
    """Identity, not equality. A meter that copies or reshapes a result is a
    meter that can change an answer."""
    db, org_id, _project = org
    inner = _Fake()
    metered = MeteredProvider(inner, meter=UsageMeter(db=db, org_id=org_id), capability="Chat")

    original = inner.generate()
    inner.calls.clear()
    got = metered.generate(instructions="x", context_chunks=[], fact_sheet={})

    assert type(got) is type(original)
    assert got.text == "hello" and got.model == "claude-sonnet-5"


def test_purpose_is_forwarded_because_the_router_picks_its_vendor_from_it(org):
    """`RoutedProvider` reads `purpose` to decide whether a call goes to the
    compile model or the generate model. Swallowing it would send every compile
    to the cheap model and the cost figures would be wrong in the flattering
    direction."""
    db, org_id, _project = org
    inner = _Fake()
    metered = MeteredProvider(inner, meter=UsageMeter(db=db, org_id=org_id),
                              capability="Compiling a template")

    metered.structured(system="s", prompt="p", schema={}, purpose="compile")
    assert inner.calls == [("structured", "compile")]


def test_an_exception_from_the_provider_is_not_swallowed(org):
    db, org_id, _project = org

    class _Angry(_Fake):
        def generate(self, **kwargs):
            raise RuntimeError("upstream is down")

    metered = MeteredProvider(_Angry(), meter=UsageMeter(db=db, org_id=org_id), capability="Chat")
    with pytest.raises(RuntimeError, match="upstream is down"):
        metered.generate()


def test_a_method_the_wrapper_does_not_define_is_forwarded(org):
    """So a provider that grows a third method keeps working -- unmetered and
    visibly so -- rather than losing it to an empty base implementation."""
    db, org_id, _project = org
    inner = _Fake()
    inner.model_name = "claude-sonnet-5"
    metered = MeteredProvider(inner, meter=UsageMeter(db=db, org_id=org_id), capability="Chat")
    assert metered.model_name == "claude-sonnet-5"


def test_a_recording_failure_never_fails_the_call_it_was_measuring(org):
    """The point of this module is a number on a chart. The caller's job is a
    customer's document."""
    db, org_id, _project = org

    class _BrokenMeter(UsageMeter):
        def _rate(self, model):
            raise RuntimeError("the rate table is on fire")

    meter = _BrokenMeter(db=db, org_id=org_id)
    metered = MeteredProvider(_Fake(), meter=meter, capability="Chat")

    assert metered.generate().text == "hello"
    assert meter.dropped == 1, "a dropped record must be counted, not silently lost"


# ---- what gets recorded ----

def test_one_call_records_one_row_with_its_tokens_and_its_cost(org):
    db, org_id, project_id = org
    meter = UsageMeter(db=db, org_id=org_id, project_id=project_id,
                       subject_type="template_file", subject_id="tf_1")
    MeteredProvider(_Fake(input_tokens=50_000, output_tokens=15_000, model="claude-opus-5"),
                    meter=meter, capability="Compiling a template").generate()
    db.flush()

    rows = _rows(db, org_id)
    assert len(rows) == 1
    row = rows[0]
    assert (row.input_tokens, row.output_tokens) == (50_000, 15_000)
    assert row.model == "claude-opus-5"
    assert row.operation == "compile"
    assert row.subject_id == "tf_1"
    assert row.outcome == "ok"
    # The vendor's own worked example: 50k in + 15k out on Opus 5 is $0.625.
    assert row.cost_micro_usd == 625_000


def test_a_refusal_is_recorded_with_its_tokens_because_it_was_billed(org):
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    MeteredProvider(_Fake(refused=True), meter=meter, capability="Chat").generate()
    db.flush()

    row = _rows(db, org_id)[0]
    assert row.outcome == "refused"
    assert row.input_tokens == 1000, "a refused call still costs what it cost"


def test_a_truncated_structured_call_is_recorded_as_truncated(org):
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    MeteredProvider(_Fake(error="Response exceeded max_tokens; output was truncated."),
                    meter=meter, capability="Compiling a template").structured(
                        system="s", prompt="p", schema={}, purpose="compile")
    db.flush()
    assert _rows(db, org_id)[0].outcome == "truncated"


def test_an_unpriced_model_records_no_cost_rather_than_a_cost_of_zero(org):
    """A call that looks free is a call nobody investigates."""
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    MeteredProvider(_Fake(model="some-model-nobody-priced"), meter=meter,
                    capability="Chat").generate()
    db.flush()

    row = _rows(db, org_id)[0]
    assert row.cost_micro_usd is None
    assert row.input_tokens == 1000, "the tokens are still known even when the price is not"


def test_the_rate_that_produced_a_cost_is_recorded_beside_it(org):
    """So a figure can be explained a year later, after the defaults have moved."""
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    MeteredProvider(_Fake(model="claude-sonnet-5"), meter=meter, capability="Chat").generate()
    db.flush()

    row = _rows(db, org_id)[0]
    assert row.rate_source.startswith("default:")
    assert row.input_rate_micro_usd_per_ktok == DEFAULT_RATES["claude-sonnet-5"].input_micro_usd_per_ktok


def test_a_dated_snapshot_alias_is_priced_as_the_model_it_is_a_snapshot_of(org):
    """Vendors answer with `claude-sonnet-5-20260214`. Without normalisation
    every real response would be an unpriced model and the cost column would be
    entirely NULL."""
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    MeteredProvider(_Fake(model="claude-sonnet-5-20260214"), meter=meter,
                    capability="Chat").generate()
    db.flush()
    assert _rows(db, org_id)[0].cost_micro_usd is not None


def test_the_rate_is_looked_up_once_per_model_per_request(org):
    """A compile makes up to twelve rounds against one model. Twelve identical
    queries for a number that cannot change mid-request is waste."""
    db, org_id, _project = org
    meter = UsageMeter(db=db, org_id=org_id)
    metered = MeteredProvider(_Fake(), meter=meter, capability="Compiling a template")
    for _ in range(5):
        metered.structured(system="s", prompt="p", schema={}, purpose="compile")
    assert len(meter._rates) == 1


# ---- the factory is where this is decided ----

def test_a_policy_carrying_a_meter_yields_a_metered_provider(monkeypatch, org):
    db, org_id, _project = org
    monkeypatch.setattr(llm.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(llm.settings, "llm_compile_provider", None)
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "sk-test")

    policy = tenancy.LLMDataPolicy(meter=UsageMeter(db=db, org_id=org_id))
    got = llm.get_llm_provider("Chat", policy=policy)
    assert isinstance(got, MeteredProvider)
    assert isinstance(got.inner, llm.AnthropicProvider)


def test_an_unscoped_call_is_not_metered_because_it_has_no_tenant_to_bill(monkeypatch):
    """`allow_unscoped=True` is a CLI or a test. Inventing an organisation to
    attribute its spend to would be worse than not recording it."""
    monkeypatch.setattr(llm.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(llm.settings, "llm_compile_provider", None)
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "sk-test")

    got = llm.get_llm_provider("Chat", allow_unscoped=True)
    assert isinstance(got, llm.AnthropicProvider)
    assert not isinstance(got, MeteredProvider)


def test_the_meter_is_not_part_of_what_makes_two_policies_equal():
    """Equality is about what the tenant *requires*. `test_residency` compares
    a resolved policy against a default one, and a meter attached to the first
    must not make them differ."""
    assert tenancy.LLMDataPolicy() == tenancy.LLMDataPolicy(meter=object())


# ---- the capability label ----

@pytest.mark.parametrize("capability,expected", [
    ("Compiling a template", "compile"),
    ("Reading a template", "authoring"),
    ("Editing a template", "authoring"),
    ("Explaining a template", "authoring"),
    ("Suggesting a document edit", "edit"),
    ("Chat", "chat"),
    ("Token-template generation", "generate"),
    ("Something nobody mapped", "other"),
])
def test_a_capability_string_becomes_a_bucket_the_analytics_can_group_on(capability, expected):
    """Matched loosely on purpose: these strings are prose written for an error
    message, so an exact table would drift to `other` the first time somebody
    improved a sentence."""
    assert operation_for(capability) == expected


def test_an_unmapped_capability_never_raises():
    """Adding a call site must not be able to break a compile."""
    assert operation_for("") == "other"
    assert operation_for(None) == "other"


# ---- the rescue: a call nobody saved ----

def _record_one(db, org_id, project_id, *, capability="Suggesting a document edit"):
    meter = UsageMeter(db=db, org_id=org_id, project_id=project_id,
                       subject_type="document_version", subject_id="v1")
    meter.record(capability=capability, purpose="generate", model="claude-sonnet-5",
                 input_tokens=1000, output_tokens=500, outcome="ok")
    return meter


def test_a_read_only_handler_no_longer_loses_what_it_spent(org):
    """The hole `db.add` alone left open, and it was not the obvious one.

    `suggest_edit` and chat make a model call and then commit nothing, because
    they have nothing to save. The row sat in `Session.new` until the request
    ended and went out with the session -- so the vendor billed for the call and
    we recorded that it had never happened.
    """
    from app.db import SessionLocal

    db, org_id, project_id = org
    _record_one(db, org_id, project_id)

    drained = metering.drain_pending(db)
    assert len(drained) == 1
    assert drained[0]["operation"] == "edit"
    # Off the caller's session entirely: closing it now must lose nothing.
    assert not [obj for obj in db.new if isinstance(obj, LlmCall)]

    assert metering.write_pending(drained) == 1

    check = SessionLocal()
    try:
        row = check.query(LlmCall).filter(
            LlmCall.org_id == org_id, LlmCall.subject_id == "v1").one()
        assert (row.input_tokens, row.output_tokens) == (1000, 500)
        assert row.cost_micro_usd is not None
        check.query(LlmCall).filter(LlmCall.id == row.id).delete()
        check.commit()
    finally:
        check.close()


def test_a_handler_that_saves_its_own_work_is_left_alone(org):
    """One transaction, not two. A committed row is not in `Session.new`, so
    the drain finds nothing and cannot write it twice."""
    db, org_id, project_id = org
    _record_one(db, org_id, project_id, capability="Chat")
    db.commit()
    try:
        assert metering.drain_pending(db) == []
    finally:
        db.query(LlmCall).filter(LlmCall.org_id == org_id).delete()
        db.commit()


def test_the_drain_does_not_take_anything_that_is_not_a_usage_row(org):
    """It must not become a general-purpose end-of-request committer: whatever
    else a handler left pending, it chose not to save."""
    from app.models import AuditLog

    db, org_id, project_id = org
    db.add(AuditLog(org_id=org_id, event="not yours to write",
                    entity_type="test", severity="info"))
    _record_one(db, org_id, project_id)

    drained = metering.drain_pending(db)
    assert len(drained) == 1
    assert any(isinstance(obj, AuditLog) for obj in db.new)
    db.rollback()


def test_a_drain_of_nothing_writes_nothing(org):
    db, _org_id, _project_id = org
    assert metering.drain_pending(db) == []
    assert metering.write_pending([]) == 0


def test_a_row_with_no_organisation_is_dropped_rather_than_written(org):
    """No tenant means no bill to attach it to, and on PostgreSQL row-level
    security would reject it anyway. Silently writing it to nobody is worse."""
    assert metering.write_pending([{"org_id": None, "model": "x"}]) == 0


def test_a_write_failure_is_swallowed_and_logged(org, monkeypatch):
    """A bookkeeping failure must never fail the request it was measuring."""
    logged = []
    monkeypatch.setattr(metering.log, "warning", lambda *a, **k: logged.append(a))

    assert metering.write_pending([{"org_id": "some-org", "not_a_column": 1}]) == 0

    assert logged and "could not record" in logged[0][0]


def test_a_drain_failure_is_swallowed_too(monkeypatch):
    class _Exploding:
        @property
        def new(self):
            raise RuntimeError("session is gone")

    logged = []
    monkeypatch.setattr(metering.log, "warning", lambda *a, **k: logged.append(a))

    assert metering.drain_pending(_Exploding()) == []

    assert logged and "could not drain" in logged[0][0]


def test_the_request_teardown_writes_what_the_handler_did_not(org):
    """`get_db` is the one place every request's session passes through, which is
    the same argument that put residency and metering in `get_llm_provider`."""
    from app.db import SessionLocal, get_db

    _db, org_id, project_id = org
    gen = get_db()
    session = next(gen)
    try:
        _record_one(session, org_id, project_id)
        session.info["marker"] = True
    finally:
        gen.close()  # runs the finally: drain, close, write

    check = SessionLocal()
    try:
        rows = check.query(LlmCall).filter(
            LlmCall.org_id == org_id, LlmCall.subject_id == "v1").all()
        assert len(rows) == 1
        check.query(LlmCall).filter(LlmCall.org_id == org_id).delete()
        check.commit()
    finally:
        check.close()


def test_a_handler_that_raised_still_records_what_it_spent(org):
    """The drain runs before the close, so an exception on its way out does not
    take the spend with it.

    A compile that dies on its ninth model call really did spend money on the
    first eight, and the vendor billed for them whether or not the work around
    them was kept. Not recording that would make the spend chart under-report
    exactly the failures worth investigating.
    """
    from app.db import SessionLocal, get_db

    _db, org_id, project_id = org
    original = RuntimeError("the handler blew up")

    gen = get_db()
    session = next(gen)
    _record_one(session, org_id, project_id, capability="Compiling a template")
    with pytest.raises(RuntimeError) as raised:
        gen.throw(original)

    # The handler's own exception reaches the caller unchanged. A bookkeeping
    # step that replaced it would hide the actual failure behind a write error.
    assert raised.value is original

    check = SessionLocal()
    try:
        rows = check.query(LlmCall).filter(
            LlmCall.org_id == org_id, LlmCall.subject_id == "v1").all()
        assert len(rows) == 1 and rows[0].operation == "compile"
        check.query(LlmCall).filter(LlmCall.org_id == org_id).delete()
        check.commit()
    finally:
        check.close()


def test_a_handler_that_committed_is_not_recorded_twice(org):
    """The drain and the handler's own commit must not both write the row."""
    from app.db import SessionLocal, get_db

    _db, org_id, project_id = org
    gen = get_db()
    session = next(gen)
    _record_one(session, org_id, project_id)
    session.commit()
    gen.close()

    check = SessionLocal()
    try:
        rows = check.query(LlmCall).filter(
            LlmCall.org_id == org_id, LlmCall.subject_id == "v1").all()
        assert len(rows) == 1
        check.query(LlmCall).filter(LlmCall.org_id == org_id).delete()
        check.commit()
    finally:
        check.close()


def test_every_live_capability_is_bucketed():
    """The guardrail the loose fallback needed.

    `operation_for` matches loosely so a reworded capability does not silently
    drift to `other` -- but loose matching has its own failure, and it is worse
    because it is confident. "Reconciling conditions across template parts" is a
    compile round; it named a template, so it was filed under `authoring` and
    charged to a bucket for people editing templates by hand. "Agentic revision"
    matched nothing and became `other`.

    Falling through to `other` stays the runtime behaviour: adding a call site
    must never be able to break a compile. This makes the drift fail here, at
    test time, instead of on the invoice.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    live = set()
    for path in root.rglob("*.py"):
        live.update(re.findall(r'get_llm_provider\(\s*"([^"]+)"', path.read_text()))

    assert live, "found no get_llm_provider call sites -- the scan is broken, not the code"

    unbucketed = sorted(c for c in live if operation_for(c) == "other")
    assert not unbucketed, (
        "these capability strings reach a model and are billed to 'other':\n  "
        + "\n  ".join(unbucketed)
        + "\nAdd each to OPERATION_BY_CAPABILITY with the bucket it belongs in."
    )

    # And the ones that matter most are not merely *some* bucket, but the right one.
    assert operation_for("Reconciling conditions across template parts") == "compile"
    assert operation_for("Agentic revision") == "compile"
    assert operation_for("Reviewing a compiled manifest") == "compile"
    assert operation_for("Editing a template") == "authoring"
