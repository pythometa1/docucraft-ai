"""What the gate refuses, and -- just as much -- what it lets through.

A gate that cries wolf is worse than no gate: people learn to click past it, and
then it is not there on the day it is right. Four of these tests exist because a
check fired on a real client template that was correct, and two of those checks
were deleted or downgraded as a result rather than argued with.
"""

import tempfile
from pathlib import Path

import pytest

from app.templates import blueprint as bp
from app.templates.blueprint_lint import (
    ADVISORY, BLOCKING, WARNING, duplicate_slugs, guessed_block_boundaries, legacy_manifest,
    lint, overlapping_regions, typed_objects,
)

REAL_TEMPLATE = "tests/fixtures/templates/compensation_letter.docx"


def _p(*segments):
    return bp.paragraph(segments)


def _body(*texts):
    return bp.normalise_body({"blocks": [_p(bp.segment("static", t)) for t in texts],
                              "sect_pr_from": None})


def _field(object_id="name", **attributes):
    base = {"object_id": object_id, "object_type": "FIELD", "type": "string",
            "slots": [{"kind": "text_match", "text": f"<{object_id}>",
                       "paragraph_index": 0, "span_index": 0}],
            "source_ref": f"source.{object_id}", "format": None, "on_missing": "BLANK",
            "status": "PROPOSED", "anchor": None}
    base.update(attributes)
    return base


def _section(object_id="b1", start=1, end=2, **attributes):
    base = {"object_id": object_id, "object_type": "SECTION", "start_paragraph": start,
            "end_paragraph": end, "repeat_over": None, "ordering": None,
            "empty_behaviour": "REMOVE", "status": "PROPOSED"}
    base.update(attributes)
    return base


def _codes(report):
    return {f.code for f in report.findings}


# ---- the projection every existing check reads ----

def test_the_legacy_lists_are_derived_rather_than_stored_twice():
    """`validate_manifest`, `assertions`, the fill engine and the source resolver
    all read `fields`/`conditions`/`blocks`. Storing that shape beside the
    objects would give the two a way to disagree."""
    manifest = legacy_manifest([_field("full_name"), _section("b1")])
    assert [f["id"] for f in manifest["fields"]] == ["full_name"]
    assert [b["id"] for b in manifest["blocks"]] == ["b1"]
    assert manifest["conditions"] == []


# ---- what approval would refuse, refused earlier ----

def test_a_field_with_no_slot_blocks_a_publish():
    report = lint(_body("nothing here"), [_field("orphan", slots=[])])
    assert "field_without_slot" in _codes(report)
    assert not report.can_publish()


def test_a_disposition_lets_a_blocker_through_and_is_recorded_as_a_choice():
    """The same idiom as a compiler warning: a finding leaves the way by being
    answered, not by being ignored."""
    report = lint(_body("nothing here"), [_field("orphan", slots=[])])
    assert not report.can_publish()
    assert report.can_publish(["field_without_slot"])


# ---- the findings nobody surfaces ----

def test_two_ids_that_slug_to_the_same_key_block():
    """Binding assigns a column to one field, so a collision does not duplicate
    -- it displaces."""
    findings = duplicate_slugs([_field("Full Name"), _field("full_name")])
    assert [f.severity for f in findings] == [BLOCKING]
    assert findings[0].fix == {"op": "rename_field", "id": "full_name"}


def test_a_block_whose_end_the_compiler_guessed_is_a_warning_with_a_fix():
    findings = guessed_block_boundaries([_section("b1", boundary_method="lookahead_cap")])
    assert [f.severity for f in findings] == [WARNING]
    assert findings[0].fix["op"] == "set_block_end"


def test_a_boundary_the_template_actually_stated_is_not_reported():
    assert guessed_block_boundaries([_section("b1", boundary_method="till_hint")]) == []


def test_two_blocks_claiming_the_same_paragraphs_block():
    findings = overlapping_regions([_section("b1", 4, 8), _section("b2", 6, 10)])
    assert [(f.severity, f.code) for f in findings] == [
        (BLOCKING, "overlapping_anchor_ranges")]


def test_the_two_arms_of_an_inline_switch_are_not_an_overlap():
    """The finding that fired twice on a real offer letter, on the arrangement
    that is correct. "For transactions initiated with recruitment" and "without
    recruitment" are two *spans* of one paragraph -- the compiler says so, with
    `start_span`/`end_span` and a boundary method of `inline_zone` -- and the
    fill engine drops one by span. An `AnchorRange` can only speak in
    paragraphs, so at that granularity they look like a conflict."""
    arms = [_section("with", 179, 179, start_span=5, end_span=5, boundary_method="inline_zone"),
            _section("without", 179, 179, start_span=12, end_span=12,
                     boundary_method="inline_zone")]
    assert overlapping_regions(arms) == []


def test_a_condition_reading_a_control_column_is_advice_not_a_defect():
    """The check that would have made the gate unpassable. `colleague_type ==
    'Full time'` reads a name that appears nowhere as a placeholder, and that is
    not a mistake -- the source spreadsheet supplies it and no letter prints it.
    Five of five conditions on a real offer letter are that case."""
    report = lint(_body("intro", "clause"), [
        _section("b1", 1, 1),
        {"object_id": "c1", "object_type": "CONDITION", "expression": "colleague_type == 'x'",
         "keeps_blocks": ["b1"], "on_true": "KEEP", "on_false": "REMOVE_BLOCK",
         "test_cases": [{"in": {"colleague_type": "x"}, "expect": "KEEP"}],
         "start_paragraph": 1, "end_paragraph": 1, "status": "PROPOSED"},
    ])
    needs = [f for f in report.findings if f.code == "condition_needs_a_source_column"]
    assert [f.severity for f in needs] == [ADVISORY]
    assert "colleague_type" in needs[0].detail


def test_a_field_the_organisation_has_never_seen_is_advice():
    report = lint(_body("Dear <name>"), [_field("name")], dictionary=["full_name"])
    advisory = [f for f in report.findings if f.code == "field_not_in_dictionary"]
    assert [f.severity for f in advisory] == [ADVISORY]


def test_no_dictionary_means_no_dictionary_findings():
    """An organisation that has not built a vocabulary is not an organisation
    whose every field is wrong."""
    report = lint(_body("Dear <name>"), [_field("name")])
    assert "field_not_in_dictionary" not in _codes(report)


# ---- the object model ----

def test_an_object_still_proposed_does_not_block_a_publish():
    """PROPOSED is the normal state of a template being written. Refusing to
    publish until somebody has approved every object would make the gate
    unpassable rather than useful -- approval is the manifest's own step."""
    report = lint(_body("intro", "clause", "end"), [_section("b1", 1, 1)])
    assert "object_not_approved" not in _codes(report)


def test_an_ambiguous_anchor_is_a_warning_because_the_fill_does_not_use_anchors():
    """Two fields of a real offer letter sit in boilerplate repeated verbatim at
    paragraphs 179 and 202, identical for sixty characters either side, so
    nothing in the document tells them apart. The fill engine fills through
    `slots`, not anchors, so those letters are correct today -- and there is no
    edit the author could make, which is the test for whether a finding should
    block."""
    body = _body("The same sentence entirely.", "The same sentence entirely.")
    objects = [_field("x", slots=[{"kind": "text_match", "text": "same",
                                   "paragraph_index": 0, "span_index": 0}],
                      anchor=None)]
    report = lint(body, objects)
    assert not [f for f in report.findings
                if f.code == "anchor_does_not_resolve_once" and f.severity == BLOCKING]


def test_a_block_boundary_the_lift_already_repaired_is_not_reported_again():
    """`objects_from_compile` walks a boundary that lands on a blank line inward
    and records where it moved to. Re-lifting from the raw range would
    rediscover the same empty paragraph and call a repaired template unlockable
    -- which it did, six times each on two client masters."""
    body = _body("intro", "", "clause", "")
    section = _section("b1", 1, 3, boundary_adjusted={"from": [1, 3], "to": [2, 2]})
    _typed, findings = typed_objects(body, [section])
    assert findings == []


# ---- the document itself ----

def test_the_emitted_file_is_asked_what_is_still_wrong(tmp_path):
    """The strongest of these checks, because it reads the file rather than the
    description of it."""
    from app.templates.emit_docx import emit

    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Dear "), bp.segment("placeholder", "<Name>")),
        _p(bp.segment("instruction", "Delete this line before sending.")),
    ], "sect_pr_from": None})
    path = str(tmp_path / "t.docx")
    emit(body, path)

    unclaimed = lint(body, [], emitted_path=path)
    assert "uncovered_placeholder" in _codes(unclaimed)
    assert "surviving_instruction" in _codes(unclaimed)


# ---- against a real client template ----

def test_a_real_template_lints_without_a_blocker_it_cannot_earn():
    """A genuine compensation letter, read and cleaned. Its only blockers are
    orphaned fields -- placeholders whose one slot sits in a paragraph the
    compile deletes, which is the defect the orphan gate exists for and which a
    reviewer previously found by reading the finished letter."""
    from app.compiler.rule_compiler import compile_manifest
    from app.templates.emit_docx import emit_from_base
    from app.templates.lift import mark_instructions, objects_from_compile
    from app.templates.parsers.docx_prescan import prescan
    from app.templates.read_docx import read_body

    body, _notes = read_body(REAL_TEMPLATE)
    compiled = compile_manifest(prescan(REAL_TEMPLATE))
    objects, _findings = objects_from_compile(body, compiled)
    body, _cleaned = mark_instructions(body, compiled)

    with tempfile.TemporaryDirectory() as workspace:
        emitted = str(Path(workspace) / "e.docx")
        emit_from_base(body, REAL_TEMPLATE, emitted)
        report = lint(body, objects, emitted_path=emitted,
                      delete_always=compiled.delete_always)

    assert {f.code for f in report.blocking} == {"orphaned_field"}, (
        [f.code for f in report.blocking])
    assert all(f.detail for f in report.findings), "a finding with no explanation is a nag"
