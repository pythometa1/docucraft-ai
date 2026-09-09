"""A conformance verdict is only as good as the units it was computed in.

The defect these tests were written against: `compare()` reduced both sides to
bare Decimals and never looked at the unit, so a residual solvent reported as
0.5 % against a limit of NMT 3000 ppm -- 5000 ppm against a 3000 ppm limit,
genuinely out of specification -- came back PASS, with a reason that read
"0.5 % is not more than 3000" and no hint that the two numbers were measured
in different things. The mirror case failed a batch that complies.

Both are the failure this module exists to prevent, and both are silent: the
Data Review grid shows the row conforming, `qc._conformance` raises no
blocker, and the batch exports as in-spec.

The rule the fix encodes: a comparison the system cannot actually perform is
UNKNOWN. Never a pass, never a failure.
"""

from decimal import Decimal

import pytest

from app.cmc.limits import FAIL, PASS, UNKNOWN, compare, evaluate_row
from app.cmc.units import comparable, convert, ratio
from app.cmc.values import parse_value


# --------------------------------------------------------------- the two bugs

def test_a_percent_result_against_a_ppm_limit_is_out_of_specification():
    """0.5 % is 5000 ppm. A limit of NMT 3000 ppm is exceeded."""
    verdict = compare("0.5 %", "NMT 3000 ppm")
    assert verdict.outcome == FAIL
    assert verdict.is_failure
    # The reason quotes the source's own string, and shows the conversion that
    # was actually compared -- a reviewer must be able to check the arithmetic.
    assert "0.5 %" in verdict.reason
    assert "5000" in verdict.reason
    assert "3000" in verdict.reason


def test_a_ppm_result_against_a_percent_limit_is_not_a_false_failure():
    """3000 ppm is 0.3 %, comfortably inside NMT 0.5 %."""
    verdict = compare("3000 ppm", "NMT 0.5 %")
    assert verdict.outcome == PASS


def test_milligrams_against_a_gram_limit():
    """500 mg is 0.5 g, which is NOT less than 0.4 g -- a pass, but it has to
    be a pass for the right reason rather than because 500 > 0.4."""
    verdict = compare("500 mg", "NLT 0.4 g")
    assert verdict.outcome == PASS
    assert "0.5" in verdict.reason  # the converted value, in the criterion's unit


def test_a_gram_result_below_a_milligram_limit_fails():
    verdict = compare("0.0001 g", "NLT 5 mg")
    assert verdict.outcome == FAIL


# ------------------------------------------------- what cannot be reconciled

def test_units_from_different_dimensions_are_unknown_not_a_pass():
    """mg/mL is a concentration and % is a mass fraction. Without a density
    nobody can compare them, so nobody -- including this module -- should."""
    verdict = compare("2.0 mg/mL", "98.0 - 102.0 %")
    assert verdict.outcome == UNKNOWN
    assert "mg/mL" in verdict.reason and "%" in verdict.reason


def test_an_unrecognised_unit_pair_is_unknown():
    verdict = compare("120 cfu/g", "NMT 100 cfu/mL")
    assert verdict.outcome == UNKNOWN


def test_the_same_unrecognised_unit_on_both_sides_still_compares():
    """`cfu/g` is not in any conversion table, but a value and a limit written
    in the same unit need no conversion to be comparable."""
    assert compare("120 cfu/g", "NMT 100 cfu/g").outcome == FAIL
    assert compare("40 cfu/g", "NMT 100 cfu/g").outcome == PASS


def test_percent_v_v_is_not_a_mass_fraction():
    """% w/w converts to ppm; % v/v does not, because that needs a density."""
    assert compare("0.5 % v/v", "NMT 3000 ppm").outcome == UNKNOWN
    assert compare("0.5 % w/w", "NMT 3000 ppm").outcome == FAIL


# --------------------------------------------- the ordinary shape must be quiet

def test_a_unit_on_only_the_criterion_compares_as_written():
    """The commonest real spec table puts the unit in the criterion alone and
    reports a bare number. That is not an ambiguity and must not become a wall
    of UNKNOWNs, or reviewers learn to click past the verdict."""
    verdict = compare("0.05", "NMT 0.10 %")
    assert verdict.outcome == PASS


def test_a_unit_on_only_the_result_compares_as_written():
    assert compare("0.05 %", "NMT 0.10").outcome == PASS


def test_identical_units_are_untouched():
    verdict = compare("99.2 %", "98.0 - 102.0 %")
    assert verdict.outcome == PASS
    # No conversion happened, so the reason is the plain one.
    assert "(" not in verdict.reason.replace("(Q)", "")


def test_the_stored_unit_reaches_the_comparison():
    """`qc` and the grid compare stored strings. A cell that read "2500" under
    a `ppm` column header stores value_text="2500", unit="ppm" -- and the unit
    column is the only place that ppm survives, so the comparison has to read
    it."""
    result = parse_value("2500", unit_hint="ppm")
    assert result.unit == "ppm"
    assert compare(result, "NMT 0.3 %").outcome == PASS       # 2500 ppm = 0.25 %
    assert compare(result, "NMT 0.1 %").outcome == FAIL       # 0.25 % > 0.10 %


def test_evaluate_row_carries_the_unit_through():
    assert evaluate_row(value_text="2500", acceptance_criterion_text="NMT 0.3 %",
                        unit="ppm").outcome == PASS
    # Without the unit there is nothing to reconcile and the bare magnitudes
    # are what the specification meant.
    assert evaluate_row(value_text="0.25",
                        acceptance_criterion_text="NMT 0.3 %").outcome == PASS


# ----------------------------------------------------- the conversion itself

@pytest.mark.parametrize("value,frm,to,expected", [
    ("0.5", "%", "ppm", "5000"),
    ("3000", "ppm", "%", "0.3"),
    ("1", "ppm", "ppb", "1000"),
    ("500", "mg", "g", "0.5"),
    ("2", "kg", "g", "2000"),
    ("1", "g/L", "mg/mL", "1"),
    ("1", "mg/dL", "mg/mL", "0.01"),
    ("1", "L", "mL", "1000"),
    ("1", "mg/kg", "ppm", "1"),
    ("1", "µg/g", "ppm", "1"),
    ("1", "mcg", "ug", "1"),
])
def test_exact_conversions(value, frm, to, expected):
    got = convert(Decimal(value), frm, to)
    assert got is not None
    assert got == Decimal(expected)


def test_conversion_is_decimal_all_the_way_down():
    """Every ratio in the table is a power of ten, so no conversion can
    introduce a representation error the way a float would."""
    assert convert(Decimal("0.1"), "%", "ppm") == Decimal("1000")
    assert convert(Decimal("0.00012345"), "%", "ppm") == Decimal("1.2345")
    # And the round trip is exact, which a float ratio would not guarantee.
    there = convert(Decimal("0.050"), "%", "ppm")
    assert convert(there, "ppm", "%") == Decimal("0.050")


def test_unknown_units_do_not_convert():
    assert convert(Decimal("1"), "cfu/g", "cfu/mL") is None
    assert convert(Decimal("1"), "%", "mg") is None
    assert ratio("mg", "L") is None


def test_comparable_is_the_predicate_the_verdict_uses():
    assert comparable("%", "ppm")
    assert comparable("cfu/g", "cfu/g")
    assert not comparable("%", "mg/mL")


def test_conversion_never_touches_the_printed_string():
    """The whole discipline of the module: a converted number exists for the
    verdict and nowhere else. `Value.text` and `as_row()["value_text"]` are
    the source's own characters, before and after any comparison."""
    value = parse_value("0.050 %")
    compare(value, "NMT 3000 ppm")
    assert value.text == "0.050 %"
    assert value.as_row()["value_text"] == "0.050 %"


# ------------------------------------------------------------- the guards

def test_a_missing_unit_normalises_to_nothing_and_converts_nothing():
    from app.cmc.units import normalise

    assert normalise(None) == ""
    assert normalise("") == ""
    assert normalise("   ") == ""
    # And nothing is comparable with a unit nobody stated, which is what sends
    # `compare` down the compare-as-written path rather than a conversion.
    assert ratio(None, "ppm") is None
    assert ratio("ppm", "") is None
    assert convert(Decimal("1"), None, "ppm") is None


def test_converting_nothing_is_nothing():
    assert convert(None, "%", "ppm") is None


def test_both_spellings_of_micro_are_one_unit():
    from app.cmc.units import normalise

    micro_sign, greek_mu = "µg", "μg"
    assert micro_sign != greek_mu
    assert normalise(micro_sign) == normalise(greek_mu) == "ug"
    assert comparable(micro_sign, greek_mu)


def test_an_absurd_magnitude_keeps_its_exponent_rather_than_failing():
    """`quantize` cannot expand an exponent past the Decimal context's
    precision. That is not a reason to lose the conversion -- the quantity is
    right either way, and only its spelling is less friendly."""
    converted = convert(Decimal("1E+47"), "%", "ppm")
    assert converted == Decimal("1E+51")
