"""End-to-end golden test: real template + real spreadsheet -> three letters.

This is the single most important test in the suite. It runs the actual
Hospira/Pfizer offer-letter template (207 paragraphs, colour-coded, 12
MERGEFIELDs) against the actual source workbook (3 rows, one per colleague
type) and asserts the thing the product exists to do: each letter keeps its
own conditional blocks and drops the others.

It skips rather than fails when the fixtures aren't present, so a clean
checkout without `backend/storage/` still runs green.
"""

import re
from pathlib import Path

import docx
import pytest

from app.generation.source_resolver import apply_binding, suggest_bindings
from app.templates.parsers.docx_prescan import prescan
from app.generation.docx_renderer import fill_template
from app.generation.source_ingestion import extract_records
from app.compiler.rule_compiler import compile_manifest

# Checked-in fixtures, not blobs under backend/storage/. The paths used to point
# into the runtime upload directory, keyed by the upload ids of one particular
# developer's database -- so this test silently skipped on any other machine, and
# wiping storage to test against a clean install deleted the very files that
# prove the product works.
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = FIXTURES / "templates" / "hospira_offer.docx"
WORKBOOK = FIXTURES / "records" / "colleagues.xlsx"

pytestmark = pytest.mark.skipif(
    not (TEMPLATE.exists() and WORKBOOK.exists()),
    reason="Hospira template/workbook fixtures are missing from backend/tests/fixtures/",
)

# The remuneration figures that belong to each colleague type, straight from the
# workbook. These are the sharpest available signal that the right conditional
# blocks survived: they come from different spreadsheet columns entirely.
FULL_TIME_MONEY = {"82,000.00", "90,200.00", "8,200.00"}
PART_TIME_MONEY = {"48,000.00", "52,800.00", "4,800.00"}


def _document_text(path: Path) -> str:
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


@pytest.fixture(scope="module")
def compiled():
    manifest = compile_manifest(prescan(str(TEMPLATE)))
    return {
        "fields": manifest.fields,
        "conditions": manifest.conditions,
        "blocks": manifest.blocks,
        "delete_always": manifest.delete_always,
    }


@pytest.fixture(scope="module")
def letters(compiled, tmp_path_factory):
    """Generate all three letters once; each test asserts a different property."""
    out_dir = tmp_path_factory.mktemp("letters")
    columns, records = extract_records(str(WORKBOOK), "xlsx")
    bindings = suggest_bindings(compiled, columns).as_field_bindings()

    produced = {}
    for record in records:
        colleague_type = record["Colleague Type"]
        target = out_dir / f"{colleague_type.replace(' ', '_')}.docx"
        result = fill_template(str(TEMPLATE), str(target), compiled, apply_binding(record, bindings))
        produced[colleague_type] = (target, result)
    return produced


def test_the_template_still_compiles(compiled):
    assert len(compiled["fields"]) == 25
    # 3 colleague-type branches plus the two-way recruitment switch. The switch
    # used to compile to nothing -- its instruction names the value with a
    # parenthesised gloss the condition grammar could not read -- and the two
    # paragraphs carrying it were deleted whole, static text and all.
    assert len(compiled["conditions"]) == 5
    assert len(compiled["blocks"]) == 11
    inline = [b for b in compiled["blocks"] if b.get("start_span") is not None]
    assert len(inline) == 4, "the recruitment switch is inline; it needs span-scoped blocks"


def test_one_letter_per_row(letters):
    assert set(letters) == {"Full Time", "Part Time", "Fixed Term"}


@pytest.mark.parametrize("colleague_type", ["Full Time", "Part Time", "Fixed Term"])
def test_every_letter_passes_qa(letters, colleague_type):
    _path, result = letters[colleague_type]
    assert result.qa_passed, result.qa_notes


@pytest.mark.parametrize("colleague_type", ["Full Time", "Part Time", "Fixed Term"])
def test_no_template_artifacts_survive(letters, colleague_type):
    """A leftover <Placeholder>, MERGEFIELD, or instruction line means the
    reader gets scaffolding instead of a letter."""
    text = _document_text(letters[colleague_type][0])
    assert re.findall(r"<[^<>]{1,60}>", text) == []
    assert "MERGEFIELD" not in text
    assert "only if the" not in text.lower()


def test_full_time_letter_carries_only_full_time_remuneration(letters):
    text = _document_text(letters["Full Time"][0])
    assert FULL_TIME_MONEY <= set(re.findall(r"\d{1,3},\d{3}\.\d{2}", text))
    assert not (PART_TIME_MONEY & set(re.findall(r"\d{1,3},\d{3}\.\d{2}", text)))


def test_part_time_letter_carries_only_part_time_remuneration(letters):
    text = _document_text(letters["Part Time"][0])
    found = set(re.findall(r"\d{1,3},\d{3}\.\d{2}", text))
    assert PART_TIME_MONEY <= found
    # 82,000/90,200/8,200 are the Full Time columns and must not appear.
    assert not (FULL_TIME_MONEY & found)


@pytest.mark.parametrize("colleague_type,first,last", [
    ("Full Time", "Olivia", "Williams"),
    ("Part Time", "Liam", "Taylor"),
    ("Fixed Term", "Emma", "Anderson"),
])
def test_each_letter_names_its_own_colleague(letters, colleague_type, first, last):
    text = _document_text(letters[colleague_type][0])
    assert first in text and last in text
    for other_first in {"Olivia", "Liam", "Emma"} - {first}:
        assert other_first not in text, f"{colleague_type} letter leaked another colleague's name"


def test_the_three_letters_are_not_identical(letters):
    texts = {t: _document_text(p) for t, (p, _) in letters.items()}
    assert len(set(texts.values())) == 3, "conditional blocks did not differentiate the letters"


def test_lineage_records_where_each_value_came_from(letters):
    _path, result = letters["Full Time"]
    sources = {entry["source"] for entry in result.field_lineage}
    assert "source_record" in sources
    assert all(entry["field_id"] for entry in result.field_lineage)
    # Condition verdicts are the audit trail for what was kept vs dropped.
    assert len(result.condition_lineage) == 5
    # One colleague-type branch and one recruitment branch: two independent
    # switches, each resolving to exactly one side.
    assert sum(1 for c in result.condition_lineage if c["result"]) == 2


# --------------------------------------------------------------------------
# Regressions found by generating the three letters on a clean install and
# reading the resulting .docx files. Both bugs were invisible to every check
# that existed at the time: QA passed, the API reported "3 generated, 0
# failed", and the dashboard looked perfect.
# --------------------------------------------------------------------------

def _table_rows(path):
    d = docx.Document(str(path))
    return [" | ".join(c.text.strip() for c in row.cells) for t in d.tables for row in t.rows]


def _instruction_texts(path):
    """MERGEFIELD instruction codes still present in the saved document. A
    resolved mergefield has its whole complex-field sequence deleted, so
    anything left here was never filled."""
    d = docx.Document(str(path))
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return [el.text for el in d.element.body.iter(f"{W}instrText") if "MERGEFIELD" in (el.text or "")]


@pytest.mark.parametrize("colleague_type", ["Full Time", "Part Time", "Fixed Term"])
def test_no_unresolved_mergefields_anywhere_including_tables(letters, colleague_type):
    """The Fixed Term letter shipped with the *Full Time* remuneration table
    still in it, showing raw «LAB__FT_SALARY__38_HR_» field codes.

    Cause: dropped tables were collected as `id(tbl)` in one lxml pass and
    looked up by that id in a second pass. lxml element proxies are created on
    demand and freed with their last reference, so `id()` is neither stable
    across passes nor unique -- making the deletion intermittently miss.
    """
    path = letters[colleague_type][0]
    assert _instruction_texts(path) == []
    assert "«" not in _document_text(path)


def test_fixed_term_letter_carries_no_other_types_salary_table(letters):
    rows = _table_rows(letters["Fixed Term"][0])
    joined = " ".join(rows)
    assert "LAB_" not in joined, rows
    for figure in FULL_TIME_MONEY | PART_TIME_MONEY:
        assert figure not in joined, f"Fixed Term letter shows {figure}"


def test_qa_gates_can_see_inside_tables():
    """`Document.paragraphs` skips table cells, so every QA gate was blind to
    the remuneration table -- the part of this template most likely to be wrong
    and the first thing a reader checks."""
    import docx as _docx
    from app.generation.docx_renderer import _q

    d = _docx.Document()
    table = d.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].add_run("Base salary <Annual Salary>")
    body_text = "\n".join(t.text or "" for t in d.element.body.iter(_q("t")))

    assert "<Annual Salary>" not in "\n".join(p.text for p in d.paragraphs), "precondition: body view misses it"
    assert "<Annual Salary>" in body_text, "the XML walk must see table text"
