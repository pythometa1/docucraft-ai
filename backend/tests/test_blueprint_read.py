"""Reading a legacy `.docx` back into something a person can edit.

This is the front half of the loop the product was missing. Every test here is
really one question: does the body describe *this* document, or a document that
merely resembles it? A body that is off by one paragraph, or that puts a merge
field on the wrong side of a dollar sign, still compiles, still approves, and
still produces letters -- wrong ones.
"""

import docx
import pytest

from app.compiler.mapping_agent import _paragraph_texts
from app.templates import blueprint as bp
from app.templates.emit_docx import emit
from app.templates.parsers.docx_prescan import prescan
from app.templates.read_docx import read_body

#: Published with the repository, unlike the client masters in
#: `conftest.CUSTOMER_FIXTURES`, so this module never has to skip.
REAL_TEMPLATE = "tests/fixtures/templates/compensation_letter.docx"


def _p(*segments, style=None):
    return bp.paragraph(segments, style=style)


def _round_trip(tmp_path, body, name="t.docx"):
    """Write a body, read it back. What survives is what the model can hold."""
    path = str(tmp_path / name)
    emit(bp.normalise_body(body), path)
    return read_body(path)


# ---- roles come back as the pre-scanner classified them ----

def test_colour_comes_back_as_a_role(tmp_path):
    body, _notes = _round_trip(tmp_path, {"blocks": [_p(
        bp.segment("static", "Dear "),
        bp.segment("placeholder", "<Name>"),
        bp.segment("instruction", "delete this line"))]})
    assert [(s["role"], s["text"]) for s in body["blocks"][0]["segments"]] == [
        ("static", "Dear "), ("placeholder", "<Name>"), ("instruction", "delete this line")]


def test_a_merge_field_keeps_its_place_in_the_sentence(tmp_path):
    """`$«SALARY» per annum` has to come back in that order. Spans and merge
    fields each know which runs they own and neither knows where it sits
    relative to the other, so the reader orders them by run position -- without
    that the field lands after the prose and the letter reads "$ per annum" with
    a number stranded at the end."""
    body, _notes = _round_trip(tmp_path, {"blocks": [_p(
        bp.segment("static", "$"),
        bp.segment("mergefield", code="LAB__FT_SALARY__38_HR_"),
        bp.segment("static", " per annum"))]})
    segments = body["blocks"][0]["segments"]
    assert [s["role"] for s in segments] == ["static", "mergefield", "static"]
    assert segments[1]["code"] == "LAB__FT_SALARY__38_HR_"


def test_a_hyperlink_keeps_its_own_target(tmp_path):
    """Read from the relationship, not matched by text: two links in one
    paragraph can share their text, and matching on it sends a reader to the
    wrong page."""
    body, _notes = _round_trip(tmp_path, {"blocks": [_p(
        bp.segment("static", "see "),
        bp.segment("hyperlink", "here", target="https://example.com/one"),
        bp.segment("static", " and "),
        bp.segment("hyperlink", "here", target="https://example.com/two"))]})
    links = [s for s in body["blocks"][0]["segments"] if s["role"] == "hyperlink"]
    assert [s["target"] for s in links] == [
        "https://example.com/one", "https://example.com/two"]


def test_a_paragraph_style_survives(tmp_path):
    """Stored as a name rather than the id the XML holds, so a body read from
    one file can be written into another without a lookup table."""
    body, _notes = _round_trip(tmp_path, {"blocks": [
        _p(bp.segment("static", "Offer"), style="Heading 1")]})
    assert body["blocks"][0]["style"] == "Heading 1"


# ---- structure ----

def test_the_table_tree_survives_not_just_the_fact_of_being_in_one(tmp_path):
    """`PreScanResult` reports table membership as a flat set of indices, which
    says a paragraph is in *a* table but not which row or cell. A body that
    cannot say that cannot be written back out."""
    body, _notes = _round_trip(tmp_path, {"blocks": [
        _p(bp.segment("static", "before")),
        bp.table([[[_p(bp.segment("static", "r0c0"))], [_p(bp.segment("static", "r0c1"))]],
                  [[_p(bp.segment("static", "r1c0"))], [_p(bp.segment("static", "r1c1"))]]]),
        _p(bp.segment("static", "after"))]})

    assert [b["kind"] for b in body["blocks"]] == ["paragraph", "table", "paragraph"]
    rows = body["blocks"][1]["rows"]
    assert len(rows) == 2 and len(rows[0]) == 2
    assert bp.paragraph_texts(body) == ["before", "r0c0", "r0c1", "r1c0", "r1c1", "after"]
    assert bp.table_paragraph_indices(body) == {1, 2, 3, 4}


def test_a_body_read_back_is_already_normalised(tmp_path):
    """It was read from a document the pre-scanner had already merged, so no two
    adjacent segments can be ones Word stores as one run."""
    body, _notes = _round_trip(tmp_path, {"blocks": [_p(
        bp.segment("static", "a"), bp.segment("static", "b"),
        bp.segment("placeholder", "<c>"))]})
    assert bp.is_normalised(body)


def test_an_empty_document_reads_as_an_empty_body(tmp_path):
    path = str(tmp_path / "empty.docx")
    docx.Document().save(path)
    body, notes = read_body(path)
    assert body == {"blocks": [], "sect_pr_from": None}
    assert notes == []


# ---- the reader checks itself ----

def test_reading_and_emitting_are_inverses(tmp_path):
    """read -> emit -> read is the fixed point that says the model is faithful
    to what it can hold."""
    original = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Offer"), style="Heading 1"),
        _p(bp.segment("static", "Dear "), bp.segment("placeholder", "<Name>"),
           bp.segment("static", ",")),
        bp.table([[[_p(bp.segment("static", "Salary"))],
                   [_p(bp.segment("static", "$"), bp.segment("mergefield", code="PA"))]]]),
    ], "sect_pr_from": None})

    once, _n = _round_trip(tmp_path, original, "one.docx")
    twice, _n = _round_trip(tmp_path, once, "two.docx")
    assert once == twice


# ---- a real client template ----

def test_a_real_template_reads_without_losing_a_paragraph():
    """The check that matters, run against a genuine compensation letter rather
    than a document this suite wrote for itself: 131 paragraphs, 52 of them
    inside tables, placeholders and author instructions throughout."""
    body, notes = read_body(REAL_TEMPLATE)
    scan = prescan(REAL_TEMPLATE)

    assert bp.paragraph_texts(body) == _paragraph_texts(scan)
    assert bp.table_paragraph_indices(body) == scan.table_paragraph_indices
    assert notes == [], "the reader passed over content it could not model"

    roles = {s["role"] for _i, block, _t in bp.walk_paragraphs(body)
             for s in block["segments"]}
    assert {"static", "placeholder", "instruction"} <= roles


def test_a_real_template_keeps_every_span_it_was_read_from():
    body, _notes = read_body(REAL_TEMPLATE)
    scan = prescan(REAL_TEMPLATE)

    declared = [seg for _i, block, _t in bp.walk_paragraphs(body)
                for seg in block["segments"] if seg["role"] in bp.SPAN_ROLES]
    assert len(declared) == len(scan.spans)
    assert [s["text"] for s in declared] == [s.text for s in scan.spans]


def test_the_reader_refuses_a_body_that_does_not_reproduce_the_document(monkeypatch, tmp_path):
    """The self-check is not decoration. A body that is off by one paragraph
    addresses the wrong text for the rest of the document, and every later stage
    reports success."""
    from app.templates import read_docx

    monkeypatch.setattr(read_docx, "_segments_for",
                        lambda *a, **k: [bp.segment("static", "wrong")])
    path = str(tmp_path / "x.docx")
    emit(bp.normalise_body({"blocks": [_p(bp.segment("static", "right"))]}), path)

    with pytest.raises(bp.BlueprintError, match="does not reproduce"):
        read_docx.read_body(path)
