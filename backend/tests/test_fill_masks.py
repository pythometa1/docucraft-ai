"""Slots drawn as a mask, and dates written into the wrong part of one.

Every gate in `placeholder_check` looked for the template's scaffolding written
as `<Colleague Name>`, `«FIELD»` or `[[ENDIF]]`. These contracts mark a slot by
*drawing* it instead -- `本合同生效日期为xxxx年xx月xx日` -- and none of that is a
bracket, so a letter carrying it was reported clean.

Found by auditing a real eleven-template run: the engine had resolved the
contract start date, had no slot to write it into, left `xxxx年xx月xx日` on the
page, and passed the document three times out of three. 395 masks survived across
that one set.

The second check is the more expensive half. A mask is visibly unfilled and a
reader catches it; a date stuffed into all three parts of one is filled, wrong,
and reads as data.
"""

import pytest

from app.qa.placeholder_check import fill_masks_in, malformed_date_parts_in
from app.qa.policy import DATE_PART_MALFORMED, FILL_MASK_REMAINS, REGISTRY


# ------------------------------------------------------------------ masks

@pytest.mark.parametrize("text,expected", [
    ("本合同生效日期为xxxx年xx月xx日。", "xxxx年xx月xx日"),
    ("签订日期：XXXX年XX月XX日", "XXXX年XX月XX日"),
    ("本合同期限为xx个月", "xx个月"),
    ("其中试用期x 个月", "x 个月"),
    ("Effective xx/xx/xxxx", "xx/xx/xxxx"),
])
def test_a_masked_slot_is_caught(text, expected):
    assert expected in fill_masks_in(text)


def test_the_real_sentence_from_the_run():
    """Verbatim from a document the old gate passed."""
    text = ("本合同期限为xx个月，其中试用期x 个月。"
            "本合同生效日期为xxxx年xx月xx日，终止日期为xxxx年xx月xx日。")
    found = fill_masks_in(text)
    assert "xxxx年xx月xx日" in found
    assert "xx个月" in found


@pytest.mark.parametrize("text", [
    # Filled values say nothing.
    "本合同期限为12个月。起始日期为2026年9月1日，终止日期为2027年9月1日。",
    "Your start date is Sep 1, 2026.",
    # A signature line is signed in ink, not filled by the engine. There were 294
    # of these in one contract; a gate that fires on them is a gate reviewers
    # learn to wave through.
    "签订日期：_________年_________月_________日",
    "鉴证机构（盖章）  鉴证时间：_________年_________月_________日",
    # Ordinary prose that happens to contain the unit, or an x.
    "乙方在本合同期限内应当将全部的时间用于履行职务",
    "保密期限为永久",
    "Max Ltd. XXX Holdings",
    "fax: 8个月前",
])
def test_legitimate_text_is_left_alone(text):
    assert fill_masks_in(text) == []


# ------------------------------------------------------- malformed date parts

def test_a_whole_date_in_a_year_slot_is_caught():
    """`xxxx年xx月xx日` is three slots. Bind all three to one date field and each
    gets the entire value."""
    text = "2026-09-01年2026-09-01月2026-09-01"
    found = malformed_date_parts_in(text)
    assert found
    assert any("2026-09-01" in f and ("年" in f or "月" in f) for f in found)


@pytest.mark.parametrize("text", [
    "起始日期为2026年9月1日",          # a properly composed CJK date
    "2026-09-01",                      # a date on its own
    "本合同期限为12个月",
    "签订日期：_________年_________月_________日",
])
def test_a_well_formed_date_is_left_alone(text):
    assert malformed_date_parts_in(text) == []


# ------------------------------------------------------------------ wiring

def test_both_checks_are_registered_and_blocking():
    """A finding nothing can route is a finding nobody sees."""
    for code in (FILL_MASK_REMAINS, DATE_PART_MALFORMED):
        assert code in REGISTRY
        assert REGISTRY[code].default_severity == "blocking"
        assert REGISTRY[code].default_enabled is True


def test_the_scan_reports_them():
    """They have to reach `PlaceholderScan.clean`, or the gate stays green."""
    import docx
    from app.qa.placeholder_check import scan

    d = docx.Document()
    d.add_paragraph("本合同生效日期为xxxx年xx月xx日。")
    found = scan(d.element.body)
    assert found.fill_masks
    assert not found.clean, "a document with an unfilled mask is not clean"
