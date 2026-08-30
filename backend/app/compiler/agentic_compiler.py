"""The one compile path: chunk, write, reconcile, ground, assert, review, repeat.

Every template is read by a model. There is no dispatch on whether the rules can
see anything, and no silent substitution when the model cannot be reached -- the
two outcomes are a manifest a person reviews, or a row marked `failed` that
carries the transcript of why. Those used to be indistinguishable: a compile with
no model configured returned the rule-based manifest, which reported fields and a
confidence and looked exactly like a compile that had worked.

The loop is a writer and a reviewer with a deterministic referee between them.

    WRITER      reads chunks of the template and says what it means
    RECONCILE   makes separately-read chunks agree on condition field names
    GROUND      locates every claim inside a real span (llm_compiler.assemble)
    ASSERT      asks the document what is still wrong (compiler.assertions)
    REVIEWER    is shown its own reading plus those faults, and corrects them

The referee is what makes this converge. A reviewer marking its own work has no
fixed point; a reviewer answering "paragraph 44 contains `<Yes/No>` and no field
claims it" has one, and the loop ends when the document stops objecting.

Corrections are applied to the writer's *raw reading*, never to the grounded
manifest, so grounding runs once per round over a single consistent input and
stays the only thing that decides where anything is.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field

from app.compiler import assertions as A
from app.compiler.chunking import build_outline, chunk_paragraphs
from app.compiler.llm_compiler import (
    MAX_CONFIDENCE,
    assemble,
    empty_manifest,
    rules_fell_short,
    write_chunk,
)
from app.compiler.rule_compiler import CompiledManifest, compile_manifest
from app.config import settings
from app.llm.provider import LLMNotConfiguredError, get_llm_provider
from app.templates.parsers.docx_prescan import PreScanResult, prescan

# How many rounds may pass without the assertion count falling before the loop
# accepts that it is not converging. One is too eager -- a round that adds a
# field and exposes an orphan it was masking legitimately holds the count level.
NON_PROGRESS_LIMIT = 2

# Split by action, one array each, because a strict structured-output schema has
# no optional properties: OpenAI requires `required` to name every key in
# `properties`, and an object carrying `type` only for a retype and `occurrences`
# only for an add cannot satisfy that. Expressing the actions as separate arrays
# removes the optionality instead of trying to encode it -- and reads better than
# a single array whose fields mean different things depending on `action`.
#
# This was caught by a live compile returning HTTP 400 rather than by a test,
# which is why `test_review_schema_is_strict_mode_compliant` now exists.
_REASON = {"type": "string"}
_OCCURRENCES = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "paragraph_index": {"type": "integer"},
            "match_text": {"type": "string"},
        },
        "required": ["paragraph_index", "match_text"],
        "additionalProperties": False,
    },
}
_FIELD_TYPE = {"type": "string", "enum": ["string", "currency", "date", "number", "percent"]}


def _array_of(**properties) -> dict:
    """An array of objects where every property is required -- strict mode's rule."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "fields_to_add": _array_of(id={"type": "string"}, type=_FIELD_TYPE,
                                   occurrences=_OCCURRENCES, reason=_REASON),
        "fields_to_remove": _array_of(id={"type": "string"}, reason=_REASON),
        "fields_to_retype": _array_of(id={"type": "string"}, type=_FIELD_TYPE, reason=_REASON),
        "fields_to_relocate": _array_of(id={"type": "string"}, occurrences=_OCCURRENCES, reason=_REASON),
        "conditions_to_add": _array_of(
            id={"type": "string"}, expression={"type": "string"},
            effect={"type": "string", "enum": ["keep", "delete"]},
            start_paragraph={"type": "integer"}, end_paragraph={"type": "integer"},
            compiled_from={"type": "string"}, reason=_REASON,
        ),
        "conditions_to_remove": _array_of(id={"type": "string"}, reason=_REASON),
        "conditions_to_rewrite": _array_of(id={"type": "string"}, expression={"type": "string"}, reason=_REASON),
        # The reviewer must be able to add everything the writer can. Both of
        # these were added to the writer's schema and not to this one, so the
        # loop was handed "instruction text nothing removes" and had no move
        # that could clear it -- it spent every round applying corrections that
        # changed nothing and parked as unconverged.
        "instruction_spans_to_add": _array_of(
            paragraph_index={"type": "integer"}, match_text={"type": "string"}, reason=_REASON,
        ),
        "inline_branches_to_add": _array_of(
            id={"type": "string"}, expression={"type": "string"},
            paragraph_index={"type": "integer"}, match_text={"type": "string"},
            compiled_from={"type": "string"}, reason=_REASON,
        ),
        "blocks_to_remove": _array_of(id={"type": "string"}, reason=_REASON),
        "scaffolding_add": {"type": "array", "items": {"type": "integer"}},
        "scaffolding_remove": {"type": "array", "items": {"type": "integer"}},
        "verdict": {"type": "string", "enum": ["approved", "needs_more_work"]},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "fields_to_add", "fields_to_remove", "fields_to_retype", "fields_to_relocate",
        "conditions_to_add", "conditions_to_remove", "conditions_to_rewrite",
        "instruction_spans_to_add", "inline_branches_to_add", "blocks_to_remove",
        "scaffolding_add", "scaffolding_remove", "verdict", "notes",
    ],
    "additionalProperties": False,
}

REVIEW_SYSTEM = (
    "You are reviewing a compiled reading of a document template, produced by another model "
    "from the same paragraphs you are given.\n\n"
    "You are also given MECHANICAL FAULTS: things the document itself says are wrong with the "
    "reading. They are computed, not opinions, and each one names a paragraph and quotes the "
    "text. Your job is to correct them, and to look for anything else that is wrong.\n\n"
    "Rules that matter more than tidiness:\n"
    "* An uncovered placeholder means the text is printed to the reader exactly as written. "
    "Add a field with an occurrence for it. If it is genuinely not a placeholder -- a ruled "
    "line to write on, a syntax example in a legend -- say so in `notes` instead of inventing "
    "a field.\n"
    "* An uncovered merge field is a placeholder Word encoded invisibly. If a bracket "
    "placeholder on the same paragraph means the same thing, they are ONE slot: do not create "
    "a second field, and say in `notes` that the two encodings collide, because filling both "
    "prints two values on top of each other.\n"
    "* A condition expression must be executable: `field_name == 'value'`, snake_case name, "
    "quoted value. Prose is not an expression. Alternatives share one field and differ only in "
    "its value -- two instructions that are alternatives must never become two booleans, "
    "because both can then be true and the letter gets both variants.\n"
    "* Never remove a field or a paragraph merely to silence a fault. Deleting a paragraph "
    "that carries a real value is worse than the fault it clears: the letter loses content "
    "silently, where the fault was at least visible.\n\n"
    "Corrections go in the array that matches what you are doing: `fields_to_add`, "
    "`fields_to_remove`, `fields_to_retype`, `fields_to_relocate`, `conditions_to_add`, "
    "`conditions_to_remove`, `conditions_to_rewrite`, `instruction_spans_to_add`, "
    "`inline_branches_to_add`, `blocks_to_remove`. Leave the rest empty.\n\n"
    "Two faults have specific answers:\n"
    "* 'instruction text that nothing removes' -- if the instruction introduces a whole section, "
    "make that section a condition. If its alternatives sit INSIDE the same paragraph next to "
    "text the letter needs, add one `inline_branches_to_add` per alternative, each naming the "
    "exact text of that alternative and the expression that keeps it; the surrounding "
    "instruction text is then removed for you. Otherwise add it to `instruction_spans_to_add`.\n"
    "* 'whole-paragraph blocks ... alternatives sit inside the one paragraph' -- remove those "
    "blocks with `blocks_to_remove` and replace them with `inline_branches_to_add`. A "
    "whole-paragraph block deletes the entire line, sentence included.\n\n"
    "Set `verdict` to \"approved\" only when you believe the reading is correct AND you are "
    "proposing no corrections. Prefer proposing nothing over proposing a guess."
)

RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "expression": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "expression", "reason"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rewrites", "notes"],
    "additionalProperties": False,
}

RECONCILE_SYSTEM = (
    "Several parts of one template were read separately and their conditions merged. Because "
    "no reader saw the whole document, alternatives may have been given different field names "
    "-- 'USE FOR NEW HIRE' and 'USE FOR CURRENT COLLEAGUES' are two values of one "
    "`employee_status` field, and as two independent booleans both can be true and the letter "
    "gets both openings.\n\n"
    "You are given every condition, with the instruction text each was compiled from. Rewrite "
    "only the expressions that should share a field with another condition. Keep the same "
    "executable shape: `field_name == 'value'`. Return an empty list when they are already "
    "consistent -- an unnecessary rewrite is a change to a working template."
)


@dataclass
class Round:
    number: int
    assertion_count: int
    assertions: list = dc_field(default_factory=list)
    verdict: str = ""
    corrections_applied: int = 0
    action: str = ""
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "round": self.number,
            "assertion_count": self.assertion_count,
            "assertions": [a.as_dict() for a in self.assertions[:20]],
            "verdict": self.verdict,
            "corrections_applied": self.corrections_applied,
            "action": self.action,
            "model": self.model,
        }


@dataclass
class CompileOutcome:
    """What the compile produced, and whether it may be persisted as a draft.

    `ok=False` is never accompanied by a usable manifest -- the caller writes a
    `failed` row. There is deliberately no third state where a partial reading is
    offered as though it were complete.
    """

    manifest: CompiledManifest
    ok: bool = False
    reason: str = ""
    transcript: list = dc_field(default_factory=list)

    def transcript_dicts(self) -> list[dict]:
        return [r.as_dict() if isinstance(r, Round) else r for r in self.transcript]


def _paragraph_texts(scan: PreScanResult) -> list[str]:
    texts = [""] * len(scan.paragraphs)
    for span in scan.spans:
        if span.paragraph_index < len(texts):
            texts[span.paragraph_index] += span.text
    return texts


#: How a Word merge field is written into the text the model reads. The
#: guillemets are Word's own notation for a field, and they cannot occur in a
#: bracket placeholder, so `assemble` can tell the two apart with no ambiguity.
MERGEFIELD_OPEN, MERGEFIELD_CLOSE = "\u00ab", "\u00bb"


def paragraph_texts_for_model(scan: PreScanResult) -> list[str]:
    """The paragraphs, with merge fields made visible.

    A MERGEFIELD is not a span -- its runs are consumed by the pre-scanner into a
    `MergeField`, and `_paragraph_texts` sums spans. So a salary row that reads
    `$«LAB__FT_SALARY__38_HR_»` in Word was handed to the model as `"$"`, and the
    model was then faulted for not claiming a field it had no way to know was
    there. The reviewer was blind to it for the same reason, so no round could
    ever fix it, and the template compiled to a manifest that left three merge
    fields unresolved and blocked every document at the generation gate.

    Writing them in as `«CODE»` is the whole fix: the model can see the slot, name
    it, and report an occurrence for it like any other.
    """
    texts = list(_paragraph_texts(scan))
    for mf in scan.mergefields:
        if mf.paragraph_index < len(texts):
            code = (mf.code or "").strip().strip('"')
            if code:
                texts[mf.paragraph_index] += f"{MERGEFIELD_OPEN}{code}{MERGEFIELD_CLOSE}"
    return texts


def _merge_readings(readings: list[dict]) -> dict:
    """Union the per-chunk readings into one, before anything is grounded.

    Chunks overlap, so the same field and the same condition arrive more than
    once. Fields merge on id and union their occurrences; conditions merge on the
    instruction they were compiled from, because two chunks that both saw an
    overlapping instruction will give it two ids for the same branch.
    """
    fields: dict[str, dict] = {}
    for reading in readings:
        for item in reading.get("fields") or []:
            fid = (item.get("id") or "").strip()
            if not fid:
                continue
            existing = fields.get(fid)
            if existing is None:
                fields[fid] = copy.deepcopy(item)
                continue
            seen = {(o.get("paragraph_index"), o.get("match_text")) for o in existing.get("occurrences") or []}
            for occurrence in item.get("occurrences") or []:
                key = (occurrence.get("paragraph_index"), occurrence.get("match_text"))
                if key not in seen:
                    seen.add(key)
                    existing.setdefault("occurrences", []).append(occurrence)
            # A typed reading beats an untyped one: one chunk seeing "$" and
            # another not is agreement about the field and disagreement about
            # the formatting, and `string` is the default, not a judgement.
            if existing.get("type") in (None, "string") and item.get("type") not in (None, "string"):
                existing["type"] = item["type"]

    conditions: dict[tuple, dict] = {}
    for reading in readings:
        for item in reading.get("conditions") or []:
            key = (
                (item.get("compiled_from") or "").strip().casefold()
                or (item.get("expression") or "").strip().casefold(),
                item.get("start_paragraph"),
            )
            if key not in conditions:
                conditions[key] = copy.deepcopy(item)
            else:
                # Keep the wider range: a chunk that saw only part of a block
                # reports a block that ends where its own paragraphs did.
                existing = conditions[key]
                if (item.get("end_paragraph") or 0) > (existing.get("end_paragraph") or 0):
                    existing["end_paragraph"] = item["end_paragraph"]

    scaffolding: set[int] = set()
    notes: list[str] = []
    language = ""
    for reading in readings:
        scaffolding |= {i for i in (reading.get("scaffolding_paragraphs") or []) if isinstance(i, int)}
        notes.extend(reading.get("notes") or [])
        language = language or (reading.get("language") or "")

    return {
        "fields": list(fields.values()),
        "conditions": list(conditions.values()),
        "scaffolding_paragraphs": sorted(scaffolding),
        "language": language,
        "notes": notes,
    }


def _slug_id(value) -> str:
    """Block ids are built as `blk_<n>_<condition id>`, so a reviewer naming
    either spelling should reach the same condition."""
    import re as _re
    return _re.sub(r"[\W_]+", "", str(value or "").lower())


def _apply_corrections(reading: dict, data: dict) -> int:
    """Fold the reviewer's corrections into the raw reading. Returns how many stuck.

    Every branch is guarded, because a correction the schema permits is not
    automatically one that makes sense: `remove` naming a field that does not
    exist, `add` with no occurrences, a rewrite that empties an expression.
    Applying those would count as progress and let a loop that is going nowhere
    run to its ceiling instead of stopping.
    """
    applied = 0
    fields = {f.get("id"): f for f in reading.get("fields") or []}

    for item in data.get("fields_to_add") or []:
        fid, occurrences = (item.get("id") or "").strip(), item.get("occurrences") or []
        if not fid or not occurrences or fid in fields:
            continue
        new_field = {
            "id": fid, "type": item.get("type") or "string",
            "occurrences": occurrences, "required": False,
            "reason": item.get("reason", ""),
        }
        reading.setdefault("fields", []).append(new_field)
        fields[fid] = new_field
        applied += 1

    for item in data.get("fields_to_remove") or []:
        fid = (item.get("id") or "").strip()
        if fid in fields:
            reading["fields"] = [f for f in reading["fields"] if f.get("id") != fid]
            del fields[fid]
            applied += 1

    for item in data.get("fields_to_retype") or []:
        fid, new_type = (item.get("id") or "").strip(), item.get("type")
        if fid in fields and new_type and fields[fid].get("type") != new_type:
            fields[fid]["type"] = new_type
            applied += 1

    for item in data.get("fields_to_relocate") or []:
        fid, occurrences = (item.get("id") or "").strip(), item.get("occurrences") or []
        if fid in fields and occurrences:
            fields[fid]["occurrences"] = occurrences
            applied += 1

    conditions = {c.get("id"): c for c in reading.get("conditions") or []}

    for item in data.get("conditions_to_add") or []:
        cid = (item.get("id") or "").strip()
        expression = (item.get("expression") or "").strip()
        start_p, end_p = item.get("start_paragraph"), item.get("end_paragraph")
        if not cid or not expression or cid in conditions or start_p is None or end_p is None:
            continue
        new_condition = {
            "id": cid, "expression": expression,
            "effect": item.get("effect") or "keep",
            "start_paragraph": start_p, "end_paragraph": end_p,
            "compiled_from": item.get("compiled_from", ""),
        }
        reading.setdefault("conditions", []).append(new_condition)
        conditions[cid] = new_condition
        applied += 1

    for item in data.get("conditions_to_remove") or []:
        cid = (item.get("id") or "").strip()
        if cid in conditions:
            reading["conditions"] = [c for c in reading["conditions"] if c.get("id") != cid]
            del conditions[cid]
            applied += 1

    for item in data.get("conditions_to_rewrite") or []:
        cid, expression = (item.get("id") or "").strip(), (item.get("expression") or "").strip()
        if cid in conditions and expression and conditions[cid].get("expression") != expression:
            conditions[cid]["expression"] = expression
            applied += 1

    for item in data.get("instruction_spans_to_add") or []:
        match_text = (item.get("match_text") or "").strip()
        if not match_text:
            continue
        entry = {
            "paragraph_index": item.get("paragraph_index", 0),
            "match_text": match_text, "reason": item.get("reason", ""),
        }
        if entry not in (reading.get("instruction_spans") or []):
            reading.setdefault("instruction_spans", []).append(entry)
            applied += 1

    inline = {b.get("id") for b in reading.get("inline_branches") or []}
    for item in data.get("inline_branches_to_add") or []:
        bid = (item.get("id") or "").strip()
        expression = (item.get("expression") or "").strip()
        match_text = (item.get("match_text") or "").strip()
        if not bid or not expression or not match_text or bid in inline:
            continue
        reading.setdefault("inline_branches", []).append({
            "id": bid, "expression": expression,
            "paragraph_index": item.get("paragraph_index", 0),
            "match_text": match_text, "compiled_from": item.get("compiled_from", ""),
        })
        inline.add(bid)
        applied += 1

    # A whole-paragraph condition replaced by inline branches has to go, or the
    # paragraph is still deleted wholesale by the block nobody removed.
    doomed_blocks = {(c.get("id") or "").strip() for c in data.get("blocks_to_remove") or []}
    if doomed_blocks:
        before = len(reading.get("conditions") or [])
        reading["conditions"] = [
            c for c in reading.get("conditions") or []
            if _slug_id(c.get("id")) not in {_slug_id(b) for b in doomed_blocks}
        ]
        applied += before - len(reading["conditions"])

    scaffolding = {i for i in (reading.get("scaffolding_paragraphs") or []) if isinstance(i, int)}
    added = {i for i in (data.get("scaffolding_add") or []) if isinstance(i, int)} - scaffolding
    removed = scaffolding & {i for i in (data.get("scaffolding_remove") or []) if isinstance(i, int)}
    if added or removed:
        reading["scaffolding_paragraphs"] = sorted((scaffolding | added) - removed)
        applied += len(added) + len(removed)
    return applied


def _reconcile(reading: dict, *, llm_policy=None) -> tuple[int, str]:
    """Make separately-read chunks agree on condition field names."""
    conditions = reading.get("conditions") or []
    if len(conditions) < 2:
        return 0, ""
    provider = get_llm_provider("Reconciling conditions across template parts", policy=llm_policy)
    listed = "\n".join(
        f"- id={c.get('id')!r} expression={c.get('expression')!r} "
        f"compiled_from={(c.get('compiled_from') or '')[:160]!r}"
        for c in conditions
    )
    result = provider.structured(
        system=RECONCILE_SYSTEM,
        prompt=f"Conditions read from this template:\n{listed}",
        schema=RECONCILE_SCHEMA,
        purpose="compile",
    )
    if result.data is None:
        return 0, result.error or "reconcile call returned nothing"
    by_id = {c.get("id"): c for c in conditions}
    applied = 0
    for rewrite in result.data.get("rewrites") or []:
        cid, expression = rewrite.get("id"), (rewrite.get("expression") or "").strip()
        if cid in by_id and expression and by_id[cid].get("expression") != expression:
            by_id[cid]["expression"] = expression
            applied += 1
    return applied, ""


def _focus_body(paragraph_texts: list[str], faults, outline: str, *, budget_chars: int) -> str:
    """The paragraphs the reviewer actually needs, with room around them.

    Handing it the first chunk would be wrong on any template that needed
    splitting -- most faults would name paragraphs it could not see, and it would
    be asked to correct text it was never shown. Handing it the whole document
    fails for the same reason the writer had to be chunked.

    So the review window is built from the faults: every paragraph one names,
    plus a few either side, because a conditional block's instruction and the
    clause it governs are neighbours and a correction to one needs the other in
    view. Gaps are marked rather than closed silently, so the model can tell a
    missing paragraph from an adjacent one.
    """
    if not paragraph_texts:
        return ""
    anchors = sorted({
        a.paragraph_index for a in faults
        if isinstance(getattr(a, "paragraph_index", None), int)
        and 0 <= a.paragraph_index < len(paragraph_texts)
    })
    if not anchors:
        wanted = set(range(len(paragraph_texts)))
    else:
        wanted = set()
        for index in anchors:
            wanted |= set(range(max(0, index - 4), min(len(paragraph_texts), index + 5)))

    lines, size, previous = [], len(outline), None
    for index in sorted(wanted):
        text = paragraph_texts[index]
        if not text.strip():
            continue
        if previous is not None and index > previous + 1:
            lines.append(f"... [{index - previous - 1} paragraph(s) not shown] ...")
        entry = f"[{index}] {text}"
        if size + len(entry) > budget_chars:
            lines.append("... [truncated] ...")
            break
        lines.append(entry)
        size += len(entry) + 1
        previous = index
    return outline + "\n".join(lines)


def _review(reading: dict, manifest: CompiledManifest, faults, body: str, *, llm_policy=None):
    """Show the reviewer its own reading, the document, and the faults."""
    provider = get_llm_provider("Reviewing a compiled manifest", policy=llm_policy)
    summary = {
        "fields": [
            {"id": f["id"], "type": f.get("type"), "occurrences": len(f.get("slots") or [])}
            for f in manifest.fields
        ],
        "conditions": [
            {"id": c["id"], "expression": c.get("expression"), "compiled_from": c.get("compiled_from")}
            for c in manifest.conditions
        ],
        "blocks": [
            {"id": b["id"], "start_paragraph": b.get("start_paragraph"), "end_paragraph": b.get("end_paragraph")}
            for b in manifest.blocks
        ],
        "scaffolding_paragraphs": reading.get("scaffolding_paragraphs") or [],
    }
    import json

    return provider.structured(
        system=REVIEW_SYSTEM,
        prompt=(
            f"THE READING SO FAR:\n{json.dumps(summary, ensure_ascii=False)}\n\n"
            f"MECHANICAL FAULTS:\n{A.render(faults)}\n\n"
            f"THE TEMPLATE:\n{body}"
        ),
        schema=REVIEW_SCHEMA,
        purpose="compile",
    )


def compile_template(
    template_path: str,
    *,
    llm_policy=None,
    evidence=None,
    test_fill=None,
    progress=None,
) -> CompileOutcome:
    """Read, review and ground a template. The only compile path.

    `test_fill` is an optional callable taking the grounded manifest dict and
    returning a list of QA failure notes -- the strongest assertion available,
    because it fills the manifest against real rows and runs the generation
    gates. Left out when the caller has no sample data.

    Never returns a partial reading dressed as a complete one. On any failure the
    outcome carries `ok=False` and an empty manifest whose `compiled_by` says
    which stage gave up.
    """
    scan = prescan(template_path)
    paragraph_texts = paragraph_texts_for_model(scan)

    # No key is "the model was unavailable", which is a compile that did not
    # happen -- recorded as failed, with the reason, so it is visible and
    # unapprovable. Deliberately NOT re-raised: raising here produced a 422 and
    # no row at all, so a reviewer had nothing to look at and no way to tell an
    # unconfigured deployment from a template the compiler choked on.
    #
    # A residency error is a different thing and still raises. `ResidencyUnscoped`
    # is a call site that forgot to name the tenant, and `ResidencyViolation` is a
    # deployment this organisation's data may not reach -- neither is a property
    # of the template, and neither should be recorded against it.
    try:
        get_llm_provider("Compiling a template", policy=llm_policy)
    except LLMNotConfiguredError as exc:
        reason = f"No model is configured to read this template: {exc}"
        return CompileOutcome(
            manifest=empty_manifest(scan, compiled_by="llm_unavailable", notes=[reason]),
            ok=False, reason=reason,
            transcript=[{"stage": "provider", "error": str(exc)}],
        )

    def _stage(key, label, kind="model"):
        if progress is None:
            class _Null:
                def __enter__(self): return {}
                def __exit__(self, *a): return False
            return _Null()
        return progress.stage(key, label, kind)

    # Recorded, not acted on. This used to decide whether a model saw the
    # template at all; it is now a diagnostic that tells a reviewer whether the
    # deterministic layer would have been enough, which is the question worth
    # asking when a compile costs money.
    rule_shortfall = rules_fell_short(scan, compile_manifest(scan))

    with _stage("chunk", "Splitting the template for reading", "deterministic"):
        chunks = chunk_paragraphs(
            paragraph_texts,
            budget_chars=settings.compile_chunk_budget_chars,
            overlap_paragraphs=settings.compile_chunk_overlap_paragraphs,
        )
        outline = build_outline(paragraph_texts) if len(chunks) > 1 else ""

    transcript: list = [{
        "stage": "scan",
        "paragraphs": len(scan.paragraphs),
        "chunks": len(chunks),
        "rules_would_have_fallen_short": bool(rule_shortfall),
        "rule_shortfall_reason": (rule_shortfall or "")[:300] or None,
    }]

    # ---- write ----------------------------------------------------------
    readings: list[dict] = []
    model_name = "llm"
    with _stage("write", f"Reading {len(chunks)} part(s) of the template"):
        for chunk in chunks:
            result = write_chunk(chunk.render(outline), evidence=evidence, llm_policy=llm_policy)
            if result.data is None:
                reason = (
                    f"The model could not read part {chunk.number} of {chunk.total} "
                    f"(paragraphs {chunk.start_index}-{chunk.end_index}): {result.error}"
                )
                transcript.append({"stage": "write", "chunk": chunk.number, "error": result.error})
                return CompileOutcome(
                    manifest=empty_manifest(scan, compiled_by="llm_failed", notes=[reason]),
                    ok=False, reason=reason, transcript=transcript,
                )
            model_name = result.model or model_name
            readings.append(result.data)

    reading = _merge_readings(readings)
    transcript.append({
        "stage": "merge", "chunks": len(readings),
        "fields": len(reading["fields"]), "conditions": len(reading["conditions"]),
    })

    # ---- reconcile ------------------------------------------------------
    if len(chunks) > 1:
        with _stage("reconcile", "Making the parts agree on condition fields"):
            rewritten, error = _reconcile(reading, llm_policy=llm_policy)
            transcript.append({"stage": "reconcile", "rewrites": rewritten, "error": error or None})

    # ---- ground / assert / review ---------------------------------------
    manifest = assemble(scan, reading, model=model_name)
    faults, warnings = A.collect_with_warnings(
        scan, _as_dict(manifest), test_fill_notes=_notes_from(test_fill, manifest))
    best = len(faults)
    stalled = 0
    rounds: list[Round] = []

    for number in range(1, max(1, settings.compile_max_rounds) + 1):
        if not faults:
            rounds.append(Round(number, 0, [], "approved", 0, "no faults; nothing to review", model_name))
            break
        with _stage("review", f"Reviewing the reading (round {number})"):
            body = _focus_body(
                paragraph_texts, faults, outline,
                budget_chars=settings.compile_chunk_budget_chars,
            )
            result = _review(reading, manifest, faults, body, llm_policy=llm_policy)
        if result.data is None:
            reason = f"The reviewer could not be reached on round {number}: {result.error}"
            rounds.append(Round(number, len(faults), faults, "", 0, reason, model_name))
            transcript.extend(r.as_dict() for r in rounds)
            return CompileOutcome(
                manifest=empty_manifest(scan, compiled_by="llm_failed", notes=[reason]),
                ok=False, reason=reason, transcript=transcript,
            )

        verdict = result.data.get("verdict", "")
        applied = _apply_corrections(reading, result.data)
        reading["notes"] = [*(reading.get("notes") or []), *(result.data.get("notes") or [])]
        manifest = assemble(scan, reading, model=model_name)
        faults, warnings = A.collect_with_warnings(
            scan, _as_dict(manifest), test_fill_notes=_notes_from(test_fill, manifest))

        rounds.append(Round(
            number, len(faults), faults, verdict, applied,
            f"applied {applied} correction(s)", result.model or model_name,
        ))

        if not faults and verdict == "approved":
            break
        if len(faults) < best:
            best, stalled = len(faults), 0
        else:
            stalled += 1
            if stalled >= NON_PROGRESS_LIMIT:
                break

    transcript.extend(r.as_dict() for r in rounds)

    if faults:
        reason = (
            f"{len(faults)} fault(s) remained after {len(rounds)} review round(s); the first is: "
            f"{faults[0].detail}"
        )
        failed = empty_manifest(scan, compiled_by="llm_unconverged", notes=[reason])
        return CompileOutcome(manifest=failed, ok=False, reason=reason, transcript=transcript)

    # Warnings ride on the manifest and block auto-approval: the compiler's own
    # contract is that any warning needs an explicit disposition from a person.
    manifest.warnings = [*(getattr(manifest, "warnings", None) or []), *warnings]
    manifest.confidence = min(MAX_CONFIDENCE, manifest.confidence)
    manifest.notes = [
        *(manifest.notes or []),
        f"Read by {model_name} in {len(chunks)} part(s) over {len(rounds)} review round(s).",
        *(
            ["The deterministic compiler alone would not have read this template's branch logic."]
            if rule_shortfall else []
        ),
    ]
    return CompileOutcome(manifest=manifest, ok=True, transcript=transcript)


def _as_dict(manifest: CompiledManifest) -> dict:
    return {
        "fields": manifest.fields, "conditions": manifest.conditions,
        "blocks": manifest.blocks, "delete_always": manifest.delete_always,
    }


def _notes_from(test_fill, manifest: CompiledManifest) -> list[str]:
    """QA failures from a real fill, or nothing when the caller supplied no data.

    A test fill that raises is itself a finding, not a reason to abandon the
    round -- the message is handed to the reviewer like any other fault.
    """
    if test_fill is None:
        return []
    try:
        return list(test_fill(_as_dict(manifest)) or [])
    except Exception as exc:  # noqa: BLE001 - surfaced to the reviewer, not swallowed
        return [f"Filling this manifest against sample data raised {type(exc).__name__}: {exc}"]
