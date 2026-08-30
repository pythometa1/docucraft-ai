"""Compiling templates that carry no colour convention.

The rule compiler is blind without colour: the Pest Control form in this repo
produces **0 fields from 495 paragraphs**, because nothing is blue, nothing is
red, and there are no `<brackets>` to anchor on. That is not an edge case — most
of a real estate looks like this, and so does every template written outside the
one convention this codebase was first built against.

The split that makes it work: the rule layer already handles everything
language-independent (colours, brackets, MERGEFIELDs, paragraph indices), so
the model is only asked to do the part rules genuinely cannot — read the
document and say which literal text is a placeholder and which paragraphs are
conditional. Its answers are then located back inside the pre-scan's real spans,
so the output is ordinary `(paragraph_index, span_index)` coordinates and the
existing fill engine executes them unchanged.

Confidence is capped and status forced to draft: an inferred manifest is a
proposal for a human, never something to generate from unreviewed.
"""

import re
from dataclasses import asdict

from app.templates.parsers.docx_prescan import PreScanResult
from app.llm.provider import get_llm_provider
from app.compiler.rule_compiler import CompiledManifest, _slug, find_control_markers

MAX_CONFIDENCE = 0.7

COMPILE_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["string", "currency", "date", "number", "percent"]},
                    # EVERY place this placeholder appears, not just the first.
                    #
                    # This was a single (paragraph_index, match_text) pair, and
                    # the gap was covered by merging the rule compiler's bracket
                    # inventory in afterwards -- rules find every occurrence of a
                    # token exactly. That merge is gone: the model authors the
                    # field list now, so it has to report the occurrences too.
                    # `<currency>` appears in three paragraphs of the
                    # compensation template and filling only the first leaves two
                    # placeholders printed in the finished letter.
                    "occurrences": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "paragraph_index": {"type": "integer"},
                                "match_text": {"type": "string"},
                            },
                            "required": ["paragraph_index", "match_text"],
                            "additionalProperties": False,
                        },
                    },
                    "required": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "type", "occurrences", "required", "reason"],
                "additionalProperties": False,
            },
        },
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "expression": {"type": "string"},
                    "effect": {
                        "type": "string",
                        "enum": ["keep", "delete"],
                        "description": "keep = the paragraphs appear when the expression is true. delete = the paragraphs are REMOVED when it is true (use this for instructions phrased as 'delete this section if ...').",
                    },
                    "start_paragraph": {"type": "integer"},
                    "end_paragraph": {"type": "integer"},
                    "compiled_from": {"type": "string"},
                },
                "required": ["id", "expression", "effect", "start_paragraph", "end_paragraph", "compiled_from"],
                "additionalProperties": False,
            },
        },
        # Instruction text that shares a paragraph with content the letter needs.
        #
        # `scaffolding_paragraphs` deletes whole paragraphs, and that is the wrong
        # tool here: the acceptance clause reads "please sign, date, and return a
        # copy of this letter to <instruction> <contact> <instruction> <contact>."
        # Deleting the paragraph takes the sentence with it -- which is a real
        # failure this estate has shipped, with `qa_passed` True.
        "instruction_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph_index": {"type": "integer"},
                    "match_text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["paragraph_index", "match_text", "reason"],
                "additionalProperties": False,
            },
        },
        # A switch whose branches are runs inside one paragraph rather than
        # paragraphs of their own. Paragraph ranges cannot express "keep this run,
        # drop that one", so without this the letter keeps both branches and names
        # both contacts in one sentence.
        "inline_branches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "expression": {"type": "string"},
                    "paragraph_index": {"type": "integer"},
                    "match_text": {"type": "string"},
                    "compiled_from": {"type": "string"},
                },
                "required": ["id", "expression", "paragraph_index", "match_text", "compiled_from"],
                "additionalProperties": False,
            },
        },
        "scaffolding_paragraphs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Indices of paragraphs that are instructions ABOUT the template rather than letter content -- control pages, legends, token syntax guides, colour keys. These must never appear in a generated document.",
        },
        "language": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fields", "conditions", "instruction_spans", "inline_branches", "scaffolding_paragraphs", "language", "notes"],
    "additionalProperties": False,
}

COMPILE_SYSTEM = (
    "You compile document templates into machine-executable manifests. You are given a "
    "template's paragraphs, each prefixed with its index.\n\n"
    "Identify five things, and put each in the array named for it. Anything you describe only "
    "in `notes` is commentary: it changes nothing and the document is generated without it.\n"
    "1. PLACEHOLDERS -> `fields` -- literal text that changes for each generated document (a person's "
    "name, a date, an amount, a role). Type them: `currency` for money, `date` for dates, "
    "`percent` for a rate the data stores as a fraction (0.05 meaning 5%), `number` for other "
    "quantities. Give each a stable snake_case id.\n"
    "   Report EVERY occurrence of the placeholder in `occurrences`, not just the first. A "
    "token like `<currency>` or `<Colleague First Name>` is commonly used in several "
    "paragraphs, and an occurrence you leave out is not filled -- it is printed to the reader "
    "exactly as written. For each one give the paragraph index it sits in and, as "
    "`match_text`, the exact substring copied character-for-character from that paragraph so "
    "it can be located again. Include the surrounding brackets when the template writes them: "
    "`<Colleague First Name>`, not `Colleague First Name`.\n"
    "   One placeholder is one field with several occurrences, never several fields. If the "
    "same text appears in the opening sentence and again in a revert clause, that is one "
    "field with two occurrences.\n"
    "   Text between GUILLEMETS -- \u00abLAB__FT_SALARY__38_HR_\u00bb -- is a Word merge field: a "
    "placeholder the previous generation of this template encoded in Word's own machinery. It "
    "is a slot like any other and you must claim it, or nothing will ever write a value into "
    "it and every document will be blocked. Give the code exactly as shown for `match_text`. A "
    "code that appears on several paragraphs is ONE field with several occurrences. When a "
    "merge field and a bracket placeholder on the same paragraph mean the SAME value, claim "
    "them as two occurrences of one field and say so in `notes`; when they mean different "
    "values -- a grade and a salary in one sentence -- they are different fields.\n"
    "2. CONDITIONAL BLOCKS -> `conditions` -- paragraph ranges whose presence depends on the data. Set "
    "`effect`: \"keep\" if the paragraphs appear when the expression is true, \"delete\" if "
    "they are removed when it is true. Templates phrase these both ways -- "
    "'[[IF bonus > 0]] ... [[ENDIF]]' is keep, while '[[IF bonus = 0]] Delete this entire "
    "section [[ENDIF]]' is delete. Let `effect` carry the polarity; do not invert it "
    "yourself.\n"
    "   The expression must always be EXECUTABLE: `field_name == 'value'`, with a snake_case "
    "field name and a quoted value. Two shapes reach you and they need different work.\n"
    "   (a) The template already writes an expression -- '[[IF bonus > 0]]'. Report it "
    "verbatim.\n"
    "   (b) The template writes an instruction in English -- 'USE IF ON TEMPORARY "
    "ASSIGNMENT', 'INCLUDE IF COLLEAGUE TYPE IS FIXED TERM', '(Include if the colleague is "
    "working part time hours)'. These name no field and no value, so you must supply both: "
    "read what the instruction means and express it as a control field, e.g. "
    "`temporary_assignment == 'Yes'`, `colleague_type == 'Fixed term'`, "
    "`work_schedule == 'Part time'`. Echoing the instruction text back is not an expression "
    "and it will be discarded -- the section then appears in every document, including the "
    "ones it was written to exclude.\n"
    "   Instructions that are alternatives of each other MUST share one field and differ "
    "only in its value. 'USE FOR NEW HIRE' and 'USE FOR CURRENT COLLEAGUES' are two values of "
    "`employee_status`, not two independent yes/no flags -- as separate booleans both can be "
    "true at once and the letter gets both openings. Look across the whole template for the "
    "set an instruction belongs to before naming its field.\n"
    "   A control field you invent this way does not appear anywhere in the document as a "
    "placeholder. That is expected: it is read from the source data to decide which sections "
    "survive, never printed.\n"
    "3. INSTRUCTION SPANS -> `instruction_spans` -- instruction text that shares a paragraph with words the letter "
    "needs. 'For transactions initiated with recruitment (e.g. Change Job with Recruitment)' "
    "sits in the middle of 'please sign, date, and return a copy of this letter to ...'. Deleting "
    "the paragraph would take the sentence with it, so report the instruction's exact text and "
    "its paragraph and it is removed on its own. Report every such run; text you leave is printed "
    "to the reader.\n"
    "4. INLINE BRANCHES -> `inline_branches` -- a switch whose alternatives are phrases inside ONE paragraph rather "
    "than paragraphs of their own. The sentence above offers two contacts, one for transactions "
    "with recruitment and one for transactions without; exactly one must survive. Give the exact "
    "text of each alternative and the expression that keeps it. Both alternatives of a switch "
    "share one field and differ only in its value, as in 2 above. Leave this empty when a "
    "conditional region spans whole paragraphs -- report those in `conditions`.\n"
    "5. SCAFFOLDING -> `scaffolding_paragraphs` -- paragraphs that document the template rather than form part of the "
    "letter: control pages, colour legends, token-syntax guides, notes to whoever maintains "
    "the file. They frequently announce themselves ('NOT PART OF THE ISSUED LETTER'). List "
    "their indices so they can be deleted. Be conservative: when a paragraph could plausibly "
    "be letter content, leave it out.\n\n"
    "The template may be in any language; interpret it in whatever language it is written. "
    "Do not invent placeholders for text that is the same in every document -- legal "
    "boilerplate, headings, and letterhead are static and must be left alone. Prefer "
    "returning nothing over guessing: a missed placeholder is corrected in review, while a "
    "wrongly-replaced clause silently corrupts every document from this template."
)


def _expression_is_executable(expression: str) -> bool:
    """Whether this string is a rule the engine can actually run.

    Parse-only: the expression is probed against a record that supplies every
    name it mentions, so the one thing that can come back is a parse failure.
    A rule that references nothing is rejected too -- its verdict can never
    change, so it is a constant wearing a condition's clothes.
    """
    from app.expressions.token_parser import condition_inputs, evaluate_condition

    text = (expression or "").strip()
    if not text:
        return False
    names = condition_inputs(text)
    if not names:
        return False
    return evaluate_condition(text, {name: "" for name in names}).reason != "unparseable"


def _squash(field_id: str) -> str:
    """Field id reduced to letters and digits, for comparing two spellings of
    the same placeholder (`address_line1` vs `address_line_1`)."""
    return re.sub(r"[^a-z0-9]", "", field_id.lower())


def _mergefield_code(match_text: str, paragraph_index: int, by_paragraph: dict) -> tuple[int, str] | None:
    """`(paragraph_index, code)` for the merge field this claim names, or None.

    Returns the paragraph the code was actually FOUND on, not the one the model
    reported. The two differ often enough to matter -- a model reading a long
    sentence reports the index of its first line -- and `docx_renderer` looks a
    slot up by the exact `(paragraph_index, code)` pair. Storing the reported
    index produced slots that named a real code at a paragraph that does not
    carry it, matched nothing, and left the merge field unresolved for the
    generation gate to block. Measured on a real contract: slots recorded at
    paragraph 65 for merge fields living at 66.

    Matched loosely on letters and digits, because a model asked to copy
    `«LAB__FT_SALARY__38_HR_»` returns it with the guillemets, without them, with
    the quotes Word stores, or re-cased.
    """
    needle = re.sub(r"[\W_]+", "", (match_text or "").lower())
    if not needle:
        return None
    for candidate_index in (paragraph_index, *range(max(0, paragraph_index - 2), paragraph_index + 3)):
        for code in by_paragraph.get(candidate_index, []):
            if re.sub(r"[\W_]+", "", code.lower()) == needle:
                return candidate_index, code
    return None


def _locate(match_text: str, paragraph_index: int, spans_by_paragraph: dict) -> tuple[int, int, str] | None:
    """Find the model's literal inside the pre-scan's real spans.

    Returns (paragraph_index, span_index, exact_text) or None when the text
    cannot be found -- a hallucinated substring is dropped rather than guessed at.
    """
    needle = match_text.strip()
    if not needle:
        return None

    for candidate_index in (paragraph_index, *range(max(0, paragraph_index - 2), paragraph_index + 3)):
        for span in spans_by_paragraph.get(candidate_index, []):
            if needle in span.text:
                return candidate_index, span.span_index, needle
        # Fall back to a case-insensitive hit, taking the span's own casing.
        for span in spans_by_paragraph.get(candidate_index, []):
            lowered = span.text.lower()
            position = lowered.find(needle.lower())
            if position >= 0:
                return candidate_index, span.span_index, span.text[position:position + len(needle)]
    return None


def _evidence_section(evidence) -> str:
    """§10 step 5: the retrieved evidence, or nothing at all.

    Without this the model names fields from the template's own wording and
    nothing else, so a template reading "Date:" becomes `letter_date` while the
    organisation's spreadsheets have always called that column `Date`. The
    binder then has to rediscover the pairing from a name the compiler invented,
    and on a near-miss it does not. Showing the model the column names this
    tenant actually uses moves that agreement upstream, where it is free.

    Advisory, not binding. A template can legitimately need a field no source
    file has yet, and a model told these are the only permitted names would
    either drop that field or force it onto the closest wrong column. So the
    instruction is to prefer an existing name when it means the same thing --
    which is the case this fixes -- and to invent one otherwise.

    Empty evidence renders to an empty string rather than an empty heading: a
    prompt saying "known columns:" with nothing under it reads as "this
    organisation has no source data", which is a claim retrieval never made.
    """
    items = list(evidence or [])
    if not items:
        return ""
    lines = []
    for item in items:
        # `text` is `describe_column`'s output -- the column name plus a type
        # and a sample. That is the whole point of passing it: the model can
        # tell a date column from a free-text one before it declares a type.
        lines.append(f"- {item.text}")
    return (
        "\n\nColumns that already exist in this organisation's source data, "
        "most relevant first:\n" + "\n".join(lines) +
        "\n\nWhen a placeholder means the same thing as one of these columns, derive its field "
        "id from that column's name rather than from the template's wording -- the snake_case "
        "form of it, so the column \"Transfer Type\" gives the id transfer_type. Field ids and "
        "every name inside a condition expression must be snake_case identifiers; a column name "
        "with spaces in it is not a valid identifier and an expression containing one cannot be "
        "executed. When a placeholder matches no column, name it from the template as usual -- "
        "do not force it onto a column that does not mean the same thing."
    )


def write_chunk(prompt_body: str, *, evidence=None, llm_policy=None):
    """One writer call: a rendered chunk in, the model's raw reading out.

    Returns the provider's `StructuredResult` untouched -- `data` is None when
    the call failed or the model refused, and the caller has to deal with that
    rather than receive a plausible-looking empty reading. That distinction is
    the whole point of removing the old fallback: "the model found nothing" and
    "the model was never asked" used to produce the same manifest.

    `llm_policy` is threaded from the request rather than resolved here: this
    module has no session and no tenant. Passing it to `get_llm_provider` is what
    makes the §16 residency check reach the compile paths, which it did not when
    the only enforcement lived in `llm.boundary`. Leaving it None raises rather
    than silently sending in-region data elsewhere.
    """
    provider = get_llm_provider("Compiling a template", policy=llm_policy)
    return provider.structured(
        system=COMPILE_SYSTEM,
        prompt=f"Compile this template.\n\n{prompt_body}{_evidence_section(evidence)}",
        schema=COMPILE_SCHEMA,
        purpose="compile",
    )


def prescan_summary(prescan: PreScanResult) -> dict:
    """What the deterministic scan saw, recorded alongside what the model read.

    Kept even though the rules no longer contribute fields: it is how a reviewer
    tells "the model found two placeholders in a document with sixty blue runs"
    apart from "the model found two placeholders in a document that has two".
    """
    return {
        "paragraph_count": len(prescan.paragraphs),
        "blue_spans": sum(1 for s in prescan.spans if s.color == "blue" and s.text.strip()),
        "red_spans": sum(1 for s in prescan.spans if s.color == "red" and s.text.strip()),
        "mergefields": len(prescan.mergefields),
        "hyperlinks": len(prescan.hyperlinks),
        "conditions_found": 0,
    }


def empty_manifest(prescan: PreScanResult, *, compiled_by: str = "llm_unavailable", notes=None) -> CompiledManifest:
    """A manifest that reports having read nothing, and says so in `compiled_by`.

    Never a silent stand-in for a real compile. The caller persists this with
    `status="failed"`, so it cannot be approved and nothing can generate from it.
    """
    return CompiledManifest(
        fields=[], conditions=[], blocks=[], delete_always=[],
        mergefield_paragraphs=sorted({mf.paragraph_index for mf in prescan.mergefields}),
        hyperlink_paragraphs=sorted({h.paragraph_index for h in prescan.hyperlinks}),
        confidence=0.0, compiled_by=compiled_by, prescan_summary=prescan_summary(prescan),
        notes=list(notes or []),
    )


def assemble(prescan: PreScanResult, data: dict, *, model: str = "llm") -> CompiledManifest:
    """Ground one merged model reading onto the document, deterministically.

    Everything from here down is mechanical: locate each `match_text` in a real
    span, reject expressions that are not executable, negate delete-when-true
    conditions, and resolve scaffolding against the blocks that govern it. The
    model decides what things mean; this decides where they are.
    """
    summary = prescan_summary(prescan)
    result_data = data

    spans_by_paragraph: dict[int, list] = {}
    for span in prescan.spans:
        spans_by_paragraph.setdefault(span.paragraph_index, []).append(span)
    # The RAW code, quotes and all. `docx_renderer` looks a slot up by
    # `(paragraph_index, code)` against the live pre-scan, where Word stores the
    # instruction as `"LAB__FT_SALARY__38_HR_"` -- with the quotes. Storing the
    # tidied form produced slots that looked perfect, matched nothing, and left
    # every merge field unresolved for the generation gate to block.
    mergefields_by_paragraph: dict[int, list[str]] = {}
    for mf in prescan.mergefields:
        if (mf.code or "").strip():
            mergefields_by_paragraph.setdefault(mf.paragraph_index, []).append(mf.code)

    fields: dict[str, dict] = {}
    #: normalised placeholder text -> the field id that owns it.
    placeholder_by_text: dict[str, str] = {}
    dropped = 0
    for item in result_data.get("fields", []):
        # Every occurrence, located independently. The rule compiler used to be
        # merged in underneath this precisely because a single located slot left
        # the token's other appearances unfilled; the writer reports them all now,
        # and each one still has to be found in a real span before it is trusted.
        located_all = []
        mergefield_slots: list[dict] = []
        for occurrence in item.get("occurrences") or []:
            match_text = occurrence.get("match_text", "")
            paragraph_index = occurrence.get("paragraph_index", 0)

            # A merge field is written into the model's view as `«CODE»`, because
            # its runs are not spans and it is otherwise invisible. A claim on one
            # becomes a `mergefield` slot, which the fill engine already knows how
            # to resolve -- it replaces the whole complex-field run sequence with
            # a single literal run.
            found_mf = _mergefield_code(match_text, paragraph_index, mergefields_by_paragraph)
            if found_mf is not None:
                mf_paragraph, code = found_mf
                slot = {"kind": "mergefield", "code": code, "paragraph_index": mf_paragraph}
                if slot not in mergefield_slots:
                    mergefield_slots.append(slot)
                continue

            found = _locate(match_text, paragraph_index, spans_by_paragraph)
            if found is None:
                dropped += 1
                continue
            if found not in located_all:
                located_all.append(found)
        if not located_all and not mergefield_slots:
            continue
        # The field is keyed on its first located occurrence, so two spellings of
        # one placeholder still collapse onto one field. A field whose only
        # occurrences are merge fields has no bracket text to key on, so it keys
        # on its code instead.
        if located_all:
            p_index, span_index, exact = located_all[0]
        else:
            p_index, span_index, exact = mergefield_slots[0]["paragraph_index"], 0, mergefield_slots[0]["code"]

        # Keyed on the placeholder, not on the id the model invented for it.
        #
        # `<Current Position Title>` appears twice in a transfer letter -- once
        # in the opening sentence and once in the revert clause -- and asked to
        # name each occurrence, a model obliges: `current_position_title` and
        # `current_position_title_temp_revert`. They squash differently, so the
        # merge below keeps both, and one placeholder becomes two fields that a
        # reviewer has to bind separately to the same column.
        #
        # It is not merely untidy. Targets then outnumber columns, and column
        # assignment is exclusive -- so a real pairing gets displaced by a
        # duplicate and the leftover field takes whatever column is still free.
        # Measured on a real template: `transfer_type` bound to `Transaction
        # Type` while `Transfer Type` went unbound, every branch evaluated
        # against "With Recruitment", and no branch survived.
        #
        # The rule compiler never has this problem because it derives the id
        # from the bracket text, so every occurrence lands on one field with
        # many slots. This does the same for the model's output.
        placeholder_key = _squash(exact)
        field_id = placeholder_by_text.get(placeholder_key) or _slug(item["id"])
        placeholder_by_text[placeholder_key] = field_id

        field = fields.setdefault(field_id, {
            "id": field_id, "type": item.get("type", "string"), "slots": [],
            "required": bool(item.get("required")), "source_hint": item.get("reason"),
            "value_rule": None, "compiled_from": item.get("reason"),
        })
        # `text_match` reuses the existing fill path: the engine already does
        # `new_text.replace(slot["text"], value, 1)`, so no new fill logic is
        # needed for uncoloured templates.
        # How many slots a (span, literal) pair gets is decided by the document,
        # not by how many times the model happened to mention it.
        #
        # The fill engine replaces ONE occurrence per slot --
        # `new_text.replace(slot["text"], value, 1)` -- and iterates the slot
        # list. So a span reading "the bonus is <currency> and the allowance is
        # <currency>" needs two slots or the second token is printed to the
        # reader verbatim. Deduplicating the model's occurrences would produce
        # exactly one; trusting its count would produce however many it listed.
        # Counting the literal in the real span text is the only answer that is
        # right in both directions, and it is mechanical.
        for occ_p, occ_span, occ_text in located_all:
            span_text = next(
                (sp.text for sp in spans_by_paragraph.get(occ_p, []) if sp.span_index == occ_span),
                "",
            )
            wanted = span_text.count(occ_text) or 1
            already = sum(
                1 for sl in field["slots"]
                if sl["paragraph_index"] == occ_p
                and sl["span_index"] == occ_span
                and sl["text"] == occ_text
            )
            for _ in range(max(0, wanted - already)):
                field["slots"].append({
                    "kind": "text_match", "text": occ_text,
                    "paragraph_index": occ_p, "span_index": occ_span,
                })
        for slot in mergefield_slots:
            if slot not in field["slots"]:
                field["slots"].append(slot)

    conditions, blocks = [], []
    unparseable: list[tuple] = []
    for item in result_data.get("conditions", []):
        start, end = item["start_paragraph"], item["end_paragraph"]
        if end < start or start >= len(prescan.paragraphs):
            dropped += 1
            continue
        block_id = f"blk_{len(blocks)}_{_slug(item['id'])}"
        blocks.append({
            "id": block_id, "start_paragraph": start,
            "end_paragraph": min(end, len(prescan.paragraphs) - 1),
            "boundary_method": "llm", "contains_fields": [],
        })
        # The manifest is keep-when-true throughout, so a delete-when-true
        # instruction is negated here rather than relying on the model to invert
        # it. Getting this backwards is silent and severe: a letter simply loses
        # a section its data called for, with nothing left behind to notice.
        expression = item["expression"]
        if item.get("effect") == "delete":
            expression = f"not ({expression})"

        # An expression that cannot be parsed is not a rule, and storing one
        # produces a manifest that can never be approved -- the reviewer finds
        # out by pressing Approve and reading a validation failure for every
        # condition in the template.
        #
        # Asked for an expression, a model sometimes returns the instruction it
        # was reading instead: `"For Permanent transfers:"` rather than
        # `transfer_type == 'Permanent'`. Same model, same template, different
        # run. Dropping it here loses the branch -- but keeping it loses the
        # branch too AND blocks the lock, so the honest move is to drop it and
        # name it in the notes, where a re-compile is the obvious next step.
        if not _expression_is_executable(expression):
            unparseable.append((item.get("id", "?"), item["expression"]))
            dropped += 1
            continue
        conditions.append({
            "id": _slug(item["id"]), "expression": expression,
            "keeps_blocks": [block_id], "compiled_from": item.get("compiled_from", ""),
            "confidence": 0.6,
        })

    # The model authors the field list. The rule compiler's bracket inventory
    # used to be merged in here and win wherever it had an answer; that merge is
    # deliberately gone, so nothing but the model decides what a field is.
    #
    # What the merge was covering for is now covered at the source: the writer
    # reports every occurrence of a placeholder rather than one, and
    # `assertions.uncovered_placeholder` fails the round when a bracket or
    # MERGEFIELD in the document is left unclaimed. The inventory is still
    # consulted -- as a test of the model's reading, never as a contributor to it.
    merged: dict[str, dict] = dict(fields)

    summary["conditions_found"] = len(conditions)
    notes = list(result_data.get("notes", []))
    if unparseable:
        notes.append(
            f"{len(unparseable)} conditional instruction(s) came back as prose rather than an "
            f"executable rule and were dropped (first: {unparseable[0][1]!r}). Their sections will "
            "appear in every document until the template is re-compiled or the instruction is "
            "rewritten as a rule."
        )
    if dropped:
        notes.append(f"{dropped} suggestion(s) discarded: the text could not be located in the document.")
    notes.append(
        f"{len(merged)} placeholder(s) and {len(conditions)} condition(s) read by the model and "
        f"located in the document."
    )
    language = result_data.get("language")
    if language:
        notes.append(f"Detected template language: {language}")

    # Control tokens are scaffolding in any dialect, so they are stripped whether
    # the rules or the model read the logic.
    delete_always = list(find_control_markers(prescan))

    # ---- instruction runs that share a paragraph with content ----
    #
    # Removed span by span, never by deleting the paragraph. `scope: "span"` is
    # what the fill engine reads to drop one run and keep its neighbours, and it
    # is deliberately kept out of the `doomed` set below so a block starting on
    # this paragraph is not advanced past content that is still there.
    for item in result_data.get("instruction_spans") or []:
        located = _locate(item.get("match_text", ""), item.get("paragraph_index", 0), spans_by_paragraph)
        if located is None:
            dropped += 1
            continue
        p_index, span_index, exact = located
        span = next(
            (sp for sp in spans_by_paragraph.get(p_index, []) if sp.span_index == span_index),
            None,
        )
        # Remove the instruction, not the run it happens to share.
        #
        # A run is only entirely an instruction when the template's markup said
        # so, and markup is exactly what a lossy conversion destroys: a legacy
        # .doc converted to .docx arrives with every run black, so "To signify
        # your acceptance of this offer ... (e.g. Change Job with Recruitment)"
        # is ONE run holding a real sentence and an instruction. Deleting the
        # span takes the sentence; leaving it prints the instruction.
        #
        # `remove` is the existing answer -- `find_control_markers` has always
        # used it to strip `[[ENDIF]]` from a line that keeps its content, and
        # the fill engine does `text.replace(literal, "")`. So a substring gets a
        # substring removal, and only a run that IS the instruction is dropped
        # whole.
        if span is not None and exact.strip() and exact.strip() != span.text.strip():
            entry = {"paragraph_index": p_index, "span_index": span_index, "remove": [exact]}
        else:
            entry = {"paragraph_index": p_index, "span_index": span_index, "scope": "span"}
        if entry not in delete_always:
            delete_always.append(entry)

    # ---- inline branches: a switch whose arms are runs in one paragraph ----
    kept_branch_spans: dict[int, set[int]] = {}
    for item in result_data.get("inline_branches") or []:
        located = _locate(item.get("match_text", ""), item.get("paragraph_index", 0), spans_by_paragraph)
        if located is None:
            dropped += 1
            continue
        expression = (item.get("expression") or "").strip()
        if not _expression_is_executable(expression):
            unparseable.append((item.get("id", "?"), expression))
            dropped += 1
            continue
        p_index, span_index, _exact = located
        block_id = f"blk_{len(blocks)}_{_slug(item['id'])}"
        blocks.append({
            "id": block_id, "start_paragraph": p_index, "end_paragraph": p_index,
            "boundary_method": "llm_inline", "contains_fields": [],
            "start_span": span_index, "end_span": span_index,
        })
        conditions.append({
            "id": _slug(item["id"]), "expression": expression,
            "keeps_blocks": [block_id], "compiled_from": item.get("compiled_from", ""),
            "confidence": 0.6,
        })
        kept_branch_spans.setdefault(p_index, set()).add(span_index)

    # Everything else inside the instruction ZONE goes, computed from the run
    # colours rather than asked for.
    #
    # The zone runs from the first instruction run to the last, and the model is
    # not a reliable enumerator of what is inside it: a real client master leaves
    # the word "initiated" unmarked between two instruction phrases, so asking
    # for the instruction text yields three fragments and the letter ships
    # "...return a copy of this letter to initiated GBS Dalian Team For
    # transactions". Reproduced exactly that way here before this existed.
    #
    # Boundaries are a mechanical question about run colour, which is what this
    # layer is for. The model still decides which branch is which and what keeps
    # it; this only decides where the scaffolding around them ends.
    for p_index, keep in kept_branch_spans.items():
        ordered = [sp for sp in spans_by_paragraph.get(p_index, []) if not sp.in_hyperlink]
        instruction_positions = [k for k, sp in enumerate(ordered) if sp.color == "red" and sp.text.strip()]
        if not instruction_positions:
            continue
        zone = range(instruction_positions[0], instruction_positions[-1] + 1)
        for k in zone:
            span = ordered[k]
            if span.span_index in keep:
                continue
            entry = {"paragraph_index": p_index, "span_index": span.span_index, "scope": "span"}
            if entry not in delete_always:
                delete_always.append(entry)

    # Scaffolding: template documentation that must not reach the reader. Bounds
    # are checked because an out-of-range index would silently delete nothing.
    scaffolding = sorted({
        i for i in result_data.get("scaffolding_paragraphs", [])
        if isinstance(i, int) and 0 <= i < len(prescan.paragraphs)
    })

    # A paragraph a condition governs is content, not documentation, whatever the
    # model also called it. The two lists are answers to different questions and
    # nothing stops a model putting a paragraph in both -- asked to find the
    # scaffolding around an instruction, it readily marks the clause underneath
    # as well. Measured on a real client master: 16 sound conditions over 16
    # blocks, and 88 paragraphs marked scaffolding including every paragraph
    # those blocks covered. `delete_always` wins over a block, so the conditions
    # were correct and the letter came out empty anyway.
    #
    # Resolved towards keeping the text. If a block genuinely does contain
    # documentation, it survives into the output where the instruction-residue
    # gate names it and blocks the document -- a loud, fixable failure. The other
    # way round deletes a clause silently, which is the failure this whole line
    # of work exists to stop.
    governed = {i for b in blocks for i in range(b["start_paragraph"], b["end_paragraph"] + 1)}
    reclaimed = [i for i in scaffolding if i in governed]
    scaffolding = [i for i in scaffolding if i not in governed]
    delete_always.extend({"paragraph_index": i, "span_index": None} for i in scaffolding)
    # A legend that documents the syntax (`<column_name>`, `[[IF <expr>]]`) yields
    # real-looking placeholders that exist nowhere in the letter. Once the page
    # they sit on is deleted they can never be filled, so keeping them only
    # leaves permanently-unbound rows in the mapping screen.
    if scaffolding:
        scaffold_set = set(scaffolding)
        for field_id in [k for k, f in merged.items() if f.get("slots") and all(
            slot.get("paragraph_index") in scaffold_set for slot in f["slots"]
        )]:
            del merged[field_id]

    if scaffolding:
        # Listed, not rendered as first-last. Seven scattered instruction lines
        # between paragraphs 13 and 74 printed as "13-74", which reads as a
        # sixty-two paragraph range and makes a correct compile look like it has
        # gutted the letter. A reviewer who has been alarmed by a correct note
        # once stops reading notes.
        shown = ", ".join(str(i) for i in scaffolding[:12])
        if len(scaffolding) > 12:
            shown += f", and {len(scaffolding) - 12} more"
        notes.append(
            f"{len(scaffolding)} paragraph(s) identified as template scaffolding (control page, "
            f"legend or syntax guide) and excluded from generated documents: {shown}."
        )
    if reclaimed:
        notes.append(
            f"{len(reclaimed)} paragraph(s) were marked as scaffolding while also sitting inside a "
            f"conditional block; they were kept as content, because a block says the letter needs "
            f"them under some condition (first at paragraph {reclaimed[0]})."
        )

    # A block must not begin on a paragraph that is itself being deleted.
    #
    # The model reports a conditional region starting at the line that
    # introduces it -- "For Permanent transfers:" -- because that is where a
    # reader would say the section starts. The rule compiler puts the marker
    # *before* the block instead, and the fill engine relies on that: a
    # `delete_always` entry inside a *kept* block's range is left alone, because
    # a red run there can be genuine content (a sub-heading, not an
    # instruction). With the marker inside its own block, that protection fires
    # on the marker itself and the finished letter carries "For Permanent
    # transfers:" above the paragraph it was describing.
    #
    # Advancing the start past the deleted lines makes the model's output obey
    # the same convention as the rules', so one fill engine can serve both.
    doomed = {
        e["paragraph_index"] for e in delete_always
        if e.get("paragraph_index") is not None and not e.get("partial")
        and not e.get("remove") and e.get("scope") != "span"
    }
    for block in blocks:
        start, end = block["start_paragraph"], block["end_paragraph"]
        while start <= end and start in doomed:
            start += 1
        if start > end:
            # The whole region was scaffolding; leave it as the model gave it
            # rather than inverting the range into something the engine would
            # read as an empty keep.
            continue
        block["start_paragraph"] = start

    return CompiledManifest(
        fields=list(merged.values()), conditions=conditions, blocks=blocks, delete_always=delete_always,
        mergefield_paragraphs=sorted({mf.paragraph_index for mf in prescan.mergefields}),
        hyperlink_paragraphs=sorted({h.paragraph_index for h in prescan.hyperlinks}),
        # Capped deliberately: inferred structure is a proposal, and the review
        # UI sorts by confidence.
        confidence=min(MAX_CONFIDENCE, 0.4 + 0.05 * len(merged)),
        compiled_by=f"llm:{model}", prescan_summary=summary, notes=notes,
    )



def compile_manifest_llm(prescan: PreScanResult, paragraph_texts: list[str], *, llm_policy=None, evidence=None) -> CompiledManifest:
    """Read a whole template in one call, then ground it.

    The single-chunk case, kept as a named entry point because it is the shape
    most templates take and the shape the tests exercise. `agentic_compiler`
    drives the chunked, reviewed loop and calls `write_chunk` / `assemble`
    directly.

    Raises LLMNotConfiguredError without a key: this path exists to read the
    template, so there is no honest result to return without a model. A call that
    fails for any other reason returns an empty manifest whose `compiled_by` says
    so, rather than a wrong one.
    """
    numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(paragraph_texts) if t.strip())
    result = write_chunk(numbered, evidence=evidence, llm_policy=llm_policy)
    if result.data is None:
        return empty_manifest(prescan, notes=[f"LLM compile failed: {result.error}"])
    return assemble(prescan, result.data, model=result.model)

# Control syntaxes that mean "this template has conditional logic", across the
# dialects real templates use. The rule compiler understands exactly one of them
# -- the English prose form on the last line -- so anything else here is a
# template whose logic the rules cannot see.
CONDITIONAL_MARKER_RE = re.compile(
    r"\[\[\s*(?:ELSE\s+)?(?:IF|ELSE|ENDIF)\b"      # [[IF x]] [[ELSE IF x]] [[ELSE]] [[ENDIF]]
    r"|\{%-?\s*(?:if|elif|else|endif)\b"            # Jinja / Django
    r"|\{\{\s*[#/](?:if|unless)\b"                  # Handlebars / Mustache
    r"|<!--\s*(?:IF|ENDIF)\b"                       # HTML-comment dialect
    r"|\bonly if the\b",                            # the English prose form
    re.IGNORECASE,
)


def choose_compiler(prescan: PreScanResult) -> str:
    """First-pass dispatch: can the rule layer see anything at all?

    Colour and brackets are language-independent, so a coloured German template
    still takes the free fast path; an uncoloured one has nothing for the rules
    to read. This only decides whether the rules are worth *trying* -- whether
    they actually succeeded is `rules_fell_short`, below.
    """
    coloured = sum(1 for s in prescan.spans if s.color in ("blue", "red") and s.text.strip())
    return "rules" if coloured or prescan.mergefields else "llm"


def rules_fell_short(prescan: PreScanResult, compiled) -> str | None:
    """Why the rule-compiled manifest should be escalated to a model, or None.

    Dispatching on colour alone is not enough, and getting this wrong is silent.
    A compensation-letter template can be fully colour-coded -- 72 blue spans, 35
    red -- and still express every one of its conditions as `[[IF x > 0]] ...
    [[ENDIF]]` rather than the English prose `INCLUDE_RE` matches. The rules then
    return a confident-looking manifest with all the fields and *zero* of the
    fifteen conditional blocks, so every letter ships with every optional clause
    in it: joining bonus paragraphs for people who got none, ESOP tables for
    people with no units.

    Fields alone are not evidence the template was understood. Conditions are.

    And *some* conditions are not evidence either, which is what this originally
    got wrong: the guard was `if compiled.conditions: return None`, so a template
    that compiled two of its five branches was treated as fully understood. That
    is the worse half of the same failure -- zero conditions at least looks
    suspicious, while a manifest with most of them looks finished. A real
    transfer letter carrying "For Permanent transfers:" and "For Temporary
    transfers:" as prose shipped every employee both paragraphs, and the count of
    conditions on the manifest gave nobody a reason to check.

    So the question is not "did the rules find any conditions" but "did they find
    everything that looks conditional". `W-MARKER-PARSE` answers it directly: the
    profiler raises one for every run that reads as a branch marker and does not
    compile as one, which is precisely a condition the rules saw and dropped.
    """
    unparsed = [
        w for w in (getattr(compiled, "warnings", None) or [])
        if (w.get("code") if isinstance(w, dict) else getattr(w, "code", None)) == "W-MARKER-PARSE"
    ]
    if unparsed:
        first = unparsed[0]
        detail = first.get("evidence") or first.get("detail") if isinstance(first, dict) else ""
        return (
            f"The rule compiler read {len(compiled.conditions)} condition(s) but left "
            f"{len(unparsed)} instruction(s) it could not parse as branches "
            f"(first at paragraph {first.get('paragraph_index') if isinstance(first, dict) else '?'}"
            f"{': ' + repr(str(detail)[:60]) if detail else ''}). A partly-understood template "
            "ships every optional clause in every letter, so its rules were read by a model instead."
        )

    if compiled.conditions:
        return None

    text = "\n".join(s.text for s in prescan.spans if s.text.strip())
    match = CONDITIONAL_MARKER_RE.search(text)
    if not match:
        return None  # genuinely has no conditional logic -- nothing was missed

    return (
        f"The rule compiler found no conditions, but this template contains conditional "
        f"markers it does not parse (first seen: {match.group(0).strip()!r}). Its rules were "
        f"read by a model instead."
    )
