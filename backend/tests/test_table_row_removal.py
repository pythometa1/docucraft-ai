"""A conditional row must not take its table with it.

`fill_template` removed the whole table whenever a dropped paragraph lived
inside one. That is right for a table which is wholly conditional -- a Full Time
remuneration table standing next to a Part Time one -- and catastrophic for a
table that merely contains a conditional row.

Measured on the compensation template: a 16-row salary breakdown carrying three
[[IF ...]] rows for variable pay, joining bonus and retention bonus. An employee
with no retention bonus dropped one row and lost the entire table -- basic
salary, HRA, conveyance, LTA, PF, gratuity, all of it -- from a letter whose
entire subject is their compensation. Three golden fixtures had that damage
recorded as expected output, so the suite asserted the bug was correct.

Row-level removal subsumes the old rule rather than contradicting it: a table
whose every row is dropped loses every row, and the empty frame is then removed.
Both tests below are needed, because a fix for one case that breaks the other is
exactly how this got here.
"""

from pathlib import Path

import docx
import pytest

from app.generation.docx_renderer import fill_template

FIXTURES = Path(__file__).parent / "fixtures"


def _tables(path):
    return docx.Document(str(path)).tables


def _first_cells(table):
    return [r.cells[0].text.strip() for r in table.rows]


def _build(tmp_path, rows):
    """A one-table document whose rows carry the given first-column text."""
    d = docx.Document()
    table = d.add_table(rows=len(rows), cols=2)
    for i, text in enumerate(rows):
        table.rows[i].cells[0].text = text
        table.rows[i].cells[1].text = f"value {i}"
    path = tmp_path / "t.docx"
    d.save(str(path))
    return path


def _paragraph_index_of(path, needle):
    """Index in the prescan walk of the paragraph containing `needle`."""
    from app.compiler.mapping_agent import _paragraph_texts
    from app.templates.parsers.docx_prescan import prescan

    for i, text in enumerate(_paragraph_texts(prescan(str(path)))):
        if needle in text:
            return i
    raise AssertionError(f"no paragraph containing {needle!r}")


def test_dropping_one_row_keeps_the_rest_of_the_table(tmp_path):
    """The defect, reduced. One doomed row, four survivors."""
    template = _build(tmp_path, ["Basic salary", "House rent", "DROP ME", "Gratuity"])
    doomed = _paragraph_index_of(template, "DROP ME")
    manifest = {
        "fields": [], "conditions": [], "blocks": [],
        "delete_always": [{"paragraph_index": doomed, "span_index": 0}],
    }
    out = tmp_path / "out.docx"
    fill_template(str(template), str(out), manifest, {})

    tables = _tables(out)
    assert len(tables) == 1, "a single dropped row destroyed the whole table"
    kept = _first_cells(tables[0])
    assert "DROP ME" not in kept
    assert kept == ["Basic salary", "House rent", "Gratuity"]


def test_a_wholly_conditional_table_still_goes(tmp_path):
    """The behaviour the old rule got right, which the fix must not lose.

    A table every row of which is dropped would otherwise survive as an empty
    frame: borders around nothing, which reads as a rendering fault rather than
    as an omission.
    """
    template = _build(tmp_path, ["DROP A", "DROP B"])
    manifest = {
        "fields": [], "conditions": [], "blocks": [],
        "delete_always": [
            {"paragraph_index": _paragraph_index_of(template, "DROP A"), "span_index": 0},
            {"paragraph_index": _paragraph_index_of(template, "DROP B"), "span_index": 0},
        ],
    }
    out = tmp_path / "out.docx"
    fill_template(str(template), str(out), manifest, {})
    assert _tables(out) == [], "an emptied table must be removed, not left as an empty frame"


@pytest.mark.parametrize("employee,expected_rows", [
    ("compensation_in10001", 14),
    ("compensation_in10002", 15),
    ("compensation_in10009", 12),
])
def test_the_golden_letters_carry_their_compensation_table(employee, expected_rows):
    """The regression, held against the real fixtures.

    Row counts differ per employee because the conditional rows differ: IN10002
    has all three bonuses, IN10009 none. A single number for all three would
    pass while the conditional rows were being ignored.
    """
    golden = FIXTURES / "goldens" / employee / "output.docx"
    tables = [t for t in _tables(golden) if t.rows[0].cells[0].text.strip() == "Component"]
    assert tables, f"{employee} has no compensation table -- the salary breakdown is missing"
    assert len(tables[0].rows) == expected_rows
    body = "\n".join(c.text for r in tables[0].rows for c in r.cells)
    assert "Basic salary" in body
