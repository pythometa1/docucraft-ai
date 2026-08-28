"""Resolve every unit a manifest declares, in dependency order.

This is the piece that makes one document able to mix strategies. A tox report
needs deterministic header fields, a computed incidence rate, a dose that a
human must sign off, a conclusion that depends on what an earlier section
resolved to, and a narrative summary grounded in study data. Previously the
deterministic filler and the RAG generator were strangers -- neither could call
the other -- so a document had to be entirely one or entirely the other.

Five unit kinds:

    static      source record lookup                deterministic
    computed    safe arithmetic over other units    deterministic
    conditional exact | fuzzy | context             deterministic | LLM
    narrative   retrieval + grounded generation     LLM, cited
    human       queued for review                   person

Ordering is not incidental. A computed total needs its inputs first, and a
"choose the paragraph based on what we concluded earlier" condition needs the
earlier unit's verdict -- so units are resolved in topological order and a
cycle is a hard error rather than a silently wrong document.

Anything the engine cannot resolve confidently parks the document for review
instead of guessing.
"""

from dataclasses import dataclass, field

from app.generation.rule_engine import formula_identifiers, safe_eval_formula
from app.expressions.token_parser import _condition_identifiers, evaluate_condition


@dataclass
class UnitOutcome:
    unit_id: str
    kind: str
    value: str | None = None
    verdict: bool | None = None
    strategy: str = ""
    confidence: float = 1.0
    source: str = ""  # source_record | computed | system | llm | human | missing
    needs_human: bool = False
    reason: str | None = None
    rationale: str | None = None
    inputs_used: dict = field(default_factory=dict)

    def as_lineage(self) -> dict:
        entry = {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "strategy": self.strategy,
            "source": self.source,
            "confidence": self.confidence,
        }
        if self.value is not None:
            entry["value"] = self.value
        if self.verdict is not None:
            entry["verdict"] = self.verdict
        if self.inputs_used:
            entry["inputs"] = self.inputs_used
        if self.rationale:
            entry["rationale"] = self.rationale
        if self.needs_human:
            entry["needs_human"] = True
            entry["reason"] = self.reason
        return entry


@dataclass
class ResolutionResult:
    values: dict = field(default_factory=dict)          # field_id -> resolved string
    condition_verdicts: dict = field(default_factory=dict)  # condition_id -> bool
    outcomes: list = field(default_factory=list)
    open_tasks: list = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.open_tasks)

    def lineage(self) -> list[dict]:
        return [o.as_lineage() for o in self.outcomes]


class CyclicDependencyError(ValueError):
    """Raised when units depend on each other. Better to fail loudly at compile
    or generate time than to emit a document whose values depend on evaluation
    order."""


def _unit_dependencies(manifest: dict) -> tuple[dict, dict]:
    """Return ({unit_id: unit}, {unit_id: {dependency_ids}})."""
    units: dict[str, dict] = {}
    deps: dict[str, set] = {}

    for f in manifest.get("fields", []):
        kind = f.get("kind") or ("computed" if f.get("formula") else "static")
        units[f["id"]] = {**f, "kind": kind, "unit_type": "field"}
        if kind == "computed":
            declared = f.get("inputs") or formula_identifiers(f.get("formula", ""))
            deps[f["id"]] = set(declared)
        else:
            deps[f["id"]] = set()

    for cond in manifest.get("conditions", []):
        cid = cond["id"]
        units[cid] = {**cond, "kind": "conditional", "unit_type": "condition"}
        referenced = set(_condition_identifiers(cond.get("expression", "")))
        # A `context` condition may also depend on units resolved earlier.
        referenced.update(cond.get("depends_on", []) or [])
        # Only keep dependencies that are themselves units; a plain source
        # field with no unit is satisfied by the record, not by ordering.
        deps[cid] = referenced

    return units, deps


def _topological_order(units: dict, deps: dict) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(uid: str, trail: tuple):
        if state.get(uid) == 1:
            return
        if state.get(uid) == 0:
            cycle = " -> ".join(trail[trail.index(uid):] + (uid,))
            raise CyclicDependencyError(f"Circular dependency between manifest units: {cycle}")
        state[uid] = 0
        for dep in sorted(deps.get(uid, ())):
            if dep in units:  # non-unit dependencies come straight from the record
                visit(dep, trail + (uid,))
        state[uid] = 1
        order.append(uid)

    for uid in units:
        visit(uid, ())
    return order


def resolve_manifest(
    manifest: dict,
    record: dict,
    *,
    narrative_resolver=None,
    fuzzy_resolver=None,
    system_values: dict | None = None,
) -> ResolutionResult:
    """Resolve every unit against one source record.

    `narrative_resolver(unit, context) -> (text, confidence, rationale)` and
    `fuzzy_resolver(condition, context) -> (verdict, confidence, rationale)` are
    injected so this module stays free of retrieval and LLM concerns -- and so
    the deterministic path runs with neither wired up.
    """
    units, deps = _unit_dependencies(manifest)
    order = _topological_order(units, deps)

    result = ResolutionResult()
    # Values already known from the source row seed the environment; computed
    # units read from here as they resolve, which is what lets a formula
    # reference another formula.
    env: dict = {k: v for k, v in record.items() if not k.startswith("_")}
    env.update(system_values or {})

    for unit_id in order:
        unit = units[unit_id]
        kind = unit["kind"]

        if unit["unit_type"] == "condition":
            outcome = _resolve_condition(unit, env, result, fuzzy_resolver)
            # Only decided verdicts are handed down. `bool(None)` is False, and
            # publishing that here told the fill engine "this condition is
            # false, drop its blocks" for a condition nobody could evaluate --
            # the undecided case has to reach the renderer as absent so it can
            # keep the section and block the document instead.
            if outcome.verdict is not None:
                result.condition_verdicts[unit_id] = bool(outcome.verdict)
        elif kind == "computed":
            outcome = _resolve_computed(unit, env)
        elif kind == "narrative":
            outcome = _resolve_narrative(unit, env, narrative_resolver)
        elif kind == "human":
            outcome = UnitOutcome(
                unit_id, "human", strategy="human", source="human", needs_human=True,
                confidence=0.0, reason=unit.get("question") or "This value requires human input.",
            )
        else:
            outcome = _resolve_static(unit, env)

        if outcome.value is not None:
            env[unit_id] = outcome.value
            result.values[unit_id] = outcome.value

        result.outcomes.append(outcome)
        if outcome.needs_human:
            result.open_tasks.append({
                "unit_id": unit_id,
                "kind": "calculation" if kind == "computed" else ("condition" if unit["unit_type"] == "condition" else kind),
                "question": outcome.reason or f"'{unit_id}' could not be resolved automatically.",
                "context": {
                    "expression": unit.get("formula") or unit.get("expression"),
                    "inputs": outcome.inputs_used,
                    "available": {k: env.get(k) for k in sorted(deps.get(unit_id, ())) if k in env},
                },
                "proposed_value": outcome.value,
            })

    return result


def _resolve_static(unit: dict, env: dict) -> UnitOutcome:
    unit_id = unit["id"]
    if unit_id in env and env[unit_id] not in (None, ""):
        return UnitOutcome(unit_id, "static", value=str(env[unit_id]), strategy="lookup", source="source_record")
    if unit.get("required"):
        return UnitOutcome(
            unit_id, "static", strategy="lookup", source="missing", needs_human=True, confidence=0.0,
            reason=f"Required field '{unit_id}' has no value in the source record.",
        )
    return UnitOutcome(unit_id, "static", value=None, strategy="lookup", source="missing", confidence=0.0)


def _resolve_computed(unit: dict, env: dict) -> UnitOutcome:
    unit_id = unit["id"]
    outcome = safe_eval_formula(unit.get("formula", ""), env)
    if not outcome.ok:
        # A calculation that cannot be trusted must not silently become blank.
        return UnitOutcome(
            unit_id, "computed", strategy="formula", source="computed", confidence=0.0,
            needs_human=True, reason=f"Could not compute '{unit_id}': {outcome.error}",
            inputs_used=outcome.inputs_used or {},
        )

    digits = unit.get("decimals")
    value = f"{outcome.value:.{digits}f}" if isinstance(digits, int) else _trim_number(outcome.value)
    return UnitOutcome(
        unit_id, "computed", value=value, strategy="formula", source="computed",
        inputs_used=outcome.inputs_used or {},
        # An explicit sign-off flag makes the calculation visible to a reviewer
        # even when it computed cleanly.
        needs_human=bool(unit.get("requires_human_check")),
        reason=f"'{unit_id}' is flagged for human verification." if unit.get("requires_human_check") else None,
    )


def _trim_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _resolve_condition(unit: dict, env: dict, result: ResolutionResult, fuzzy_resolver) -> UnitOutcome:
    unit_id = unit["id"]
    condition_kind = unit.get("kind_of_condition") or unit.get("condition_kind") or "exact"
    expression = unit.get("expression", "")

    if condition_kind in ("fuzzy", "context"):
        if fuzzy_resolver is None:
            # Refusing is the right default: a judgement call with no judge
            # available should reach a person, not resolve to False.
            return UnitOutcome(
                unit_id, "conditional", verdict=False, strategy=condition_kind, source="missing",
                confidence=0.0, needs_human=True,
                reason=f"Condition '{unit_id}' needs judgement but no evaluator is configured.",
            )
        context = dict(env)
        if condition_kind == "context":
            context["_prior_verdicts"] = dict(result.condition_verdicts)
            context["_prior_values"] = dict(result.values)
        verdict, confidence, rationale = fuzzy_resolver(unit, context)
        threshold = unit.get("confidence_threshold", 0.7)
        return UnitOutcome(
            unit_id, "conditional", verdict=bool(verdict), strategy=condition_kind, source="llm",
            confidence=confidence, rationale=rationale,
            needs_human=confidence < threshold,
            reason=(f"Low confidence ({confidence:.0%}) on '{unit_id}'." if confidence < threshold else None),
        )

    inputs_used = {name: env.get(name) for name in _condition_identifiers(expression) if name in env}
    decision = evaluate_condition(expression, env)
    if decision.value is None:
        # Undecidable, not false. A condition that reads a field the source
        # record does not carry is a schema problem; resolving it to False here
        # deletes the section it governs and the letter goes out short of a
        # clause with nothing to show for it.
        detail = (
            f"needs {', '.join(decision.missing_fields)}, which this record does not provide"
            if decision.reason == "missing_input"
            else f"could not be parsed: {expression!r}"
        )
        return UnitOutcome(
            unit_id, "conditional", verdict=None, strategy="exact", source="missing",
            confidence=0.0, needs_human=True, inputs_used=inputs_used,
            reason=f"Condition '{unit_id}' {detail}.",
        )
    return UnitOutcome(
        unit_id, "conditional", verdict=decision.value, strategy="exact", source="source_record",
        inputs_used=inputs_used,
    )


def _resolve_narrative(unit: dict, env: dict, narrative_resolver) -> UnitOutcome:
    unit_id = unit["id"]
    if narrative_resolver is None:
        return UnitOutcome(
            unit_id, "narrative", strategy="rag", source="missing", confidence=0.0, needs_human=True,
            reason=f"Narrative unit '{unit_id}' needs retrieval, which is not configured for this run.",
        )
    text, confidence, rationale = narrative_resolver(unit, env)
    threshold = unit.get("grounding_threshold", 0.5)
    return UnitOutcome(
        unit_id, "narrative", value=text, strategy="rag", source="llm",
        confidence=confidence, rationale=rationale,
        # Poorly-grounded prose in a regulated report is exactly what review is for.
        needs_human=confidence < threshold,
        reason=(f"Grounding score {confidence:.0%} is below the threshold for '{unit_id}'." if confidence < threshold else None),
    )
