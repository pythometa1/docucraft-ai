"""The body model, and the one rule that makes it emittable.

Everything here exists to protect a single property: one segment becomes one
span. The pre-scanner merges adjacent runs on colour and hyperlink state, so a
body holding two segments where the document will hold one span describes a file
that does not exist -- and the consequence is not a crash. Every `span_index`
after the merge is one too high, the manifest's slots address the span to their
left, and the fill writes a colleague's salary into the sentence before it.
"""

import pytest

from app.templates import blueprint as bp
from app.templates.parsers.docx_prescan import BLUE_RGBS, RED_RGBS


def _p(*segments, style=None):
    return bp.paragraph(segments, style=style)


# ---- the colours are the pre-scanner's, not this module's ----

def test_the_emitted_colours_are_ones_the_prescanner_classifies():
    """A literal that drifted out of these sets would emit templates that
    compile to an empty manifest -- and the compile would report success,
    because a document with no placeholders is a valid document with none."""
    assert bp.PLACEHOLDER_RGB in BLUE_RGBS
    assert bp.INSTRUCTION_RGB in RED_RGBS


def test_a_hyperlink_is_not_classified_as_a_placeholder():
    """Word's own hyperlink colour, 0563C1, is *in* `BLUE_RGBS`. A link styled
    the way Word styles it would read back as a placeholder and the compiler
    would try to fill it with data, so emitted links carry no colour at all and
    are told apart by their hyperlink flag."""
    assert "0563C1" in BLUE_RGBS
    assert bp.ROLE_COLOR[bp.HYPERLINK] == "black"
    assert bp.HYPERLINK not in bp.ROLE_RGB


# ---- normalisation ----

def test_adjacent_segments_of_the_same_role_become_one():
    """Two placeholders side by side are one run in Word, therefore one span,
    therefore one segment."""
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("placeholder", "<First>"), bp.segment("placeholder", " <Last>"))]})
    segments = body["blocks"][0]["segments"]
    assert [(s["role"], s["text"]) for s in segments] == [("placeholder", "<First> <Last>")]


def test_segments_of_different_roles_are_left_alone():
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Dear "), bp.segment("placeholder", "<Name>"),
           bp.segment("static", ","))]})
    assert len(body["blocks"][0]["segments"]) == 3


def test_a_mergefield_between_two_static_segments_keeps_them_apart():
    """The pre-scanner consumes a merge field's runs and resets the span
    accumulator, so the static text either side stays two spans. A body that
    merged them across the field would predict one span too few."""
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "$"), bp.segment("mergefield", code="SALARY"),
           bp.segment("static", " per annum"))]})
    assert [s["role"] for s in body["blocks"][0]["segments"]] == [
        "static", "mergefield", "static"]


def test_a_hyperlink_between_two_static_segments_keeps_them_apart():
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "See the "),
           bp.segment("hyperlink", "policy", target="https://x/p"),
           bp.segment("static", " for detail."))]})
    assert [s["role"] for s in body["blocks"][0]["segments"]] == [
        "static", "hyperlink", "static"]


def test_two_links_to_different_targets_are_not_merged():
    """They are two `w:hyperlink` wrappers and two relationships. Merging them
    would emit one link and silently lose the other's destination."""
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("hyperlink", "one", target="https://x/1"),
           bp.segment("hyperlink", "two", target="https://x/2"))]})
    assert len(body["blocks"][0]["segments"]) == 2


def test_an_empty_segment_is_dropped_rather_than_written():
    """`prescan` skips a run with no text without breaking the span it was in,
    so an empty segment would be counted here and absent there."""
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "a"), bp.segment("placeholder", ""),
           bp.segment("static", "b"))]})
    # And with the empty one gone the two statics are adjacent, so they merge.
    assert [(s["role"], s["text"]) for s in body["blocks"][0]["segments"]] == [("static", "ab")]


def test_a_merge_that_costs_emphasis_says_so_rather_than_dropping_it_quietly():
    """Bold is not part of the pre-scanner's merge key, so bold text directly
    beside plain text of the same colour cannot survive as its own run. The
    author is told; they are not shown a document that quietly lost it."""
    body, notes = bp.normalise_with_notes({"blocks": [
        _p(bp.segment("static", "Dear "), bp.segment("static", "John", bold=True))]})
    assert len(body["blocks"][0]["segments"]) == 1
    assert notes and "bold" in notes[0] and "John" in notes[0]


def test_normalising_twice_changes_nothing():
    body = {"blocks": [_p(bp.segment("static", "a"), bp.segment("static", "b"),
                          bp.segment("placeholder", "<c>"))]}
    once = bp.normalise_body(body)
    assert bp.normalise_body(once) == once
    assert bp.is_normalised(once)
    assert not bp.is_normalised(body)


def test_normalisation_reaches_inside_table_cells():
    body = bp.normalise_body({"blocks": [
        bp.table([[[_p(bp.segment("static", "a"), bp.segment("static", "b"))]]])]})
    cell = body["blocks"][0]["rows"][0][0]
    assert [(s["role"], s["text"]) for s in cell[0]["segments"]] == [("static", "ab")]


# ---- the walk ----

def test_table_paragraphs_interleave_with_body_ones_in_document_order():
    """The index is not "the nth top-level paragraph": `_walk_paragraphs`
    descends into cells, so a table's paragraphs take indices in the middle of
    the document and everything after them shifts."""
    body = {"blocks": [
        _p(bp.segment("static", "before")),
        bp.table([[[_p(bp.segment("static", "r0c0"))], [_p(bp.segment("static", "r0c1"))]],
                  [[_p(bp.segment("static", "r1c0"))], [_p(bp.segment("static", "r1c1"))]]]),
        _p(bp.segment("static", "after")),
    ]}
    assert bp.paragraph_texts(body) == ["before", "r0c0", "r0c1", "r1c0", "r1c1", "after"]
    assert bp.table_paragraph_indices(body) == {1, 2, 3, 4}


def test_a_table_nested_in_a_cell_still_walks_in_order():
    body = {"blocks": [bp.table([[[
        _p(bp.segment("static", "outer")),
        bp.table([[[_p(bp.segment("static", "inner"))]]]),
    ]]])]}
    assert bp.paragraph_texts(body) == ["outer", "inner"]
    assert bp.table_paragraph_indices(body) == {0, 1}


def test_paragraph_text_sums_spans_and_excludes_merge_fields():
    """It has to match `mapping_agent._paragraph_texts`, which sums `RunSpan`
    text -- that is the string every anchor is resolved against. A merge field
    is not a span, so its code is not in the text."""
    body = {"blocks": [_p(bp.segment("static", "$"),
                          bp.segment("mergefield", code="SALARY"),
                          bp.segment("static", " gross"))]}
    assert bp.paragraph_texts(body) == ["$ gross"]


def test_span_plan_numbers_only_the_segments_that_become_spans():
    segments = [bp.segment("static", "$"), bp.segment("mergefield", code="S"),
                bp.segment("static", " net")]
    assert bp.span_plan(segments) == [(0, 0), (2, 1)]


# ---- refusals ----

def test_an_unknown_role_is_refused_at_construction():
    with pytest.raises(bp.BlueprintError):
        bp.segment("footnote", "x")


def test_an_unknown_block_kind_is_refused_rather_than_skipped():
    """Skipping it would shift every paragraph index after it, which is the one
    failure this module exists to prevent."""
    with pytest.raises(bp.BlueprintError):
        bp.paragraph_texts({"blocks": [{"kind": "sidebar", "segments": []}]})
