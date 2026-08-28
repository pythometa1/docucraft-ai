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
from app.compiler.rule_compiler import CompiledManifest, _slug, compile_manifest, find_control_markers

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
                    "paragraph_index": {"type": "integer"},
                    "match_text": {"type": "string"},
                    "required": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "type", "paragraph_index", "match_text", "required", "reason"],
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
        "scaffolding_paragraphs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Indices of paragraphs that are instructions ABOUT the template rather than letter content -- control pages, legends, token syntax guides, colour keys. These must never appear in a generated document.",
        },
        "language": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fields", "conditions", "scaffolding_paragraphs", "language", "notes"],
    "additionalProperties": False,
}

COMPILE_SYSTEM = (
    "You compile document templates into machine-executable manifests. You are given a "
    "template's paragraphs, each prefixed with its index.\n\n"
    "Identify two things:\n"
    "1. PLACEHOLDERS -- literal text that changes for each generated document (a person's "
    "name, a date, an amount, a role). Type them: `currency` for money, `date` for dates, "
    "`percent` for a rate the data stores as a fraction (0.05 meaning 5%), `number` for other "
    "quantities. Return the exact substring as `match_text`, copied "
    "character-for-character from the paragraph so it can be located again. Give each a "
    "stable snake_case id.\n"
    "2. CONDITIONAL BLOCKS -- paragraph ranges whose presence depends on the data. Set "
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
    "3. SCAFFOLDING -- paragraphs that document the template rather than form part of the "
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


def compile_manifest_llm(prescan: PreScanResult, paragraph_texts: list[str], *, llm_policy=None, evidence=None) -> CompiledManifest:
    """Compile a template the rule layer cannot see.

    Raises LLMNotConfiguredError without a key: this path is reached precisely
    because the deterministic layer found nothing, so there is no honest result
    to return. Returning an empty manifest here would report "0 fields" for a
    template that was never actually read.

    A call that fails for any other reason still returns an empty manifest
    rather than a wrong one -- the caller reports zero fields and a reviewer
    takes over.
    """
    # `llm_policy` is threaded from the request rather than resolved here: this
    # module has no session and no tenant. Passing it on to `get_llm_provider`
    # is what makes the residency check reach the compile paths, which it did
    # not when the only enforcement lived in `llm.boundary`. Leaving it None
    # raises rather than silently sending in-region data elsewhere.
    provider = get_llm_provider("Compiling a template with no colour coding", policy=llm_policy)
    summary = {
        "paragraph_count": len(prescan.paragraphs),
        "blue_spans": sum(1 for s in prescan.spans if s.color == "blue" and s.text.strip()),
        "red_spans": sum(1 for s in prescan.spans if s.color == "red" and s.text.strip()),
        "mergefields": len(prescan.mergefields),
        "hyperlinks": len(prescan.hyperlinks),
        "conditions_found": 0,
    }
    empty = CompiledManifest(
        fields=[], conditions=[], blocks=[], delete_always=[],
        mergefield_paragraphs=sorted({mf.paragraph_index for mf in prescan.mergefields}),
        hyperlink_paragraphs=sorted({h.paragraph_index for h in prescan.hyperlinks}),
        confidence=0.0, compiled_by="llm_unavailable", prescan_summary=summary,
    )
    numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(paragraph_texts) if t.strip())[:60000]
    result = provider.structured(
        system=COMPILE_SYSTEM,
        prompt=f"Compile this template.\n\n{numbered}{_evidence_section(evidence)}",
        schema=COMPILE_SCHEMA,
        purpose="compile",
    )
    if result.data is None:
        empty.notes = [f"LLM compile failed: {result.error}"]
        return empty

    spans_by_paragraph: dict[int, list] = {}
    for span in prescan.spans:
        spans_by_paragraph.setdefault(span.paragraph_index, []).append(span)

    fields: dict[str, dict] = {}
    #: normalised placeholder text -> the field id that owns it.
    placeholder_by_text: dict[str, str] = {}
    dropped = 0
    for item in result.data.get("fields", []):
        located = _locate(item["match_text"], item["paragraph_index"], spans_by_paragraph)
        if located is None:
            dropped += 1
            continue
        p_index, span_index, exact = located

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
        field["slots"].append({
            "kind": "text_match", "text": exact,
            "paragraph_index": p_index, "span_index": span_index,
        })

    conditions, blocks = [], []
    unparseable: list[tuple] = []
    for item in result.data.get("conditions", []):
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

    # The rule layer already extracted every `<bracket>` and MERGEFIELD in the
    # document, exactly and at *every* occurrence. The model is asked for one
    # `match_text` per field, so relying on its list alone means a token used
    # three times is filled once -- `<currency>` appeared in three paragraphs of
    # the compensation template and only the first was replaced. Rules win where
    # they have an answer; the model's fields fill the gaps.
    rule_based = compile_manifest(prescan)
    merged: dict[str, dict] = {f["id"]: f for f in rule_based.fields}
    # Compared without separators, so the model re-slugging a token the rules
    # already found exactly (`address_line1` -> `address_line_1`) does not
    # create a second, unbindable field sitting next to the real one.
    seen_shapes = {_squash(f["id"]): f["id"] for f in rule_based.fields}
    added_by_model = 0
    for field_id, field in fields.items():
        existing_id = seen_shapes.get(_squash(field_id)) if field_id not in merged else field_id
        if existing_id:
            # Keep the rule layer's slots -- they cover every occurrence -- but
            # take the model's type. The rules classify every `<bracket>` as a
            # string, so without this an amount renders as `1200000` instead of
            # `12,00,000`: structurally correct and obviously wrong to a reader.
            existing = merged[existing_id]
            if existing.get("type") in (None, "string") and field.get("type") not in (None, "string"):
                existing["type"] = field["type"]
            continue
        merged[field_id] = field
        seen_shapes[_squash(field_id)] = field_id
        added_by_model += 1

    summary["conditions_found"] = len(conditions)
    notes = list(result.data.get("notes", []))
    if unparseable:
        notes.append(
            f"{len(unparseable)} conditional instruction(s) came back as prose rather than an "
            f"executable rule and were dropped (first: {unparseable[0][1]!r}). Their sections will "
            "appear in every document until the template is re-compiled or the instruction is "
            "rewritten as a rule."
        )
    if dropped:
        notes.append(f"{dropped} suggestion(s) discarded: the text could not be located in the document.")
    if rule_based.fields:
        notes.append(
            f"{len(rule_based.fields)} placeholder(s) located deterministically at every occurrence; "
            f"the model contributed {added_by_model} more and all {len(conditions)} condition(s)."
        )
    language = result.data.get("language")
    if language:
        notes.append(f"Detected template language: {language}")

    # Control tokens are scaffolding in any dialect, so they are stripped whether
    # the rules or the model read the logic.
    delete_always = [*rule_based.delete_always, *find_control_markers(prescan)]

    # Scaffolding: template documentation that must not reach the reader. Bounds
    # are checked because an out-of-range index would silently delete nothing.
    scaffolding = sorted({
        i for i in result.data.get("scaffolding_paragraphs", [])
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
        notes.append(
            f"{len(scaffolding)} paragraph(s) identified as template scaffolding (control page, "
            f"legend or syntax guide) and excluded from generated documents: "
            f"{scaffolding[0]}-{scaffolding[-1]}."
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
        mergefield_paragraphs=empty.mergefield_paragraphs, hyperlink_paragraphs=empty.hyperlink_paragraphs,
        # Capped deliberately: inferred structure is a proposal, and the review
        # UI sorts by confidence.
        confidence=min(MAX_CONFIDENCE, 0.4 + 0.05 * len(merged)),
        compiled_by=f"llm:{result.model}", prescan_summary=summary, notes=notes,
    )


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
