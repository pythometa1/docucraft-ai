"""Reading a laboratory value without changing it.

This is the module's central rule made into code. A certificate of analysis
says "0.050 %" and a specification says "NMT 0.1 %", and both are claims about
a method as much as about a quantity: 0.050 asserts three significant figures,
0.05 asserts two, and a dossier that prints the second where the source said
the first has misreported an analytical result. So every value keeps its own
string, forever, and that string is what a rendered table prints.

The parsed `Decimal` beside it exists for exactly one purpose: comparing a
result against an acceptance criterion. It is never rendered, never
round-tripped back into text, and never used to "tidy" the original. The
renderer that reaches for `value_numeric` is a bug this module is arranged to
make obvious.

Decimal, never float -- `Decimal(str(x))`, the same discipline
`app.finance.invoicing` applies to money, for the same reason: a float that
looks right for one dataset is how two numbers disagree in the fourth decimal
place three years later.

Non-numeric results are first-class. "Complies", "Conforms to reference",
"ND", "Report result" and "Pass" are what a great many tests legitimately
report, and a store that could only hold numbers would either drop them or
coerce them into zeros.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: How a value relates to its number.
EQ = "eq"          # 99.2
LT = "lt"          # < 0.05
GT = "gt"          # > 100
LTE = "lte"        # <= 0.05
GTE = "gte"        # >= 100
NMT = "nmt"        # not more than 0.1
NLT = "nlt"        # not less than 98.0
ND = "nd"          # not detected
RANGE = "range"    # 98.0 - 102.0
TEXT = "text"      # Complies

OPERATORS = (EQ, LT, GT, LTE, GTE, NMT, NLT, ND, RANGE, TEXT)

#: Results that are legitimately not numbers. Matched case-insensitively on
#: the whole trimmed cell, never as a substring: "Complies" is a result and
#: "Complies with USP <711>" is the same result, but "9.2 (complies)" is a
#: number with a comment and must keep its 9.2.
_NON_NUMERIC = (
    "complies", "conforms", "conforms to reference", "pass", "passes",
    "meets requirements", "report result", "to be reported", "not detected",
    "nd", "n/d", "absent", "none detected", "negative", "positive",
    "clear", "colourless", "colorless", "white", "off-white",
)

#: Leading words that state a bound rather than a value.
_PREFIX_OPERATORS = (
    ("not more than", NMT), ("no more than", NMT), ("nmt", NMT),
    ("not less than", NLT), ("no less than", NLT), ("nlt", NLT),
    ("less than or equal to", LTE), ("greater than or equal to", GTE),
    ("less than", LT), ("greater than", GT),
    ("<=", LTE), ("≤", LTE), (">=", GTE), ("≥", GTE),
    ("<", LT), (">", GT), ("=", EQ),
)

#: A number as a source writes one. The alternation is ordered longest-first
#: and the comma-grouped form REQUIRES a comma, because the obvious spelling
#: -- `\d{1,3}(?:,\d{3})*` -- matches "123" out of "1234.5678" and reports
#: an assay of 1234.5678 as 123. Regex alternation takes the first branch that
#: matches, not the longest, so the order here is load-bearing.
_NUMBER = (r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?"   # 1,234  1,234.56
           r"|[+-]?\d+\.\d+"                       # 1234.5678
           r"|[+-]?\.\d+"                           # .5
           r"|[+-]?\d+")                             # 1234
_NUMBER_RE = re.compile(_NUMBER)

#: "98.0 - 102.0", "98.0 to 102.0", "98.0-102.0 %". The dash may be a hyphen,
#: an en dash or an em dash: a specification typed in Word contains all three.
_RANGE_RE = re.compile(
    rf"^\s*(?P<low>{_NUMBER})\s*(?:-|‐|‑|‒|–|—|to)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>[^\d\s].*)?$",
    re.IGNORECASE)

#: What follows the number: "%", "mg", "mg/mL", "ppm", "cfu/g", "µg".
_UNIT_RE = re.compile(r"[A-Za-z%µμ°][A-Za-z%/µμ°().·^\d-]*$")


@dataclass(frozen=True)
class Value:
    """One reported value, kept as reported.

    `text` is the source's own string and the only thing a document prints.
    `number` is for comparison; it is None whenever the value is not a single
    quantity, which includes ranges (`low`/`high` carry those) and every
    non-numeric result.
    """

    text: str
    operator: str = TEXT
    number: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    unit: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.number is not None or self.low is not None

    def as_row(self) -> dict:
        """The columns a `cmc_results` row stores. `value_numeric` is a string
        here because the caller hands it to SQLAlchemy's Numeric, and going
        through float on the way would undo the whole point of this module."""
        return {
            "value_text": self.text,
            "value_numeric": (format(self.number, "f") if self.number is not None else None),
            "operator": self.operator,
            "unit": self.unit,
        }


def _to_decimal(raw: str) -> Decimal | None:
    """A Decimal from a source's own digits, or None.

    Thousands separators are stripped because "1,234" is one number in every
    convention that writes it that way. A bare comma decimal ("1,5") is NOT
    accepted: it means 1.5 in half the world and 15 in the other, and a guess
    that is wrong by a factor of ten is worse than an unparsed value the grid
    asks a person about.
    """
    cleaned = (raw or "").strip().replace(" ", "")
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", cleaned):
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    # NaN and Infinity construct without raising, and "Assay: NaN" is exactly
    # the confident-looking wrong number this module exists to refuse.
    return value if value.is_finite() else None


def _split_unit(remainder: str) -> str | None:
    unit = (remainder or "").strip().lstrip("(").rstrip(")").strip()
    if not unit:
        return None
    match = _UNIT_RE.search(unit)
    return match.group(0) if match else (unit or None)


def parse_value(raw, *, unit_hint: str | None = None) -> Value:
    """One reported cell, read without being rewritten.

    The returned `text` is the input trimmed of surrounding whitespace and
    nothing else -- no case folding, no unit normalisation, no re-formatting
    of the digits. Everything else the function works out is additional
    information ABOUT that string, never a replacement for it.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return Value(text="", operator=TEXT)

    lowered = text.lower().rstrip(".")
    if lowered in _NON_NUMERIC:
        return Value(text=text, operator=ND if lowered in ("nd", "n/d", "not detected",
                                                           "none detected", "absent")
                     else TEXT)

    # A range: two numbers and a separator. Checked before the single-number
    # path so "98.0 - 102.0" is not read as 98.0 with a trailing comment.
    range_match = _RANGE_RE.match(text)
    if range_match:
        low = _to_decimal(range_match.group("low"))
        high = _to_decimal(range_match.group("high"))
        if low is not None and high is not None:
            return Value(text=text, operator=RANGE, low=low, high=high,
                         unit=_split_unit(range_match.group("unit")) or unit_hint)

    operator = EQ
    body = text
    for token, token_operator in _PREFIX_OPERATORS:
        if body.lower().startswith(token):
            operator = token_operator
            body = body[len(token):].strip()
            break

    # The number has to LEAD what is left after any operator word. A cell
    # reading "Complies with USP <711>" contains 711 and asserts nothing about
    # it; searching anywhere in the string turns a monograph reference into an
    # acceptance limit of 711.
    number_match = _NUMBER_RE.match(body.strip())
    if number_match is None:
        return Value(text=text, operator=TEXT)
    body = body.strip()
    number = _to_decimal(number_match.group(0))
    if number is None:
        return Value(text=text, operator=TEXT)

    tail = body[number_match.end():]
    # "1,5" must never come back as the number 1. The decimal comma means 1.5
    # in half the world and a thousands separator in the other, and either
    # reading of a truncated match is a value the source did not report -- so
    # an unconsumed comma-digit tail sends the whole cell to the review grid
    # as text instead.
    if re.match(r"^\s*,\s*\d", tail):
        return Value(text=text, operator=TEXT)
    # A number followed by prose is a comment, not a unit: keep the number but
    # do not pretend the sentence is a unit symbol.
    unit = _split_unit(tail) if len(tail.strip()) <= 24 else None
    return Value(text=text, operator=operator, number=number,
                 unit=unit or unit_hint)


def parse_criterion(raw) -> Value:
    """An acceptance criterion, read the same way a result is.

    A criterion is a value with a bound rather than a measurement, so the same
    parser serves: "98.0 - 102.0 %" comes back as a range, "NMT 0.2 %" as an
    upper bound, "Complies" as text. What is NOT done is inventing a bound
    for a criterion the parser cannot read -- an unreadable criterion is left
    as text so that QC reports it could not check rather than checking
    something the specification never said.
    """
    return parse_value(raw)


def limits_of(criterion: Value) -> tuple:
    """`(limit_lower, limit_upper, limit_operator)` for a `cmc_tests` row.

    Strings, not Decimals: the columns store what the specification wrote, and
    a bound reformatted on its way into the database is a bound nobody set.
    """
    if criterion.operator == RANGE:
        return (format(criterion.low, "f"), format(criterion.high, "f"), "between")
    if criterion.operator in (NMT, LT, LTE) and criterion.number is not None:
        return (None, format(criterion.number, "f"), criterion.operator)
    if criterion.operator in (NLT, GT, GTE) and criterion.number is not None:
        return (format(criterion.number, "f"), None, criterion.operator)
    if criterion.operator == EQ and criterion.number is not None:
        return (format(criterion.number, "f"), format(criterion.number, "f"), "eq")
    return (None, None, criterion.operator)
