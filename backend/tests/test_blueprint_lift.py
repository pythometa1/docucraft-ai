"""Attaching a compile to a body, without letting one bad object cost the rest.

The lift is deterministic -- the compiler has already interpreted, this only
places -- so every test here is about placement and about failure. Placement:
does the anchor address the occurrence the slot actually held. Failure: a legacy
template *will* produce a condition whose block starts on a blank line, and the
question is whether that costs one object or the whole template.
"""

import pytest

from app.compiler.rule_compiler import compile_manifest
from app.manifests.models import ManifestEnvelope, to_legacy_objects, to_row_values
from app.templates import blueprint as bp
from app.templates.lift import (
    _synthesised_test_cases, mark_instructions, objects_from_compile, reslot_against,
)
from app.templates.parsers.docx_prescan import prescan
from app.templates.read_docx import read_body
from app.templates.semantic_model import KEEP, PROPOSED, REMOVE_BLOCK, resolve

REAL_TEMPLATE = "tests/fixtures/templates/compensation_letter.docx"


def _p(*segments):
    return bp.paragraph(segments)


def _compiled(**lists):
    class _Compiled:
        fields = lists.get("fields", [])
        conditions = lists.get("conditions", [])
        blocks = lists.get("blocks", [])
        delete_always = lists.get("delete_always", [])
    return _Compiled()


def _body(*texts):
    return bp.normalise_body({"blocks": [_p(bp.segment("static", t)) for t in texts],
                              "sect_pr_from": None})


def _by_id(objects):
    return {o["object_id"]: o for o in objects}


# ---- placement ----

def test_a_field_is_given_an_anchor_that_resolves_to_its_own_occurrence():
    """`lift_slot_to_anchor` refuses to guess when a token repeats -- assuming
    the first would put the value in the wrong half of "report to <Manager>,
    copying <Manager>" -- so the lift counts occurrences as the compiler emits
    them, in document order."""
    body = _body("report to <Manager>, copying <Manager>")
    compiled = _compiled(fields=[{
        "id": "manager", "type": "string", "slots": [
            {"kind": "text_match", "text": "<Manager>", "paragraph_index": 0, "span_index": 0},
            {"kind": "text_match", "text": "<Manager>", "paragraph_index": 0, "span_index": 1},
        ]}])

    objects, findings = objects_from_compile(body, compiled)
    anchor = _by_id(objects)["manager"]["anchor"]

    assert anchor["ordinal"] == 1
    assert anchor["token"] == "<Manager>"
    assert [f for f in findings if f["severity"] == "blocking"] == []


def test_a_merge_field_is_lifted_to_a_merge_field_anchor_not_a_run_path():
    """MERGEFIELD outranks a run path for stability; rewriting a stable anchor
    as a less stable one to keep the code uniform is a downgrade."""
    body = bp.normalise_body({"blocks": [_p(
        bp.segment("static", "$"), bp.segment("mergefield", code="SALARY_PA"))]})
    compiled = _compiled(fields=[{
        "id": "salary_pa", "type": "currency",
        "slots": [{"kind": "mergefield", "code": "SALARY_PA", "paragraph_index": 0}]}])

    anchor = _by_id(objects_from_compile(body, compiled)[0])["salary_pa"]["anchor"]
    assert anchor["kind"] == "mergefield"
    assert anchor["field_code"] == "SALARY_PA"


def test_a_field_asks_its_source_for_a_column_named_after_it():
    """`semantic_model` requires a non-empty source_ref, and is right to: a
    field that reads nothing fills nothing. The compiler's slug is exactly what
    `suggest_bindings` matches a column against."""
    objects, _f = objects_from_compile(_body("Dear <Name>"), _compiled(fields=[
        {"id": "name", "type": "string",
         "slots": [{"kind": "text_match", "text": "<Name>", "paragraph_index": 0, "span_index": 0}]}]))
    field = _by_id(objects)["name"]
    assert field["source_ref"] == "source.name"
    assert field["on_missing"] == "BLANK", "an unbound field blanks; it does not block every letter"
    assert field["status"] == PROPOSED


def test_a_condition_is_anchored_over_the_union_of_the_blocks_it_governs():
    body = _body("intro", "clause one", "clause two", "outro")
    compiled = _compiled(
        blocks=[{"id": "b1", "start_paragraph": 1, "end_paragraph": 1},
                {"id": "b2", "start_paragraph": 2, "end_paragraph": 2}],
        conditions=[{"id": "c1", "expression": "band == 'senior'", "keeps_blocks": ["b1", "b2"]}])

    objects, findings = objects_from_compile(body, compiled)
    condition = _by_id(objects)["c1"]

    assert condition["anchor_range"] == {"from": "body/p[1]", "to": "body/p[2]"}
    assert condition["on_true"] == KEEP and condition["on_false"] == REMOVE_BLOCK
    assert condition["input_fields"] == ["band"]
    assert [f for f in findings if f["severity"] == "blocking"] == []


# ---- one bad object costs one object ----

def test_a_block_starting_on_a_blank_line_is_walked_inward_rather_than_discarded():
    """An empty paragraph cannot anchor anything -- an empty token matches every
    blank line -- so `lift_block_to_anchor_range` raises on one. That is a block
    whose boundary is a line off, not a broken block. Measured on two real client
    masters: six of them each."""
    body = _body("intro", "", "clause", "")
    compiled = _compiled(blocks=[{"id": "b1", "start_paragraph": 1, "end_paragraph": 3}])

    objects, findings = objects_from_compile(body, compiled)
    section = _by_id(objects)["b1"]

    assert section["anchor_range"] == {"from": "body/p[2]", "to": "body/p[2]"}
    assert section["boundary_adjusted"] == {"from": [1, 3], "to": [2, 2]}
    assert [f["code"] for f in findings if f["code"] == "block_boundary_adjusted"]
    assert [f for f in findings if f["severity"] == "blocking"] == []


def test_a_block_with_no_text_anywhere_in_range_is_a_finding_not_an_exception():
    body = _body("intro", "", "", "outro")
    compiled = _compiled(blocks=[{"id": "b1", "start_paragraph": 1, "end_paragraph": 2}])

    objects, findings = objects_from_compile(body, compiled)
    assert _by_id(objects)["b1"].get("anchor_range") is None
    assert [f["code"] for f in findings if f["severity"] == "blocking"] == [
        "block_boundary_unanchorable"]


def test_one_unliftable_field_does_not_cost_the_others():
    """The whole reason objects are built one at a time. A template that cannot
    be opened cannot be fixed, and fixing it is what the editor is for."""
    body = _body("Dear <Name>")
    compiled = _compiled(fields=[
        {"id": "name", "type": "string",
         "slots": [{"kind": "text_match", "text": "<Name>", "paragraph_index": 0, "span_index": 0}]},
        {"id": "ghost", "type": "string",
         "slots": [{"kind": "text_match", "text": "<Absent>", "paragraph_index": 0, "span_index": 1}]},
    ])

    objects, findings = objects_from_compile(body, compiled)
    assert _by_id(objects)["name"]["anchor"] is not None
    assert _by_id(objects)["ghost"]["anchor"] is None
    assert [f["code"] for f in findings] == ["field_anchor_unliftable"]


def test_a_field_with_nowhere_to_go_is_blocking():
    objects, findings = objects_from_compile(
        _body("nothing here"), _compiled(fields=[{"id": "orphan", "type": "string", "slots": []}]))
    assert [(f["severity"], f["code"]) for f in findings] == [("blocking", "field_without_slot")]


def test_a_condition_governing_no_block_is_blocking():
    """Whichever way it evaluates, the letter comes out the same -- so it is not
    a condition, it is a decoration that reads like one."""
    _objects, findings = objects_from_compile(_body("intro"), _compiled(
        conditions=[{"id": "c1", "expression": "band == 'x'", "keeps_blocks": ["gone"]}]))
    assert [f["code"] for f in findings if f["severity"] == "blocking"] == [
        "condition_governs_nothing"]


# ---- test cases ----

def test_generated_test_cases_record_what_the_engine_does_and_say_so():
    """A guessed expectation would fail `run_test_cases` and turn a reviewable
    condition into an unlockable one, so the expectation is measured."""
    cases = _synthesised_test_cases("colleague_type == 'Full time'",
                                    on_true=KEEP, on_false=REMOVE_BLOCK)
    assert len(cases) == 2
    assert {c["expect"] for c in cases} == {KEEP, REMOVE_BLOCK}
    assert all(c["generated"] for c in cases)
    matching = next(c for c in cases if c["expect"] == KEEP)
    assert matching["in"] == {"colleague_type": "Full time"}


def test_a_condition_arrives_with_test_cases_and_a_finding_asking_for_real_ones():
    body = _body("intro", "clause")
    compiled = _compiled(
        blocks=[{"id": "b1", "start_paragraph": 1, "end_paragraph": 1}],
        conditions=[{"id": "c1", "expression": "band == 'senior'", "keeps_blocks": ["b1"]}])

    objects, findings = objects_from_compile(body, compiled)
    assert _by_id(objects)["c1"]["test_cases"]
    assert "generated_test_case_unreviewed" in {f["code"] for f in findings}


# ---- instructions ----

def test_an_instruction_the_compile_removes_is_marked_not_deleted():
    """"The AI re-maps it properly" made visible -- and reversible, because the
    author is the one who decides whether a red note was addressed to them."""
    body = bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Dear Priya,")),
        _p(bp.segment("instruction", "Delete this line before sending.")),
    ], "sect_pr_from": None})
    compiled = _compiled(delete_always=[{"paragraph_index": 1, "span_index": 0}])

    body, marked = mark_instructions(body, compiled)
    assert marked == 1
    assert body["blocks"][1]["segments"][0]["emit"] is False
    assert "emit" not in body["blocks"][0]["segments"][0]


# ---- the whole thing, on a real client template ----

def test_a_real_template_lifts_with_every_field_addressed():
    """131 paragraphs of a genuine compensation letter, 50 fields, no blocking
    finding and no exception."""
    body, _notes = read_body(REAL_TEMPLATE)
    compiled = compile_manifest(prescan(REAL_TEMPLATE))
    objects, findings = objects_from_compile(body, compiled)

    fields = [o for o in objects if o["object_type"] == "FIELD"]
    assert len(fields) == len(compiled.fields)
    assert all(o["anchor"] for o in fields), "a field with no address cannot be checked for drift"
    assert [f for f in findings if f["severity"] == "blocking"] == []


def test_every_lifted_anchor_still_resolves_against_the_template():
    body, _notes = read_body(REAL_TEMPLATE)
    texts = bp.paragraph_texts(body)
    compiled = compile_manifest(prescan(REAL_TEMPLATE))
    objects, _findings = objects_from_compile(body, compiled)

    from app.templates.semantic_model import Anchor

    for obj in objects:
        anchor = obj.get("anchor")
        if not anchor or anchor.get("kind") != "run_path":
            continue
        resolve(Anchor(**anchor), texts)          # raises unless it matches exactly once


def test_the_legacy_lists_still_say_exactly_what_the_compiler_said():
    """The fill engine, the validator and the source resolver read
    `fields`/`conditions`/`blocks`. If lifting changed them, every one of those
    would be reading a different manifest from the one that was compiled."""
    body, _notes = read_body(REAL_TEMPLATE)
    compiled = compile_manifest(prescan(REAL_TEMPLATE))
    objects, _findings = objects_from_compile(body, compiled)

    envelope = ManifestEnvelope(
        manifest_id="mf_1", manifest_version=1, status="DRAFT", organization_id="org_1",
        template_version_id="tv_1", objects=objects)
    legacy = to_legacy_objects(envelope)

    assert len(legacy["fields"]) == len(compiled.fields)
    slots_by_id = {f["id"]: f["slots"] for f in legacy["fields"]}
    for field in compiled.fields:
        assert slots_by_id[field["id"]] == field["slots"]

    assert to_row_values(envelope, objects_column=True).is_complete()


# ---- re-addressing a manifest to the file being published ----

def test_a_slot_is_re_addressed_to_the_span_it_ends_up_in():
    """Publishing renumbers spans. An author instruction the compile removes is
    written as an empty run, an empty run is invisible to the pre-scanner, and
    the runs either side of it therefore merge -- so removing one instruction can
    cost two spans and shift everything after it. A manifest carrying the old
    numbering would fill the span to the left of every later slot."""
    published = bp.normalise_body({"blocks": [bp.paragraph([
        bp.segment("static", "Signed  by "),
        bp.segment("placeholder", "<Name>")])], "sect_pr_from": None})

    objects = [{"object_id": "name", "object_type": "FIELD", "type": "string",
                "slots": [{"kind": "text_match", "text": "<Name>",
                           "paragraph_index": 0, "span_index": 3}]}]

    reslotted, findings = reslot_against(objects, published)
    assert findings == []
    assert reslotted[0]["slots"][0]["span_index"] == 1


def test_a_slot_is_found_inside_the_span_that_carries_it():
    """A slot's `text` is the token; the span holding it carries more -- "Dear
    <Name>," -- and after a merge it carries more again. Containment is what the
    fill engine uses when it writes a value into a span."""
    published = bp.normalise_body({"blocks": [bp.paragraph([
        bp.segment("static", "Dear <Name>, welcome")])], "sect_pr_from": None})
    objects = [{"object_id": "name", "object_type": "FIELD",
                "slots": [{"kind": "text_match", "text": "<Name>",
                           "paragraph_index": 0, "span_index": 9}]}]

    reslotted, findings = reslot_against(objects, published)
    assert findings == []
    assert reslotted[0]["slots"][0]["span_index"] == 0


def test_two_merge_field_slots_sharing_a_code_take_different_paragraphs():
    """The same salary code appears in a Full Time table and a Part Time one.
    Sending both slots to the first would make the fill engine work twice on one
    field -- and it detaches a field's runs when it replaces them, so the second
    pass operates on elements no longer in the document."""
    published = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("mergefield", code="SALARY")]),
        bp.paragraph([bp.segment("mergefield", code="SALARY")]),
    ], "sect_pr_from": None})
    objects = [{"object_id": "salary", "object_type": "FIELD", "slots": [
        {"kind": "mergefield", "code": "SALARY", "paragraph_index": 0},
        {"kind": "mergefield", "code": "SALARY", "paragraph_index": 1}]}]

    reslotted, findings = reslot_against(objects, published)
    assert findings == []
    assert [s["paragraph_index"] for s in reslotted[0]["slots"]] == [0, 1]


def test_a_slot_the_published_template_does_not_carry_is_blocking():
    """A placeholder that lived inside a removed instruction goes with it. The
    field can then never be filled, and saying so at publish is the last moment
    it is cheap."""
    published = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "nothing here")])], "sect_pr_from": None})
    objects = [{"object_id": "gone", "object_type": "FIELD", "slots": [
        {"kind": "text_match", "text": "<Gone>", "paragraph_index": 0, "span_index": 0}]}]

    reslotted, findings = reslot_against(objects, published)
    assert [(f["severity"], f["code"]) for f in findings] == [
        ("blocking", "slot_not_in_published_template")]
    assert reslotted[0]["slots"] == []


def test_objects_that_are_not_fields_pass_through_untouched():
    section = {"object_id": "b1", "object_type": "SECTION", "start_paragraph": 1}
    reslotted, findings = reslot_against([section], bp.normalise_body(
        {"blocks": [], "sect_pr_from": None}))
    assert reslotted == [section] and findings == []
