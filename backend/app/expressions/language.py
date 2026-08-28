"""The expression dialect this system actually runs, pinned as a language.

§7 of the architecture record makes conditions the load-bearing part of a
manifest and then says the requirement is empty until the language is named:
free-form Python "is unbounded, unsafe to execute on tenant data, impossible to
diff meaningfully, and unreadable to the business approver who has to sign off
on what the rule means".

The record recommends CEL and, in the same breath, flags that Python binding
maturity has to be verified before committing to it. That verification has not
happened, and acquiring a new runtime dependency for the rule engine of a
legally binding document pipeline is not a decision to take quietly. So this
module takes the other route the record leaves open -- the restricted-AST
allow-list, whose cost the record states plainly ("you own the entire safety
surface") -- and pays that cost explicitly: it names the dialect
`documind-expr/1.0` and gives it the six properties §7 demands of whichever
language wins.

  Total          `parse` rejects calls, attribute traversal, indexing,
                 comprehensions, arithmetic and lambdas outright, so an
                 unsupported construct fails at compile time instead of meeting
                 the evaluator halfway through a production batch.
  Deterministic  the only inputs are the expression and the record. No now(),
                 no random, no locale-dependent parsing.
  Statically typed against the pinned source schema (`typecheck`), so a rule
                 that compares a date to a boolean is caught when it is
                 compiled rather than one row at a time.
  Serialisable   `parse`/`unparse` round-trip through plain dicts, which is what
                 lets an expression be hashed (`expression_hash`), versioned and
                 diffed between manifest versions.
  Renderable     back into plain English -- see `plain_english.py`; an approver
                 signs the meaning, not the syntax.
  Budgeted       `evaluate` refuses an expression whose cost exceeds
                 `budget_ops` rather than letting it stall a batch.

What this module is deliberately not is a second evaluator. `evaluate`
delegates to `token_parser.evaluate_condition` and hands back its three-state
`ConditionVerdict` unchanged, because the entire value of the third state --
undecided is not false, and a false condition *deletes* paragraphs from a legal
letter -- evaporates the moment two evaluators can disagree about a record.

One deliberate difference from the record's worked example: this dialect has no
`has()`. Presence is a property of the whole expression here, so a referenced
field that is absent or null makes the expression undecided rather than false.
That is what §7's own missing/null/empty table asks for ("field absent from the
record -> schema violation -> block the document"); what it loses is the short
circuit CEL gets from `&&`, so `assignment_type == 'PERMANENT' and
transfer_end_date != ''` blocks on a record with no `transfer_end_date` column
at all instead of quietly answering false. A column that exists and is empty is
a real value and compares normally -- `x is not blank` normalises to `x != ''`
-- so only true absence blocks.
"""

import ast
import hashlib
import json
from dataclasses import dataclass
from datetime import date

from app.expressions.token_parser import (
    ConditionVerdict,
    _as_number,
    evaluate_condition,
    normalise_expression,
)

#: Written into `TemplateManifest.expression_lang` so a later dialect cannot
#: silently reinterpret a rule that was approved under this one.
LANGUAGE_ID = "documind-expr/1.0"

#: Ops one expression may spend before `evaluate` refuses to run it at all.
DEFAULT_BUDGET_OPS = 10_000

#: How deep an expression may nest. Generous next to anything a compiler emits
#: (a three-clause condition nests four levels), and low enough that neither the
#: cost walker nor the evaluator can be driven into a RecursionError.
MAX_DEPTH = 64

#: The declared source-schema types `typecheck` reasons about.
TYPES = ("string", "number", "date", "boolean")
_ANY_TYPE = frozenset(TYPES)

#: Reason this module adds to `ConditionVerdict`'s vocabulary, alongside
#: token_parser's evaluated | missing_input | unparseable | supplied. It is an
#: *undecided* verdict: an expression too expensive to run has not been answered
#: in the negative, and treating it as false would delete the block it governs.
BUDGET_EXCEEDED = "budget_exceeded"


class ExpressionSyntaxError(ValueError):
    """Raised when text is not a `documind-expr/1.0` expression.

    `code` says which rule it broke -- `syntax` (not an expression at all),
    `unsupported` (a construct the language excludes on purpose) or `too_deep`
    -- because a compiler reporting to a human needs to distinguish "you typed
    this wrongly" from "we will not run this".
    """

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class _TooDeep(Exception):
    """Internal: the cost walker refused to recurse further."""


# ---- parse ----

_COMPARE_SYMBOLS = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
    ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in",
}
_BOOL_SYMBOLS = {ast.And: "and", ast.Or: "or"}
_ORDERING_SYMBOLS = frozenset({"<", "<=", ">", ">="})
_MEMBERSHIP_SYMBOLS = frozenset({"in", "not in"})

# Named rather than lumped under "unsupported element" so the compile error
# tells an author which §7 property their expression would have broken. Every
# one of these also happens to be a node `token_parser._eval_node` raises on, so
# rejecting them here moves an existing runtime failure to compile time rather
# than narrowing what the system can run.
_REJECTED_NODES = (
    (ast.Call, "function calls"),
    (ast.Attribute, "attribute traversal"),
    (ast.Subscript, "indexing"),
    (ast.Lambda, "lambdas"),
    (ast.ListComp, "comprehensions"),
    (ast.SetComp, "comprehensions"),
    (ast.DictComp, "comprehensions"),
    (ast.GeneratorExp, "comprehensions"),
    (ast.IfExp, "conditional expressions"),
    (ast.BinOp, "arithmetic"),
    (ast.Dict, "dict literals"),
    (ast.JoinedStr, "f-strings"),
    (ast.Starred, "unpacking"),
    (ast.NamedExpr, "assignment"),
    (ast.Await, "await"),
)


def parse(expression: str) -> dict:
    """The expression as a serialisable AST of plain dicts.

    Normalises first, so the two dialects template authors actually write --
    `joining_bonus = 0 AND esop_units = 0` and `address_line2 is not blank` --
    land on the same AST as their canonical spelling. That is what makes the
    hash a hash of the *rule* rather than of one author's punctuation.

    Node shapes, all JSON-safe:

      {"node": "compare", "left": <node>, "ops": [{"op": "==", "right": <node>}]}
      {"node": "bool",    "op": "and" | "or", "values": [<node>, ...]}
      {"node": "not",     "operand": <node>}
      {"node": "list",    "elements": [<node>, ...]}
      {"node": "field",   "name": "assignment_type"}
      {"node": "literal", "value": "TEMPORARY"}
    """
    text = normalise_expression(expression)
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise ExpressionSyntaxError(
            f"{expression!r} does not parse as {LANGUAGE_ID}: {exc}", code="syntax",
        ) from exc
    return _from_python(tree.body, 0)


def _from_python(node, depth: int) -> dict:
    if depth > MAX_DEPTH:
        raise ExpressionSyntaxError(
            f"expression nests deeper than {MAX_DEPTH} levels", code="too_deep",
        )
    if isinstance(node, ast.Compare):
        ops = []
        for op, comparator in zip(node.ops, node.comparators):
            symbol = _COMPARE_SYMBOLS.get(type(op))
            if symbol is None:
                raise ExpressionSyntaxError(
                    f"{type(op).__name__} is not a {LANGUAGE_ID} comparison", code="unsupported",
                )
            ops.append({"op": symbol, "right": _from_python(comparator, depth + 1)})
        return {"node": "compare", "left": _from_python(node.left, depth + 1), "ops": ops}
    if isinstance(node, ast.BoolOp):
        return {
            "node": "bool",
            "op": _BOOL_SYMBOLS[type(node.op)],
            "values": [_from_python(v, depth + 1) for v in node.values],
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"node": "not", "operand": _from_python(node.operand, depth + 1)}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        # One canonical container. `role in ('Manager',)` and `role in
        # ['Manager']` are the same rule, and a diff between manifest versions
        # that reports a changed rule because someone retyped the brackets is a
        # diff nobody will read twice.
        return {"node": "list", "elements": [_from_python(e, depth + 1) for e in node.elts]}
    if isinstance(node, ast.Name):
        return {"node": "field", "name": node.id}
    if isinstance(node, ast.Constant):
        return {"node": "literal", "value": node.value}

    for kind, why in _REJECTED_NODES:
        if isinstance(node, kind):
            raise ExpressionSyntaxError(
                f"{LANGUAGE_ID} has no {why}: §7 requires the language to be total, "
                "and the evaluator would refuse this expression at generation time.",
                code="unsupported",
            )
    if isinstance(node, ast.UnaryOp):
        raise ExpressionSyntaxError(
            f"{LANGUAGE_ID} has no unary operator other than `not` -- a negative literal "
            "is unary minus applied to a number, which the evaluator cannot run.",
            code="unsupported",
        )
    raise ExpressionSyntaxError(
        f"{type(node).__name__} is not part of {LANGUAGE_ID}", code="unsupported",
    )


# ---- unparse ----

# `not` binds looser than a comparison and tighter than `and`, exactly as in the
# Python subset this dialect is carved out of.
_PRECEDENCE = {"or": 1, "and": 2, "not": 3, "compare": 4, "atom": 5}
_ATOM = _PRECEDENCE["atom"]


def unparse(node: dict) -> str:
    """Canonical source text for an AST. `unparse(parse(x))` parses back to the
    same AST for every expression this language accepts, which is the property
    that lets a manifest store the AST and still show a human the rule."""
    return _unparse(node, 0)


def _unparse(node: dict, min_precedence: int) -> str:
    kind = node.get("node") if isinstance(node, dict) else None
    if kind == "field":
        return str(node["name"])
    if kind == "literal":
        return _literal_text(node["value"])
    if kind == "list":
        return "[" + ", ".join(_unparse(e, 0) for e in node["elements"]) + "]"
    if kind == "compare":
        # Every operand is rendered as if it needed atom precedence, so a nested
        # comparison or boolean gets brackets rather than changing meaning.
        parts = [_unparse(node["left"], _ATOM)]
        for step in node["ops"]:
            parts.append(str(step["op"]))
            parts.append(_unparse(step["right"], _ATOM))
        return _wrap(" ".join(parts), _PRECEDENCE["compare"], min_precedence)
    if kind == "bool":
        op = str(node["op"])
        own = _PRECEDENCE.get(op)
        if own is None:
            raise ExpressionSyntaxError(f"{op!r} is not a {LANGUAGE_ID} connective", code="unsupported")
        joined = f" {op} ".join(_unparse(v, own) for v in node["values"])
        return _wrap(joined, own, min_precedence)
    if kind == "not":
        return _wrap(f"not {_unparse(node['operand'], _ATOM)}", _PRECEDENCE["not"], min_precedence)
    raise ExpressionSyntaxError(f"{kind!r} is not a {LANGUAGE_ID} node", code="unsupported")


def _wrap(text: str, own_precedence: int, min_precedence: int) -> str:
    return f"({text})" if own_precedence < min_precedence else text


def _literal_text(value) -> str:
    """Source text for a literal that parses back to the same value.

    `repr` is the right tool for strings here and only here: it is the function
    whose whole contract is round-tripping through the Python parser, which is
    the parser this dialect borrows.
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value)
    raise ExpressionSyntaxError(
        f"{type(value).__name__} is not a {LANGUAGE_ID} literal", code="unsupported",
    )


# ---- hashing ----

def canonical_json(node: dict) -> str:
    """The exact bytes `expression_hash` digests. Separate so a caller diffing
    two manifest versions can compare the same canonical form the hash saw."""
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def expression_hash(expression: str) -> str:
    """sha256 over the canonical AST, so two spellings of one rule hash alike.

    Structural canonicalisation only: `role = 'X' AND flag is not blank` and
    `role == 'X' and flag != ''` are the same rule and hash identically, but
    `'Full time'` and `'FULL TIME'` do not -- the evaluator happens to compare
    them equal after casefolding, while an approver reads two different
    sentences, and the hash exists to identify what was approved.

    The language id is part of the preimage so that a future dialect which
    parses the same text differently cannot inherit this dialect's hashes.
    """
    preimage = f"{LANGUAGE_ID}\n{canonical_json(parse(expression))}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


# ---- static type checking ----

UNKNOWN_FIELD = "unknown_field"
INCOMPATIBLE_COMPARISON = "incompatible_comparison"
ORDERING_NOT_DEFINED = "ordering_not_defined"
MEMBERSHIP_NEEDS_LIST = "membership_needs_list"
CONDITION_NOT_BOOLEAN = "condition_not_boolean"
UNPARSEABLE = "unparseable"


@dataclass(frozen=True)
class TypeIssue:
    """One reason an expression cannot be compiled against a source schema."""

    code: str
    detail: str
    field_name: str | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "field": self.field_name}


def typecheck(expression: str, schema: dict) -> list[TypeIssue]:
    """Every static reason this expression cannot run against this schema.

    An empty list means it can. `schema` maps source field name to one of
    `TYPES`; a declaration outside that set raises rather than being ignored,
    because a typo in a schema that silently disables type checking is worse
    than no type checking at all.

    This is §7's "statically typed against the pinned source schema, so failures
    surface at compile time rather than during a production batch". The failures
    it catches are the quiet ones: a field name that no longer exists upstream
    reads as null for every row and blocks an entire batch, and a comparison
    between a boolean column and the text 'yes' is false for every row and
    deletes the block it governs from every letter.
    """
    misdeclared = sorted(
        f"{name}={declared!r}" for name, declared in schema.items() if declared not in TYPES
    )
    if misdeclared:
        raise ValueError(
            f"source schema declares types outside {TYPES}: {', '.join(misdeclared)}"
        )

    try:
        node = parse(expression)
    except ExpressionSyntaxError as exc:
        return [TypeIssue(UNPARSEABLE, str(exc))]

    issues: list[TypeIssue] = []
    _check(node, schema, issues, set())
    if "boolean" not in _types_of(node, schema):
        issues.append(TypeIssue(
            CONDITION_NOT_BOOLEAN,
            f"`{unparse(node)}` is a value, not a test. A condition decides whether a block "
            "is kept, so it has to evaluate to true or false.",
        ))
    return issues


def _check(node: dict, schema: dict, issues: list, reported: set) -> None:
    kind = node["node"]
    if kind == "field":
        name = node["name"]
        if name not in schema and name not in reported:
            reported.add(name)
            issues.append(TypeIssue(
                UNKNOWN_FIELD,
                f"`{name}` is not a field of the pinned source schema, so it reads as null for "
                "every record and the condition can never be decided.",
                name,
            ))
        return
    if kind == "literal":
        return
    if kind == "list":
        for element in node["elements"]:
            _check(element, schema, issues, reported)
        return
    if kind == "bool":
        for value in node["values"]:
            _check(value, schema, issues, reported)
        return
    if kind == "not":
        _check(node["operand"], schema, issues, reported)
        return
    if kind == "compare":
        left = node["left"]
        _check(left, schema, issues, reported)
        for step in node["ops"]:
            right = step["right"]
            _check(right, schema, issues, reported)
            _check_comparison(left, step["op"], right, schema, issues)
            left = right  # chained form: `a < b < c` compares b to c, not a
        return
    raise ExpressionSyntaxError(f"{kind!r} is not a {LANGUAGE_ID} node", code="unsupported")


def _check_comparison(left: dict, symbol: str, right: dict, schema: dict, issues: list) -> None:
    where = left["name"] if left["node"] == "field" else None
    rendered = f"{unparse(left)} {symbol} {unparse(right)}"

    if symbol in _MEMBERSHIP_SYMBOLS:
        if right["node"] != "list":
            issues.append(TypeIssue(
                MEMBERSHIP_NEEDS_LIST,
                f"`{rendered}` tests membership of something that is not a list. The evaluator "
                "answers false for every record rather than raising, so the block disappears "
                "from every letter and nothing reports it.",
                where,
            ))
            return
        if not right["elements"]:
            issues.append(TypeIssue(
                MEMBERSHIP_NEEDS_LIST,
                f"`{rendered}` tests membership of an empty list, which is false for every record.",
                where,
            ))
            return

    left_types, right_types = _types_of(left, schema), _types_of(right, schema)
    shared = left_types & right_types
    if not shared:
        issues.append(TypeIssue(
            INCOMPATIBLE_COMPARISON,
            f"`{rendered}` compares {_describe_types(left_types)} with "
            f"{_describe_types(right_types)}. The evaluator will not raise -- it answers false "
            "for every record, which deletes the block this condition governs.",
            where,
        ))
        return
    if symbol in _ORDERING_SYMBOLS and not (shared & {"number", "date"}):
        issues.append(TypeIssue(
            ORDERING_NOT_DEFINED,
            f"`{rendered}` orders {_describe_types(shared)}. Only numbers and dates have an "
            "order here; text is compared casefolded, so the answer is alphabetical and almost "
            "certainly not what the rule meant.",
            where,
        ))


def _types_of(node: dict, schema: dict) -> frozenset:
    kind = node["node"]
    if kind == "field":
        declared = schema.get(node["name"])
        # An unknown field is already reported once; treating it as compatible
        # with everything keeps one missing column from generating a second
        # error against every literal it is compared to.
        return frozenset({declared}) if declared in TYPES else _ANY_TYPE
    if kind == "literal":
        return _literal_types(node["value"])
    if kind == "list":
        if not node["elements"]:
            return frozenset()
        return frozenset().union(*(_types_of(e, schema) for e in node["elements"]))
    return frozenset({"boolean"})


def _literal_types(value) -> frozenset:
    """Which declared types a literal may legitimately be compared against.

    A set rather than a single type, because the evaluator coerces: a
    spreadsheet cell arrives as text and `scheduled_weekly_hours >= 38` has a
    str on one side and an int on the other. A checker stricter than the
    evaluator would reject expressions that demonstrably work, and would be
    switched off within a week.
    """
    if value is None:
        return _ANY_TYPE
    if isinstance(value, bool):
        return frozenset({"boolean"})
    if isinstance(value, (int, float)):
        return frozenset({"number"})
    if isinstance(value, str):
        if value == "":
            # The blank sentinel. `x is not blank` normalises to `x != ''` for
            # every field whatever its declared type, so refusing `''` against a
            # date would make the most common condition in the corpus a type
            # error.
            return _ANY_TYPE
        kinds = {"string"}
        if _as_number(value) is not None:
            kinds.add("number")
        if _is_iso_date(value):
            kinds.add("date")
        return frozenset(kinds)
    return frozenset()


def _is_iso_date(text: str) -> bool:
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return False
    return True


def _describe_types(types) -> str:
    if not types:
        return "nothing"
    if types == _ANY_TYPE:
        return "any type"
    return "/".join(sorted(types))


# ---- evaluation, with a budget ----

# One op is one step `token_parser._eval_node` can take: a node visited, a
# comparison performed, or one element scanned by a membership test. The dialect
# is total -- no loops, no recursion, no calls -- so the number of steps an
# expression can take is a pure function of its AST and the size of the record
# values it touches, and the cost can therefore be bounded *before* evaluation
# starts. That is deliberate: a bound that refuses to begin beats a counter that
# trips halfway through, and it needs no fork of the evaluator, which is the one
# thing that must stay single-sourced.
_CHARS_PER_OP = 64


def _value_cost(value) -> int:
    """Ops a single record value can cost when the evaluator touches it.

    Sized by `len` and never by walking the contents: estimating the cost of a
    five-million-element list must not itself be a five-million-step walk.
    """
    if isinstance(value, str):
        # `_normalise_scalar` splits and casefolds, so a long value is not free.
        return 1 + len(value) // _CHARS_PER_OP
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return 1 + len(value)
    return 1


def _estimate_cost(node, record: dict, depth: int = 0) -> int:
    """An upper bound on the ops evaluating `node` against `record` can take.

    Walks the raw Python AST rather than this module's dict AST so that the
    bound also covers expressions `parse` rejects but `evaluate_condition` would
    still try -- the budget has to hold for everything that can reach the
    evaluator, not only for what the language admits.
    """
    if depth > MAX_DEPTH:
        raise _TooDeep()
    cost = 1
    if isinstance(node, ast.Name):
        cost += _value_cost(record.get(node.id))
    elif isinstance(node, ast.Constant):
        cost += _value_cost(node.value)
    for child in ast.iter_child_nodes(node):
        cost += _estimate_cost(child, record, depth + 1)
    return cost


def evaluate(expression: str, record: dict, budget_ops: int = DEFAULT_BUDGET_OPS) -> ConditionVerdict:
    """Evaluate a condition under a per-expression op budget.

    The verdict is `token_parser.evaluate_condition`'s, unchanged: missing input
    stays separate from false, and the caller still has to branch on `.value is
    None`. The only thing added is §7's sandbox requirement -- "a per-expression
    evaluation budget, so a pathological expression cannot stall a batch".

    Exceeding the budget is an *undecided* verdict with reason
    `budget_exceeded`, never a quiet false. A false verdict deletes the
    paragraphs the condition governs, so an expression the system declined to
    run must block the document rather than silently shorten it.
    """
    if budget_ops <= 0:
        raise ValueError("budget_ops must be positive; a zero budget can never decide anything")

    try:
        tree = ast.parse(normalise_expression(expression), mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        # Unparseable here is unparseable there. Delegating keeps one module
        # deciding what an unparseable condition means.
        return evaluate_condition(expression, record)

    try:
        cost = _estimate_cost(tree, record)
    except _TooDeep:
        cost = budget_ops + 1  # deeper than the walker will go, so unbounded

    if cost > budget_ops:
        return ConditionVerdict(None, BUDGET_EXCEEDED)
    return evaluate_condition(expression, record)


def estimated_cost(expression: str, record: dict) -> int:
    """The bound `evaluate` checks against `budget_ops`. Public so a compiler can
    reject an expression that is too expensive before it is ever approved,
    rather than discovering it in the batch that needed it."""
    try:
        tree = ast.parse(normalise_expression(expression), mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise ExpressionSyntaxError(
            f"{expression!r} does not parse as {LANGUAGE_ID}: {exc}", code="syntax",
        ) from exc
    try:
        return _estimate_cost(tree, record)
    except _TooDeep as exc:
        raise ExpressionSyntaxError(
            f"expression nests deeper than {MAX_DEPTH} levels", code="too_deep",
        ) from exc


# ---- test cases ----

KEEP = "KEEP"
REMOVE_BLOCK = "REMOVE_BLOCK"
BLOCK_MISSING_INPUT = "BLOCK_MISSING_INPUT"

#: The three outcomes §7's worked example writes its test cases against.
EXPECTATIONS = (KEEP, REMOVE_BLOCK, BLOCK_MISSING_INPUT)


@dataclass(frozen=True)
class CaseResult:
    index: int
    expected: str
    actual: str
    passed: bool
    reason: str
    missing_fields: tuple = ()

    def as_dict(self) -> dict:
        return {
            "index": self.index, "expected": self.expected, "actual": self.actual,
            "passed": self.passed, "reason": self.reason,
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True)
class CaseReport:
    expression: str
    results: tuple

    @property
    def passed(self) -> tuple:
        return tuple(r for r in self.results if r.passed)

    @property
    def failed(self) -> tuple:
        return tuple(r for r in self.results if not r.passed)

    @property
    def all_passed(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict:
        return {
            "expression": self.expression,
            "all_passed": self.all_passed,
            "results": [r.as_dict() for r in self.results],
        }


def verdict_expectation(verdict: ConditionVerdict) -> str:
    """The verdict in the vocabulary §7 writes test cases in.

    Undecided maps to BLOCK_MISSING_INPUT rather than to REMOVE_BLOCK, which is
    the whole point of the record's third test case: a temporary assignment with
    no end date "is not a false condition -- it is incomplete source data".
    """
    if verdict.value is True:
        return KEEP
    if verdict.value is False:
        return REMOVE_BLOCK
    return BLOCK_MISSING_INPUT


def run_test_cases(expression: str, cases, budget_ops: int = DEFAULT_BUDGET_OPS) -> CaseReport:
    """Run an expression's declared test cases and report each one.

    A case is `{"in": {...}, "expect": "KEEP" | "REMOVE_BLOCK" |
    "BLOCK_MISSING_INPUT"}` -- §6 requires every expression to carry at least one
    passing case before its manifest can be locked, and this is what checks them.

    Malformed cases raise instead of being skipped. A test suite that quietly
    ignores the case it could not read reports green for a rule nobody tested,
    which is the exact failure the requirement exists to prevent.
    """
    cases = list(cases)
    if not cases:
        raise ValueError(
            f"{expression!r} carries no test cases; untested is not the same as passing"
        )

    results = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"test case {index} is a {type(case).__name__}, not a mapping")
        record = case.get("in")
        if not isinstance(record, dict):
            raise ValueError(f"test case {index} has no `in` record")
        expected = case.get("expect")
        if expected not in EXPECTATIONS:
            raise ValueError(
                f"test case {index} expects {expected!r}; expected one of {', '.join(EXPECTATIONS)}"
            )
        verdict = evaluate(expression, record, budget_ops=budget_ops)
        actual = verdict_expectation(verdict)
        results.append(CaseResult(
            index=index, expected=expected, actual=actual, passed=actual == expected,
            reason=verdict.reason, missing_fields=tuple(verdict.missing_fields),
        ))
    return CaseReport(expression=expression, results=tuple(results))
