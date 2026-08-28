"""The gates that stop a wrong document being issued.

Every case here is a failure that used to be silent. A silently blank field, a
section deleted because nobody could evaluate the condition that governs it, and
a QA verdict that was computed and then thrown away all produce a letter that
looks finished and is not -- which is worse than an outright failure, because
nobody notices until the employee does.
"""

import pytest

from app.generation.batch_runner import _canary_positions
from app.generation.docx_renderer import _sentence_bounds
from app.generation.missing_policy import (
    BLANK, BLOCK, DEFAULT, REMOVE_SENTENCE, field_on_missing, normalise_source_value,
)
from app.generation.renderers import (
    DOCX_TEMPLATE_ASSEMBLY, HTML_ASSEMBLY, OOXML_FILL, is_html_editable,
)
from app.generation.resolution_engine import resolve_manifest
from app.expressions.token_parser import condition_inputs, evaluate_condition, safe_eval_condition


# --------------------------------------------------------------- three states

def test_missing_input_is_not_false():
    """The §7 worked example: temporary assignment, no end date.

    The record is not saying "not temporary" -- it is failing to say anything,
    and the letter cannot be issued either way until someone supplies the date.
    """
    expr = "assignment_type == 'TEMPORARY' and transfer_end_date != ''"
    record = {"assignment_type": "TEMPORARY"}

    verdict = evaluate_condition(expr, record)
    assert verdict.value is None
    assert verdict.reason == "missing_input"
    assert verdict.missing_fields == ("transfer_end_date",)


def test_the_bool_evaluator_guesses_in_both_directions():
    """Why a third state was needed rather than a better default.

    A missing field reads as `None`, and `None` compares as a value: against
    `!= ''` it looks present and the block is *kept*, against `== 'X'` it looks
    wrong and the block is *deleted*. Same absent data, opposite outcomes,
    neither of them a decision anyone made.
    """
    record = {"assignment_type": "TEMPORARY"}
    assert safe_eval_condition("transfer_end_date != ''", record) is True
    assert safe_eval_condition("transfer_end_date == '2026-11-30'", record) is False
    for expr in ("transfer_end_date != ''", "transfer_end_date == '2026-11-30'"):
        assert evaluate_condition(expr, record).value is None


def test_present_and_complete_still_decides():
    expr = "assignment_type == 'TEMPORARY' and transfer_end_date != ''"
    kept = evaluate_condition(expr, {"assignment_type": "TEMPORARY", "transfer_end_date": "2026-11-30"})
    assert kept.value is True and kept.reason == "evaluated"

    dropped = evaluate_condition(expr, {"assignment_type": "PERMANENT", "transfer_end_date": ""})
    assert dropped.value is False and dropped.reason == "evaluated"


def test_empty_string_is_a_value_not_a_gap():
    """"" is legitimately blank. Only absent and null are undecidable."""
    assert evaluate_condition("address_line2 != ''", {"address_line2": ""}).value is False
    assert evaluate_condition("address_line2 != ''", {"address_line2": "Flat 2"}).value is True
    assert evaluate_condition("address_line2 != ''", {"address_line2": None}).value is None
    assert evaluate_condition("address_line2 != ''", {}).value is None


def test_unparseable_expression_is_undecided_not_false():
    verdict = evaluate_condition("this is not (an expression", {})
    assert verdict.value is None
    assert verdict.reason == "unparseable"


def test_verdict_is_not_truthy_testable():
    """`if not verdict:` is the bug this type exists to prevent."""
    with pytest.raises(TypeError):
        bool(evaluate_condition("a == 1", {"a": 1}))


def test_condition_inputs_ignores_dialect_phantoms():
    # `is not blank` is valid Python, so a naive parse invents a field `blank`.
    assert condition_inputs("address_line2 is not blank") == ["address_line2"]


# ------------------------------------------------------- resolution behaviour

def _manifest(**over):
    base = {
        "fields": [],
        "conditions": [{"id": "c_temp", "expression": "assignment_type == 'TEMPORARY'", "keeps_blocks": ["b1"]}],
        "blocks": [{"id": "b1", "start_paragraph": 1, "end_paragraph": 2, "boundary_method": "marker"}],
        "delete_always": [],
    }
    base.update(over)
    return base


def test_undecidable_condition_is_not_published_as_a_verdict():
    """`bool(None)` is False. Publishing that told the renderer to drop the block."""
    result = resolve_manifest(_manifest(), {})
    assert "c_temp" not in result.condition_verdicts
    assert result.needs_review is True
    assert "does not provide" in result.open_tasks[0]["question"]


def test_decidable_condition_is_published():
    result = resolve_manifest(_manifest(), {"assignment_type": "TEMPORARY"})
    assert result.condition_verdicts["c_temp"] is True
    assert result.needs_review is False


# ------------------------------------------------------------ missing policy

@pytest.mark.parametrize("field,expected", [
    ({"id": "x"}, BLANK),                                  # optional, undeclared
    ({"id": "x", "required": True}, BLOCK),                # required, undeclared
    ({"id": "x", "on_missing": "BLANK"}, BLANK),
    ({"id": "x", "on_missing": "block"}, BLOCK),           # case-insensitive
    ({"id": "x", "on_missing": "DEFAULT"}, DEFAULT),
    ({"id": "x", "on_missing": "REMOVE_SENTENCE"}, REMOVE_SENTENCE),
    # A typo must not silently downgrade to "leave it empty".
    ({"id": "x", "on_missing": "BLNAK"}, BLOCK),
    ({"id": "x", "required": True, "on_missing": "BLANK"}, BLANK),
])
def test_on_missing_resolution(field, expected):
    assert field_on_missing(field) == expected


@pytest.mark.parametrize("raw,expected", [
    ("N/A", None), ("n/a", None), (" - ", None), ("NULL", None), ("none", None),
    ("", ""),                # legitimately blank survives as itself
    ("0", "0"), (0, 0), ("Manager", "Manager"), (None, None),
])
def test_sentinel_normalisation(raw, expected):
    assert normalise_source_value(raw) == expected


# --------------------------------------------------------- sentence removal

@pytest.mark.parametrize("text,needle,kept", [
    ("One. Two here. Three.", "Two", "One. Three."),
    ("Only sentence here.", "Only", ""),
    ("First. Second.", "First", "Second."),
    # An abbreviation must not be read as a sentence end.
    ("Paid by Acme Ltd. of Dublin. Next.", "Dublin", "Next."),
])
def test_sentence_bounds_cuts_the_right_span(text, needle, kept):
    lo, hi = _sentence_bounds(text, text.index(needle))
    assert (text[:lo] + text[hi:]).strip() == kept


# ------------------------------------------------------------ renderer guard

@pytest.mark.parametrize("renderer,editable", [
    (HTML_ASSEMBLY, True),
    (OOXML_FILL, False),
    (DOCX_TEMPLATE_ASSEMBLY, False),
    # A row written before the renderer was recorded. Fails closed: the cost is
    # a legacy document losing in-app editing, not a template losing its layout.
    (None, False),
    ("something_new/2.0", False),
])
def test_html_editability_is_decided_by_renderer(renderer, editable):
    assert is_html_editable(renderer) is editable


# -------------------------------------------------------------- canary set

@pytest.mark.parametrize("count,size,expected", [
    (0, 3, []),
    (1, 3, [0]),
    (3, 3, [0, 1, 2]),
    (10, 3, [0, 3, 6]),
    (1000, 3, [0, 333, 666]),
])
def test_canary_positions_are_spread_across_the_batch(count, size, expected):
    assert _canary_positions(count, size) == expected


def test_canary_never_exceeds_the_batch():
    for count in range(0, 25):
        positions = _canary_positions(count)
        assert len(positions) <= min(count, 3)
        assert all(0 <= p < count for p in positions)
        assert len(set(positions)) == len(positions)
