"""Turn a compile into something a person can edit.

`compile_template` answers "what does this template mean" as a `CompiledManifest`
-- fields with positional slots, conditions with paragraph ranges, instruction
spans to delete. That is a reading *about* a document. This module attaches that
reading *to* a document body, so the result is a template a person can open, see
their own letter in, and change.

Everything here is deterministic. No model call, no heuristic: the compiler has
already done the interpreting, and re-doing any of it in a second place is how
two answers to one question start to drift.

Three decisions worth stating, because each looks like an omission.

**Objects stay attribute bags, not `semantic_model`'s typed classes.** The typed
classes refuse a great deal at construction -- a condition with no test case, an
anchor range whose boundary paragraph is empty, a field whose DEFAULT policy has
no default -- and a legacy compile hits all of it. More importantly the
`template_manifests` row's `fields`/`conditions`/`blocks` lists are what the fill
engine, the validator and the source resolver read, and those carry `slots`,
which no §6 class has. So the objects written here are the compiled shape *plus*
the §6 attributes, and `manifests.models.to_legacy_objects` still reproduces
exactly what the compiler produced. The typed classes are the linter's
instrument, built on demand from these bags -- which is how `lock_blockers` was
always meant to be reached.

**No STATIC objects are emitted.** §6 defines them and the row can now hold them,
but their content is `text_hash`, and the prose they would hash is already in the
body verbatim -- that is what makes the body editable. A per-segment hash beside
it is the same fact stored twice in the weaker of the two forms, and the
guarantee it exists for, that approved text has not changed, is the template
hash's job. A hundred and twenty of them on one offer letter is bytes, not
information.

**Every object is built inside its own try/except.** A legacy template will
produce a condition whose block boundary is a blank line, or an expression the
grammar cannot parse. One of those must not make the whole template unopenable;
it becomes a finding beside the object it came from, and the author fixes it in
the editor -- which is the entire reason there is an editor.
"""

import ast

from app.expressions.token_parser import condition_inputs
from app.templates import blueprint as bp
from app.templates.semantic_model import (
    KEEP, PROPOSED, REMOVE_BLOCK, condition_outcome, lift_block_to_anchor_range,
    lift_slot_to_anchor,
)

#: What a freshly compiled field asks its source for. The compiler's field id is
#: a slug of the placeholder text and `suggest_bindings` matches a column against
#: exactly that, so writing it as a source ref says the honest thing -- "this
#: field wants a column named after it" -- and binding is what refines it.
#: `semantic_model` requires it to be non-empty, and is right to: a field that
#: reads nothing fills nothing.
SOURCE_PREFIX = "source."

#: A field the compiler found but nobody has bound yet renders blank rather than
#: blocking the document. BLOCK is the right policy for a field a reviewer has
#: approved; applying it to one nobody has looked at would block every document
#: the moment a template is compiled.
DEFAULT_ON_MISSING = "BLANK"


def _finding(code, detail, *, severity="warning", object_id=None, paragraph_index=None):
    return {"code": code, "severity": severity, "detail": detail,
            "object_id": object_id, "paragraph_index": paragraph_index}


def _occurrence_of(slot: dict, seen: dict) -> int:
    """Which occurrence of its token this slot is, within its paragraph.

    `lift_slot_to_anchor` refuses to guess when a token appears more than once --
    assuming the first would put the value in the wrong half of "report to
    <Manager>, copying <Manager>". The compiler emits slots in document order, so
    counting them as they go past is the answer it asked for.
    """
    key = (slot.get("paragraph_index"), slot.get("text"))
    seen[key] = seen.get(key, 0) + 1
    return seen[key]


def _literals_in(expression: str) -> list:
    """Every constant the expression compares against, in source order."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and not isinstance(node.value, bool)]


def _synthesised_test_cases(expression: str, *, on_true: str, on_false: str) -> tuple:
    """Cases whose expectation is what the engine actually does, marked as such.

    `semantic_model` refuses a condition with no test case, and is right to: "a
    rule nobody has ever run against a record is not reviewable". But a compile
    has no records, so the choice is between refusing every legacy condition and
    writing down what the evaluator does today for a reviewer to confirm.

    These are the second thing, tagged `generated` so the linter can say so. The
    expectation is *measured*, never assumed -- a guessed one would fail
    `run_test_cases` and turn a reviewable condition into an unlockable one.
    """
    names = condition_inputs(expression) or ()
    literals = _literals_in(expression)

    candidates = []
    if literals:
        candidates.append({name: literals[0] for name in names})
        if len(literals) > 1:
            candidates.append({name: literals[1] for name in names})
    candidates.append({name: "" for name in names})
    candidates.append({name: "not this one" for name in names})

    cases, outcomes = [], set()
    for record in candidates:
        try:
            outcome = condition_outcome(expression, record, on_true=on_true, on_false=on_false)
        except Exception:                                    # pragma: no cover - defensive
            continue
        if outcome in outcomes:
            continue
        outcomes.add(outcome)
        cases.append({"in": record, "expect": outcome, "generated": True})
        if len(cases) == 2:
            break
    return tuple(cases)


def _non_empty_boundary(index: int, texts, *, forward: bool):
    """The nearest paragraph at or after (or before) `index` that carries text.

    An empty paragraph cannot be a block boundary -- an empty token would match
    every blank line in the document -- so `lift_block_to_anchor_range` raises on
    one. A block whose range happens to start on a blank line is not a broken
    block, it is a block whose boundary is one line off, so the boundary is
    walked inward and the adjustment recorded rather than the block discarded.
    """
    step = 1 if forward else -1
    while 0 <= index < len(texts):
        if (texts[index] or "").strip():
            return index
        index += step
    return None


def _section_objects(compiled, texts, findings, status):
    """SECTION objects, and the paragraph range each one ended up claiming."""
    objects, ranges = [], {}
    for block in getattr(compiled, "blocks", None) or ():
        if str(block.get("object_type") or "").upper() == "TABLE_ROW":
            # Not a region: a §6 TABLE_ROW is one template row located by its
            # column tokens, and lifting it as a paragraph range would report a
            # correct manifest as unanchorable. It rides through unchanged, the
            # same way `reslot_against` passes every non-FIELD object through.
            attributes = dict(block)
            block_id = attributes.pop("id", None) or attributes.pop("object_id", None)
            attributes.pop("object_type", None)
            objects.append({"object_id": block_id, "object_type": "TABLE_ROW", **attributes})
            continue
        attributes = dict(block)
        block_id = attributes.pop("id", None)
        start, end = attributes.get("start_paragraph"), attributes.get("end_paragraph")

        adjusted_start = _non_empty_boundary(start, texts, forward=True) if isinstance(start, int) else None
        adjusted_end = _non_empty_boundary(end, texts, forward=False) if isinstance(end, int) else None

        if adjusted_start is None or adjusted_end is None or adjusted_start > adjusted_end:
            findings.append(_finding(
                "block_boundary_unanchorable",
                f"Block {block_id!r} runs from paragraph {start} to {end}, and neither end lands "
                "on a paragraph with text, so nothing in the document marks where it begins or "
                "ends. Set the boundaries in the editor.",
                severity="blocking", object_id=block_id,
                paragraph_index=start if isinstance(start, int) else None))
        else:
            if (adjusted_start, adjusted_end) != (start, end):
                attributes["boundary_adjusted"] = {"from": [start, end],
                                                   "to": [adjusted_start, adjusted_end]}
                findings.append(_finding(
                    "block_boundary_adjusted",
                    f"Block {block_id!r} began or ended on a blank line, which cannot anchor "
                    f"anything, so its range moved to paragraphs {adjusted_start} to "
                    f"{adjusted_end}. Confirm that is the region you meant.",
                    object_id=block_id, paragraph_index=adjusted_start))
            try:
                anchor_range = lift_block_to_anchor_range(
                    {"id": block_id, "start_paragraph": adjusted_start,
                     "end_paragraph": adjusted_end}, texts)
                attributes["anchor_range"] = anchor_range.as_dict()
                ranges[block_id] = (adjusted_start, adjusted_end)
            except (ValueError, TypeError) as exc:
                findings.append(_finding(
                    "block_boundary_unanchorable", str(exc), severity="blocking",
                    object_id=block_id))

        attributes.setdefault("repeat_over", None)
        attributes.setdefault("ordering", None)
        attributes.setdefault("empty_behaviour", "REMOVE")
        attributes.setdefault("status", status)
        objects.append({"object_id": block_id, "object_type": "SECTION", **attributes})
    return objects, ranges


def _field_objects(compiled, texts, findings, status):
    objects, seen = [], {}
    for field in getattr(compiled, "fields", None) or ():
        attributes = dict(field)
        field_id = attributes.pop("id", None)
        slots = attributes.get("slots") or ()

        # One anchor, for the first slot that can carry one. A field appearing
        # three times is still one field asking one source column for one value;
        # `slots` addresses every occurrence for the fill engine, and the anchor
        # is what detects that the template moved underneath the manifest.
        anchor = None
        for slot in slots:
            occurrence = _occurrence_of(slot, seen)
            if anchor is not None:
                continue
            try:
                anchor = lift_slot_to_anchor(slot, texts, occurrence=occurrence).as_dict()
            except (ValueError, TypeError) as exc:
                findings.append(_finding(
                    "field_anchor_unliftable",
                    f"Field {field_id!r} could not be given a stable address: {exc}",
                    object_id=field_id, paragraph_index=slot.get("paragraph_index")))

        if not slots:
            findings.append(_finding(
                "field_without_slot",
                f"Field {field_id!r} has nowhere in the document to go, so its value would never "
                "appear. Place it, or remove it.",
                severity="blocking", object_id=field_id))

        attributes["anchor"] = anchor
        attributes.setdefault("source_ref", f"{SOURCE_PREFIX}{field_id}")
        attributes.setdefault("format", None)
        attributes.setdefault("on_missing", DEFAULT_ON_MISSING)
        attributes.setdefault("value_type", attributes.get("type") or "string")
        attributes.setdefault("status", status)
        objects.append({"object_id": field_id, "object_type": "FIELD", **attributes})
    return objects


def _condition_objects(compiled, texts, ranges, findings, status):
    objects = []
    for condition in getattr(compiled, "conditions", None) or ():
        attributes = dict(condition)
        condition_id = attributes.pop("id", None)
        expression = attributes.get("expression") or ""

        governed = [ranges[b] for b in attributes.get("keeps_blocks") or () if b in ranges]
        if governed:
            start = min(s for s, _e in governed)
            end = max(e for _s, e in governed)
            try:
                attributes["anchor_range"] = lift_block_to_anchor_range(
                    {"id": condition_id, "start_paragraph": start, "end_paragraph": end},
                    texts).as_dict()
            except (ValueError, TypeError) as exc:
                findings.append(_finding(
                    "condition_range_unanchorable", str(exc), object_id=condition_id))
        else:
            findings.append(_finding(
                "condition_governs_nothing",
                f"Condition {condition_id!r} names no region this template defines, so whichever "
                "way it evaluates the letter comes out the same. Point it at a block, or remove "
                "it.",
                severity="blocking", object_id=condition_id))

        attributes.setdefault("on_true", KEEP)
        attributes.setdefault("on_false", REMOVE_BLOCK)
        cases = _synthesised_test_cases(
            expression, on_true=attributes["on_true"], on_false=attributes["on_false"])
        attributes["test_cases"] = list(cases)
        if cases:
            findings.append(_finding(
                "generated_test_case_unreviewed",
                f"Condition {condition_id!r} was given {len(cases)} test case(s) recording what "
                "the engine does today, because a compile has no records to draw real ones from. "
                "Confirm they are what this clause is supposed to do.",
                object_id=condition_id))
        else:
            findings.append(_finding(
                "condition_has_no_test_case",
                f"Condition {condition_id!r} could not be run against any record, so nothing "
                f"confirms what {expression!r} does.",
                severity="blocking", object_id=condition_id))

        attributes.setdefault("input_fields", list(condition_inputs(expression) or ()))
        attributes.setdefault("status", status)
        objects.append({"object_id": condition_id, "object_type": "CONDITION", **attributes})
    return objects


def objects_from_compile(body: dict, compiled, *, status: str = PROPOSED) -> tuple:
    """`(objects, findings)` for a compiled manifest read against `body`.

    `compiled` is a `rule_compiler.CompiledManifest`, or anything carrying the
    same `fields` / `conditions` / `blocks` lists.

    Sections are lifted first because a condition's region is the union of the
    blocks it governs, and it cannot be anchored until they have been.
    """
    texts = bp.paragraph_texts(body)
    findings: list = []

    sections, ranges = _section_objects(compiled, texts, findings, status)
    fields = _field_objects(compiled, texts, findings, status)
    conditions = _condition_objects(compiled, texts, ranges, findings, status)

    return fields + conditions + sections, findings


def mark_instructions(body: dict, compiled) -> tuple:
    """Flag every instruction segment the compile deletes as not to be emitted.

    This is the "the AI re-maps it properly" half, made visible. A red run the
    manifest removes is an instruction addressed to whoever assembles the letter,
    and the cleaned template should not carry it -- but the author is the one who
    decides, so it is marked rather than dropped and the editor offers the
    toggle.
    """
    doomed = set()
    for entry in getattr(compiled, "delete_always", None) or ():
        index, span = entry.get("paragraph_index"), entry.get("span_index")
        if isinstance(index, int):
            doomed.add((index, span if isinstance(span, int) else None))

    marked = 0
    for index, block, _in_table in bp.walk_paragraphs(body):
        for position, span_index in bp.span_plan(block["segments"]):
            seg = block["segments"][position]
            if seg.get("role") != bp.INSTRUCTION:
                continue
            if (index, span_index) in doomed or (index, None) in doomed:
                seg["emit"] = False
                marked += 1
    return body, marked


def reslot_against(objects, emitted_body: dict) -> tuple:
    """`(objects, findings)` with every slot re-addressed to the emitted file.

    A published template is not byte-for-byte the one that was read. Author
    instructions the compile removes are written as empty runs, and an empty run
    is invisible to the pre-scanner -- which also means the runs either side of
    it become adjacent and merge. So removing one instruction can renumber every
    span after it in that paragraph, and a manifest carrying the *old* numbering
    would fill the span to the left of each slot for the rest of the paragraph.

    Slots are therefore re-derived by text against the document being published,
    the same way `lift_slot_to_anchor` re-derives anchors. Which is the same rule
    as everywhere else here: a position is never carried across a rewrite, it is
    measured again on the far side.
    """
    findings: list = []
    # (paragraph_index -> [(span_index, span_text)]) for the file being written.
    spans_by_paragraph: dict = {}
    codes: dict = {}
    for index, block, _in_table in bp.walk_paragraphs(emitted_body):
        emitted = bp.emitted_spans(block["segments"])
        for position, span_index in bp.span_plan(emitted):
            spans_by_paragraph.setdefault(index, []).append(
                (span_index, emitted[position].get("text") or ""))
        for seg in block["segments"]:
            if seg.get("role") == bp.MERGEFIELD and bp.emits(seg):
                codes.setdefault((seg.get("code") or "").strip(), []).append(index)

    out = []
    for obj in objects or ():
        if obj.get("object_type") != "FIELD":
            out.append(obj)
            continue
        rebuilt, taken = [], {}
        for slot in obj.get("slots") or ():
            if slot.get("kind") == "mergefield" or slot.get("code"):
                # Consumed in order, not always the first. A code that appears in
                # two paragraphs -- the same salary field in a Full Time table and
                # a Part Time one -- would otherwise send both slots to the same
                # merge field, and the fill engine detaches a field's run sequence
                # when it replaces it, so the second pass would work on elements
                # that are no longer in the document.
                code = (slot.get("code") or "").strip()
                where = codes.get(code) or []
                nth = taken.get(("mf", code), 0)
                if nth < len(where):
                    taken[("mf", code)] = nth + 1
                    rebuilt.append({**slot, "paragraph_index": where[nth]})
                    continue
            # A slot's `text` is the *token* -- `<Colleague First Name>` -- and the
            # span holding it usually carries more: "Dear <Colleague First Name>,".
            # Matching on the whole span text finds nothing, and after an
            # instruction is removed its neighbours merge, so the span text
            # changes again. Containment is what the fill engine itself uses when
            # it writes a value into a span, so it is the right test here too.
            index, token = slot.get("paragraph_index"), slot.get("text") or ""
            key = (index, token)
            candidates = [span_index for span_index, text
                          in spans_by_paragraph.get(index, ()) if token and token in text]
            nth = taken.get(key, 0)
            if nth < len(candidates):
                taken[key] = nth + 1
                rebuilt.append({**slot, "span_index": candidates[nth]})
                continue
            findings.append(_finding(
                "slot_not_in_published_template",
                f"Field {obj.get('object_id')!r} claims {slot.get('text')!r} in paragraph "
                f"{slot.get('paragraph_index')}, which the template being published does not "
                "carry there. It would never be filled.",
                severity="blocking", object_id=obj.get("object_id"),
                paragraph_index=slot.get("paragraph_index")))
        out.append({**obj, "slots": rebuilt})
    return out, findings
