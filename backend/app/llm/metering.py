"""Recording what every model call cost, without asking thirteen callers to.

All three providers already return `input_tokens` and `output_tokens` on every
path. All thirteen call sites threw them away, and the column the analytics page
summed -- `generation_jobs.token_usage` -- was never written by anything, so the
token figure was structurally always zero.

**Why the wrapper, and not thirteen edits.** `get_llm_provider`'s own docstring
records the same problem being solved the same way once already: residency was
enforced at the call sites, *"so five of the six paths that reach a model,
including every compile path, skipped it entirely,"* and the fix was to move the
check into the factory, where *"there is no way to get a provider without passing
through this function."* Metering has that disease exactly. Editing the call
sites would also be opt-in, so the failure mode is silent under-billing -- which
is the failure the codebase already had.

**Why the policy carries the meter.** Eight of the thirteen call sites are inside
the compiler, which has no session, no request and no organisation *by design*.
The one object that already reaches all thirteen is `LLMDataPolicy`, threaded
down from the router precisely because the factory refuses to build a provider
without it. So the meter travels on the thing that already travels.

**Recording rides on the caller's session, and is rescued if the caller never
commits.** `record` calls `db.add` and nothing else, exactly as
`metrics.record_timing` does, so a handler that commits its own work commits the
usage with it and there is one transaction, not two.

That alone was not enough, and the gap was not the obvious one. Handlers that
*read* -- `suggest_edit`, chat -- make a model call and then commit nothing at
all, because they have nothing to save. Their rows sat in `Session.new` until the
request ended and were discarded, so every interactive call was billed by the
vendor and recorded by us as never having happened. `drain_pending` and
`write_pending` below close that: `get_db` harvests anything still unwritten at
the end of a request and puts it down on its own session, after the caller's is
closed.

What it deliberately does not do is commit the caller's session -- that would
persist whatever half-finished work the handler chose not to keep. `drain_pending`
takes `LlmCall` rows and nothing else, so a handler's own unsaved work stays
unsaved.

**A handler that raises still records what it spent.** The drain runs before the
session is closed, so the rows are still in `Session.new` when the exception is
on its way out, and they are written. That is deliberate and it is the honest
answer: the vendor billed for those calls whether or not we kept the result of
the work around them, and a compile that dies on its ninth model call really did
spend money on the first eight. Recording it costs a row; not recording it means
the spend chart under-reports exactly the failures worth investigating.

The one case that still loses a record is a handler that calls `db.rollback()`
*itself* before raising, which expunges the pending rows before this sees them.
Nothing in the codebase does that on a path that has already reached a model, and
it is called out here so that a future one is a decision rather than a surprise.
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

COMPILE = "compile"
AUTHORING = "authoring"
GENERATE = "generate"
EDIT = "edit"
CHAT = "chat"
OTHER = "other"

OPERATIONS = (COMPILE, AUTHORING, GENERATE, EDIT, CHAT, OTHER)

#: The `get_llm_provider(capability=...)` string each call site already passes,
#: mapped to a coarse bucket analytics can group on. Grouping on the raw string
#: would make a reworded capability look like a new category on the chart.
#:
#: An unmapped capability becomes `other` rather than raising: adding a new call
#: site must not be able to break a compile, and a bucket nobody chose is a
#: smaller problem than an exception in the middle of a model call.
OPERATION_BY_CAPABILITY = {
    "Compiling a template": COMPILE,
    "Template compilation": COMPILE,
    "Manifest refinement": COMPILE,
    "Agentic compile": COMPILE,
    "Reviewing a compiled manifest": COMPILE,
    # Two rounds of the agentic compile that the loose fallback got wrong, and
    # both in the expensive direction. "Reconciling conditions across template
    # parts" names a template and nothing else the table matches, so it landed in
    # `authoring` -- a compile-phase model call billed to the bucket for a person
    # editing a template, on orgs whose users never opened the editor. "Agentic
    # revision" matched nothing at all and landed in `other`. The compile total is
    # the number an operator uses to decide whether compiles are too expensive,
    # and it was short by these rounds while the other two buckets were inflated.
    # `test_every_live_capability_is_bucketed` now fails if a new call site drifts
    # the same way.
    "Reconciling conditions across template parts": COMPILE,
    "Agentic revision": COMPILE,
    "Editing a template": AUTHORING,
    "Explaining a template": AUTHORING,
    "Authoring a template from a description": AUTHORING,
    "Suggesting a document edit": EDIT,
    "Chat": CHAT,
    "Token-template generation": GENERATE,
}


def operation_for(capability: str) -> str:
    """The bucket this capability belongs to, matched loosely.

    Loosely because the capability strings are prose written at the call site
    and read by a person in an error message -- "Reading a template needs a
    language model" -- so an exact-match table would silently drift to `other`
    the first time somebody improved a sentence.
    """
    text = (capability or "").strip()
    if text in OPERATION_BY_CAPABILITY:
        return OPERATION_BY_CAPABILITY[text]
    lowered = text.lower()
    # Order matters, and the reason is "Token-template generation": it names a
    # template *and* a generation, and it is the latter. Checking "template"
    # first would file every document produced from the token library under
    # authoring, which is where a person edits one.
    if "compil" in lowered or "manifest" in lowered:
        return COMPILE
    if "chat" in lowered:
        return CHAT
    if "generat" in lowered or "narrative" in lowered:
        return GENERATE
    if "template" in lowered or "blueprint" in lowered:
        return AUTHORING
    if "edit" in lowered or "suggest" in lowered:
        return EDIT
    return OTHER


def _outcome_of(result) -> str:
    """What happened, in the four words the analytics groups on."""
    if getattr(result, "refused", False):
        return "refused"
    error = getattr(result, "error", None)
    if error:
        return "truncated" if "max_tokens" in str(error) or "truncat" in str(error).lower() else "error"
    return "ok"


@dataclass
class UsageMeter:
    """Where one request's model calls are recorded.

    Built by `tenancy.llm_policy_for`, which is the only place that knows both
    the organisation and the session. Carried to the model on the policy.
    """

    db: object
    org_id: str
    project_id: str | None = None
    user_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    #: One rate lookup per model per request rather than per call. A compile
    #: makes up to twelve rounds against one model; twelve identical queries for
    #: a number that cannot change mid-request is a waste.
    _rates: dict = field(default_factory=dict, repr=False)
    #: Calls this meter could not record. Counted rather than raised, and
    #: surfaced so that silence is never mistaken for zero spend.
    dropped: int = field(default=0, repr=False)

    def _rate(self, model: str):
        from app.llm.pricing import canonical_model, rate_for

        key = canonical_model(model)
        if key not in self._rates:
            self._rates[key] = rate_for(self.db, org_id=self.org_id, model=key)
        return self._rates[key]

    def record(self, *, capability: str, purpose: str, model: str,
               input_tokens: int, output_tokens: int, outcome: str):
        """Add one row for one model call. Never raises.

        A failure to record must not fail the call it was measuring: the point
        of this module is a number on a chart, and the caller's job is a
        customer's document.
        """
        from app.llm.pricing import cost_micro_usd, rate_source
        from app.models import LlmCall

        try:
            rate = self._rate(model)
            row = LlmCall(
                org_id=self.org_id,
                project_id=self.project_id,
                user_id=self.user_id,
                capability=(capability or "")[:200],
                operation=operation_for(capability),
                purpose=purpose or "generate",
                subject_type=self.subject_type,
                subject_id=self.subject_id,
                model=(model or "unknown")[:200],
                input_tokens=max(0, int(input_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
                outcome=outcome,
                input_rate_micro_usd_per_ktok=rate.input_micro_usd_per_ktok if rate else None,
                output_rate_micro_usd_per_ktok=rate.output_micro_usd_per_ktok if rate else None,
                cost_micro_usd=cost_micro_usd(
                    rate, input_tokens=input_tokens, output_tokens=output_tokens),
                rate_source=rate_source(rate),
            )
            self.db.add(row)
            return row
        except Exception:  # noqa: BLE001 - see the docstring
            self.dropped += 1
            log.warning("could not record model usage for org %s", self.org_id, exc_info=True)
            return None


#: Column values copied off an unwritten row. Plain data rather than the ORM
#: object, because the object belongs to a session that is about to close.
_USAGE_COLUMNS = (
    "org_id", "project_id", "user_id", "capability", "operation", "purpose",
    "subject_type", "subject_id", "model", "input_tokens", "output_tokens", "outcome",
    "input_rate_micro_usd_per_ktok", "output_rate_micro_usd_per_ktok",
    "cost_micro_usd", "rate_source",
)


def drain_pending(db) -> list[dict]:
    """Take every unwritten usage row off this session. Never raises.

    Called at the end of a request, before the session is closed. A handler that
    committed has nothing here, so this is a no-op on every path that already
    worked -- it exists for the handlers that make a model call and save nothing.
    """
    from app.models import LlmCall

    try:
        rows = [obj for obj in db.new if isinstance(obj, LlmCall)]
        if not rows:
            return []
        values = [{name: getattr(row, name, None) for name in _USAGE_COLUMNS} for row in rows]
        for row in rows:
            db.expunge(row)
        return values
    except Exception:  # noqa: BLE001 - a bookkeeping failure must not fail a request
        log.warning("could not drain pending model usage", exc_info=True)
        return []


def write_pending(values: list[dict]) -> int:
    """Persist drained rows on a session of their own. Never raises.

    Its own session because the caller's is closed by the time this runs, and
    because a usage row must not be able to drag anything else along with it.

    Scoped per organisation: on PostgreSQL `llm_calls` is behind a row-level
    security policy keyed on `app.current_org`, so a session that has not
    declared the tenant inserts nothing and reports success.
    """
    if not values:
        return 0
    from app.db import SessionLocal
    from app.models import LlmCall
    from app.tenancy import release_org_scope, set_current_org

    written = 0
    by_org: dict[str, list[dict]] = {}
    for value in values:
        by_org.setdefault(value.get("org_id"), []).append(value)

    for org_id, rows in by_org.items():
        if not org_id:
            continue
        db = SessionLocal()
        try:
            set_current_org(db, org_id)
            db.add_all([LlmCall(**row) for row in rows])
            db.commit()
            written += len(rows)
        except Exception:  # noqa: BLE001 - see the docstring
            log.warning("could not record %d model call(s) for org %s", len(rows), org_id,
                        exc_info=True)
            db.rollback()
        finally:
            release_org_scope(db)
            db.close()
    return written


class MeteredProvider:
    """A provider that records what it spent. Transparent in every other way.

    Wraps the **outermost** provider, so `RoutedProvider`'s choice of vendor is
    invisible here and `result.model` is whatever actually answered -- which is
    what the cost must key on, because compile and generate can be different
    vendors in one deployment.

    Not a subclass of `LLMProvider`: it is a proxy, and inheriting would invite
    a future method to be answered by an empty base implementation instead of
    being forwarded. `__getattr__` forwards everything this class does not
    define, so a provider that grows a third method keeps working, unmetered and
    visibly so, rather than losing it.
    """

    def __init__(self, inner, *, meter: UsageMeter, capability: str):
        self._inner = inner
        self._meter = meter
        self._capability = capability

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def inner(self):
        """The provider underneath, for tests that assert which vendor was built."""
        return self._inner

    def generate(self, **kwargs):
        result = self._inner.generate(**kwargs)
        self._meter.record(
            capability=self._capability, purpose="generate",
            model=getattr(result, "model", ""),
            input_tokens=getattr(result, "input_tokens", 0),
            output_tokens=getattr(result, "output_tokens", 0),
            outcome=_outcome_of(result),
        )
        return result

    def structured(self, *, system: str, prompt: str, schema: dict, purpose: str = "generate"):
        # `purpose` is forwarded rather than defaulted: `RoutedProvider` picks
        # its vendor from it, so swallowing it would silently send every compile
        # to the generate model.
        result = self._inner.structured(
            system=system, prompt=prompt, schema=schema, purpose=purpose)
        self._meter.record(
            capability=self._capability, purpose=purpose,
            model=getattr(result, "model", ""),
            input_tokens=getattr(result, "input_tokens", 0),
            output_tokens=getattr(result, "output_tokens", 0),
            outcome=_outcome_of(result),
        )
        return result
