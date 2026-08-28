"""Formula evaluation, dependency ordering, and binding.

The failure modes here are all silent-and-severe: a formula that quietly
returns nothing, a computed value resolved before its inputs, a condition
variable that was never bindable. None of them raise on their own — they just
produce a confident, wrong document.
"""

import pytest

from app.generation.source_resolver import apply_binding, bindable_targets, suggest_bindings
from app.generation.rule_engine import formula_identifiers, safe_eval_formula
from app.generation.resolution_engine import CyclicDependencyError, resolve_manifest


# --------------------------------------------------------------------------- formulas
@pytest.mark.parametrize("expr,values,expected", [
    ("base + super", {"base": "82000", "super": "8200"}, 90200.0),   # Excel gives strings
    ("round(affected / total * 100, 1)", {"affected": "7", "total": "40"}, 17.5),
    ("max(a, b)", {"a": "3", "b": "9"}, 9.0),
    ("mean(a, b, c)", {"a": "1", "b": "2", "c": "3"}, 2.0),
    ("base * 1.1", {"base": 100}, 110.00000000000001),
])
def test_formulas_evaluate(expr, values, expected):
    result = safe_eval_formula(expr, values)
    assert result.ok, result.error
    assert result.value == pytest.approx(expected)


@pytest.mark.parametrize("expr,values,fragment", [
    ("a / b", {"a": "7", "b": "0"}, "division by zero"),
    ("a / b", {"a": "7"}, "no value"),
    ("a * 2", {"a": "n/a"}, "not a number"),
    ("a + ", {"a": "1"}, "Could not parse"),
])
def test_formula_failures_are_explicit(expr, values, fragment):
    """Never a silent blank and never a plausible wrong number — the caller
    needs to know it must route this to a human."""
    result = safe_eval_formula(expr, values)
    assert not result.ok
    assert fragment.lower() in result.error.lower()
    assert result.value is None


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwned')",
    "open('/etc/passwd').read()",
    "(lambda: 1)()",
    "eval('1+1')",
])
def test_formulas_refuse_arbitrary_code(expr):
    assert safe_eval_formula(expr, {}).ok is False


def test_formula_identifiers_excludes_function_names():
    assert formula_identifiers("round(affected / total * 100, 1)") == ["affected", "total"]


# --------------------------------------------------------------------------- ordering
def test_computed_units_resolve_after_their_inputs():
    manifest = {
        "fields": [
            {"id": "base"}, {"id": "super"},
            {"id": "total", "kind": "computed", "formula": "base + super"},
            # depends on another computed unit, so ordering must be transitive
            {"id": "pct", "kind": "computed", "formula": "round(super / total * 100, 1)"},
        ],
        "conditions": [],
    }
    result = resolve_manifest(manifest, {"base": "82000", "super": "8200"})
    assert result.values["total"] == "90200"
    assert result.values["pct"] == "9.1"
    assert not result.needs_review

    order = [o.unit_id for o in result.outcomes]
    assert order.index("total") < order.index("pct")


def test_circular_dependencies_fail_loudly():
    manifest = {"fields": [
        {"id": "a", "kind": "computed", "formula": "b + 1"},
        {"id": "b", "kind": "computed", "formula": "a + 1"},
    ]}
    with pytest.raises(CyclicDependencyError):
        resolve_manifest(manifest, {})


def test_uncomputable_value_parks_for_review_rather_than_blanking():
    manifest = {"fields": [
        {"id": "affected"}, {"id": "total"},
        {"id": "rate", "kind": "computed", "formula": "affected / total"},
    ]}
    result = resolve_manifest(manifest, {"affected": "7", "total": "0"})
    assert result.needs_review
    task = result.open_tasks[0]
    assert task["unit_id"] == "rate"
    assert task["kind"] == "calculation"
    assert "rate" not in result.values


def test_flagged_calculation_parks_even_when_it_computes_cleanly():
    manifest = {"fields": [
        {"id": "dose"}, {"id": "weight"},
        {"id": "mg_per_kg", "kind": "computed", "formula": "dose / weight", "requires_human_check": True},
    ]}
    result = resolve_manifest(manifest, {"dose": "500", "weight": "70"})
    assert result.values["mg_per_kg"]           # it did compute
    assert result.needs_review                   # and still needs sign-off


def test_judgement_condition_without_an_evaluator_escalates():
    """A fuzzy condition with nothing to judge it must reach a person, not
    quietly resolve to False and delete its block."""
    manifest = {"conditions": [
        {"id": "c_mgr", "condition_kind": "fuzzy", "expression": "role is managerial"},
    ]}
    result = resolve_manifest(manifest, {"role": "Head of QC"})
    assert result.needs_review
    assert result.condition_verdicts["c_mgr"] is False


def test_context_condition_sees_earlier_verdicts():
    seen = {}

    def judge(unit, context):
        seen.update(context.get("_prior_verdicts", {}))
        return True, 0.9, "because the earlier finding was adverse"

    manifest = {"conditions": [
        {"id": "c_first", "expression": "finding == 'adverse'"},
        {"id": "c_second", "condition_kind": "context", "expression": "conclusion should note the finding",
         "depends_on": ["c_first"]},
    ]}
    result = resolve_manifest(manifest, {"finding": "Adverse"}, fuzzy_resolver=judge)
    assert result.condition_verdicts["c_first"] is True
    assert seen.get("c_first") is True   # the later unit could see the earlier verdict


# --------------------------------------------------------------------------- bindings
HOSPIRA_SHAPE = {
    "fields": [
        {"id": "colleague_first_name", "type": "string", "slots": [{"kind": "blue_placeholder"}]},
        {"id": "lab_ft_salary_38_hr", "type": "currency",
         "slots": [{"kind": "mergefield", "code": "LAB__FT_SALARY__38_HR_"}]},
    ],
    "conditions": [{"id": "c_ft", "expression": "colleague_type == 'Full time'"}],
    "blocks": [],
}


def test_condition_variables_are_bindable():
    """`colleague_type` is referenced by every condition but is not a field —
    it has no placeholder anywhere in the document. A binding surface built
    only from manifest['fields'] leaves it unbound and every conditional block
    gets deleted."""
    ids = {t.field_id for t in bindable_targets(HOSPIRA_SHAPE)}
    assert "colleague_type" in ids
    assert {t.origin for t in bindable_targets(HOSPIRA_SHAPE) if t.field_id == "colleague_type"} == {"condition"}


def test_suggestions_match_columns_by_slug_and_mergefield_code():
    columns = ["Colleague Type", "Colleague First Name", "LAB__FT_SALARY__38_HR_", "Unrelated"]
    plan = suggest_bindings(HOSPIRA_SHAPE, columns)
    bindings = plan.as_field_bindings()
    assert bindings["colleague_first_name"] == "Colleague First Name"
    assert bindings["colleague_type"] == "Colleague Type"
    assert bindings["lab_ft_salary_38_hr"] == "LAB__FT_SALARY__38_HR_"
    assert plan.unused_columns == ["Unrelated"]


def test_a_column_is_never_bound_to_two_fields():
    columns = ["Name"]
    manifest = {"fields": [{"id": "name"}, {"id": "name_2"}], "conditions": []}
    plan = suggest_bindings(manifest, columns)
    used = [s.column for s in plan.suggestions if s.column]
    assert len(used) == len(set(used))


def test_value_map_closes_vocabulary_gaps():
    """Normalisation fixes casing; it cannot know that "FT" means "Full time"."""
    record = {"Colleague Type": "FT"}
    out = apply_binding(record, {"colleague_type": "Colleague Type"},
                        {"colleague_type": {"FT": "Full time"}})
    assert out["colleague_type"] == "Full time"
