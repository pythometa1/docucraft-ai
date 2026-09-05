"""Authoring a whole template from a description, without trusting a word of it.

The co-pilot edits documents that exist; this writes one that does not. The
model never touches the blueprint format directly -- it emits a deliberately
small, flat body vocabulary (paragraphs, table rows, two run roles), and the
server assembles, sanitises, normalises and then *proves* the result: the body
is emitted to a real `.docx`, and `emit` asserts the round-trip property
(blueprint -> emit -> prescan -> blueprint) on every call. A body that cannot
survive that is retried once with the error in the prompt, and then given up
on -- the caller falls back to a shipped kit rather than persisting a template
nobody has verified.

The vocabulary is strict-mode JSON schema: `additionalProperties: false`,
every property required, no recursion and no oneOf -- the same constraints
`blueprint_agent.OPERATION_SCHEMA` records a live HTTP 400 for. That is why a
block always carries both `segments` and `cells` and a flag says which one
counts, and why tables arrive as consecutive `table_row` blocks the server
groups rather than as a nested structure.
"""

import re

from app.compiler.agentic_compiler import _array_of
from app.llm.provider import get_llm_provider
from app.templates import blueprint as bp
from app.templates.blueprint_lint import lint as lint_blueprint
from app.templates.kits import table_row_objects

#: The capability string metering buckets on; see `llm.metering`.
CAPABILITY = "Authoring a template from a description"

_SEGMENT = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["static", "placeholder"]},
        "text": {"type": "string"},
    },
    "required": ["role", "text"],
    "additionalProperties": False,
}

BODY_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["paragraph", "table_row"]},
                    "style": {"type": "string", "enum": ["", "Title", "Heading 1", "Heading 2"]},
                    # True on exactly the one table row that renders once per
                    # record of `line_items_key`. False everywhere else.
                    "repeat": {"type": "boolean"},
                    "segments": {"type": "array", "items": _SEGMENT},
                    "cells": {"type": "array",
                              "items": {"type": "array", "items": _SEGMENT}},
                },
                "required": ["kind", "style", "repeat", "segments", "cells"],
                "additionalProperties": False,
            },
        },
        # "" when the document has no repeating table.
        "line_items_key": {"type": "string"},
        "field_types": _array_of(
            token={"type": "string"},
            type={"type": "string", "enum": ["string", "currency", "date", "number", "percent"]},
            on_missing={"type": "string", "enum": ["BLANK", "BLOCK"]},
        ),
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["blocks", "line_items_key", "field_types", "notes"],
    "additionalProperties": False,
}

AUTHOR_SYSTEM = """You write document templates for a deterministic fill engine.

A template is paragraphs of runs. Each run is one of two things:
  static       prose printed into every document exactly as written
  placeholder  a slot filled from data, written in angle brackets: <Like This>

Rules that are not negotiable:
  - Every value that changes per document is a placeholder. Never write example
    values as static text -- "Acme Ltd" hard-coded into a template prints
    "Acme Ltd" on every document forever.
  - Placeholder names are short noun phrases in Title Case inside the brackets:
    <Customer Name>, <Invoice Date>, <Amount>. The same value reused later in
    the document uses the exact same token.
  - No tabs or line breaks inside a run's text. Split lines into paragraphs.
  - A table is consecutive table_row blocks with the same number of cells. Each
    cell is a list of runs forming one line.
  - When the document has a list that repeats per record (line items, doses,
    entries), give exactly ONE table_row `repeat: true` -- the prototype row,
    each cell holding one placeholder -- and name the collection in
    line_items_key. The header row and any totals rows are NOT the repeat row.
  - Type every numeric or date placeholder in field_types: amounts as currency,
    counts as number, dates as date. Untyped placeholders print as strings.
  - You cannot place images or logos; where one belongs, use the business name
    styled as a heading and say so in notes.

Write complete, professional documents a real business would send -- full
sentences, sensible order, nothing left as an exercise."""

#: Per-service scaffolding appended to the prompt. The invoice pack is the
#: first; a later vertical adds its own entry rather than a new mechanism.
PROMPT_PACKS = {
    "invoice": """This template is an INVOICE. It must contain, in a sensible order:
  - a heading, and the business's own identity: <Business Name>, <Business Address>,
    plus tax registration when the description implies one (<Business GSTIN> for
    Indian GST businesses, <Business Tax ID> elsewhere)
  - invoice metadata: <Invoice Number>, <Invoice Date>, and <Due Date> when
    payment terms exist
  - a bill-to section: <Customer Name>, <Customer Address>, and the customer's
    tax id when relevant
  - ONE line-item table: a static header row, then the repeat row with
    placeholders such as <Item Description>, <Quantity>, <Unit Price>, <Amount>
    (line_items_key: line_items)
  - totals after the table: <Subtotal>, tax (<Tax Rate>, <Tax Amount> -- or
    <CGST Amount> and <SGST Amount> for Indian GST), and <Grand Total>,
    all typed currency except the rate
  - payment terms or instructions.
Currency conventions come from the description (GST and rupees for India)."""
}


class AuthoringFailed(Exception):
    """No verifiable template came back, even after a retry."""


_WS_RE = re.compile(r"[\t\r\n\f\v]+")


def _clean(text: str) -> str:
    """Run text the emitter will accept: no tabs, no line breaks."""
    return _WS_RE.sub(" ", str(text or ""))


def _segment(seg: dict) -> dict | None:
    role = seg.get("role")
    text = _clean(seg.get("text"))
    if role == "placeholder":
        inner = text.strip().strip("<>").strip()
        if not inner:
            return None
        return bp.segment(bp.PLACEHOLDER, f"<{inner}>")
    if not text:
        return None
    return bp.segment(bp.STATIC, text)


def _style(block: dict) -> str | None:
    style = (block.get("style") or "").strip()
    return style or None


def assemble_body(data: dict) -> tuple[dict, list, list]:
    """`(body, table_row_specs, problems)` from the model's flat block list.

    Deterministic and forgiving where forgiveness is safe (short rows are
    padded, empty runs dropped), strict where it is not (a repeating table
    declared with no repeat row is a problem, not a guess).
    """
    problems: list = []
    blocks: list = []
    specs: list = []
    line_items_key = str(data.get("line_items_key") or "").strip()
    field_types = {
        _clean(e.get("token")).strip(): e
        for e in (data.get("field_types") or ())
        if _clean(e.get("token")).strip()
    }

    pending_rows: list = []       # rows of the table being accumulated
    pending_repeat: list | None = None  # the repeat row's cell segments

    def flush_table():
        nonlocal pending_rows, pending_repeat
        if not pending_rows:
            return
        width = max(len(row) for row in pending_rows)
        padded = [row + [[] for _ in range(width - len(row))] for row in pending_rows]
        blocks.append(bp.table([
            [[bp.paragraph(cell)] for cell in row] for row in padded
        ]))
        if pending_repeat is not None:
            tokens = [seg.get("text") for cell in pending_repeat for seg in cell
                      if seg.get("role") == bp.PLACEHOLDER]
            if tokens and line_items_key:
                specs.append({
                    "id": line_items_key, "iterate_over": line_items_key,
                    "empty_behaviour": "REMOVE_ROW", "required": True,
                    "columns": [
                        {"token": token,
                         "type": (field_types.get(token) or {}).get("type") or "string",
                         "on_missing": (field_types.get(token) or {}).get("on_missing") or "BLANK"}
                        for token in tokens
                    ],
                })
        pending_rows, pending_repeat = [], None

    for raw in data.get("blocks") or ():
        kind = raw.get("kind")
        if kind == "table_row":
            cells = []
            for cell in raw.get("cells") or ():
                segments = [s for s in (_segment(seg) for seg in cell or ()) if s]
                cells.append(segments)
            if not cells:
                continue
            pending_rows.append(cells)
            if raw.get("repeat"):
                if pending_repeat is not None:
                    problems.append("two rows of one table are marked repeat: true; "
                                    "exactly one may be")
                pending_repeat = cells
            continue
        flush_table()
        if kind != "paragraph":
            continue
        segments = [s for s in (_segment(seg) for seg in raw.get("segments") or ()) if s]
        if not segments:
            continue
        blocks.append(bp.paragraph(segments, style=_style(raw)))
    flush_table()

    if not blocks:
        problems.append("the reply contained no usable blocks")
    if line_items_key and not specs:
        problems.append(
            f"line_items_key names {line_items_key!r} but no table row is marked "
            "repeat: true with placeholder cells")

    body = bp.normalise_body({"blocks": blocks, "sect_pr_from": None})
    return body, specs, problems


def _decorated_objects(body: dict, specs: list, field_types: dict) -> list:
    """FIELD objects for every placeholder, typed, plus the TABLE_ROW objects."""
    from app.compiler.rule_compiler import _slug
    from app.templates.kits import objects_for

    by_slug = {}
    for token, entry in field_types.items():
        inner = token.strip().strip("<>").strip()
        if inner:
            by_slug[_slug(inner)] = entry

    fields = objects_for(body)
    for obj in fields:
        entry = by_slug.get(obj["object_id"])
        if not entry:
            continue
        declared = entry.get("type")
        if declared in ("string", "currency", "date", "number", "percent"):
            obj["type"] = obj["value_type"] = declared
        if entry.get("on_missing") in ("BLANK", "BLOCK"):
            obj["on_missing"] = entry["on_missing"]
    return fields + table_row_objects(body, specs)


def _probe_emit(body: dict) -> None:
    """Emit to a throwaway file; `emit` itself asserts the round-trip."""
    import tempfile
    from pathlib import Path

    from app.templates.emit_docx import emit

    with tempfile.TemporaryDirectory() as workspace:
        emit(body, str(Path(workspace) / "candidate.docx"))


def author_blueprint(description: str, *, service: str | None = None,
                     llm_policy=None) -> dict:
    """`{body, objects, findings, notes, model}` for one described template.

    Raises `AuthoringFailed` when neither the first attempt nor the retry
    produced a body the emitter can verify. Never returns an unproven body.
    """
    provider = get_llm_provider(CAPABILITY, policy=llm_policy)
    pack = PROMPT_PACKS.get((service or "").strip().lower())
    prompt = f"Write a template for this business:\n\n{description.strip()}"
    if pack:
        prompt += f"\n\n{pack}"

    feedback = None
    last_error = "the model returned nothing usable"
    for _attempt in range(2):
        attempt_prompt = prompt if feedback is None else (
            f"{prompt}\n\nYour previous attempt failed validation:\n{feedback}\n"
            "Produce a corrected template.")
        result = provider.structured(
            system=AUTHOR_SYSTEM, prompt=attempt_prompt,
            schema=BODY_SCHEMA, purpose="compile")
        data = result.data
        if not data:
            last_error = result.error or "the model returned no structured reply"
            feedback = last_error
            continue

        body, specs, problems = assemble_body(data)
        if problems:
            last_error = "; ".join(problems)
            feedback = last_error
            continue

        field_types = {
            _clean(e.get("token")).strip(): e
            for e in (data.get("field_types") or ())
            if _clean(e.get("token")).strip()
        }
        objects = _decorated_objects(body, specs, field_types)
        try:
            _probe_emit(body)
        except Exception as exc:  # EmitError, RoundTripError, BlueprintError
            last_error = str(exc)
            feedback = last_error
            continue

        report = lint_blueprint(body, objects, delete_always=[])
        return {
            "body": body,
            "objects": objects,
            "findings": [f.as_dict() for f in report.findings],
            "notes": list(data.get("notes") or ()),
            "model": getattr(result, "model", None),
        }

    raise AuthoringFailed(last_error)
