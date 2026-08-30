"""Mechanical checks on what the model read, and the loop's only objective signal.

The writer decides what a template means. Nothing here second-guesses that. What
these do is ask the questions the document itself can answer -- is this
placeholder claimed by some field, does this expression parse, could this field
ever be printed -- and turn every "no" into a correction the reviewer is handed
on the next round.

That distinction matters more than it sounds. The rule compiler's bracket and
MERGEFIELD inventory used to be *merged into* the model's field list, so the
rules quietly supplied whatever the model missed and no one could tell which
layer had produced a given field. The inventory is still read here, but only as
an oracle: it can fail a round and force another pass, and it can never put a
field in the manifest. If the writer cannot see a placeholder after being told
twice that it is there, that is a template a person needs to look at -- not a
gap for the rules to paper over.

Every check is deterministic. A loop that converged against a model's opinion of
its own work would have no fixed point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.expressions.token_parser import condition_inputs, evaluate_condition
from app.qa.orphaned_fields import orphans
from app.qa.placeholder_check import INSTRUCTION_PAREN_RE, INSTRUCTION_SHOUT_RE
from app.templates.parsers.docx_prescan import extract_bracket_tokens

# Bracket tokens that are not placeholders at all. A template writes
# `<__________________>` as a ruled line for someone to hand-write on, and
# `<>` as nothing. Demanding the writer claim these would make the loop
# unsatisfiable: it would correctly decline to invent a field, the coverage
# check would fire again, and the compile would run to its ceiling and park as
# failed on a template that is otherwise perfectly read.
_NOT_A_PLACEHOLDER_RE = re.compile(r"^[\W_]*$", re.UNICODE)

UNCOVERED_PLACEHOLDER = "uncovered_placeholder"
UNCOVERED_MERGEFIELD = "uncovered_mergefield"
SURVIVING_INSTRUCTION = "surviving_instruction"
PARAGRAPH_SCOPED_SWITCH = "paragraph_scoped_switch"
UNEXECUTABLE_CONDITION = "unexecutable_condition"
ORPHANED_FIELD = "orphaned_field"
FIELD_WITHOUT_SLOT = "field_without_slot"
SCAFFOLDING_CONFLICT = "scaffolding_conflict"
TEST_FILL_FAILURE = "test_fill_failure"


@dataclass
class Assertion:
    """One thing the document says is wrong with the reading.

    `detail` is written to be pasted into the reviewer prompt unchanged, so it
    names the paragraph and quotes the text rather than referring to an id the
    model has no way to resolve.
    """

    check: str
    detail: str
    paragraph_index: int | None = None
    object_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "detail": self.detail,
            "paragraph_index": self.paragraph_index,
            "object_id": self.object_id,
        }


def _claimed_literals(manifest: dict) -> set[tuple[int, str]]:
    """(paragraph_index, literal) for every slot the manifest declares."""
    out: set[tuple[int, str]] = set()
    for f in manifest.get("fields") or []:
        for slot in f.get("slots") or []:
            text = (slot.get("text") or "").strip()
            index = slot.get("paragraph_index")
            if text and isinstance(index, int):
                out.add((index, text))
    return out


def uncovered_placeholders(prescan, manifest: dict) -> list[Assertion]:
    """Bracket tokens in the document that no field claims.

    Read from the pre-scan's spans rather than the paragraph text, because a
    token split across runs is one token to a reader and two to a naive scan.
    """
    claimed = _claimed_literals(manifest)
    claimed_texts = {text for _i, text in claimed}
    seen: set[tuple[int, str]] = set()
    out: list[Assertion] = []
    for span in prescan.spans:
        for token, inner in extract_bracket_tokens(span.text or ""):
            if _NOT_A_PLACEHOLDER_RE.match(inner or ""):
                continue
            key = (span.paragraph_index, token)
            if key in seen:
                continue
            seen.add(key)
            # A literal claimed anywhere counts: the writer may legitimately
            # record one occurrence in a neighbouring paragraph index, and
            # `_locate` already searched a window around it.
            if key in claimed or token in claimed_texts:
                continue
            out.append(Assertion(
                UNCOVERED_PLACEHOLDER,
                f"Paragraph {span.paragraph_index} contains the placeholder {token!r}, "
                f"which no field claims. Either add a field with an occurrence for it, or say "
                f"why it is not a placeholder.",
                paragraph_index=span.paragraph_index,
            ))
    return out


def uncovered_mergefields(prescan, manifest: dict) -> tuple[list[Assertion], list[dict]]:
    """Word MERGEFIELD codes with no field behind them.

    Returns `(faults, warnings)`.

    Unclaimed is a fault. It renders as its cached result -- usually blank -- so
    it is invisible in the paragraph text, and nothing but this can see that a
    slot exists at all. The `unresolved_mergefield` gate blocks every document
    generated from a manifest that leaves one, so a compile that ignores them
    produces a template nothing can ever be generated from.

    This was briefly a warning, on the theory that a merge field beside a bracket
    is always a duplicate the reviewer cannot resolve. That theory was wrong, and
    the counter-example is the ordinary case: a remuneration table where the cell
    reads `$«LAB__FT_SALARY__38_HR_»` has no bracket at all, and the sentence
    above it carries `<Grade>` next to two merge fields that are a salary and a
    total package -- three different slots on one paragraph, not one slot three
    times. Structure cannot tell those apart from a true duplicate; only meaning
    can, which is why the model is now shown the codes and asked.

    The warning is kept for what the model itself reports as one slot: a field
    claiming both a bracket and a merge field on the same paragraph. Filling both
    prints two values on top of each other, so the template needs one encoding
    removed and a human has to choose.
    """
    claimed_codes = {
        re.sub(r"[\W_]+", "", str(slot.get("code") or "").lower())
        for f in manifest.get("fields") or []
        for slot in f.get("slots") or []
        if slot.get("kind") == "mergefield"
    } - {""}

    faults: list[Assertion] = []
    for mf in prescan.mergefields:
        code = (mf.code or "").strip().strip('"')
        if not code or re.sub(r"[\W_]+", "", code.lower()) in claimed_codes:
            continue
        faults.append(Assertion(
            UNCOVERED_MERGEFIELD,
            f"Paragraph {mf.paragraph_index} carries the Word merge field {code!r}, which no "
            f"field claims. It is written in the text you were given as \u00ab{code}\u00bb. Claim it "
            f"as a field occurrence -- until you do, nothing writes a value into it and every "
            f"document from this template is blocked.",
            paragraph_index=mf.paragraph_index,
        ))

    warnings: list[dict] = []
    for f in manifest.get("fields") or []:
        slots = f.get("slots") or []
        by_paragraph: dict[int, set] = {}
        for slot in slots:
            by_paragraph.setdefault(slot.get("paragraph_index"), set()).add(slot.get("kind"))
        for paragraph_index, kinds in by_paragraph.items():
            if {"mergefield", "text_match"} <= kinds:
                warnings.append({
                    "code": "W-FIELD-CODE",
                    "paragraph_index": paragraph_index,
                    "evidence": f.get("id"),
                    "detail": "one field is written twice on one paragraph, as a bracket and as a merge field",
                    "message": (
                        f"On paragraph {paragraph_index}, field {f.get('id')!r} is encoded twice -- "
                        f"once as a bracket placeholder and once as a legacy Word merge field. Both "
                        f"will be filled, so the value prints twice in a row. Remove one encoding "
                        f"from the template and re-compile."
                    ),
                })
    return faults, warnings


def surviving_instructions(prescan, manifest: dict) -> list[Assertion]:
    """Instruction runs the manifest does nothing about, so the reader sees them.

    The pre-scanner already knows which runs are instructions -- that is what the
    `red` role means. A run the manifest neither deletes nor scopes to a branch
    is printed verbatim, and `instruction_text_remains` blocks the document at
    generation. Catching it here is the difference between a compile that tells
    the model to fix it and a compile that reports success while guaranteeing
    every document fails.

    This assertion is the reason the writer uses `inline_branches` at all. The
    capability and the schema existed first, and the model still described the
    recruitment switch in prose and left the array empty -- because nothing ever
    told it the instruction text was still there. A tool with no feedback is a
    tool nobody picks up.

    Runs inside a conditional block are skipped. A red run there is frequently a
    sub-heading rather than an instruction -- `Dates of Effect` above the clause
    it titles -- and the fill engine makes the same allowance for the same
    reason: deleting it silently costs the letter a heading.
    """
    # Mirrors `docx_renderer.fill_template`'s own condition for dropping a
    # paragraph -- "no `partial`, no `remove`, not span-scoped" -- rather than
    # restating it. It used to additionally require `span_index is None`, which
    # the renderer never tests: it reads `span_index` only when the paragraph
    # sits inside a kept block. So an entry naming both a paragraph and a span,
    # which is what `rule_compiler` emits for a whole instruction line, was
    # deleted by the renderer and reported as surviving by this check.
    #
    # Latent rather than harmless. The two producers disagree in shape --
    # `llm_compiler.assemble` always sets `scope` or `remove`, `rule_compiler`
    # sets neither -- and `agentic_compiler` only ever feeds this the former. Had
    # it ever seen the latter, the loop would have been handed a fault no
    # correction can clear, burned all `compile_max_rounds`, and returned
    # `llm_unconverged` for a template that compiles perfectly well.
    whole = {
        d.get("paragraph_index") for d in manifest.get("delete_always") or []
        if not d.get("partial") and not d.get("remove") and d.get("scope") != "span"
    }
    spanned = {
        (d.get("paragraph_index"), d.get("span_index"))
        for d in manifest.get("delete_always") or [] if d.get("scope") == "span"
    }
    # Literals the manifest strips from a run it otherwise keeps.
    #
    # Without this the check reads the run as the document stores it and never
    # sees the removal, so an instruction sharing a sentence stayed a fault
    # however many times the reviewer correctly removed it -- the loop applied
    # four corrections a round and the count never fell. Seven legacy contracts
    # failed to compile on the same two paragraphs for this reason alone.
    removals: dict[tuple, list[str]] = {}
    for d in manifest.get("delete_always") or []:
        if d.get("remove"):
            removals.setdefault((d.get("paragraph_index"), d.get("span_index")), []).extend(d["remove"])
    # Only a block spanning MORE THAN ONE paragraph exempts a red run.
    #
    # The exemption exists for a sub-heading inside a section -- `Dates of
    # Effect` above the clause it titles, in a block covering 25..27. A block
    # scoped to a single paragraph is not that case, and treating it as one is a
    # loophole a model will find: handed "instruction text nothing removes", it
    # wrapped the paragraph in four single-paragraph blocks, silenced this
    # check, and produced a manifest that deletes the whole paragraph -- taking
    # "To signify your acceptance of this offer" with it.
    governed = {
        i for b in manifest.get("blocks") or []
        if b.get("end_paragraph", -1) > b.get("start_paragraph", 0)
        for i in range(b.get("start_paragraph", 0), b.get("end_paragraph", -1) + 1)
    }

    claimed = _claimed_literals(manifest)

    out: list[Assertion] = []
    for span in prescan.spans:
        # A run inside a hyperlink is protected: `docx_renderer` never deletes
        # one, so an instruction reported here is a fault no correction can
        # clear. A real offer letter links the word "FUSE" -- an internal system
        # -- and colours the link red, which is a link the reader is meant to
        # follow rather than a note to whoever assembles the letter.
        if span.in_hyperlink:
            continue
        text = span.text or ""
        # Judge what the reader will see, not what the file stores.
        for literal in removals.get((span.paragraph_index, span.span_index), []):
            text = text.replace(literal, "")
        text = text.strip()
        if len(text) <= 3:
            continue
        # Red is the convention, but not every author follows it. A real
        # contract writes "(remove this section if there is no higher duty
        # allowance)" in ordinary black text beside a blue heading -- an
        # instruction to whoever assembles the letter, marked up as nothing.
        # The compile saw no fault, reported success, and every document was
        # then blocked by `instruction_text_remains` at generation.
        #
        # The shapes come from `placeholder_check`, so the compile-time check
        # and the generation-time gate agree by construction rather than by two
        # people keeping two patterns in step.
        instruction_shaped = bool(
            INSTRUCTION_PAREN_RE.search(text) or INSTRUCTION_SHOUT_RE.search(text)
        )
        if span.color != "red" and not instruction_shaped:
            continue
        # A red run a field fills is not a run that survives. The convention
        # marks instructions red, but it also marks some placeholders red -- the
        # rule compiler emits a `red_placeholder` slot for a bracket in a red
        # span, and a real offer letter writes `<Date DD/MM/YYYY>` exactly that
        # way. The value is written over it at fill time, so it is never printed
        # to the reader, and reporting it here hands the review loop a fault no
        # correction can clear: the model cannot delete a span it correctly
        # claimed, so the round count runs out and a template that was read
        # properly comes back `llm_unconverged`.
        #
        # `_claimed_literals` is this module's own helper and was already being
        # used two checks above for the same purpose.
        if any(literal in text for index, literal in claimed if index == span.paragraph_index):
            continue
        if span.paragraph_index in whole or (span.paragraph_index, span.span_index) in spanned:
            continue
        # The block exemption does not cover text that reads as an instruction.
        #
        # It exists for a sub-heading coloured like a marker -- `Dates of Effect`
        # -- and those do not match the instruction shapes. "(remove this section
        # if there is no higher duty allowance)" does, and it sat on the first
        # paragraph of the very block it introduces, so the exemption hid it: the
        # compile reported success and every document was blocked at generation
        # by the gate reading the same pattern.
        if span.paragraph_index in governed and not instruction_shaped:
            continue
        out.append(Assertion(
            SURVIVING_INSTRUCTION,
            f"Paragraph {span.paragraph_index} carries instruction text that nothing removes: "
            f"{text[:90]!r}. It will be printed to the reader. If it introduces a section, make "
            f"that section conditional; if its alternatives sit inside this same paragraph, use "
            f"`inline_branches`; if it is documentation, list the paragraph as scaffolding.",
            paragraph_index=span.paragraph_index,
        ))
    return out


def paragraph_scoped_switches(prescan, manifest: dict) -> list[Assertion]:
    """A switch modelled as whole-paragraph blocks when its arms share a paragraph.

    Two or more blocks scoped to the same single paragraph are a switch inside
    that paragraph. A paragraph-ranged block cannot say "keep this run, drop that
    one" -- it can only keep or delete the whole line -- so when the record picks
    one arm every other block is dropped and the paragraph goes with them,
    sentence and all.

    That is what happened on a real contract: the acceptance clause was wrapped
    in four single-paragraph blocks and "To signify your acceptance of this offer
    of employment ... please sign, date, and return a copy of this letter to" was
    deleted outright. The branch gate caught it at generation, which is late; the
    manifest was wrong at compile time and could say so.

    Only flagged when the paragraph carries static text outside the branches --
    which is what makes deleting it a loss rather than a clean removal.
    """
    by_paragraph: dict[int, list] = {}
    for b in manifest.get("blocks") or []:
        start, end = b.get("start_paragraph"), b.get("end_paragraph")
        if start is None or start != end or b.get("start_span") is not None:
            continue
        by_paragraph.setdefault(start, []).append(b)

    static_text = {}
    for span in prescan.spans:
        if span.color == "black" and (span.text or "").strip():
            static_text.setdefault(span.paragraph_index, []).append(span.text.strip())

    out: list[Assertion] = []
    for paragraph_index, blocks in sorted(by_paragraph.items()):
        if len(blocks) < 2 or paragraph_index not in static_text:
            continue
        keeps = "; ".join(b.get("id", "?") for b in blocks[:4])
        out.append(Assertion(
            PARAGRAPH_SCOPED_SWITCH,
            f"Paragraph {paragraph_index} carries {len(blocks)} whole-paragraph blocks "
            f"({keeps}), but its alternatives sit inside the one paragraph alongside text the "
            f"letter needs: {static_text[paragraph_index][0][:70]!r}. Whole-paragraph blocks can "
            f"only keep or delete the entire line, so choosing one arm deletes the sentence. Use "
            f"`inline_branches` instead, one per alternative, naming the exact text of each.",
            paragraph_index=paragraph_index,
        ))
    return out


def unexecutable_conditions(manifest: dict) -> list[Assertion]:
    """Conditions the expression engine cannot run.

    Duplicates the parse probe `validate_manifest` uses at approval time, on
    purpose: catching it here costs another round, and catching it there means a
    reviewer discovers it by pressing Approve and reading a failure for every
    condition in the template.
    """
    out: list[Assertion] = []
    for c in manifest.get("conditions") or []:
        cid = c.get("id")
        expression = (c.get("expression") or "").strip()
        if not expression:
            out.append(Assertion(UNEXECUTABLE_CONDITION, f"Condition {cid!r} has no expression.", object_id=cid))
            continue
        names = condition_inputs(expression)
        if not names:
            out.append(Assertion(
                UNEXECUTABLE_CONDITION,
                f"Condition {cid!r} references no field: {expression!r}. An expression must test "
                f"a snake_case field name against a value, e.g. colleague_type == 'Fixed term'.",
                object_id=cid,
            ))
            continue
        if evaluate_condition(expression, {n: "" for n in names}).reason == "unparseable":
            out.append(Assertion(
                UNEXECUTABLE_CONDITION,
                f"Condition {cid!r} cannot be parsed: {expression!r}.",
                object_id=cid,
            ))
    return out


def structural_faults(manifest: dict) -> list[Assertion]:
    """Fields that can never print, and scaffolding that eats its own content."""
    out: list[Assertion] = []
    for f in manifest.get("fields") or []:
        if not f.get("slots"):
            out.append(Assertion(
                FIELD_WITHOUT_SLOT,
                f"Field {f.get('id')!r} has no occurrence in the document, so its value would "
                f"never appear. Give it one, or drop the field.",
                object_id=f.get("id"),
            ))
    for orphan in orphans(manifest):
        out.append(Assertion(
            ORPHANED_FIELD,
            f"Field {orphan.field_id!r} sits only in paragraph(s) "
            f"{sorted(orphan.paragraphs)} which the compile deletes, so no document can ever "
            f"contain it. Either the deletion is wrong or the field is.",
            object_id=orphan.field_id,
        ))
    return out


def test_fill_faults(notes: list[str]) -> list[Assertion]:
    """QA failures from filling the manifest against real sample rows."""
    return [Assertion(TEST_FILL_FAILURE, note) for note in notes]


def collect(prescan, manifest: dict, *, test_fill_notes: list[str] | None = None) -> list[Assertion]:
    """Every assertion, in the order a reviewer should read them.

    Coverage first because an unclaimed placeholder is the failure that reaches
    the reader as visible scaffolding; structure last because it is usually a
    consequence of one of the earlier ones rather than an independent mistake.
    """
    faults, _warnings = collect_with_warnings(prescan, manifest, test_fill_notes=test_fill_notes)
    return faults


def collect_with_warnings(prescan, manifest: dict, *, test_fill_notes=None):
    """`(faults, warnings)`. Faults drive the loop; warnings ride on the manifest.

    A warning is something a person must decide and no reviewer round can fix.
    Keeping the two apart is what stops the loop chasing a goal it cannot reach.
    """
    mergefield_faults, warnings = uncovered_mergefields(prescan, manifest)
    faults = [
        *uncovered_placeholders(prescan, manifest),
        *mergefield_faults,
        *surviving_instructions(prescan, manifest),
        *paragraph_scoped_switches(prescan, manifest),
        *unexecutable_conditions(manifest),
        *structural_faults(manifest),
        *test_fill_faults(test_fill_notes or []),
    ]
    return faults, warnings


def render(assertions: list[Assertion], *, limit: int = 60) -> str:
    """The assertion list as the reviewer prompt carries it."""
    if not assertions:
        return "No mechanical faults were found."
    shown = assertions[:limit]
    lines = [f"- [{a.check}] {a.detail}" for a in shown]
    if len(assertions) > len(shown):
        lines.append(f"- (+{len(assertions) - len(shown)} more of the same kinds)")
    return "\n".join(lines)
