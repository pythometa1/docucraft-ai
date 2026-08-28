"""The pinned expression language, and the six properties §7 asks of it.

A condition is the one part of a manifest that can delete text from a legally
binding letter, so every case here is anchored to a way that deletion could
happen without anyone deciding it: a rule that hashes differently because
someone retyped its brackets, a comparison against a column that no longer
exists, an expression too expensive to finish, or a sentence on the approval
screen that no longer says what the rule does.
"""

import pytest

from app.expressions.language import (
    BLOCK_MISSING_INPUT,
    BUDGET_EXCEEDED,
    CONDITION_NOT_BOOLEAN,
    DEFAULT_BUDGET_OPS,
    INCOMPATIBLE_COMPARISON,
    KEEP,
    LANGUAGE_ID,
    MAX_DEPTH,
    MEMBERSHIP_NEEDS_LIST,
    ORDERING_NOT_DEFINED,
    REMOVE_BLOCK,
    TYPES,
    UNKNOWN_FIELD,
    UNPARSEABLE,
    ExpressionSyntaxError,
    canonical_json,
    estimated_cost,
    evaluate,
    expression_hash,
    parse,
    run_test_cases,
    typecheck,
    unparse,
    verdict_expectation,
)
from app.expressions.plain_english import (
    describe,
    humanise_field,
    to_approval_sentence,
    to_plain_english,
)
from app.expressions.token_parser import evaluate_condition
from app.models import TemplateManifest

# Expressions that exist in the corpus this system already runs: the three
# Hospira colleague-type conditions, the two dialects template authors write in,
# and §7's own worked example.
CORPUS = [
    "colleague_type == 'Full time'",
    "colleague_type == 'Fixed Term'",
    "assignment_type == 'TEMPORARY' and transfer_end_date != ''",
    "assignment_type = 'TEMPORARY' AND transfer_end_date is not blank",
    "address_line2 is not blank",
    "joining_bonus == 0 and esop_units == 0",
    "role in ['Manager', 'Director']",
    "region in ('EU', 'UK')",
    "scheduled_weekly_hours >= 38",
    "not (probation_months == 0)",
    "18 <= age < 65",
    "colleague_type == 'Full time' and (region == 'EU' or region == 'UK')",
]

SCHEMA = {
    "assignment_type": "string",
    "transfer_end_date": "date",
    "colleague_type": "string",
    "scheduled_weekly_hours": "number",
    "salary": "number",
    "hire_date": "date",
    "is_manager": "boolean",
    "address_line2": "string",
    "region": "string",
}


# ------------------------------------------------------- the language is named

def test_the_language_id_is_the_one_manifests_record():
    """A manifest that records a dialect nobody named cannot be replayed.

    `TemplateManifest.expression_lang` pins the dialect a rule was approved
    under so a later language cannot silently reinterpret it. That guarantee is
    only worth something while the string in the column and the language that
    parses it are the same string.
    """
    assert LANGUAGE_ID == "documind-expr/1.0"
    assert TemplateManifest.__table__.c.expression_lang.default.arg == LANGUAGE_ID


# ------------------------------------------------------------ total by construction

@pytest.mark.parametrize("expression,fragment", [
    ("len(name) > 2", "function calls"),
    ("source.assignment_type == 'TEMPORARY'", "attribute traversal"),
    ("benefits[0] == 'car'", "indexing"),
    ("[b for b in benefits] == []", "comprehensions"),
    ("salary + bonus > 1000", "arithmetic"),
    ("salary > 1000 if senior else salary > 500", "conditional expressions"),
    ("salary > -1", "unary operator"),
])
def test_the_language_refuses_what_would_make_it_unbounded(expression, fragment):
    """§7's first requirement: total -- no loops, no recursion, no I/O, no
    imports, no attribute traversal.

    Every one of these already fails inside `_eval_node`, which raises and is
    caught as an undecided verdict -- during a batch, per record, with the
    document blocked. Rejecting them at parse time moves that discovery to
    compile time, which is the whole reason §7 asks for a typed language.
    """
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse(expression)
    assert caught.value.code == "unsupported"
    assert fragment in str(caught.value)


def test_nesting_deeper_than_the_limit_is_refused_rather_than_recursed():
    """A recursion error in the compiler is a 500 on an upload, not a diagnosis."""
    expression = "not " * (MAX_DEPTH + 5) + "(a == 1)"
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse(expression)
    assert caught.value.code == "too_deep"


def test_text_that_is_not_an_expression_is_a_syntax_error_not_a_crash():
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse("this is not (an expression")
    assert caught.value.code == "syntax"


# ------------------------------------------------------------------ serialisable

@pytest.mark.parametrize("expression", CORPUS)
def test_an_expression_round_trips_through_its_serialised_ast(expression):
    """§7: serialisable to and from an AST, so an expression can be hashed,
    versioned and diffed between manifest versions.

    Round-tripping is what makes the stored AST authoritative. If `unparse`
    could produce text that parses to something else, a manifest would show an
    approver one rule and run another.
    """
    tree = parse(expression)
    assert parse(unparse(tree)) == tree


@pytest.mark.parametrize("expression", CORPUS)
def test_the_serialised_ast_is_json_and_nothing_but_json(expression):
    """It is stored in a JSON column and hashed as bytes, so no AST object,
    no tuple and no set may survive the parse."""
    import json

    tree = parse(expression)
    assert json.loads(canonical_json(tree)) == tree


def test_the_ast_names_fields_and_literals_separately():
    """The shape the compiler and the approval screen both read. A rule whose
    field names cannot be picked out of the AST cannot have its required source
    fields computed, and §6 wants that closure computed rather than typed."""
    assert parse("colleague_type == 'Full time'") == {
        "node": "compare",
        "left": {"node": "field", "name": "colleague_type"},
        "ops": [{"op": "==", "right": {"node": "literal", "value": "Full time"}}],
    }


def test_containers_canonicalise_so_a_retyped_bracket_is_not_a_changed_rule():
    assert parse("region in ('EU', 'UK')") == parse("region in ['EU', 'UK']")
    assert parse("region in {'EU', 'UK'}") == parse("region in ['EU', 'UK']")


# ----------------------------------------------------------------------- hashing

def test_the_two_dialects_authors_write_in_hash_identically():
    """`joining_bonus = 0 AND esop_units = 0` and its canonical spelling are the
    same rule. Hashing the text rather than the AST would report a manifest diff
    every time an author's punctuation changed, and a diff nobody trusts is a
    diff nobody reads."""
    assert expression_hash("joining_bonus = 0 AND esop_units = 0") == expression_hash(
        "joining_bonus == 0 and esop_units == 0"
    )
    assert expression_hash("address_line2 is not blank") == expression_hash("address_line2 != ''")
    assert expression_hash("region in ('EU', 'UK')") == expression_hash("region in ['EU', 'UK']")


def test_a_changed_meaning_changes_the_hash():
    left = expression_hash("colleague_type == 'Full time'")
    assert left != expression_hash("colleague_type != 'Full time'")
    assert left != expression_hash("colleague_type == 'Part time'")
    assert left != expression_hash("employment_type == 'Full time'")
    # Casefolding is an evaluator behaviour, not an identity: an approver reads
    # two different sentences and signs one of them.
    assert left != expression_hash("colleague_type == 'FULL TIME'")


def test_the_hash_is_pinned_so_the_canonical_form_cannot_drift_silently():
    """A stored `manifest_hash` is only reproducible while the canonical form is
    stable. Changing the AST shape or the hash preimage without changing
    `LANGUAGE_ID` would break every already-locked manifest's replay, and this
    is the test that says so out loud.
    """
    assert expression_hash("assignment_type == 'TEMPORARY' and transfer_end_date is not blank") == (
        "a0c50ea946b23312c7e8c0e2e9490abbc4c3b5b2dc1b5d5fa4da7d2ca923e67e"
    )


# ------------------------------------------------------------------- static types

@pytest.mark.parametrize("expression", CORPUS)
def test_the_running_corpus_typechecks_against_its_schema(expression):
    """A checker that rejects expressions the system already runs correctly gets
    switched off, so the corpus is the floor it has to clear."""
    schema = dict(SCHEMA, joining_bonus="number", esop_units="number",
                  role="string", probation_months="number", age="number")
    assert typecheck(expression, schema) == []


def test_a_field_the_schema_does_not_declare_is_reported_once():
    """The §19 register's second most likely failure: a column renamed upstream.

    It reads as null for every row, so the batch blocks on every document rather
    than one -- and it is invisible until then, because the expression parses,
    stores and approves cleanly.
    """
    issues = typecheck("cost_centre == 'A' or cost_centre == 'B'", SCHEMA)
    assert [i.code for i in issues] == [UNKNOWN_FIELD]
    assert issues[0].field_name == "cost_centre"


def test_an_unknown_field_does_not_cascade_into_type_errors():
    """One missing column should produce one finding, not one per comparison."""
    issues = typecheck("cost_centre >= 5", SCHEMA)
    assert [i.code for i in issues] == [UNKNOWN_FIELD]


@pytest.mark.parametrize("expression,code", [
    ("colleague_type >= 5", INCOMPATIBLE_COMPARISON),
    ("is_manager == 'yes'", INCOMPATIBLE_COMPARISON),
    ("hire_date == True", INCOMPATIBLE_COMPARISON),
    ("salary == 'senior'", INCOMPATIBLE_COMPARISON),
    ("colleague_type > 'M'", ORDERING_NOT_DEFINED),
    ("region in colleague_type", MEMBERSHIP_NEEDS_LIST),
    ("region in []", MEMBERSHIP_NEEDS_LIST),
])
def test_incompatible_comparisons_are_caught_at_compile_time(expression, code):
    """§7: "statically typed against the pinned source schema, so failures
    surface at compile time rather than during a production batch".

    None of these raises at runtime. `_compare` answers false, the condition
    deletes the block it governs, and every letter in the batch goes out short a
    paragraph that nobody asked to remove.
    """
    assert [i.code for i in typecheck(expression, SCHEMA)] == [code]


@pytest.mark.parametrize("declared", TYPES)
def test_the_blank_test_typechecks_against_every_declared_type(declared):
    """`x is not blank` normalises to `x != ''` whatever x is.

    It is the most common condition in the corpus, and a checker that called it
    a type error on a date column would be wrong about the one expression it
    would be asked about most.
    """
    assert typecheck("transfer_end_date is not blank", {"transfer_end_date": declared}) == []


@pytest.mark.parametrize("expression", [
    "hire_date >= '2026-01-01'",          # dates are written as ISO text
    "scheduled_weekly_hours >= '38'",     # spreadsheet cells arrive as text
])
def test_the_checker_allows_exactly_the_coercions_the_evaluator_performs(expression):
    """A checker stricter than the evaluator rejects rules that demonstrably
    work, and the first person to hit one will stop believing the rest."""
    assert typecheck(expression, SCHEMA) == []


def test_a_condition_that_is_not_a_test_is_reported():
    issues = typecheck("address_line2", SCHEMA)
    assert [i.code for i in issues] == [CONDITION_NOT_BOOLEAN]


def test_an_unparseable_expression_typechecks_to_one_finding_not_an_exception():
    """The compiler typechecks whatever the template produced, including the
    text it failed to compile. Raising here would take out the whole review
    screen instead of flagging one condition."""
    issues = typecheck("this is not (an expression", SCHEMA)
    assert [i.code for i in issues] == [UNPARSEABLE]


def test_a_schema_declaring_a_type_that_does_not_exist_raises():
    """Silently treating `varchar` as "no information" would disable type
    checking for that column and report the expression clean."""
    with pytest.raises(ValueError) as caught:
        typecheck("colleague_type == 'x'", {"colleague_type": "varchar"})
    assert "varchar" in str(caught.value)


# ------------------------------------------------------- evaluation and its budget

@pytest.mark.parametrize("expression", CORPUS)
@pytest.mark.parametrize("record", [
    {},
    {"colleague_type": "Full Time", "assignment_type": "TEMPORARY", "transfer_end_date": "2026-11-30",
     "address_line2": "Flat 2", "joining_bonus": 0, "esop_units": 0, "role": "Manager",
     "region": "EU", "scheduled_weekly_hours": "38", "probation_months": 3, "age": 40},
    {"colleague_type": "Part Time", "assignment_type": "PERMANENT", "transfer_end_date": "",
     "address_line2": "", "joining_bonus": 5000, "esop_units": 10, "role": "Analyst",
     "region": "US", "scheduled_weekly_hours": "20", "probation_months": 0, "age": 17},
])
def test_evaluation_is_the_same_verdict_the_generator_already_produces(expression, record):
    """One evaluator, not two.

    The three-state verdict is only safe while every caller gets the same answer
    for the same record. A second implementation that agreed with the first
    everywhere except one edge would be discovered as a letter with a missing
    clause, months later, by the employee it was sent to.
    """
    assert evaluate(expression, record) == evaluate_condition(expression, record)


def test_an_expression_over_budget_is_undecided_and_never_false():
    """§7: "sandboxed with a per-expression evaluation budget, so a pathological
    expression cannot stall a batch".

    Undecided is the only safe refusal. Reporting false would delete the block
    the condition governs from the letter -- so an expression the system
    declined to run would silently shorten the document instead of stopping it.
    """
    record = {"region": "EU", "approved_regions": [f"R{i}" for i in range(50_000)]}
    verdict = evaluate("region in approved_regions", record)
    assert verdict.value is None
    assert verdict.reason == BUDGET_EXCEEDED
    with pytest.raises(TypeError):
        bool(verdict)


def test_the_same_expression_decides_when_the_budget_allows_it():
    """The budget must bound cost, not capability: raise it and the expression
    still answers, so nothing is quietly unsupported."""
    record = {"region": "EU", "approved_regions": [f"R{i}" for i in range(50_000)] + ["EU"]}
    assert evaluate("region in approved_regions", record, budget_ops=200_000).value is True


def test_an_ordinary_condition_costs_a_fraction_of_the_default_budget():
    """The budget has to be invisible to real rules or it will be raised until
    it is meaningless."""
    record = {"assignment_type": "TEMPORARY", "transfer_end_date": "2026-11-30"}
    expression = "assignment_type == 'TEMPORARY' and transfer_end_date != ''"
    assert estimated_cost(expression, record) < DEFAULT_BUDGET_OPS // 100
    assert evaluate(expression, record, budget_ops=DEFAULT_BUDGET_OPS).value is True


def test_an_expression_too_deep_to_bound_is_undecided_rather_than_evaluated():
    """If the cost cannot be bounded the expression does not run. Anything else
    makes the budget advisory."""
    verdict = evaluate("not " * (MAX_DEPTH + 5) + "(a == 1)", {"a": 1})
    assert verdict.value is None
    assert verdict.reason == BUDGET_EXCEEDED


def test_a_budget_of_zero_is_a_programming_error_not_a_verdict():
    with pytest.raises(ValueError):
        evaluate("a == 1", {"a": 1}, budget_ops=0)


def test_evaluation_is_deterministic_over_repeated_runs():
    """§7's second requirement. Identical inputs, identical verdict -- which is
    what makes a document reproducible from (template_hash, manifest_hash) plus
    the record, and the §17 audit trail defensible rather than decorative."""
    record = {"colleague_type": "Full Time", "region": "  eu  "}
    expression = "colleague_type == 'Full time' and region == 'EU'"
    assert len({evaluate(expression, record) for _ in range(25)}) == 1


# -------------------------------------------------------------------- test cases

WORKED_EXAMPLE = "assignment_type == 'TEMPORARY' and transfer_end_date != ''"


def test_the_worked_examples_three_cases_pass():
    """§7's own worked example, in this dialect's spelling of it.

    The third case is the one that matters: a temporary assignment with no end
    date "is not a false condition -- it is incomplete source data, and the
    correct behaviour is to block the document rather than quietly drop a
    paragraph the employee was entitled to see".
    """
    report = run_test_cases(WORKED_EXAMPLE, [
        {"in": {"assignment_type": "TEMPORARY", "transfer_end_date": "2026-11-30"}, "expect": KEEP},
        {"in": {"assignment_type": "PERMANENT", "transfer_end_date": ""}, "expect": REMOVE_BLOCK},
        {"in": {"assignment_type": "TEMPORARY", "transfer_end_date": None}, "expect": BLOCK_MISSING_INPUT},
    ])
    assert report.all_passed
    assert len(report.passed) == 3
    assert report.as_dict()["all_passed"] is True


def test_a_column_missing_from_the_record_blocks_rather_than_removing():
    """Where this dialect deliberately differs from the record's CEL sketch.

    CEL gets a short circuit from `&&`, so `has(transfer_end_date)` answers
    false for a permanent assignment whose record has no end-date column at all.
    Here presence is a property of the whole expression, so that record is
    undecided -- which is what §7's own missing/null/empty table asks for
    ("field absent from the record -> schema violation -> block the document").
    It blocks more documents than CEL would, and never removes a paragraph on
    the strength of a column that was never delivered.
    """
    report = run_test_cases(WORKED_EXAMPLE, [
        {"in": {"assignment_type": "PERMANENT"}, "expect": REMOVE_BLOCK},
    ])
    assert not report.all_passed
    failed = report.failed[0]
    assert failed.actual == BLOCK_MISSING_INPUT
    assert failed.reason == "missing_input"
    assert failed.missing_fields == ("transfer_end_date",)


def test_a_failing_case_is_reported_rather_than_raised():
    """These run inside compilation and approval. One wrong case must mark one
    condition unapprovable, not abort the review of the other forty."""
    report = run_test_cases("colleague_type == 'Full time'", [
        {"in": {"colleague_type": "Full Time"}, "expect": KEEP},
        {"in": {"colleague_type": "Part Time"}, "expect": KEEP},
    ])
    assert [r.passed for r in report.results] == [True, False]
    assert report.failed[0].index == 1
    assert report.failed[0].actual == REMOVE_BLOCK


@pytest.mark.parametrize("cases,fragment", [
    ([], "no test cases"),
    ([{"in": {"a": 1}, "expect": "TRUE"}], "expects 'TRUE'"),
    ([{"in": {"a": 1}}], "expects None"),
    ([{"expect": KEEP}], "no `in` record"),
    (["KEEP"], "not a mapping"),
])
def test_a_malformed_case_raises_instead_of_being_skipped(cases, fragment):
    """§6 will not lock a manifest whose expressions have no passing test case.
    A suite that silently drops the case it could not read reports green for a
    rule nobody tested, which is the failure the requirement exists to prevent.
    """
    with pytest.raises(ValueError) as caught:
        run_test_cases("a == 1", cases)
    assert fragment in str(caught.value)


def test_verdict_expectation_maps_undecided_to_block_not_remove():
    assert verdict_expectation(evaluate("a == 1", {"a": 1})) == KEEP
    assert verdict_expectation(evaluate("a == 1", {"a": 2})) == REMOVE_BLOCK
    assert verdict_expectation(evaluate("a == 1", {})) == BLOCK_MISSING_INPUT


# ------------------------------------------------------------------ plain English

@pytest.mark.parametrize("expression,sentence", [
    ("assignment_type == 'TEMPORARY' and transfer_end_date != ''",
     "the assignment type is TEMPORARY and the transfer end date is not empty"),
    ("assignment_type = 'TEMPORARY' AND transfer_end_date is not blank",
     "the assignment type is TEMPORARY and the transfer end date is not empty"),
    ("address_line2 is blank", "the address line 2 is empty"),
    ("colleague_type == 'Full time'", "the colleague type is Full time"),
    ("role in ['Manager', 'Director', 'VP']", "the role is one of Manager, Director or VP"),
    ("region not in ['EU']", "the region is none of EU"),
    ("scheduled_weekly_hours >= 38", "the scheduled weekly hours is at least 38"),
    ("salary > 50000", "the salary is more than 50000"),
    ("probation_months < 6", "the probation months is less than 6"),
    ("age <= 65", "the age is at most 65"),
    ("is_manager == True", "the manager flag is true"),
    ("not (probation_months == 0)", "it is not the case that the probation months is 0"),
    ("18 <= age < 65", "18 is at most the age and the age is less than 65"),
])
def test_an_expression_reads_as_the_sentence_an_approver_signs(expression, sentence):
    """§7: "renderable back into plain English for the approval screen -- an
    approver signs the meaning, not the syntax".

    The approval queue is where a wrong rule is supposed to be caught, by an
    operations manager who knows the business and does not read Python. A screen
    showing `colleague_type == 'Fixed Term'` gets skimmed, and the sign-off then
    records a review that never happened.
    """
    assert to_plain_english(expression) == sentence


def test_a_nested_alternative_keeps_its_brackets_in_english():
    """English has no operator precedence to lean on, and "A and B or C" is a
    different rule from the one that was written."""
    assert to_plain_english("region == 'EU' and (role == 'Manager' or role == 'Director')") == (
        "the region is EU and (the role is Manager or the role is Director)"
    )


@pytest.mark.parametrize("name,words", [
    ("assignment_type", "the assignment type"),
    ("transfer_end_date", "the transfer end date"),
    ("address_line2", "the address line 2"),
    ("transferEndDate", "the transfer end date"),
    ("is_manager", "the manager flag"),
    ("has_company_car", "the company car flag"),
    ("HRBP_name", "the HRBP name"),
])
def test_field_names_read_as_words(name, words):
    assert humanise_field(name) == words


def test_the_sentence_is_rendered_from_the_ast_the_evaluator_runs():
    """A `plain_english` string stored beside its expression drifts the first
    time the expression is edited, and a drifted description is worse than none:
    it is a false statement of what was approved. Rendering from the AST makes
    that impossible by construction."""
    expression = "colleague_type == 'Full time'"
    assert describe(parse(expression)) == to_plain_english(expression)


def test_the_approval_sentence_names_what_happens_when_input_is_missing():
    """The true and the false case are the two an approver imagines. The third
    one -- the record that does not answer the question -- is the one that stops
    the letter, so the screen has to say it."""
    sentence = to_approval_sentence(WORKED_EXAMPLE)
    assert sentence.startswith("Keep this block when the assignment type is TEMPORARY")
    assert "otherwise it is removed" in sentence
    assert "the assignment type or the transfer end date" in sentence
    assert "the document is blocked" in sentence


def test_the_approval_sentence_follows_the_declared_outcomes():
    sentence = to_approval_sentence(
        "colleague_type == 'Fixed Term'", on_true=REMOVE_BLOCK, on_false=KEEP,
    )
    assert sentence.startswith("Remove this block when the colleague type is Fixed Term")
    assert "otherwise it is kept" in sentence


def test_a_condition_whose_outcomes_are_identical_is_refused():
    with pytest.raises(ValueError):
        to_approval_sentence("a == 1", on_true=KEEP, on_false=KEEP)


def test_an_expression_that_cannot_be_explained_raises():
    """An expression nobody can put into a sentence is not one anybody should
    approve, and `manifests.validator` already refuses to approve a condition
    that does not parse. Returning the raw syntax instead would put the approver
    back in front of exactly the thing they cannot check."""
    with pytest.raises(ExpressionSyntaxError):
        to_plain_english("source.assignment_type == 'TEMPORARY'")
