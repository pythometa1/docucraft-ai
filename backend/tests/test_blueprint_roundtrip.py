"""The property the whole authoring path rests on.

    blueprint -> emit -> prescan -> compile -> blueprint'     and     blueprint' == blueprint

Stated as a claim anyone can check: **a template this codebase writes is one it
can read.** If that holds, an authored template is indistinguishable from a
well-formed legacy one and re-enters the pipeline with no special case anywhere
-- same pre-scanner, same compiler, same fill engine, same QA gates. If it does
not hold, every later stage still reports success and the defect surfaces as a
wrong letter after approval.

The compile here is the **rule** compiler, not the agentic one, and that is not a
compromise. `conftest.py` blanks every provider key so the suite can never reach
a model, and the claim worth making offline is the stronger one anyway:
`assertions.collect_with_warnings` returning no faults is the document itself
saying nothing was left unmapped -- no uncovered placeholder, no uncovered merge
field, no instruction that would survive into the letter. That is what "this
template can be mapped" means, and it is checked mechanically rather than
believed.
"""

import pytest

from app.compiler import assertions as A
from app.compiler.mapping_agent import _paragraph_texts
from app.compiler.rule_compiler import compile_manifest
from app.generation.docx_renderer import fill_template
from app.templates import blueprint as bp
from app.templates.emit_docx import emit
from app.templates.parsers.docx_prescan import prescan
from app.templates.semantic_model import AnchorError, lift_slot_to_anchor, resolve


def _p(*segments, style=None):
    return bp.paragraph(segments, style=style)


def _letter():
    """The shape the compiler was built against: salutation, prose, a
    remuneration table with a merge field, a hyperlink, an author instruction."""
    return bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Offer of Employment"), style="Heading 1"),
        _p(bp.segment("static", "Dear "), bp.segment("placeholder", "<Colleague First Name>"),
           bp.segment("static", ",")),
        _p(bp.segment("static", "You are offered the role of "),
           bp.segment("placeholder", "<Position Title>"),
           bp.segment("static", ", reporting to "),
           bp.segment("placeholder", "<New Reporting To>"), bp.segment("static", ".")),
        _p(bp.segment("static", "Remuneration"), style="Heading 1"),
        bp.table([[[_p(bp.segment("static", "Base salary"))],
                   [_p(bp.segment("static", "$"),
                       bp.segment("mergefield", code="LAB__FT_SALARY__38_HR_"))]]]),
        _p(bp.segment("static", "See the "),
           bp.segment("hyperlink", "relocation policy", target="https://example.com/reloc"),
           bp.segment("static", " for detail.")),
        _p(bp.segment("instruction", "Delete this line before sending.")),
    ], "sect_pr_from": None})


def _minimal():
    return bp.normalise_body({"blocks": [
        _p(bp.segment("static", "Hello "), bp.segment("placeholder", "<full_name>"))]})


def _tables_only():
    """Every paragraph inside a table, which is where the paragraph index is
    least like "the nth paragraph in the file"."""
    return bp.normalise_body({"blocks": [bp.table([
        [[_p(bp.segment("static", "Field"))], [_p(bp.segment("static", "Value"))]],
        [[_p(bp.segment("static", "Start date"))], [_p(bp.segment("placeholder", "<Start Date>"))]],
        [[_p(bp.segment("static", "Salary"))],
         [_p(bp.segment("static", "$"), bp.segment("mergefield", code="SALARY_PA"))]],
    ])], "sect_pr_from": None})


def _no_placeholders():
    """A template that asks for nothing still has to survive the trip -- a
    compile that finds no fields is a valid answer, not a failure."""
    return bp.normalise_body({"blocks": [
        _p(bp.segment("static", "This document is intentionally static."))]})


CORPUS = {
    "letter": _letter,
    "minimal": _minimal,
    "tables_only": _tables_only,
    "no_placeholders": _no_placeholders,
}

RECORDS = {
    "letter": {"colleague_first_name": "Priya", "position_title": "Clinical Data Manager",
               "new_reporting_to": "Dana Ruiz", "LAB__FT_SALARY__38_HR_": 82000},
    "minimal": {"full_name": "Priya Sharma"},
    "tables_only": {"start_date": "2026-03-02", "SALARY_PA": 96000},
    "no_placeholders": {},
}


@pytest.fixture(params=sorted(CORPUS))
def emitted(request, tmp_path):
    name = request.param
    body = CORPUS[name]()
    path = str(tmp_path / f"{name}.docx")
    emit(body, path)
    return name, body, path


def _manifest_dict(compiled) -> dict:
    return {"fields": compiled.fields, "conditions": compiled.conditions,
            "blocks": compiled.blocks, "delete_always": compiled.delete_always}


# ---- the trip ----

def test_the_prescan_recovers_the_body_it_was_written_from(emitted):
    _name, body, path = emitted
    scan = prescan(path)

    assert _paragraph_texts(scan) == bp.paragraph_texts(body)
    assert scan.table_paragraph_indices == bp.table_paragraph_indices(body)

    declared = [(i, position, span_index, block["segments"][position])
                for i, block, _t in bp.walk_paragraphs(body)
                for position, span_index in bp.span_plan(block["segments"])]
    assert len(scan.spans) == len(declared)
    for (p_index, _pos, span_index, seg), span in zip(declared, scan.spans):
        assert (span.paragraph_index, span.span_index) == (p_index, span_index)
        assert span.text == seg["text"]


def test_every_anchor_lifted_from_the_emitted_file_resolves_exactly_once(emitted):
    """Zero matches means the manifest addresses text that is not there; two
    means the anchor was never specific enough to name one place. `resolve`
    treats both as errors, and an authored template must produce neither."""
    _name, _body, path = emitted
    scan = prescan(path)
    texts = _paragraph_texts(scan)
    compiled = compile_manifest(scan)

    lifted = 0
    for compiled_field in compiled.fields:
        for slot in compiled_field.get("slots") or ():
            if slot.get("kind") != "text_match":
                continue
            anchor = lift_slot_to_anchor(slot, texts)
            try:
                resolve(anchor, texts)
            except AnchorError as exc:                      # pragma: no cover - the failure path
                pytest.fail(f"{compiled_field['id']}: {exc}")
            lifted += 1
    assert lifted or not any(
        f.get("slots") and f["slots"][0].get("kind") == "text_match" for f in compiled.fields)


def test_the_document_reports_nothing_left_unmapped(emitted):
    """The mappability claim, made by `assertions` rather than by this test: no
    uncovered placeholder, no uncovered merge field, no surviving instruction,
    no field without a slot."""
    _name, _body, path = emitted
    scan = prescan(path)
    faults, _warnings = A.collect_with_warnings(scan, _manifest_dict(compile_manifest(scan)))
    assert faults == [], [f"{f.check}: {f.detail}" for f in faults]


def test_every_placeholder_and_merge_field_comes_back_as_a_field(emitted):
    """blueprint' == blueprint, in the terms the compiler speaks: the ids are
    slugs of the bracket text, so what is checked is that nothing was lost or
    invented."""
    _name, body, path = emitted
    compiled = compile_manifest(prescan(path))

    expected_placeholders = {
        seg["text"] for _i, block, _t in bp.walk_paragraphs(body)
        for seg in block["segments"] if seg["role"] == bp.PLACEHOLDER}
    expected_codes = {
        seg["code"] for _i, block, _t in bp.walk_paragraphs(body)
        for seg in block["segments"] if seg["role"] == bp.MERGEFIELD}

    slot_texts = {slot.get("text") for f in compiled.fields for slot in f.get("slots") or ()}
    slot_codes = {slot.get("code") for f in compiled.fields for slot in f.get("slots") or ()}

    assert expected_placeholders <= slot_texts
    assert expected_codes <= slot_codes


def test_the_fill_engine_produces_a_clean_document_from_it(emitted, tmp_path):
    """The end of the trip. A template that compiles but cannot be filled has
    not been shown to work -- and the QA gates here are the same ones a real
    batch runs."""
    name, _body, path = emitted
    scan = prescan(path)
    manifest = _manifest_dict(compile_manifest(scan))
    out = str(tmp_path / f"{name}-filled.docx")

    result = fill_template(path, out, manifest, RECORDS[name])
    assert result.qa_passed, result.qa_notes
    assert result.qa_notes == []


def test_the_values_actually_land_in_the_letter(tmp_path):
    """Spelled out on one blueprint rather than parametrised, because the point
    is the text a person would read."""
    import docx

    body = _letter()
    path = str(tmp_path / "letter.docx")
    emit(body, path)
    scan = prescan(path)
    out = str(tmp_path / "letter-filled.docx")
    fill_template(path, out, _manifest_dict(compile_manifest(scan)), RECORDS["letter"])

    document = docx.Document(out)
    text = "\n".join(p.text for p in document.paragraphs)
    assert "Dear Priya," in text
    assert "role of Clinical Data Manager, reporting to Dana Ruiz." in text
    assert "<" not in text, "a placeholder survived into the letter"
    assert "Delete this line before sending." not in text, "an author instruction survived"
    assert "$82,000.00" in "\n".join(
        c.text for t in document.tables for r in t.rows for c in r.cells)
