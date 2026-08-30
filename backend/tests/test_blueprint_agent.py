"""The co-pilot's contract with the model, and with the applier.

Two things are worth pinning and neither is "does the model give good advice",
which no test here can answer. The first is that the schema is legal: a
strict-mode violation is an HTTP 400 at runtime, and this codebase has already
had one. The second is that what comes back can only ever become an operation
the applier recognises -- so the worst a model can do is have every suggestion
refused, which is a wasted request rather than a wrong contract.
"""

import pytest

from app.compiler.blueprint_agent import (
    EXPLAIN_SCHEMA, OPERATION_SCHEMA, _TRANSLATION, _render_body, to_operations,
)
from app.templates import blueprint as bp
from app.templates.blueprint_ops import OPERATIONS, apply_operations


def _walk_objects(schema, seen=None):
    """Every object node in a JSON Schema, so strictness can be checked on all."""
    seen = seen if seen is not None else []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            seen.append(schema)
        for value in schema.values():
            _walk_objects(value, seen)
    elif isinstance(schema, list):
        for value in schema:
            _walk_objects(value, seen)
    return seen


@pytest.mark.parametrize("schema,name", [(OPERATION_SCHEMA, "OPERATION_SCHEMA"),
                                         (EXPLAIN_SCHEMA, "EXPLAIN_SCHEMA")])
def test_the_schema_is_strict_mode_compliant(schema, name):
    """Every object sets `additionalProperties: false` and names every property
    in `required`. There are no optional keys, which is why these are per-action
    arrays rather than one object with a `kind` -- the same rule that forced
    `REVIEW_SCHEMA` into that shape, after a live HTTP 400."""
    for node in _walk_objects(schema):
        assert node.get("additionalProperties") is False, f"{name}: an object allows extra keys"
        assert set(node.get("required") or ()) == set(node.get("properties") or {}), (
            f"{name}: an object has an optional property")


def test_every_array_translates_to_an_operation_the_applier_knows():
    """Mechanical on purpose. A translation with room for judgement is a place
    to invent an operation nothing implements."""
    # Arrays *of objects* are actions; `questions` and `notes` are arrays of
    # strings and carry no operation.
    action_arrays = {
        key for key, value in OPERATION_SCHEMA["properties"].items()
        if (value.get("items") or {}).get("type") == "object"}
    assert set(_TRANSLATION) == action_arrays
    assert {operation for operation, _keys in _TRANSLATION.values()} <= set(OPERATIONS)


def test_a_structured_reply_becomes_operations():
    empty = {key: [] for key in OPERATION_SCHEMA["properties"]}
    data = {**empty, "verdict": "proposed",
            "fields_to_rename": [{"id": "name", "new_id": "full_name", "reason": "clearer"}],
            "text_to_replace": [{"paragraph_index": 0, "span_index": 0, "text": "Hello ",
                                 "reason": "warmer"}]}
    ops = to_operations(data)
    assert {o["op"] for o in ops} == {"rename_field", "set_segment_text"}
    assert all("reason" in o for o in ops)


def test_removing_a_run_marks_it_rather_than_deleting_it():
    """A template's instruction is marked as not-emitted so the author can put it
    back, and because deleting the run would renumber every span after it."""
    empty = {key: [] for key in OPERATION_SCHEMA["properties"]}
    ops = to_operations({**empty, "runs_to_remove": [
        {"paragraph_index": 1, "span_index": 0, "reason": "an author note"}]})
    assert ops == [{"op": "set_segment_emit", "paragraph_index": 1, "span_index": 0,
                    "emit": False, "reason": "an author note"}]


def test_a_proposal_that_addresses_nothing_is_refused_rather_than_applied():
    """The guarantee that makes this safe. The model never writes the document;
    it proposes operations that meet the same guards a human edit meets, so the
    worst case is a wasted request."""
    body = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "Dear colleague")])], "sect_pr_from": None})
    empty = {key: [] for key in OPERATION_SCHEMA["properties"]}
    ops = to_operations({**empty, "text_to_replace": [
        {"paragraph_index": 40, "span_index": 3, "text": "x", "reason": "invented"}]})

    result = apply_operations(body, [], ops)
    assert result.applied == []
    assert "no run at paragraph 40" in result.rejected[0]["reason"]


def test_the_model_is_shown_the_coordinates_it_has_to_address():
    """A proposal names a paragraph and a span, so the rendering has to carry
    both -- otherwise the model is guessing at the only thing it must get right."""
    body = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", "Dear "), bp.segment("placeholder", "<Name>")]),
        bp.paragraph([bp.segment("instruction", "Delete me", emit=False)]),
    ], "sect_pr_from": None})

    rendered = _render_body(body)
    assert "0: [0]Dear [1P]<Name>" in rendered
    assert "1: [0I REMOVED]Delete me" in rendered


def test_a_long_template_is_truncated_rather_than_sent_whole():
    body = bp.normalise_body({"blocks": [
        bp.paragraph([bp.segment("static", f"line {i}")]) for i in range(60)],
        "sect_pr_from": None})
    rendered = _render_body(body, limit=10)
    assert "more paragraphs" in rendered
    assert "line 59" not in rendered
