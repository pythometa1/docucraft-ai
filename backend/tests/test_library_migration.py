"""Turning the token library into templates that can actually fill a document.

Those entries were authored out of four coloured tokens serialising to
`<span data-token>` HTML, and that HTML can never become a manifest -- the fill
engine works on OOXML runs addressed by position, and an HTML span is neither.
So every one of them is real authoring work that could not be used, and the
question each test here asks is whether the migration is honest about what
survives the trip.
"""

from app.templates import blueprint as bp
from app.templates.library_migration import migrate


def _texts(body):
    return bp.paragraph_texts(body)


def _codes(findings):
    return {f["code"] for f in findings}


def test_a_source_token_becomes_a_placeholder_and_a_field():
    body, objects, findings = migrate(
        '<p>Dear <span data-token="source" field="full_name"></span>,</p>')

    assert _texts(body) == ["Dear <full_name>,"]
    assert [(o["object_type"], o["object_id"]) for o in objects] == [("FIELD", "full_name")]
    assert objects[0]["slots"][0]["text"] == "<full_name>"
    assert findings == []


def test_the_placeholder_is_written_in_the_brackets_the_prescanner_reads():
    """So a migrated template and a legacy one are the same kind of document.
    Anything else would need the pre-scanner to learn a second convention."""
    body, _objects, _findings = migrate(
        '<p><span data-token="source" field="annual_salary"></span></p>')
    from app.templates.parsers.docx_prescan import extract_brackets

    assert extract_brackets(_texts(body)[0]) == ["annual_salary"]


def test_a_fallback_becomes_a_declared_default_rather_than_being_lost():
    _body, objects, _findings = migrate(
        '<p><span data-token="source" field="city" fallback="London"></span></p>')
    assert objects[0]["on_missing"] == "DEFAULT"
    assert objects[0]["default"] == "London"


def test_the_same_field_twice_is_one_field_with_two_slots():
    _body, objects, _findings = migrate(
        '<p><span data-token="source" field="name"></span> and '
        '<span data-token="source" field="name"></span></p>')
    assert len(objects) == 1
    assert len(objects[0]["slots"]) == 2


def test_a_runnable_condition_becomes_a_condition():
    _body, objects, findings = migrate(
        '<p><span data-token="conditional" condition="region == \'EU\'" '
        'body="GDPR clause applies."></span></p>')
    conditions = [o for o in objects if o["object_type"] == "CONDITION"]
    assert len(conditions) == 1
    assert conditions[0]["expression"] == "region == 'EU'"
    assert "condition_does_not_parse" not in _codes(findings)


def test_a_condition_written_as_prose_is_refused_rather_than_stored():
    """The old editor's condition field was free text, so it holds "if the
    employee is eligible" as often as `region == 'EU'`. Stored as a condition
    that is a rule which evaluates false for every record and removes its
    section from every document -- silently, because nothing ever said it could
    not be run."""
    body, objects, findings = migrate(
        '<p><span data-token="conditional" condition="if the employee is eligible" '
        'body="Bonus applies."></span></p>')

    assert [o for o in objects if o["object_type"] == "CONDITION"] == []
    assert "condition_does_not_parse" in _codes(findings)
    assert [f["severity"] for f in findings if f["code"] == "condition_does_not_parse"] == [
        "blocking"]
    # The text it governed is kept. Dropping it would silently delete a clause.
    assert _texts(body) == ["Bonus applies."]


def test_an_ai_prompt_arrives_needing_review_rather_than_becoming_a_field():
    """`NarrativeObject` requires a grounding source and a prompt token has none,
    so nothing constrains what it would write."""
    body, objects, findings = migrate(
        '<p><span data-token="prompt" prompt="Write a warm welcome"></span></p>')

    assert objects == []
    assert "narrative_without_grounding" in _codes(findings)
    assert "[AI: Write a warm welcome]" in _texts(body)[0]


def test_a_repeat_says_what_it_would_take_to_express_it():
    _body, _objects, findings = migrate(
        '<p><span data-token="repeat" variable="benefit" collection="benefits" '
        'body="- {benefit}"></span></p>')
    assert "repeat_needs_a_table" in _codes(findings)


def test_a_source_token_naming_no_field_is_kept_as_text_and_reported():
    body, objects, findings = migrate(
        '<p>Dear <span data-token="source">somebody</span></p>')
    assert objects == []
    assert "token_without_field" in _codes(findings)
    assert "somebody" in _texts(body)[0]


def test_headings_keep_their_level():
    body, _objects, _findings = migrate("<h1>Offer</h1><p>Dear colleague</p>")
    assert body["blocks"][0]["style"] == "Heading 1"
    assert body["blocks"][1]["style"] is None


def test_the_migrated_body_is_normalised_and_its_slots_match_it():
    """Normalising merges adjacent static segments, which moves span indices. A
    slot numbered before the merge would address the span to its left."""
    body, objects, findings = migrate(
        '<p>Dear <span data-token="source" field="name"></span>, welcome aboard.</p>')

    assert bp.is_normalised(body)
    assert "slot_not_in_published_template" not in _codes(findings)
    slot = objects[0]["slots"][0]
    spans = bp.emitted_spans(body["blocks"][0]["segments"])
    assert slot["text"] in spans[slot["span_index"]]["text"]


def test_a_migrated_template_emits_and_reads_back_as_a_normal_one(tmp_path):
    """The whole claim. What comes out is not a special kind of template -- it
    is one the pre-scanner, the compiler and the fill engine treat like any
    other."""
    from app.compiler.rule_compiler import compile_manifest
    from app.generation.docx_renderer import fill_template
    from app.templates.emit_docx import emit
    from app.templates.parsers.docx_prescan import prescan

    body, _objects, _findings = migrate(
        '<h1>Offer</h1>'
        '<p>Dear <span data-token="source" field="full_name"></span>,</p>'
        '<p>Your role is <span data-token="source" field="position_title"></span>.</p>')

    path = str(tmp_path / "migrated.docx")
    emit(body, path)
    scan = prescan(path)
    assert sorted(s.text for s in scan.spans if s.color == "blue") == [
        "<full_name>", "<position_title>"]

    compiled = compile_manifest(scan)
    manifest = {"fields": compiled.fields, "conditions": compiled.conditions,
                "blocks": compiled.blocks, "delete_always": compiled.delete_always}
    result = fill_template(path, str(tmp_path / "out.docx"), manifest,
                           {"full_name": "Priya Sharma", "position_title": "Data Manager"})
    assert result.qa_passed, result.qa_notes


def test_empty_content_migrates_to_an_empty_template_rather_than_raising():
    body, objects, findings = migrate("")
    assert body["blocks"] == [] and objects == [] and findings == []
