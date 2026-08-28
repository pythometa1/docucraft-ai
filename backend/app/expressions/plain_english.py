"""Say what a rule means, in the words the approver signs off on.

§7 makes this a property the expression language has to have, not a nicety:
"renderable back into plain English for the approval screen -- an approver signs
the meaning, not the syntax". The failure it prevents is quiet and entirely
human. An approval queue that shows `colleague_type == 'Full time'` next to
`colleague_type == 'Fixed Term'` gets skimmed, because reading punctuation is
work and the two lines look the same; the reviewer approves a manifest whose
conditions they did not actually read, and the audit trail then records a
sign-off that never happened in anyone's head. It is also the screen where an
operations manager, who is the person who knows whether temporary transfers
really do always carry an end date, is expected to catch a wrong rule -- and
they do not read Python.

So the sentence is generated from the same AST the evaluator runs, never from a
separate description a compiler wrote alongside it. A hand-written
`plain_english` string in a manifest drifts from its expression the first time
the expression is edited, and a drifted description is worse than none: it is a
false statement of what was approved.

Nothing here softens the rule to read nicely. If an expression cannot be
rendered it raises, because an expression nobody can explain is not one anybody
should approve -- and `manifests.validator` already refuses to approve a
condition that does not parse.
"""

import re

from app.expressions.language import (
    KEEP,
    REMOVE_BLOCK,
    ExpressionSyntaxError,
    parse,
    unparse,
)

# Splits a source field name into words: underscores, camel humps, and the
# letter/digit boundary that turns `address_line2` into "address line 2".
_WORD_SPLIT_RE = re.compile(
    r"[_\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
)

# Prefixes that mark a boolean column. "the is manager is true" is a sentence
# nobody reads twice; "the manager flag is true" is one they do.
_FLAG_PREFIXES = ("is", "has")

_COMPARISON_PHRASES = {
    "==": "is",
    "!=": "is not",
    ">": "is more than",
    ">=": "is at least",
    "<": "is less than",
    "<=": "is at most",
    "in": "is one of",
    "not in": "is none of",
}

# Where a bracket is needed in English: an `or` nested inside an `and` changes
# the meaning of the sentence, and English has no precedence rules to lean on.
_PRECEDENCE = {"or": 1, "and": 2}


def to_plain_english(expression: str) -> str:
    """The expression as a sentence, e.g.

        assignment_type == 'TEMPORARY' and transfer_end_date != ''
        -> the assignment type is TEMPORARY and the transfer end date is not empty

    Raises `ExpressionSyntaxError` when the expression is not in the language.
    """
    return describe(parse(expression))


def describe(node: dict) -> str:
    """Render an already-parsed AST. Public so an approval screen can render the
    AST a locked manifest stores without re-parsing text that may have been
    edited since."""
    return _describe(node, 0)


def to_approval_sentence(expression: str, *, on_true: str = KEEP, on_false: str = REMOVE_BLOCK) -> str:
    """The full statement an approver is asked to sign for one condition.

    Names the undecided case explicitly. An approver reading only "keep this
    block when the assignment is temporary and an end date is given" cannot tell
    what happens to an employee whose record has no end date column at all, and
    that -- not the true or the false case -- is the one that stops their
    letter.
    """
    actions = {KEEP: ("Keep", "kept"), REMOVE_BLOCK: ("Remove", "removed")}
    if on_true not in actions or on_false not in actions:
        raise ValueError(
            f"on_true/on_false must be one of {', '.join(sorted(actions))}; got {on_true!r}/{on_false!r}"
        )
    if on_true == on_false:
        raise ValueError(
            f"a condition whose outcomes are both {on_true} decides nothing and should not exist"
        )

    node = parse(expression)
    verb, _ = actions[on_true]
    _, otherwise = actions[on_false]
    inputs = sorted(_field_names(node))
    reads = _join_words([humanise_field(name) for name in inputs])
    return (
        f"{verb} this block when {describe(node)}; otherwise it is {otherwise}. "
        f"If the source record does not supply {reads}, the document is blocked rather than "
        "the block being decided either way."
    )


def humanise_field(name: str) -> str:
    """`transfer_end_date` -> "the transfer end date"."""
    words = [w for w in _WORD_SPLIT_RE.split(name) if w]
    if not words:
        return f"the field {name!r}"
    if len(words) > 1 and words[0].lower() in _FLAG_PREFIXES:
        words = words[1:] + ["flag"]
    return "the " + " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)


# ---- rendering ----

def _describe(node: dict, min_precedence: int) -> str:
    kind = node.get("node") if isinstance(node, dict) else None
    if kind == "compare":
        return _describe_compare(node)
    if kind == "bool":
        op = node["op"]
        own = _PRECEDENCE.get(op)
        if own is None:
            raise ExpressionSyntaxError(f"{op!r} is not a connective", code="unsupported")
        joined = f" {op} ".join(_describe(v, own) for v in node["values"])
        return f"({joined})" if own < min_precedence else joined
    if kind == "not":
        return f"it is not the case that {_describe(node['operand'], 0)}"
    if kind == "field":
        # A bare field in a condition is a truthiness test, and saying so is the
        # point: an approver who reads "the address line 2 is true" will ask what
        # that means, which is the correct reaction to that rule.
        return f"{humanise_field(node['name'])} is true"
    if kind == "literal":
        value = node["value"]
        if value is True:
            return "always"
        if value is False:
            return "never"
        return f"the fixed value {_value_text(value)}"
    if kind == "list":
        return _join_words([_value_text_of(e) for e in node["elements"]])
    raise ExpressionSyntaxError(f"{kind!r} cannot be rendered in plain English", code="unsupported")


def _describe_compare(node: dict) -> str:
    clauses = []
    left = node["left"]
    for step in node["ops"]:
        right = step["right"]
        clauses.append(_clause(left, step["op"], right))
        left = right  # chained form: `18 <= age < 65` says two things about age
    return " and ".join(clauses)


def _clause(left: dict, symbol: str, right: dict) -> str:
    subject = _operand_text(left)
    # `x is not blank` normalises to `x != ''` on the way in, so the empty
    # string is how "given" and "not given" are written in every real manifest.
    # Rendering it as "is not ''" would put the approver back in front of syntax.
    if right.get("node") == "literal" and right.get("value") == "" and symbol in ("==", "!="):
        return f"{subject} is empty" if symbol == "==" else f"{subject} is not empty"

    phrase = _COMPARISON_PHRASES.get(symbol)
    if phrase is None:
        raise ExpressionSyntaxError(f"{symbol!r} has no plain-English form", code="unsupported")
    if symbol in ("in", "not in"):
        if right.get("node") != "list":
            # `role in region` is a type error the checker reports; there is no
            # honest sentence for it, so it is not given one.
            raise ExpressionSyntaxError(
                f"`{unparse(left)} {symbol} {unparse(right)}` tests membership of something that "
                "is not a list and has no plain-English form",
                code="unsupported",
            )
        return f"{subject} {phrase} {_join_words([_value_text_of(e) for e in right['elements']])}"
    return f"{subject} {phrase} {_operand_text(right)}"


def _operand_text(node: dict) -> str:
    kind = node.get("node") if isinstance(node, dict) else None
    if kind == "field":
        return humanise_field(node["name"])
    if kind == "literal":
        return _value_text(node["value"])
    if kind == "list":
        return _join_words([_value_text_of(e) for e in node["elements"]])
    return _describe(node, 0)


def _value_text_of(node: dict) -> str:
    return _operand_text(node)


def _value_text(value) -> str:
    """A literal as an approver would read it aloud.

    Unquoted, and with its case preserved. The evaluator compares casefolded, so
    'Full time' and 'FULL TIME' behave alike, but the sentence has to show what
    the template author actually wrote -- that is the string a reviewer checks
    against the source spreadsheet.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "nothing"
    if value == "":
        return "empty"
    return str(value)


def _join_words(items) -> str:
    items = list(items)
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} or {items[-1]}"


def _field_names(node: dict) -> set:
    kind = node["node"]
    if kind == "field":
        return {node["name"]}
    if kind == "literal":
        return set()
    if kind == "list":
        return set().union(*(_field_names(e) for e in node["elements"])) if node["elements"] else set()
    if kind == "bool":
        return set().union(*(_field_names(v) for v in node["values"]))
    if kind == "not":
        return _field_names(node["operand"])
    if kind == "compare":
        names = _field_names(node["left"])
        for step in node["ops"]:
            names |= _field_names(step["right"])
        return names
    raise ExpressionSyntaxError(f"{kind!r} is not a node", code="unsupported")


# ---- serialisation for the approval screen ----

#: The keys `annotate_conditions` adds. Named so a write path can strip exactly
#: what a read path added, rather than guessing at the shape.
DERIVED_KEYS = ("plain_english", "approval_sentence", "plain_english_error")


def strip_derived(conditions) -> list:
    """Remove the rendered sentences before a condition is stored.

    The manifest is the production contract and it is hashed. A reviewer's save
    is a round trip -- the screen GETs conditions, the reviewer edits one, the
    screen PUTs them back -- so without this the sentences rendered for display
    would be written into the contract itself. That is the drift this module
    exists to prevent, stated in its own opening: a stored description is a
    false record of what was approved the moment the expression beside it is
    edited. It would also move `manifest_hash` for a change that altered no
    rule.
    """
    stripped = []
    for condition in conditions or []:
        if not isinstance(condition, dict):
            stripped.append(condition)
            continue
        stripped.append({k: v for k, v in condition.items() if k not in DERIVED_KEYS})
    return stripped


def annotate_conditions(conditions) -> list[dict]:
    """Copy each condition with the two sentences an approver reads.

    `to_plain_english` raises on an expression outside the language, which is
    correct for a caller deciding whether to run it and wrong for the screen
    that has to *show* the reviewer what they are being asked to approve. A
    manifest containing one unrenderable condition must still render its other
    nine, and the broken one has to appear as broken rather than as absent --
    an approval screen that silently omits a condition invites a sign-off on a
    manifest the reviewer never saw in full.

    So the failure is carried in the payload instead of raised: `plain_english`
    is None and `plain_english_error` says why. `manifests.validator` is what
    actually refuses the approval; this only makes the reason visible.
    """
    annotated = []
    for condition in conditions or []:
        if not isinstance(condition, dict):
            continue
        item = dict(condition)
        expression = item.get("expression") or ""
        try:
            item["plain_english"] = to_plain_english(expression)
            item["approval_sentence"] = to_approval_sentence(expression)
            item["plain_english_error"] = None
        except (ExpressionSyntaxError, ValueError) as exc:
            item["plain_english"] = None
            item["approval_sentence"] = None
            item["plain_english_error"] = str(exc)
        annotated.append(item)
    return annotated
