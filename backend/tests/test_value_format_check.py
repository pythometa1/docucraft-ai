"""Values that read wrong because two sources both supplied the unit.

From a real generated offer letter:

    "Your base salary will be $AUD 76,800 per annum per annum."

The template writes "$<Base Salary> per annum". The source cell held
"AUD 76,800 per annum". Both are individually reasonable and together they are
unsignable, and no gate saw it -- every other check asks whether a placeholder
survived, not whether what replaced it reads like English.

The doubled-word half is separated from the doubled-unit half deliberately. A
doubled unit is always the engine's doing: nobody types "per annum per annum".
A doubled word may be the template author's own typo -- the transfer letter this
estate uses says "offer to to the position" in the master -- so it is compared
against the template and reported as a warning rather than a block. Refusing to
render somebody's letter over their own punctuation is how a gate gets switched
off, and a switched-off gate protects nothing.
"""

from pathlib import Path

import docx
import pytest

from app.qa.value_format_check import (
    format_failures,
    paragraph_texts,
    word_failures,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------ doubled units

@pytest.mark.parametrize("text", [
    "Your base salary will be $76,800 per annum per annum.",
    "You will be paid 38 hours per week per week.",
    "The rate is $45 per hour per hour.",
    "Salary of $80,000 p.a. p.a. applies.",
])
def test_a_doubled_unit_is_caught(text):
    assert format_failures([text]), f"missed a doubled unit in {text!r}"


def test_the_real_defect_is_caught():
    """The sentence that shipped."""
    [*failures] = format_failures(["Your base salary will be $AUD 76,800 per annum per annum."])
    joined = " ".join(failures)
    assert "per annum per annum" in joined
    assert "$AUD" in joined


# --------------------------------------------------------- doubled currency

@pytest.mark.parametrize("text", [
    "Your base salary will be $AUD 76,800.",
    "The amount is $ USD 40,000.",
    "A payment of £GBP 1,000 is due.",
])
def test_a_doubled_currency_marker_is_caught(text):
    assert format_failures([text]), f"missed a doubled currency marker in {text!r}"


@pytest.mark.parametrize("text", [
    "Your base salary will be $76,800 per annum.",
    "The amount is AUD 76,800 per annum.",
    "A total package of INR 12,00,000.00 applies.",
    "Payment of £1,000 is due on the first of the month.",
])
def test_a_single_currency_marker_is_fine(text):
    assert format_failures([text]) == [], f"false positive on correct currency: {text!r}"


# ------------------------------------------------------------ doubled words

def test_a_doubling_the_template_also_has_is_not_blamed_on_the_engine():
    """The customer's typo. The master says "offer to to the position", so the
    letter saying it too is faithful rendering, not a fill defect."""
    output = ["We are pleased to confirm your offer to to the position of Analyst."]
    template = ["We are pleased to confirm your offer to to the position of <Position Title>."]
    assert word_failures(output, template) == []


def test_a_doubling_the_template_does_not_have_is_reported():
    output = ["We are pleased to confirm your offer to to the position of Analyst."]
    template = ["We are pleased to confirm your offer to the position of <Position Title>."]
    assert word_failures(output, template), "a doubling the engine introduced must be reported"


def test_without_the_template_the_finding_says_so():
    """Honest about what it does not know, rather than asserting blame it cannot
    establish."""
    [note] = word_failures(["your offer to to the position"], None)
    assert "not compared" in note


@pytest.mark.parametrize("text", [
    "The payment that that the schedule describes is due.",
    "She had had no prior engagement with the company.",
])
def test_legitimate_english_repeats_are_not_flagged(text):
    assert word_failures([text], None) == [], f"false positive on valid English: {text!r}"


# ------------------------------------------------------- the golden guarantee

def _body(path):
    return docx.Document(str(path)).element.body


@pytest.mark.parametrize("golden", sorted((FIXTURES / "goldens").glob("*/output.docx")))
def test_no_golden_letter_has_a_format_doubling(golden):
    """Held against every letter the suite considers correct. A change to these
    patterns that starts flagging a golden is wrong about the pattern, not about
    the letter."""
    assert format_failures(paragraph_texts(_body(golden))) == []
