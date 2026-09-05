"""Authoring a template from a description -- the constrained vocabulary, the
server-side assembly, and the proof-before-persist contract.

The model is stubbed throughout: what is under test is everything the server
refuses to take on trust. A valid reply must come back as a working blueprint
whose emit round-trip holds; an invalid one must be retried with the error in
the prompt and then given up on; and the API must fall back to a shipped kit,
with the reason recorded, rather than dead-end when no model is configured.
"""

from dataclasses import dataclass

import pytest

from app.compiler import blueprint_author as author_module
from app.compiler.blueprint_author import (
    AuthoringFailed, assemble_body, author_blueprint,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seg(role, text):
    return {"role": role, "text": text}


def _paragraph(*segments, style=""):
    return {"kind": "paragraph", "style": style, "repeat": False,
            "segments": list(segments), "cells": []}


def _table_row(*cells, repeat=False):
    return {"kind": "table_row", "style": "", "repeat": repeat,
            "segments": [], "cells": [list(c) for c in cells]}


GOOD_REPLY = {
    "blocks": [
        _paragraph(_seg("static", "INVOICE"), style="Heading 1"),
        _paragraph(_seg("placeholder", "<Business Name>")),
        _paragraph(_seg("static", "Invoice number: "), _seg("placeholder", "<Invoice Number>")),
        _table_row([_seg("static", "Description")], [_seg("static", "Amount")]),
        _table_row([_seg("placeholder", "<Item Description>")],
                   [_seg("placeholder", "<Amount>")], repeat=True),
        _paragraph(_seg("static", "Total: "), _seg("placeholder", "<Grand Total>")),
    ],
    "line_items_key": "line_items",
    "field_types": [
        {"token": "<Amount>", "type": "currency", "on_missing": "BLANK"},
        {"token": "<Grand Total>", "type": "currency", "on_missing": "BLOCK"},
    ],
    "notes": ["No logo support; the business name is the heading."],
}


@dataclass
class _StubResult:
    data: dict | None
    model: str = "stub-model"
    error: str | None = None


class _StubProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def structured(self, *, system, prompt, schema, purpose="generate"):
        self.prompts.append(prompt)
        return _StubResult(data=self.replies.pop(0))


@pytest.fixture
def stub_provider(monkeypatch):
    def install(*replies):
        provider = _StubProvider(replies)
        monkeypatch.setattr(author_module, "get_llm_provider",
                            lambda *a, **k: provider)
        return provider
    return install


# ---------------------------------------------------------------- assembly

def test_assemble_groups_consecutive_rows_into_one_table():
    body, specs, problems = assemble_body(GOOD_REPLY)
    assert problems == []
    tables = [b for b in body["blocks"] if b["kind"] == "table"]
    assert len(tables) == 1
    assert len(tables[0]["rows"]) == 2
    assert len(specs) == 1
    assert specs[0]["iterate_over"] == "line_items"
    assert [c["token"] for c in specs[0]["columns"]] == ["<Item Description>", "<Amount>"]


def test_ragged_rows_are_padded_not_refused():
    reply = {**GOOD_REPLY, "blocks": [
        _table_row([_seg("static", "A")], [_seg("static", "B")], [_seg("static", "C")]),
        _table_row([_seg("placeholder", "<X>")], repeat=True),
    ], "line_items_key": "rows", "field_types": []}
    body, _specs, problems = assemble_body(reply)
    assert problems == []
    table = next(b for b in body["blocks"] if b["kind"] == "table")
    assert all(len(row) == 3 for row in table["rows"])


def test_unbracketed_placeholder_text_is_coerced():
    reply = {"blocks": [_paragraph(_seg("placeholder", "Customer Name"))],
             "line_items_key": "", "field_types": [], "notes": []}
    body, _specs, problems = assemble_body(reply)
    assert problems == []
    assert body["blocks"][0]["segments"][0]["text"] == "<Customer Name>"


def test_tabs_and_newlines_are_scrubbed_from_run_text():
    reply = {"blocks": [_paragraph(_seg("static", "one\ttwo\nthree"))],
             "line_items_key": "", "field_types": [], "notes": []}
    body, _specs, problems = assemble_body(reply)
    assert problems == []
    assert body["blocks"][0]["segments"][0]["text"] == "one two three"


def test_line_items_key_with_no_repeat_row_is_a_problem():
    reply = {**GOOD_REPLY, "blocks": [
        _paragraph(_seg("static", "Hello")),
    ]}
    _body, _specs, problems = assemble_body(reply)
    assert any("repeat: true" in p for p in problems)


def test_two_repeat_rows_are_a_problem():
    reply = {**GOOD_REPLY, "blocks": [
        _table_row([_seg("placeholder", "<A>")], repeat=True),
        _table_row([_seg("placeholder", "<B>")], repeat=True),
    ]}
    _body, _specs, problems = assemble_body(reply)
    assert any("exactly one" in p for p in problems)


# ---------------------------------------------------------------- authoring

def test_a_valid_reply_becomes_a_proven_blueprint(stub_provider):
    stub_provider(GOOD_REPLY)
    out = author_blueprint("a decoration company in Pune", service="invoice",
                           llm_policy=None)

    fields = {o["object_id"]: o for o in out["objects"] if o["object_type"] == "FIELD"}
    assert fields["amount"]["type"] == "currency"
    assert fields["grand_total"]["on_missing"] == "BLOCK"
    rows = [o for o in out["objects"] if o["object_type"] == "TABLE_ROW"]
    assert len(rows) == 1 and rows[0]["iterate_over"] == "line_items"
    assert out["notes"] == GOOD_REPLY["notes"]
    assert out["model"] == "stub-model"

    # The proof is not a formality: the body actually emits and fills.
    import tempfile
    from pathlib import Path

    from app.generation.docx_renderer import fill_template
    from app.templates.blueprint_lint import legacy_manifest
    from app.templates.emit_docx import emit

    with tempfile.TemporaryDirectory() as workspace:
        template = str(Path(workspace) / "t.docx")
        emit(out["body"], template)
        manifest = legacy_manifest(out["objects"], delete_always=[])
        fill = fill_template(template, str(Path(workspace) / "out.docx"), manifest, {
            "business_name": "Sundar Decorations", "invoice_number": "INV-1",
            "grand_total": 100,
            "line_items": [{"item_description": "Décor", "amount": 100}],
        })
        assert fill.qa_passed, fill.qa_notes


def test_a_bad_first_reply_is_retried_with_the_error(stub_provider):
    bad = {"blocks": [], "line_items_key": "", "field_types": [], "notes": []}
    provider = stub_provider(bad, GOOD_REPLY)
    out = author_blueprint("an invoice", service="invoice", llm_policy=None)
    assert out["model"] == "stub-model"
    assert len(provider.prompts) == 2
    assert "failed validation" in provider.prompts[1]


def test_two_bad_replies_raise(stub_provider):
    bad = {"blocks": [], "line_items_key": "", "field_types": [], "notes": []}
    stub_provider(bad, bad)
    with pytest.raises(AuthoringFailed):
        author_blueprint("an invoice", llm_policy=None)


# ---------------------------------------------------------------- the API

def test_endpoint_falls_back_to_the_kit_when_no_model_is_configured(app_client, two_orgs):
    """conftest blanks every provider key, so this exercises the real path."""
    token, project_id, _tb, _pb = two_orgs
    res = app_client.post("/api/v1/template-blueprints:from-description",
                          headers=_auth(token), json={
                              "description": "decoration company, GST, Pune",
                              "service": "invoice", "project_id": project_id})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["generation"]["source"] == "kit_fallback"
    assert "No language model" in body["generation"]["notes"][0]
    assert body["kind"] == "kit"
    # The fallback is the real invoice kit, repeat row and all.
    rows = [o for o in body["version"]["objects"] if o.get("object_type") == "TABLE_ROW"]
    assert len(rows) == 1


def test_endpoint_refuses_an_empty_description(app_client, two_orgs):
    token, _pa, _tb, _pb = two_orgs
    res = app_client.post("/api/v1/template-blueprints:from-description",
                          headers=_auth(token), json={"description": "   "})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "DESCRIPTION_REQUIRED"


def test_endpoint_without_a_service_answers_503_when_unconfigured(app_client, two_orgs):
    """No service, no fallback kit -- the refusal is explicit, never silent."""
    token, _pa, _tb, _pb = two_orgs
    res = app_client.post("/api/v1/template-blueprints:from-description",
                          headers=_auth(token), json={"description": "a memo template"})
    assert res.status_code == 503
