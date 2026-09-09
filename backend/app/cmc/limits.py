"""Whether a result meets its acceptance criterion.

The comparison a specification actually asks for, done in Decimal against the
parsed forms and never against the printed strings. Three rules shape it:

* An answer of "cannot tell" is a real answer. A criterion the parser could
  not reduce to bounds, or a result with no numeric form, produces UNKNOWN --
  never a pass. A conformance claim the system could not actually check is the
  one output a quality reviewer must not be handed.
* Non-numeric pairs are compared as text where that is meaningful ("Complies"
  against "Complies") and left UNKNOWN where it is not.
* Nothing is rounded to the criterion's precision before comparing. If a
  method reports 0.1049 against a limit of NMT 0.10, that is a question for a
  human about significant figures, not a rounding this module performs
  quietly on the way to a pass.
* A result and a criterion written in different units are reconciled before
  any magnitude is compared, or the comparison does not happen. `app.cmc.units`
  converts only where the ratio is an exact power of ten; anything else is
  UNKNOWN. Comparing the bare numbers -- which this module used to do --
  reported a residual solvent at 0.5 % (5000 ppm) as conforming to a limit of
  NMT 3000 ppm, and failed a compliant batch on the mirror case.
"""

from dataclasses import dataclass

from app.cmc.units import comparable, convert
from app.cmc.values import (
    EQ, GT, GTE, LT, LTE, ND, NLT, NMT, RANGE, TEXT, Value, parse_criterion,
    parse_value,
)

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

#: Result texts that satisfy a "Complies"-style criterion. Compared on the
#: whole trimmed, lowercased cell.
_CONFORMING = frozenset({
    "complies", "conforms", "conforms to reference", "pass", "passes",
    "meets requirements", "acceptable", "satisfactory",
})


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str

    @property
    def is_failure(self) -> bool:
        return self.outcome == FAIL


def _aligned(result_value: Value, criterion_value: Value):
    """The result's magnitude expressed in the criterion's unit, and the
    string a reason should quote for it.

    A result reported as "< 0.05" asserts an upper bound rather than a
    quantity; it is compared as that bound, which is what a limit of NMT 0.1
    is actually satisfied by.

    Returns `(number, shown)`, or **None** when both sides state a unit and
    the two cannot be reconciled exactly -- the caller answers UNKNOWN, since
    a verdict computed across two different quantities is the one output a
    quality reviewer must not be handed.

    A unit on only one side is not an ambiguity. The commonest real
    specification table states its unit once, in the acceptance criterion, and
    reports bare numbers underneath; those compare as written. Refusing them
    would produce a wall of UNKNOWNs on correct data, which teaches reviewers
    to click past the verdict -- the same harm by a different route.
    """
    number = result_value.number
    left, right = result_value.unit, criterion_value.unit
    if number is None or not left or not right:
        return (number, result_value.text)
    if not comparable(left, right):
        return None
    converted = convert(number, left, right)
    if converted == number:
        return (number, result_value.text)
    # The source's own string first, always -- the conversion is shown beside
    # it so a reviewer can check the arithmetic, never in place of it.
    return (converted, f"{result_value.text} ({converted} {right})")


def compare(result, criterion) -> Verdict:
    """`Verdict` for one result against one acceptance criterion.

    Both arguments may be raw strings or already-parsed `Value`s, because the
    grid compares what a person just typed and the QC pass compares what the
    extractor stored.
    """
    result_value = result if isinstance(result, Value) else parse_value(result)
    criterion_value = criterion if isinstance(criterion, Value) else parse_criterion(criterion)

    if not (result_value.text or "").strip():
        return Verdict(UNKNOWN, "no result recorded")
    if not (criterion_value.text or "").strip():
        return Verdict(UNKNOWN, "no acceptance criterion recorded")

    # -- text against text --
    if criterion_value.operator in (TEXT, ND) and not criterion_value.is_numeric:
        result_text = result_value.text.strip().lower().rstrip(".")
        criterion_text = criterion_value.text.strip().lower().rstrip(".")
        if criterion_value.operator == ND:
            return (Verdict(PASS, "not detected, as required")
                    if result_value.operator == ND
                    else Verdict(FAIL, f"criterion requires not detected; reported "
                                       f"{result_value.text!r}"))
        if result_text == criterion_text or result_text in _CONFORMING:
            return Verdict(PASS, f"reported {result_value.text!r}")
        if result_value.is_numeric:
            return Verdict(UNKNOWN,
                           f"criterion {criterion_value.text!r} is not a numeric limit; "
                           "a person must judge the reported value against it")
        return Verdict(UNKNOWN,
                       f"reported {result_value.text!r} against {criterion_value.text!r}; "
                       "no rule to compare them")

    # -- numeric criteria --
    aligned = _aligned(result_value, criterion_value)
    if aligned is None:
        return Verdict(UNKNOWN,
                       f"{result_value.text!r} is measured in {result_value.unit} and "
                       f"{criterion_value.text!r} sets its limit in {criterion_value.unit}; "
                       "there is no exact conversion between them, so this result has "
                       "to be checked against its specification by a person")
    number, shown = aligned
    if number is None:
        if result_value.operator == ND:
            # Not detected satisfies an upper bound and cannot satisfy a lower
            # one, which is a real distinction on an impurity versus an assay.
            if criterion_value.operator in (NMT, LT, LTE):
                return Verdict(PASS, "not detected, below the upper limit")
            return Verdict(UNKNOWN,
                           "reported not detected against a lower or two-sided limit")
        return Verdict(UNKNOWN,
                       f"reported {result_value.text!r} has no numeric value to compare "
                       f"against {criterion_value.text!r}")

    if criterion_value.operator == RANGE:
        low, high = criterion_value.low, criterion_value.high
        if low is not None and number < low:
            return Verdict(FAIL, f"{shown} is below the lower limit {low}")
        if high is not None and number > high:
            return Verdict(FAIL, f"{shown} is above the upper limit {high}")
        return Verdict(PASS, f"{shown} is within {criterion_value.text}")

    bound = criterion_value.number
    if bound is None:
        return Verdict(UNKNOWN,
                       f"criterion {criterion_value.text!r} states no bound to compare against")

    if criterion_value.operator in (NMT, LTE):
        return (Verdict(PASS, f"{shown} is not more than {bound}")
                if number <= bound
                else Verdict(FAIL, f"{shown} exceeds the limit of {bound}"))
    if criterion_value.operator == LT:
        return (Verdict(PASS, f"{shown} is below {bound}")
                if number < bound
                else Verdict(FAIL, f"{shown} is not below {bound}"))
    if criterion_value.operator in (NLT, GTE):
        return (Verdict(PASS, f"{shown} is not less than {bound}")
                if number >= bound
                else Verdict(FAIL, f"{shown} is below the limit of {bound}"))
    if criterion_value.operator == GT:
        return (Verdict(PASS, f"{shown} is above {bound}")
                if number > bound
                else Verdict(FAIL, f"{shown} is not above {bound}"))
    if criterion_value.operator == EQ:
        return (Verdict(PASS, f"{shown} equals {bound}")
                if number == bound
                else Verdict(FAIL, f"{shown} differs from the required {bound}"))
    return Verdict(UNKNOWN,
                   f"criterion {criterion_value.text!r} could not be reduced to a limit")


def evaluate_row(*, value_text: str, acceptance_criterion_text: str | None,
                 unit: str | None = None) -> Verdict:
    """The comparison as the QC pass and the grid both need it.

    `unit` is the stored `cmc_results.unit`, and passing it is not optional
    dressing: a cell reading "2500" under a `ppm` column header stores its
    unit in that column and nowhere else, so a caller that hands over only the
    text has thrown away the one thing that makes the comparison meaningful.
    """
    return compare(parse_value(value_text, unit_hint=unit),
                   acceptance_criterion_text or "")
