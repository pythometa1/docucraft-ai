"""Parse and render the native template-library token HTML produced by the
frontend's `template-editor.tsx` (spec §8.1) -- span[data-token] nodes for
source/prompt/conditional/repeat, e.g.:

  <span data-token="source" field="full_name"></span>
  <span data-token="prompt" prompt="Write a warm welcome..."></span>
  <span data-token="conditional" condition="region == 'EU'" body="GDPR clause."></span>
  <span data-token="repeat" variable="benefit" collection="benefits" body="- {benefit}"></span>
"""

import ast
import operator
import re
import uuid
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

_SAFE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
    ast.Lt: operator.lt, ast.GtE: operator.ge, ast.LtE: operator.le,
    ast.And: all, ast.Or: any,
}


def _normalise_scalar(value):
    """Casefold and collapse whitespace so label matching survives the gap
    between how a template author writes a value and how a spreadsheet stores
    it -- the real Hospira source says "Full Time" where its compiled manifest
    says 'Full time'."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _as_number(value) -> float | None:
    """Return a float when `value` is numeric or numeric-looking text, else None.
    CSV/XLSX cells arrive as strings, so `scheduled_weekly_hours >= 38` has a
    str on the left and an int on the right until something coerces them."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _compare(op_type, left, right) -> bool:
    """Compare two operands the way the person who wrote the template meant it.

    Exact `==` on raw values is wrong here for two reasons that both show up in
    real data: numbers written as text must compare numerically, and label
    matching must ignore case and stray whitespace. Getting the second one
    wrong is silent and severe -- every conditional block whose condition
    evaluates false is *deleted* from the document, so a case mismatch produces
    a plausible-looking letter with entire sections missing.
    """
    if op_type in (ast.In, ast.NotIn):
        if not isinstance(right, (list, tuple, set)):
            return False
        hit = any(_compare(ast.Eq, left, item) for item in right)
        return hit if op_type is ast.In else not hit

    fn = _SAFE_OPS.get(op_type)
    if fn is None:
        return False
    left_num, right_num = _as_number(left), _as_number(right)
    try:
        if left_num is not None and right_num is not None:
            return bool(fn(left_num, right_num))
        return bool(fn(_normalise_scalar(left), _normalise_scalar(right)))
    except TypeError:
        # e.g. ordering a missing (None) field against a literal -- treat as
        # "condition not met" rather than crashing the whole generation.
        return False


@dataclass
class TemplateToken:
    token_id: str
    token_type: str  # source | prompt | conditional | repeat
    attrs: dict = field(default_factory=dict)


def parse_tokens(content_html: str) -> tuple[str, list[TemplateToken], list[str]]:
    """Returns (stamped_html, tokens, source_fields). `stamped_html` has a
    `data-tid` written onto every token span -- callers MUST render against this
    stamped copy (not the original `content_html`) so `render_content_html` can
    match each resolved value back to the exact span it came from."""
    soup = BeautifulSoup(content_html, "lxml")
    tokens: list[TemplateToken] = []
    source_fields: list[str] = []
    for span in soup.find_all("span", attrs={"data-token": True}):
        token_type = span["data-token"]
        attrs = {k: v for k, v in span.attrs.items() if k not in ("data-token", "data-tid")}
        token_id = span.get("data-tid") or str(uuid.uuid4())
        span["data-tid"] = token_id
        tokens.append(TemplateToken(token_id=token_id, token_type=token_type, attrs=attrs))
        if token_type == "source" and attrs.get("field"):
            source_fields.append(attrs["field"])
    body = soup.find("body")
    stamped_html = "".join(str(c) for c in body.contents) if body else str(soup)
    return stamped_html, tokens, sorted(set(source_fields))


_BLANK_RE = re.compile(r"\b(\w+)\s+is\s+not\s+blank\b", re.IGNORECASE)
_IS_BLANK_RE = re.compile(r"\b(\w+)\s+is\s+blank\b", re.IGNORECASE)
_WORD_OPS = ((r"\bAND\b", "and"), (r"\bOR\b", "or"), (r"\bNOT\b", "not"))
# A single `=` that is not part of ==, !=, <= or >=.
_SINGLE_EQ_RE = re.compile(r"(?<![=!<>])=(?!=)")


def normalise_expression(expression: str) -> str:
    """Rewrite a template's own condition syntax into the Python subset the
    evaluator parses.

    Templates write `joining_bonus = 0 AND esop_units = 0` and
    `address_line2 is not blank`. `ast.parse` rejects the first as an assignment
    and reads `blank` as an undefined name in the second, and because a parse
    failure evaluates to False, the block silently disappears from every
    document. Converting here means the manifest stores something that actually
    runs, whichever dialect the template author used.
    """
    text = expression.strip()
    text = _BLANK_RE.sub(r"\1 != ''", text)
    text = _IS_BLANK_RE.sub(r"\1 == ''", text)
    for pattern, replacement in _WORD_OPS:
        text = re.sub(pattern, replacement, text)
    return _SINGLE_EQ_RE.sub("==", text)


def _condition_identifiers(expr: str) -> list[str]:
    """Field names a condition depends on -- what the binding screen must offer.

    Normalised first, and not only for the syntax errors: `address_line2 is not
    blank` is *valid* Python (the identity operator), so the raw parse yields a
    phantom field called `blank` that appears in the mapping UI and can never be
    bound to anything.
    """
    try:
        tree = ast.parse(normalise_expression(expr), mode="eval")
    except SyntaxError:
        return []
    return [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]


def fact_sheet_fields(tokens: list[TemplateToken]) -> list[str]:
    """Every field name generation needs to look up to resolve `source` tokens'
    values *and* `conditional`/`repeat` tokens' variables -- broader than
    `parse_tokens`'s `source_fields` return, which is scoped to just the
    Inspector's source-field dropdown (spec §8.1)."""
    names: set[str] = set()
    for t in tokens:
        if t.token_type == "source" and t.attrs.get("field"):
            names.add(t.attrs["field"])
        elif t.token_type == "conditional" and t.attrs.get("condition"):
            names.update(_condition_identifiers(t.attrs["condition"]))
        elif t.token_type == "repeat" and t.attrs.get("collection"):
            names.add(t.attrs["collection"])
    return sorted(names)


def _eval_node(n, fact_sheet: dict):
    """Walk one node of the whitelisted expression AST. Never Python eval()."""
    if isinstance(n, ast.Compare):
        left = _eval_node(n.left, fact_sheet)
        for op, comparator in zip(n.ops, n.comparators):
            right = _eval_node(comparator, fact_sheet)
            if not _compare(type(op), left, right):
                return False
            left = right  # chained form: a < b < c compares b to c, not a
        return True
    if isinstance(n, ast.BoolOp):
        values = [_eval_node(v, fact_sheet) for v in n.values]
        return _SAFE_OPS[type(n.op)](values)
    # Needed to express "keep this block unless X", which is how a template
    # instruction like "delete this section if all bonuses are zero" is
    # compiled into the manifest's keep-when-true semantics.
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return not _eval_node(n.operand, fact_sheet)
    if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
        return [_eval_node(e, fact_sheet) for e in n.elts]
    if isinstance(n, ast.Name):
        return fact_sheet.get(n.id)
    if isinstance(n, ast.Constant):
        return n.value
    raise ValueError("Unsupported expression element")


def safe_eval_condition(expr: str, fact_sheet: dict) -> bool:
    """Restricted boolean-expression evaluator for `conditional` tokens (spec §14.4)
    -- never Python eval(). Supports `field OP literal` with and/or, `in` against a
    literal list (`role in ['Manager', 'Director']`), and chained comparisons.

    Collapses "false" and "could not be decided" into the same `False`. That is
    the right shape for the native-token path, where a conditional token simply
    does or does not render, but it is the wrong shape for a manifest condition,
    which *deletes* paragraphs from a legal letter. Use `evaluate_condition`
    there instead.
    """
    try:
        node = ast.parse(normalise_expression(expr), mode="eval").body
    except SyntaxError:
        return False
    try:
        return bool(_eval_node(node, fact_sheet))
    except Exception:
        return False


@dataclass(frozen=True)
class ConditionVerdict:
    """The three outcomes a manifest condition actually has.

    Deliberately not truthy-testable. `safe_eval_condition` returns a bool, so
    `if not verdict:` reads the undecided case as "false" -- and a false
    condition deletes its blocks. Forcing callers to branch on `.value is None`
    is the point: §7 of the architecture record calls a temporary assignment
    with no end date "not a false condition, it is incomplete source data", and
    the correct response is to block the document rather than quietly drop a
    paragraph the employee was entitled to see.
    """

    value: bool | None  # None == undecided; never collapse this to False
    reason: str  # evaluated | missing_input | unparseable | supplied
    missing_fields: tuple = ()

    def __bool__(self):
        raise TypeError(
            "ConditionVerdict has three states; test `.value is True` / `is False` / `is None`. "
            "Truth-testing it would read 'undecided' as 'keep', and `not verdict` would read it "
            "as 'delete the block' -- the two failures this type exists to separate."
        )


def condition_inputs(expr: str) -> list[str]:
    """Every source field a condition reads. Public because manifest validation
    needs the closure of referenced fields, not a hand-maintained list."""
    return sorted(set(_condition_identifiers(expr)))


def evaluate_condition(expr: str, record: dict) -> ConditionVerdict:
    """Evaluate a manifest condition with missing input kept separate from false.

    A field that is absent, or present as null, makes the condition undecidable:
    `assignment_type == 'TEMPORARY' and has(transfer_end_date)` against a record
    with no `transfer_end_date` column is a schema problem, not a "no". An empty
    string is *not* undecidable -- a legitimately blank field is a real value
    and compares normally.
    """
    normalised = normalise_expression(expr)
    try:
        node = ast.parse(normalised, mode="eval").body
    except SyntaxError:
        return ConditionVerdict(None, "unparseable")

    referenced = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    missing = tuple(sorted(name for name in referenced if record.get(name) is None))
    if missing:
        return ConditionVerdict(None, "missing_input", missing)

    try:
        return ConditionVerdict(bool(_eval_node(node, record)), "evaluated")
    except Exception:
        return ConditionVerdict(None, "unparseable")


def render_content_html(content_html: str, resolved: dict[str, str]) -> str:
    """Replace each <span data-token data-tid=X> with its resolved plain text/HTML."""
    soup = BeautifulSoup(content_html, "lxml")
    for span in soup.find_all("span", attrs={"data-token": True}):
        tid = span.get("data-tid")
        value = resolved.get(tid, "")
        span.replace_with(value)
    body = soup.find("body")
    return "".join(str(c) for c in body.contents) if body else str(soup)
