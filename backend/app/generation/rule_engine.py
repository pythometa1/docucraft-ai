"""Deterministic arithmetic over resolved values.

A tox or clinical report needs numbers that are *derived*, not copied -- totals,
means, incidence rates. The LLM's job is to propose the formula once, at compile
time; executing it is arithmetic and must never involve a model. A model doing
sums in a regulated report is not a cost question, it's a correctness one.

Built on the same whitelisted-AST approach as `safe_eval_condition` -- never
Python `eval` -- extended with the operators and functions a report actually
needs. Anything it cannot compute confidently returns an explicit failure so
the caller can route the document to a human, rather than emitting a blank or
a plausible-looking wrong number.
"""

import ast
import math
import operator
from dataclasses import dataclass

from app.expressions.token_parser import _as_number

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _mean(*values):
    flat = _flatten(values)
    if not flat:
        raise ValueError("mean() of no values")
    return sum(flat) / len(flat)


def _flatten(values):
    out = []
    for v in values:
        if isinstance(v, (list, tuple)):
            out.extend(_flatten(v))
        else:
            out.append(v)
    return out


_FUNCTIONS = {
    "round": lambda value, digits=0: round(value, int(digits)),
    "abs": abs,
    "min": lambda *v: min(_flatten(v)),
    "max": lambda *v: max(_flatten(v)),
    "sum": lambda *v: sum(_flatten(v)),
    "mean": _mean,
    "avg": _mean,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
}


@dataclass
class FormulaResult:
    value: float | None
    ok: bool
    error: str | None = None
    inputs_used: dict | None = None


def formula_identifiers(expression: str) -> list[str]:
    """Names a formula depends on -- used to build the dependency graph so a
    computed value is never resolved before its inputs."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    return sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _FUNCTIONS
    })


def safe_eval_formula(expression: str, values: dict) -> FormulaResult:
    """Evaluate `expression` against `values`.

    Every failure mode -- a missing input, a non-numeric cell, division by zero
    -- returns `ok=False` with a readable reason. None of them silently produce
    a number, because a wrong number that looks right is the worst outcome
    available here.
    """
    try:
        tree = ast.parse(expression, mode="eval").body
    except SyntaxError as exc:
        return FormulaResult(None, False, f"Could not parse formula: {exc}")

    used: dict = {}

    def ev(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"Only numeric literals are allowed, got {node.value!r}")
            return node.value

        if isinstance(node, ast.Name):
            if node.id not in values:
                raise KeyError(f"'{node.id}' has no value")
            number = _as_number(values[node.id])
            if number is None:
                raise ValueError(f"'{node.id}' is {values[node.id]!r}, which is not a number")
            used[node.id] = number
            return number

        if isinstance(node, ast.BinOp):
            fn = _BIN_OPS.get(type(node.op))
            if fn is None:
                raise ValueError("Unsupported operator")
            left, right = ev(node.left), ev(node.right)
            if fn in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
                raise ZeroDivisionError("division by zero")
            return fn(left, right)

        if isinstance(node, ast.UnaryOp):
            fn = _UNARY_OPS.get(type(node.op))
            if fn is None:
                raise ValueError("Unsupported unary operator")
            return fn(ev(node.operand))

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                name = getattr(node.func, "id", "?")
                raise ValueError(f"'{name}' is not an allowed function")
            if node.keywords:
                raise ValueError("Keyword arguments are not supported in formulas")
            return _FUNCTIONS[node.func.id](*[ev(a) for a in node.args])

        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]

        raise ValueError(f"Unsupported expression element: {type(node).__name__}")

    try:
        result = ev(tree)
    except ZeroDivisionError as exc:
        return FormulaResult(None, False, str(exc), used)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return FormulaResult(None, False, str(exc).strip("'"), used)

    if isinstance(result, (list, tuple)):
        return FormulaResult(None, False, "Formula produced a list, not a single value", used)
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        return FormulaResult(None, False, "Formula produced a non-finite result", used)
    return FormulaResult(float(result), True, None, used)
