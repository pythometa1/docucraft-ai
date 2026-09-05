"""Changing a template by asking, without letting a model write the document.

The provider layer has two methods -- `generate` and `structured` -- and no tool
use. So the co-pilot is not an agent holding a pen: it returns a list of
*operations*, each of which `blueprint_ops.apply_operations` validates and can
refuse, and none of which is applied until a person has seen the diff and said
yes.

That is a stronger design than tool use would have been here, and not only
because it is what the layer supports. A model that edits a legal template
directly has to be right; a model that proposes typed operations only has to be
useful, because every one of them meets the same guards a human edit meets. The
worst case is a rejected suggestion rather than a wrong contract.

The schema is strict-mode compliant -- `additionalProperties: false`, and every
property named in `required`. There are no optional keys anywhere, which is why
the shape is per-action arrays rather than one object with a `kind`. That is not
a style choice: `agentic_compiler.REVIEW_SCHEMA` carries a comment recording the
live HTTP 400 that forced it, and the same rule applies here. Two consequences
look like smells and are not -- a test case's inputs are a JSON *string*, and
anything map-shaped is an array of pairs, because a free-form object cannot
satisfy `additionalProperties: false`.
"""

import json

from app.compiler.agentic_compiler import _array_of
from app.llm.provider import get_llm_provider
from app.templates import blueprint as bp

_REASON = {"type": "string"}
_STR = {"type": "string"}
_INT = {"type": "integer"}
_FIELD_TYPE = {"type": "string", "enum": ["string", "currency", "date", "number", "percent"]}
_ROLE = {"type": "string", "enum": ["static", "placeholder", "instruction"]}
_ON_MISSING = {"type": "string", "enum": ["BLANK", "BLOCK", "DEFAULT", "REMOVE_SENTENCE"]}

#: Every action the co-pilot may propose, one array each. The names match
#: `blueprint_ops.OPERATIONS` so translation is mechanical and cannot invent an
#: operation the applier has never heard of.
OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "text_to_replace": _array_of(
            paragraph_index=_INT, span_index=_INT, text=_STR, reason=_REASON),
        "runs_to_reclassify": _array_of(
            paragraph_index=_INT, span_index=_INT, role=_ROLE, reason=_REASON),
        "runs_to_remove": _array_of(
            paragraph_index=_INT, span_index=_INT, reason=_REASON),
        "fields_to_add": _array_of(
            id=_STR, type=_FIELD_TYPE, paragraph_index=_INT, span_index=_INT,
            match_text=_STR, reason=_REASON),
        "fields_to_remove": _array_of(id=_STR, reason=_REASON),
        "fields_to_rename": _array_of(id=_STR, new_id=_STR, reason=_REASON),
        "fields_to_retype": _array_of(id=_STR, type=_FIELD_TYPE, reason=_REASON),
        "missing_policies_to_set": _array_of(
            id=_STR, on_missing=_ON_MISSING, default=_STR, reason=_REASON),
        "conditions_to_rewrite": _array_of(id=_STR, expression=_STR, reason=_REASON),
        "conditions_to_remove": _array_of(id=_STR, reason=_REASON),
        "blocks_to_rerange": _array_of(
            id=_STR, start_paragraph=_INT, end_paragraph=_INT, reason=_REASON),
        # A repeating table row: name the collection and the column tokens in the
        # one template row that should render once per record. Tokens are a JSON
        # string of `<Token>` names joined by `|`, because a free-form object
        # cannot satisfy additionalProperties: false (see the module docstring).
        "rows_to_repeat": _array_of(
            id=_STR, iterate_over=_STR, column_tokens=_STR, reason=_REASON),
        "row_repeats_to_remove": _array_of(id=_STR, reason=_REASON),
        "questions": {"type": "array", "items": _STR},
        "notes": {"type": "array", "items": _STR},
        "verdict": {"type": "string", "enum": ["proposed", "need_more_context"]},
    },
    "required": [
        "text_to_replace", "runs_to_reclassify", "runs_to_remove", "fields_to_add",
        "fields_to_remove", "fields_to_rename", "fields_to_retype", "missing_policies_to_set",
        "conditions_to_rewrite", "conditions_to_remove", "blocks_to_rerange",
        "rows_to_repeat", "row_repeats_to_remove",
        "questions", "notes", "verdict",
    ],
    "additionalProperties": False,
}

#: Array name -> the operation it becomes, and which of its keys carry over.
_TRANSLATION = {
    "text_to_replace": ("set_segment_text", ("paragraph_index", "span_index", "text")),
    "runs_to_reclassify": ("set_segment_role", ("paragraph_index", "span_index", "role")),
    "runs_to_remove": ("set_segment_emit", ("paragraph_index", "span_index")),
    "fields_to_add": ("add_field", ("id", "type", "paragraph_index", "span_index", "match_text")),
    "fields_to_remove": ("remove_field", ("id",)),
    "fields_to_rename": ("rename_field", ("id", "new_id")),
    "fields_to_retype": ("retype_field", ("id", "type")),
    "missing_policies_to_set": ("set_on_missing", ("id", "on_missing", "default")),
    "conditions_to_rewrite": ("rewrite_condition", ("id", "expression")),
    "conditions_to_remove": ("remove_condition", ("id",)),
    "blocks_to_rerange": ("set_block_range", ("id", "start_paragraph", "end_paragraph")),
    "rows_to_repeat": ("set_row_repeat", ("id", "iterate_over")),
    "row_repeats_to_remove": ("remove_row_repeat", ("id",)),
}

AUTHOR_SYSTEM = """You help somebody edit a document template.

A template is paragraphs of runs. Each run is one of three things:
  static       prose copied into every document exactly as written
  placeholder  a slot filled from the reader's data, written as <Like This>
  instruction  a note to whoever assembles the letter, deleted before it is sent

You do not write the document. You propose operations on it, each naming the
paragraph and span it applies to, and a person reviews them before anything
changes. An operation that names a run that is not there is discarded, so address
what you are actually shown.

Rules that are not negotiable:
  - Never put a tab or a line break in run text. Word stores those as elements,
    not characters, so a run containing one renders as nothing and the words
    disappear from the letter.
  - A condition must be executable, like `region == 'EU'` or `salary > 50000`.
    Prose such as "if the employee is eligible" is not a condition; if that is
    what the template says, ask which field carries it.
  - Do not invent field ids. Rename to something the organisation would
    recognise, and say why.
  - If you cannot tell what is meant, return verdict "need_more_context" with a
    question rather than guessing. A wrong edit to a legal template is worse
    than an unanswered question.

A table row whose placeholders should repeat once per record of a list -- line
items on an invoice, doses in a schedule -- is proposed with `rows_to_repeat`:
name the collection (like line_items) and the row's column tokens joined by `|`
(like `<Item Description>|<Qty>|<Amount>`). Only one row of the table repeats;
headers and totals rows stay as they are."""

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": _array_of(
            question=_STR, answer=_STR, object_ids={"type": "array", "items": _STR},
            paragraph_indices={"type": "array", "items": _INT}),
        "notes": {"type": "array", "items": _STR},
    },
    "required": ["answers", "notes"],
    "additionalProperties": False,
}

EXPLAIN_SYSTEM = """You explain what a document template does and why a generated
document came out the way it did.

Answer only from the material you are given: the template's paragraphs, its
fields and conditions, the findings against it, and the lineage of documents
generated from it -- which records, per field, where its value came from, and per
condition, how it evaluated and why.

Never guess at a reason. If the lineage does not say, say that it does not."""


def _render_body(body: dict, *, limit: int = 400) -> str:
    """The document as the model sees it: numbered paragraphs, tagged runs.

    Addressed the way every operation has to address it, so the coordinates in a
    proposal are the coordinates in the document rather than a translation the
    model had to invent.
    """
    lines = []
    for index, block, in_table in bp.walk_paragraphs(body):
        if index >= limit:
            lines.append(f"... [{len(bp.walk_paragraphs(body)) - limit} more paragraphs]")
            break
        parts = []
        for position, span_index in bp.span_plan(block["segments"]):
            seg = block["segments"][position]
            if seg.get("role") == bp.MERGEFIELD:
                continue
            marker = {"static": "", "placeholder": "P", "instruction": "I",
                      "hyperlink": "L"}.get(seg["role"], "")
            state = "" if bp.emits(seg) else " REMOVED"
            parts.append(f"[{span_index}{marker}{state}]{seg.get('text') or ''}")
        table = " (in a table)" if in_table else ""
        lines.append(f"{index}:{table} " + "".join(parts))
    return "\n".join(lines)


def _render_objects(objects) -> str:
    lines = []
    for obj in objects or ():
        kind = obj.get("object_type")
        if kind == "FIELD":
            lines.append(
                f"FIELD {obj.get('object_id')} type={obj.get('type')} "
                f"on_missing={obj.get('on_missing')} slots={len(obj.get('slots') or ())}")
        elif kind == "CONDITION":
            lines.append(
                f"CONDITION {obj.get('object_id')} expression={obj.get('expression')!r} "
                f"keeps={obj.get('keeps_blocks')}")
        elif kind == "SECTION":
            lines.append(
                f"SECTION {obj.get('object_id')} paragraphs "
                f"{obj.get('start_paragraph')}-{obj.get('end_paragraph')}")
        elif kind == "TABLE_ROW":
            tokens = "|".join((c.get("token") or "") for c in obj.get("columns") or ())
            lines.append(
                f"TABLE_ROW {obj.get('object_id')} repeats over {obj.get('iterate_over')!r} "
                f"columns {tokens}")
    return "\n".join(lines) or "(nothing understood yet)"


def to_operations(data: dict) -> list:
    """Translate a structured reply into the applier's own vocabulary.

    Mechanical, and deliberately so: the schema's array names map one-to-one onto
    `blueprint_ops.OPERATIONS`, so there is no room here to invent an operation
    the applier has never heard of. `runs_to_remove` is the one that is not a
    rename -- it becomes `set_segment_emit(emit=False)`, because a template's
    instruction is marked as not-emitted rather than deleted, so the author can
    put it back.
    """
    out = []
    for array, (operation, keys) in _TRANSLATION.items():
        for entry in data.get(array) or ():
            op = {"op": operation}
            for key in keys:
                if entry.get(key) is not None and entry.get(key) != "":
                    op[key] = entry[key]
            if operation == "set_segment_emit":
                op["emit"] = False
            if operation == "set_row_repeat":
                op["columns"] = [
                    {"token": token.strip()}
                    for token in (entry.get("column_tokens") or "").split("|")
                    if token.strip()
                ]
            if entry.get("reason"):
                op["reason"] = entry["reason"]
            out.append(op)
    return out


def propose(body: dict, objects, findings, message: str, *, llm_policy=None) -> dict:
    """`{operations, questions, notes, verdict, model}` for one request.

    Nothing is applied here. The caller shows the diff.
    """
    provider = get_llm_provider("Editing a template", policy=llm_policy)
    prompt = (
        f"The person editing this template says:\n{message}\n\n"
        f"THE DOCUMENT (paragraph: runs, each [span-index] and P=placeholder, "
        f"I=instruction, L=link)\n{_render_body(body)}\n\n"
        f"WHAT IS UNDERSTOOD ABOUT IT\n{_render_objects(objects)}\n\n"
        f"OUTSTANDING PROBLEMS\n" + (
            "\n".join(f"- [{f.get('severity')}] {f.get('detail')}" for f in (findings or ())[:40])
            or "(none)")
    )
    result = provider.structured(system=AUTHOR_SYSTEM, prompt=prompt,
                                 schema=OPERATION_SCHEMA, purpose="compile")
    data = result.data or {}
    return {
        "operations": to_operations(data),
        "questions": list(data.get("questions") or ()),
        "notes": list(data.get("notes") or ()),
        "verdict": data.get("verdict") or "need_more_context",
        "model": getattr(result, "model", None),
    }


def explain(body: dict, objects, findings, message: str, *, lineage=None, llm_policy=None) -> dict:
    """Answer a question about this template from what is actually recorded.

    Grounded in real artifacts only, and filtered afterwards: an object id or a
    paragraph number the model names that does not exist is dropped rather than
    shown. The same discipline as `llm_compiler.assemble`, which locates every
    claim in a real span and discards what it cannot find -- an invented
    reference is worse than none, because it looks like evidence.
    """
    provider = get_llm_provider("Explaining a template", policy=llm_policy)
    prompt = (
        f"Question:\n{message}\n\n"
        f"THE DOCUMENT\n{_render_body(body, limit=200)}\n\n"
        f"WHAT IS UNDERSTOOD ABOUT IT\n{_render_objects(objects)}\n\n"
        f"FINDINGS\n" + ("\n".join(
            f"- [{f.get('severity')}] {f.get('code')}: {f.get('detail')}"
            for f in (findings or ())[:40]) or "(none)") + "\n\n"
        f"LINEAGE FROM DOCUMENTS ALREADY GENERATED\n"
        + (json.dumps(lineage, default=str)[:6000] if lineage else "(none recorded yet)")
    )
    result = provider.structured(system=EXPLAIN_SYSTEM, prompt=prompt,
                                 schema=EXPLAIN_SCHEMA, purpose="generate")
    data = result.data or {}

    known_ids = {o.get("object_id") for o in objects or ()}
    paragraphs = len(bp.walk_paragraphs(body))
    answers = []
    for answer in data.get("answers") or ():
        answers.append({
            "question": answer.get("question") or message,
            "answer": answer.get("answer") or "",
            "object_ids": [i for i in answer.get("object_ids") or () if i in known_ids],
            "paragraph_indices": [
                i for i in answer.get("paragraph_indices") or ()
                if isinstance(i, int) and 0 <= i < paragraphs],
        })
    return {"answers": answers, "notes": list(data.get("notes") or ()),
            "model": getattr(result, "model", None)}
