"""Condition evaluation.

Every case here is anchored to a real failure mode, because a wrong verdict is
silent and severe: a condition that evaluates false *deletes* its blocks from
the document, so the output looks plausible while whole sections are missing.
"""

import pytest

from app.expressions.token_parser import safe_eval_condition as ev

# The three conditions the real Hospira template compiles to, paired with the
# values its real source spreadsheet actually contains. Before normalisation
# "Full Time" did not match 'Full time' and two of these three rows produced a
# letter with every conditional block stripped out.
HOSPIRA_CONDITIONS = [
    "colleague_type == 'Full time'",
    "colleague_type == 'Part time'",
    "colleague_type == 'Fixed Term'",
]


@pytest.mark.parametrize("excel_value,expected_index", [
    ("Full Time", 0),
    ("Part Time", 1),
    ("Fixed Term", 2),
])
def test_exactly_one_hospira_condition_matches(excel_value, expected_index):
    verdicts = [ev(expr, {"colleague_type": excel_value}) for expr in HOSPIRA_CONDITIONS]
    assert verdicts.count(True) == 1, f"{excel_value!r} matched {verdicts.count(True)} conditions"
    assert verdicts[expected_index] is True


@pytest.mark.parametrize("expr,facts,expected", [
    # Normalisation must not become over-matching.
    ("colleague_type == 'Full time'", {"colleague_type": "Part Time"}, False),
    ("colleague_type == 'Full time'", {}, False),
    ("colleague_type == 'Full time'", {"colleague_type": None}, False),
    ("colleague_type != 'Full time'", {"colleague_type": "FULL TIME"}, False),
    ("region == 'EU'", {"region": "  eu  "}, True),
    ("region == 'EU'", {"region": "EUR"}, False),
])
def test_equality_normalisation(expr, facts, expected):
    assert ev(expr, facts) is expected


@pytest.mark.parametrize("expr,facts,expected", [
    # Spreadsheet cells arrive as strings; manifests are authored with numbers.
    ("hours >= 38", {"hours": "38"}, True),
    ("hours >= 38", {"hours": "24"}, False),
    ("salary > 50000", {"salary": "95,000"}, True),
    ("salary > 50000", {"salary": 95000}, True),
    ("salary > 50000", {"salary": "not a number"}, False),
    ("hours > 10", {}, False),  # missing field must not raise
])
def test_numeric_coercion(expr, facts, expected):
    assert ev(expr, facts) is expected


@pytest.mark.parametrize("expr,facts,expected", [
    # Role-driven paragraph selection, stated deterministically.
    ("role in ['Manager', 'Director']", {"role": "manager"}, True),
    ("role in ['Manager', 'Director']", {"role": "Analyst"}, False),
    ("role not in ['Manager']", {"role": "Analyst"}, True),
    ("role in ['Manager']", {}, False),
])
def test_membership(expr, facts, expected):
    assert ev(expr, facts) is expected


@pytest.mark.parametrize("expr,facts,expected", [
    ("a == 'x' and b == 'y'", {"a": "X", "b": "Y"}, True),
    ("a == 'x' and b == 'y'", {"a": "X", "b": "z"}, False),
    ("a == 'x' or b == 'y'", {"a": "no", "b": "Y"}, True),
])
def test_boolean_operators(expr, facts, expected):
    assert ev(expr, facts) is expected


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwned')",
    "open('/etc/passwd').read()",
    "(lambda: 1)()",
    "[x for x in range(10)]",
    "not a syntax tree at all ((",
])
def test_unsupported_expressions_are_refused(expr):
    """The evaluator walks a whitelisted AST rather than calling eval(); anything
    outside that whitelist must resolve false instead of executing."""
    assert ev(expr, {"a": "x"}) is False


# --------------------------------------------- template-dialect expressions
@pytest.mark.parametrize("expr,record,expected", [
    # A single `=` is an assignment to ast.parse, so this raised SyntaxError and
    # evaluated False -- deleting the block from every document, silently.
    ("joining_bonus = 0 AND retention_bonus = 0", {"joining_bonus": "0", "retention_bonus": "0"}, True),
    ("joining_bonus = 0 AND retention_bonus = 0", {"joining_bonus": "100000", "retention_bonus": "0"}, False),
    # `blank` read as an undefined name.
    ("address_line2 is not blank", {"address_line2": "Near City Centre Mall"}, True),
    ("address_line2 is not blank", {"address_line2": ""}, False),
    ("address_line2 is blank", {"address_line2": ""}, True),
    ('relocation_applicable = "Yes"', {"relocation_applicable": "Yes"}, True),
    ('employment_type = "Permanent" AND probation_months > 0',
     {"employment_type": "Permanent", "probation_months": "3"}, True),
    ("variable_pay_percent > 0 OR joining_bonus > 0", {"variable_pay_percent": "0", "joining_bonus": "5"}, True),
    # `not` is what a delete-when-true instruction compiles to.
    ("not (joining_bonus = 0 AND esop_units = 0)", {"joining_bonus": "100000", "esop_units": "0"}, True),
    ("not (joining_bonus = 0 AND esop_units = 0)", {"joining_bonus": "0", "esop_units": "0"}, False),
])
def test_a_templates_own_condition_syntax_is_understood(expr, record, expected):
    """Template authors write SQL/Excel-flavoured conditions, not Python. Every
    form here appeared verbatim in a real compensation template."""
    from app.expressions.token_parser import safe_eval_condition

    assert safe_eval_condition(expr, record) is expected


def test_python_expressions_are_unchanged_by_normalisation():
    """The Hospira manifest is already Python; normalisation must not touch it."""
    from app.expressions.token_parser import normalise_expression

    for expr in ("colleague_type == 'Full time'", "hours >= 38", "a != b", "x <= 3 and y >= 1"):
        assert normalise_expression(expr) == expr


# ------------------------------------- what the LLM compiler may emit as a rule

def test_prose_is_not_accepted_as_an_expression():
    """Asked for an expression, a model sometimes returns the instruction it was
    reading: "For Permanent transfers:" rather than `transfer_type ==
    'Permanent'`. Same model, same template, different run.

    Storing that produces a manifest nobody can approve -- every condition fails
    pre-lock validation, and the reviewer discovers it by pressing Approve.
    """
    from app.compiler.llm_compiler import _expression_is_executable

    for prose in (
        "For Permanent transfers:",
        "For transfers with salary change:",
        "Include this paragraph only if the transfer is temporary",
        "",
        "   ",
    ):
        assert _expression_is_executable(prose) is False, prose


def test_a_real_rule_is_accepted():
    from app.compiler.llm_compiler import _expression_is_executable

    for rule in (
        "transfer_type == 'Permanent'",
        "salary_change == 'Yes'",
        "assignment_type == 'TEMPORARY' and transfer_end_date != ''",
        "joining_bonus > 0",
    ):
        assert _expression_is_executable(rule) is True, rule


def test_a_condition_that_reads_nothing_is_rejected():
    """`1 == 1` parses perfectly and is not a condition: its verdict can never
    change, so it is a constant wearing a condition's clothes. A block governed
    by one is unconditional, which is the failure §11 warns about -- every
    letter carrying every optional clause."""
    from app.compiler.llm_compiler import _expression_is_executable

    assert _expression_is_executable("1 == 1") is False
    assert _expression_is_executable("'x' == 'x'") is False


# ------------------------------------------------- a condition named after its input

def test_a_condition_named_after_a_column_it_reads_does_not_depend_on_itself():
    """The China letter, which failed every row with an error about internals.

    `<Work Location/City Location>` compiles to two conditions, one per branch,
    and the natural name for each is the identifier that decides it. So the
    condition `city_location` has the expression
    `city_location != '' and city_location != work_location`: the id names the
    branch, the identifier names the source column, and they are the same word.

    Reading that identifier as a unit reference made the condition its own
    dependency, and every row failed with "Circular dependency between manifest
    units: city_location -> city_location" -- for a manifest that was correct.
    """
    from app.generation.resolution_engine import _topological_order, _unit_dependencies

    manifest = {
        "fields": [{"id": "date", "type": "date"}],
        "conditions": [
            {"id": "city_location",
             "expression": "city_location != '' and city_location != work_location",
             "keeps_blocks": ["blk_0_city_location"]},
            {"id": "work_location",
             "expression": "city_location == '' or city_location == work_location",
             "keeps_blocks": ["blk_1_work_location"]},
        ],
    }

    units, deps = _unit_dependencies(manifest)
    assert deps["city_location"] == set()
    assert deps["work_location"] == set()
    # And it orders, rather than raising.
    assert set(_topological_order(units, deps)) == {"date", "city_location", "work_location"}


def test_an_identifier_naming_a_computed_field_is_still_a_dependency():
    """The fix must not cost the ordering it exists to provide: a condition that
    reads a computed field has to be evaluated after that field."""
    from app.generation.resolution_engine import _topological_order, _unit_dependencies

    manifest = {
        "fields": [
            {"id": "pro_rata", "kind": "computed", "formula": "salary * fraction"},
            {"id": "salary", "type": "number"},
        ],
        "conditions": [{"id": "bonus_block", "expression": "pro_rata > 1000"}],
    }
    units, deps = _unit_dependencies(manifest)
    assert deps["bonus_block"] == {"pro_rata"}
    order = _topological_order(units, deps)
    assert order.index("pro_rata") < order.index("bonus_block")


def test_depends_on_still_orders_one_condition_after_another():
    """Expression identifiers no longer reach conditions, so `depends_on` is the
    only way to say it -- which is what it was always for."""
    from app.generation.resolution_engine import _topological_order, _unit_dependencies

    manifest = {
        "fields": [],
        "conditions": [
            {"id": "first", "expression": "region == 'CN'"},
            {"id": "second", "expression": "region != ''", "depends_on": ["first"]},
        ],
    }
    units, deps = _unit_dependencies(manifest)
    assert deps["second"] == {"first"}
    assert _topological_order(units, deps).index("first") < \
        _topological_order(units, deps).index("second")


def test_a_condition_cannot_depend_on_itself_even_when_asked_to():
    """`depends_on: [its own id]` is a manifest nobody can satisfy. Dropping it
    is better than refusing to generate: the intent is unambiguous and there is
    nothing an operator could do with the error."""
    from app.generation.resolution_engine import _topological_order, _unit_dependencies

    manifest = {
        "fields": [],
        "conditions": [{"id": "loop", "expression": "x == 1", "depends_on": ["loop"]}],
    }
    units, deps = _unit_dependencies(manifest)
    assert deps["loop"] == set()
    assert _topological_order(units, deps) == ["loop"]


def test_a_genuine_cycle_is_still_refused():
    """The guard has to keep working. Two computed fields feeding each other
    cannot be resolved in any order, and silently picking one would produce a
    document filled from a value that was never computed."""
    import pytest as _pytest

    from app.generation.resolution_engine import (
        CyclicDependencyError, _topological_order, _unit_dependencies,
    )

    manifest = {
        "fields": [
            {"id": "a", "kind": "computed", "formula": "b + 1"},
            {"id": "b", "kind": "computed", "formula": "a + 1"},
        ],
        "conditions": [],
    }
    units, deps = _unit_dependencies(manifest)
    with _pytest.raises(CyclicDependencyError, match="Circular dependency"):
        _topological_order(units, deps)
