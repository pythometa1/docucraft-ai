"""The rule this module exists to enforce: a reported value is not rewritten.

If any of these fail, the product is misreporting analytical results. That is
a different class of defect from a wrong sentence -- a reviewer reading the
draft cannot catch it, because the number looks exactly like a number.
"""

from decimal import Decimal

import pytest

from app.cmc.limits import FAIL, PASS, UNKNOWN, compare
from app.cmc.values import (
    EQ, LT, LTE, ND, NLT, NMT, RANGE, TEXT, limits_of, parse_criterion, parse_value,
)


# ------------------------------------------------- significant figures

@pytest.mark.parametrize("raw", [
    "0.050", "12.30", "1234.5678", "0.00012345", "100.0", "0.10", "98.000",
])
def test_the_reported_string_survives_verbatim(raw):
    """`value_text` is what a document prints, so it must come back byte for
    byte. This is the assertion that stands between the product and a
    certificate of analysis reported to three significant figures appearing in
    a dossier with two."""
    assert parse_value(raw).text == raw


def test_trailing_zeros_are_kept_in_the_text_and_in_the_decimal():
    value = parse_value("0.050")
    assert value.text == "0.050"
    # Decimal keeps the scale too, which is what makes format(..., "f")
    # round-trip rather than "tidy" the value on its way to the database.
    assert format(value.number, "f") == "0.050"
    assert value.number == Decimal("0.05")  # equal in value, distinct in scale


def test_the_stored_row_never_passes_a_value_through_float():
    row = parse_value("1234.5678").as_row()
    assert row["value_text"] == "1234.5678"
    assert row["value_numeric"] == "1234.5678"
    assert isinstance(row["value_numeric"], str)


def test_a_value_the_number_formatter_would_destroy():
    """`app.generation.value_format` renders a number-typed 0.00012345 as "0"
    and 1234.5678 as "1,234.568". Those are the exact inputs that made this
    module keep its own string, so they are pinned here."""
    from app.generation.value_format import format_value

    for raw in ("0.00012345", "1234.5678", "0.050"):
        mangled = format_value(raw, {"type": "number"}, "en_US")
        kept = parse_value(raw).text
        assert kept == raw
        if mangled != raw:
            # Proves the hazard is real rather than hypothetical.
            assert kept != mangled


# ------------------------------------------------- operators and shapes

def test_operators_are_read_not_guessed():
    assert parse_value("NMT 0.1 %").operator == NMT
    assert parse_value("not more than 0.1").operator == NMT
    assert parse_value("NLT 98.0").operator == NLT
    assert parse_value("< 0.05").operator == LT
    assert parse_value("≤ 0.2 %").operator == LTE
    assert parse_value("99.2").operator == EQ
    assert parse_value("ND").operator == ND
    assert parse_value("Complies").operator == TEXT


def test_a_range_keeps_both_bounds_and_no_single_number():
    value = parse_value("98.0 - 102.0 %")
    assert value.operator == RANGE
    assert (format(value.low, "f"), format(value.high, "f")) == ("98.0", "102.0")
    assert value.number is None
    assert value.unit == "%"
    # Word turns a hyphen into an en dash; the source is still one range.
    assert parse_value("98.0 – 102.0").operator == RANGE
    assert parse_value("98.0 to 102.0").operator == RANGE


def test_non_numeric_results_are_first_class():
    for raw in ("Complies", "Conforms to reference", "Report result", "Pass"):
        value = parse_value(raw)
        assert value.text == raw
        assert value.number is None
        assert value.is_numeric is False


def test_an_ambiguous_decimal_comma_is_refused_rather_than_truncated():
    """"1,5" is 1.5 in half the world and 15 in the other. Reading it as 1 --
    which a bare number regex does -- is a value the source never reported."""
    value = parse_value("1,5")
    assert value.operator == TEXT
    assert value.number is None
    assert value.text == "1,5"
    # A genuine thousands separator still parses.
    assert parse_value("1,234").number == Decimal("1234")
    assert parse_value("1,234.56").number == Decimal("1234.56")


def test_nan_and_infinity_never_become_numbers():
    for raw in ("NaN", "Infinity", "-inf"):
        assert parse_value(raw).number is None


def test_units_are_captured_without_being_normalised():
    assert parse_value("99.2 %").unit == "%"
    assert parse_value("0.5 mg/mL").unit == "mg/mL"
    assert parse_value("12 ppm").unit == "ppm"
    # A number followed by a sentence is a number with a comment, not a unit.
    assert parse_value("9.2 (result of a repeat determination after dilution)").unit is None


# ------------------------------------------------- criteria and limits

def test_limits_are_stored_as_the_specification_wrote_them():
    assert limits_of(parse_criterion("98.0 - 102.0 %")) == ("98.0", "102.0", "between")
    assert limits_of(parse_criterion("NMT 0.2 %")) == (None, "0.2", "nmt")
    assert limits_of(parse_criterion("NLT 98.0 %")) == ("98.0", None, "nlt")
    # An unreadable criterion states no bound rather than an invented one.
    assert limits_of(parse_criterion("Complies")) == (None, None, "text")
    assert limits_of(parse_criterion("Report result")) == (None, None, "text")


# ------------------------------------------------- conformance

def test_conformance_against_a_range():
    assert compare("99.2 %", "98.0 - 102.0 %").outcome == PASS
    assert compare("98.0", "98.0 - 102.0").outcome == PASS      # inclusive
    assert compare("97.9 %", "98.0 - 102.0 %").outcome == FAIL
    assert compare("102.1", "98.0 - 102.0").outcome == FAIL


def test_conformance_against_one_sided_limits():
    assert compare("0.05 %", "NMT 0.1 %").outcome == PASS
    assert compare("0.15 %", "NMT 0.1 %").outcome == FAIL
    assert compare("0.1", "NMT 0.1").outcome == PASS            # the bound itself
    assert compare("98.0", "NLT 98.0 %").outcome == PASS
    assert compare("97.0", "NLT 98.0").outcome == FAIL


def test_not_detected_satisfies_an_upper_bound_and_not_a_lower_one():
    assert compare("ND", "NMT 0.1 %").outcome == PASS
    assert compare("ND", "NLT 98.0 %").outcome == UNKNOWN


def test_a_reported_bound_is_compared_as_that_bound():
    assert compare("< 0.05", "NMT 0.1").outcome == PASS
    assert compare("< 0.5", "NMT 0.1").outcome == FAIL


def test_what_cannot_be_checked_is_never_reported_as_a_pass():
    """The output a quality reviewer must not be handed is a conformance claim
    the system did not actually make."""
    assert compare("99.2", "Complies").outcome == UNKNOWN
    assert compare("12.30", "Report result").outcome == UNKNOWN
    assert compare("White powder", "White to off-white powder").outcome == UNKNOWN
    assert compare("", "NMT 0.1").outcome == UNKNOWN
    assert compare("99.2", "").outcome == UNKNOWN


def test_text_criteria_pass_on_conforming_language():
    assert compare("Complies", "Complies").outcome == PASS
    assert compare("Conforms", "Complies with USP <711>").outcome == PASS
    assert compare("Fails", "Complies").outcome == UNKNOWN


def test_nothing_is_rounded_to_the_criterion_before_comparing():
    """0.1049 against NMT 0.10 is a question about significant figures for a
    person, not a rounding this module performs on the way to a pass."""
    assert compare("0.1049", "NMT 0.10").outcome == FAIL
    assert compare("0.0999", "NMT 0.10").outcome == PASS


def test_the_remaining_comparison_paths():
    """Each of these is a verdict a quality reviewer could be shown, so each
    is exercised rather than left to the first real dossier to discover."""
    from app.cmc.limits import evaluate_row

    # Strict inequalities, both directions.
    assert compare("0.04", "< 0.05").outcome == PASS
    assert compare("0.05", "< 0.05").outcome == FAIL       # the bound is excluded
    assert compare("101", "> 100").outcome == PASS
    assert compare("100", "> 100").outcome == FAIL
    assert compare("99", ">= 98").outcome == PASS
    assert compare("97", ">= 98").outcome == FAIL

    # An equality criterion.
    assert compare("7.0", "= 7.0").outcome == PASS
    assert compare("7.1", "= 7.0").outcome == FAIL

    # A criterion that demands absence.
    assert compare("ND", "ND").outcome == PASS
    assert compare("0.02 %", "ND").outcome == FAIL

    # A numeric result against a criterion with no bound to compare against.
    assert compare("0.02", "Report result").outcome == UNKNOWN

    # The wrapper the grid and the QC pass both call.
    assert evaluate_row(value_text="99.2", acceptance_criterion_text="98.0 - 102.0").outcome == PASS
    assert evaluate_row(value_text="99.2", acceptance_criterion_text=None).outcome == UNKNOWN

    # A failing verdict says so through its own predicate.
    assert compare("97.0", "NLT 98.0").is_failure is True
    assert compare("99.0", "NLT 98.0").is_failure is False
