"""One validated way to change a template, and every way it declines to.

The editor's "Apply fix", the co-pilot and an API caller all post through
`apply_operations`, so most of this file is refusals. That is the point: an
operation that silently does nothing counts as progress, and here that means a
person believing a change landed when it did not, and then publishing.
"""

import pytest

from app.templates import blueprint as bp
from app.templates.blueprint_ops import OPERATIONS, apply_operations


def _body():
    return bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "Dear "),
                      bp.segment("placeholder", "<Name>"),
                      bp.segment("static", ", welcome.")]),
        bp.paragraph([bp.segment("instruction", "Delete before sending.")]),
        bp.paragraph([bp.segment("static", "$"), bp.segment("mergefield", code="SALARY")]),
    ], "sect_pr_from": None})


def _objects():
    return [
        {"object_id": "name", "object_type": "FIELD", "type": "string",
         "slots": [{"kind": "text_match", "text": "<Name>", "paragraph_index": 0, "span_index": 1}],
         "source_ref": "source.name", "on_missing": "BLANK"},
        {"object_id": "c1", "object_type": "CONDITION", "expression": "band == 'senior'",
         "keeps_blocks": ["b1"]},
        {"object_id": "b1", "object_type": "SECTION", "start_paragraph": 1, "end_paragraph": 1},
    ]


def _apply(*ops):
    return apply_operations(_body(), _objects(), list(ops))


def _by_id(objects):
    return {o["object_id"]: o for o in objects}


# ---- what works ----

def test_text_is_replaced_in_the_run_it_names():
    result = _apply({"op": "set_segment_text", "paragraph_index": 0, "span_index": 0,
                     "text": "Hello "})
    assert len(result.applied) == 1 and result.rejected == []
    assert bp.paragraph_texts(result.body)[0] == "Hello <Name>, welcome."


def test_a_run_can_be_reclassified():
    result = _apply({"op": "set_segment_role", "paragraph_index": 0, "span_index": 2,
                     "role": "instruction"})
    assert result.rejected == []
    assert result.body["blocks"][0]["segments"][2]["role"] == "instruction"


def test_an_instruction_is_marked_rather_than_deleted():
    """So the author can put it back. Deleting the run would also renumber every
    span after it in the paragraph."""
    result = _apply({"op": "set_segment_emit", "paragraph_index": 1, "span_index": 0,
                     "emit": False})
    assert result.rejected == []
    assert result.body["blocks"][1]["segments"][0]["emit"] is False
    assert result.body["blocks"][1]["segments"][0]["text"] == "Delete before sending."


def test_renaming_a_field_takes_its_source_ref_with_it():
    result = _apply({"op": "rename_field", "id": "name", "new_id": "full_name"})
    field = _by_id(result.objects)["full_name"]
    assert field["source_ref"] == "source.full_name"


def test_renaming_leaves_a_source_ref_somebody_chose_alone():
    """Overwriting it would silently repoint a field at a different column."""
    objects = _objects()
    objects[0]["source_ref"] = "source.employee_legal_name"
    result = apply_operations(_body(), objects,
                              [{"op": "rename_field", "id": "name", "new_id": "full_name"}])
    assert _by_id(result.objects)["full_name"]["source_ref"] == "source.employee_legal_name"


def test_a_field_can_be_added_over_text_that_is_actually_there():
    result = _apply({"op": "add_field", "id": "greeting", "type": "string",
                     "paragraph_index": 0, "span_index": 0, "match_text": "Dear"})
    assert result.rejected == []
    assert "greeting" in _by_id(result.objects)
    assert result.body["blocks"][0]["segments"][0]["role"] == "placeholder"


def test_a_block_range_can_be_set_and_stops_being_a_guess():
    result = _apply({"op": "set_block_range", "id": "b1", "start_paragraph": 0,
                     "end_paragraph": 2})
    block = _by_id(result.objects)["b1"]
    assert (block["start_paragraph"], block["end_paragraph"]) == (0, 2)
    assert block["boundary_method"] == "author"


# ---- what it refuses ----

def test_an_operation_nobody_defined_is_named_rather_than_ignored():
    result = _apply({"op": "teleport", "id": "x"})
    assert result.applied == []
    assert "teleport" in result.rejected[0]["reason"]


def test_an_edit_to_a_run_that_is_not_there_is_refused():
    result = _apply({"op": "set_segment_text", "paragraph_index": 99, "span_index": 0,
                     "text": "x"})
    assert result.applied == []
    assert "no run at paragraph 99" in result.rejected[0]["reason"]


def test_an_edit_that_changes_nothing_is_refused():
    """Applying it would count as progress -- and progress is what tells a person
    their change landed."""
    result = _apply({"op": "set_segment_text", "paragraph_index": 0, "span_index": 0,
                     "text": "Dear "})
    assert result.applied == []
    assert "already reads" in result.rejected[0]["reason"]


def test_text_with_a_line_break_is_refused():
    """Word stores a break as an element, not a character, so a run containing
    one renders as nothing and the words disappear from the letter."""
    result = _apply({"op": "set_segment_text", "paragraph_index": 0, "span_index": 0,
                     "text": "Dear\nColleague"})
    assert result.applied == []
    assert "line break" in result.rejected[0]["reason"]


def test_a_merge_field_cannot_be_edited_as_text():
    """It carries its meaning in the document's own field structure; rewriting
    its text would leave a field that no longer names anything."""
    body = _body()
    result = apply_operations(body, _objects(), [
        {"op": "set_segment_text", "paragraph_index": 2, "span_index": 1, "text": "x"}])
    # Span 1 of paragraph 2 does not exist -- a merge field is not a span -- so
    # this is refused for that reason, which is the same protection.
    assert result.applied == []


def test_a_field_added_over_text_that_is_not_there_is_refused():
    result = _apply({"op": "add_field", "id": "ghost", "type": "string",
                     "paragraph_index": 0, "span_index": 0, "match_text": "Sincerely"})
    assert result.applied == []
    assert "does not appear" in result.rejected[0]["reason"]


def test_a_duplicate_field_id_is_refused():
    result = _apply({"op": "add_field", "id": "name", "type": "string",
                     "paragraph_index": 0, "span_index": 0, "match_text": "Dear"})
    assert result.applied == []
    assert "already exists" in result.rejected[0]["reason"]


def test_a_type_the_renderer_cannot_format_is_refused():
    result = _apply({"op": "retype_field", "id": "name", "type": "colour"})
    assert result.applied == []
    assert "can format" in result.rejected[0]["reason"]


def test_a_condition_rewritten_as_prose_is_refused():
    """It would evaluate false for every record and remove its section from every
    document, silently."""
    result = _apply({"op": "rewrite_condition", "id": "c1",
                     "expression": "if the employee is eligible"})
    assert result.applied == []
    assert "not something the engine can run" in result.rejected[0]["reason"]


def test_a_default_policy_with_nothing_to_fall_back_to_is_refused():
    result = _apply({"op": "set_on_missing", "id": "name", "on_missing": "DEFAULT"})
    assert result.applied == []
    assert "fall back to" in result.rejected[0]["reason"]


def test_a_block_range_that_runs_backwards_is_refused():
    result = _apply({"op": "set_block_range", "id": "b1", "start_paragraph": 2,
                     "end_paragraph": 0})
    assert result.applied == []
    assert "backwards" in result.rejected[0]["reason"]


def test_a_block_range_past_the_end_of_the_document_is_refused():
    result = _apply({"op": "set_block_range", "id": "b1", "start_paragraph": 0,
                     "end_paragraph": 99})
    assert result.applied == []
    assert "addresses text that is not there" in result.rejected[0]["reason"]


def test_one_bad_operation_does_not_cost_the_good_ones_beside_it():
    result = _apply(
        {"op": "rename_field", "id": "name", "new_id": "full_name"},
        {"op": "teleport"},
        {"op": "set_segment_text", "paragraph_index": 0, "span_index": 0, "text": "Hello "})
    assert len(result.applied) == 2
    assert len(result.rejected) == 1


# ---- the shape of the batch ----

def test_the_body_is_normalised_once_at_the_end():
    """Normalising between operations would renumber spans underneath the ones
    that had not run yet, so a batch naming two spans of one paragraph would
    apply the first correctly and the second to the wrong run."""
    result = _apply(
        {"op": "set_segment_role", "paragraph_index": 0, "span_index": 1, "role": "static"},
        {"op": "set_segment_text", "paragraph_index": 0, "span_index": 2, "text": " and welcome."})
    assert len(result.applied) == 2
    assert bp.is_normalised(result.body)


def test_every_operation_the_agent_can_propose_is_one_the_applier_knows():
    """The translation is mechanical precisely so it cannot invent an operation
    nothing implements."""
    from app.compiler.blueprint_agent import _TRANSLATION

    assert {operation for operation, _keys in _TRANSLATION.values()} <= set(OPERATIONS)


# ---- the remaining guards, each one a way to be silently wrong ----

def test_a_hyperlink_cannot_be_edited_as_text():
    """Its destination lives in a relationship, not in the run, so rewriting the
    words would leave a link pointing somewhere they no longer describe."""
    body = bp.normalise_body({"blocks": [bp.paragraph([
        bp.segment("hyperlink", "policy", target="https://x/p")])], "sect_pr_from": None})
    result = apply_operations(body, [], [
        {"op": "set_segment_text", "paragraph_index": 0, "span_index": 0, "text": "rules"}])
    assert result.applied == []
    assert "structure" in result.rejected[0]["reason"]


def test_a_role_that_is_not_one_of_the_three_is_refused():
    result = _apply({"op": "set_segment_role", "paragraph_index": 0, "span_index": 0,
                     "role": "mergefield"})
    assert result.applied == []
    assert "static, placeholder, instruction" in result.rejected[0]["reason"]


def test_reclassifying_a_run_to_what_it_already_is_is_refused():
    result = _apply({"op": "set_segment_role", "paragraph_index": 0, "span_index": 0,
                     "role": "static"})
    assert result.applied == []
    assert "already static" in result.rejected[0]["reason"]


def test_an_instruction_can_be_put_back():
    body = _body()
    body["blocks"][1]["segments"][0]["emit"] = False
    result = apply_operations(body, _objects(), [
        {"op": "set_segment_emit", "paragraph_index": 1, "span_index": 0, "emit": True}])
    assert len(result.applied) == 1
    assert "emit" not in result.body["blocks"][1]["segments"][0]


def test_a_field_added_with_no_id_is_refused():
    result = _apply({"op": "add_field", "type": "string", "paragraph_index": 0,
                     "span_index": 0, "match_text": "Dear"})
    assert result.applied == []
    assert "needs an id" in result.rejected[0]["reason"]


def test_adding_a_field_to_a_run_that_is_not_there_is_refused():
    result = _apply({"op": "add_field", "id": "x", "type": "string", "paragraph_index": 9,
                     "span_index": 0, "match_text": "Dear"})
    assert result.applied == []
    assert "no run at paragraph 9" in result.rejected[0]["reason"]


def test_a_field_can_be_removed():
    result = _apply({"op": "remove_field", "id": "name"})
    assert len(result.applied) == 1
    assert "name" not in _by_id(result.objects)


def test_a_condition_can_be_removed():
    result = _apply({"op": "remove_condition", "id": "c1"})
    assert len(result.applied) == 1
    assert "c1" not in _by_id(result.objects)


def test_removing_something_that_is_not_there_is_refused():
    assert _apply({"op": "remove_field", "id": "ghost"}).applied == []
    assert _apply({"op": "remove_condition", "id": "ghost"}).applied == []


def test_removing_a_field_by_naming_a_condition_is_refused():
    """The id exists, so a check on existence alone would delete the wrong
    object."""
    result = _apply({"op": "remove_field", "id": "c1"})
    assert result.applied == []
    assert "no field called 'c1'" in result.rejected[0]["reason"]


def test_a_rename_with_no_new_id_is_refused():
    result = _apply({"op": "rename_field", "id": "name", "new_id": ""})
    assert result.applied == []
    assert "needs a new id" in result.rejected[0]["reason"]


def test_a_rename_onto_a_name_already_taken_is_refused():
    result = _apply({"op": "rename_field", "id": "name", "new_id": "c1"})
    assert result.applied == []
    assert "already taken" in result.rejected[0]["reason"]


def test_renaming_something_that_is_not_a_field_is_refused():
    assert _apply({"op": "rename_field", "id": "b1", "new_id": "x"}).applied == []
    assert _apply({"op": "retype_field", "id": "b1", "type": "date"}).applied == []


def test_a_default_policy_with_a_value_is_accepted():
    result = _apply({"op": "set_on_missing", "id": "name", "on_missing": "DEFAULT",
                     "default": "Colleague"})
    assert len(result.applied) == 1
    assert _by_id(result.objects)["name"]["default"] == "Colleague"


def test_a_missing_value_policy_nobody_defined_is_refused():
    result = _apply({"op": "set_on_missing", "id": "name", "on_missing": "SHRUG"})
    assert result.applied == []
    assert "not a missing-value policy" in result.rejected[0]["reason"]


def test_setting_a_policy_on_something_that_is_not_there_is_refused():
    assert _apply({"op": "set_on_missing", "id": "ghost", "on_missing": "BLANK"}).applied == []


def test_a_condition_rewritten_to_nothing_is_refused():
    result = _apply({"op": "rewrite_condition", "id": "c1", "expression": "   "})
    assert result.applied == []
    assert "governs nothing" in result.rejected[0]["reason"]


def test_rewriting_something_that_is_not_a_condition_is_refused():
    assert _apply({"op": "rewrite_condition", "id": "name",
                   "expression": "a == 'b'"}).applied == []


def test_a_condition_can_be_rewritten_to_something_runnable():
    result = _apply({"op": "rewrite_condition", "id": "c1", "expression": "band == 'lead'"})
    assert len(result.applied) == 1
    assert _by_id(result.objects)["c1"]["expression"] == "band == 'lead'"


def test_a_block_range_with_no_numbers_is_refused():
    result = _apply({"op": "set_block_range", "id": "b1", "start_paragraph": "one",
                     "end_paragraph": 2})
    assert result.applied == []
    assert "two paragraph numbers" in result.rejected[0]["reason"]


def test_setting_a_range_on_something_that_is_not_a_block_is_refused():
    assert _apply({"op": "set_block_range", "id": "name", "start_paragraph": 0,
                   "end_paragraph": 1}).applied == []


def test_setting_a_range_clears_the_boundary_the_lift_had_repaired():
    """Once an author has said where a block ends, the adjustment the lift made
    to a blank-line boundary is history rather than the current answer."""
    objects = _objects()
    objects[2]["boundary_adjusted"] = {"from": [1, 3], "to": [1, 1]}
    result = apply_operations(_body(), objects, [
        {"op": "set_block_range", "id": "b1", "start_paragraph": 0, "end_paragraph": 1}])
    assert "boundary_adjusted" not in _by_id(result.objects)["b1"]


def test_an_empty_batch_changes_nothing_and_complains_about_nothing():
    result = _apply()
    assert result.applied == [] and result.rejected == []
