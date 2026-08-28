"""Instructions to the author must not survive into the letter.

The gate existed and matched one phrase -- "include the following text|section"
-- which is not how this estate writes instructions at all. A real offer letter
shipped to a reviewer carrying "(Remove all table once used)" and the gate
stayed silent.

Two shapes account for everything these templates actually write:
a parenthesised imperative inside a clause, and a shouted line standing above
the clause it governs. The hard part is not catching them; it is not catching
ordinary contract prose, because a gate that fires on correct documents gets
overridden and an overridden gate protects nothing.
"""

from pathlib import Path

import docx
import pytest

from app.qa.placeholder_check import instruction_text_in

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- true positives

@pytest.mark.parametrize("text", [
    "(Remove all table once used)",
    "(Include if working part time hours)",
    "(Include the below clause if the position is covered by the Clerks Award)",
    "(Insert the colleague's grade here)",
    "(E.G. Monday (7.2 hours)",
    "USE IF ON TEMPORARY ASSIGNMENT",
    "INCLUDE IF COLLEAGUE TYPE IS FIXED TERM",
    "USE FOR NEW HIRE NOT ON TEMPORARY ASSIGNMENT",
    "ALWAYS INCLUDE",
])
def test_real_instructions_are_caught(text):
    assert instruction_text_in(text), f"missed an instruction the estate actually writes: {text!r}"


def test_the_leak_that_shipped_is_caught():
    """The specific string that reached a reviewer inside a finished letter."""
    letter = "…may cease to apply to you in the future.  (Remove all table once used)   If your position changes…"
    assert instruction_text_in(letter) == ["(Remove all table once used)"]


def test_the_finding_names_the_instruction():
    """"Leftover instruction text found" sends a reviewer hunting through six
    pages. The text itself sends them to the line."""
    [found] = instruction_text_in("blah (Remove all table once used) blah")
    assert found == "(Remove all table once used)"


def test_repeats_collapse_to_one():
    """A template that repeats one instruction six times has one defect."""
    text = "(Include for all contracts) x (Include for all contracts) y (Include for all contracts)"
    assert instruction_text_in(text) == ["(Include for all contracts)"]


# ---------------------------------------------------------------- false positives

@pytest.mark.parametrize("text", [
    # The one that motivated the word boundary. Ordinary contract prose, and it
    # sits mid-clause in a paragraph nobody should be blocked on.
    "The total payments made to you in an Employment Year, including your Base salary and any incentive.",
    "Your remuneration includes superannuation.",
    "Use of company property",
    "Benefits include private health cover (subject to eligibility).",
    "This includes (but is not limited to) the following.",
    # The inflected forms the word boundary exists to spare. Each of these is
    # flagged as an instruction if the boundary is removed.
    "Superannuation (included in your remuneration package) is paid quarterly.",
    "Your package (includes superannuation) at the statutory rate.",
    "The schedule (used for reference only) is not contractual.",
    "The clause (removed from the schedule) no longer applies.",
    "Deletion of your data is governed by our retention policy.",
])
def test_ordinary_contract_prose_is_not_flagged(text):
    assert instruction_text_in(text) == [], f"false positive on legitimate prose: {text!r}"


def test_a_shouted_heading_is_not_an_instruction():
    """"USE OF COMPANY PROPERTY" is a section heading. The verb alone is not
    enough -- these are separated by whether the line reads as addressed to the
    author or to the colleague, and heading case is the only signal available."""
    assert instruction_text_in("TERMS AND CONDITIONS OF EMPLOYMENT") == []


def _letter_text(path: Path) -> str:
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


@pytest.mark.parametrize("golden", sorted((FIXTURES / "goldens").glob("*/output.docx")))
def test_no_golden_letter_is_flagged(golden):
    """The false-positive guarantee, held against every letter we consider correct.

    These are the outputs the fill engine is pinned to. If a change to the
    patterns starts flagging one of them, the change is wrong -- not the letter.
    """
    assert instruction_text_in(_letter_text(golden)) == []
