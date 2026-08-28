"""The Template Compiler (docs/TEMPLATE_COMPILER_RESEARCH.md §2, §4.1) -- turns
a pre-scanned template's blue/red run inventory into a machine-executable
Template Manifest. Rule-based by default -- it works with no LLM key at all, because colours,
brackets and MERGEFIELDs are language-independent structure; when a key is
configured,
`refine_with_llm` asks the model to double-check the two genuinely hard parts
the report calls out: canonical field naming and conditional block boundaries.
"""

import json
import re
from dataclasses import asdict, dataclass, field

from app.templates.conventions import DEFAULT_FAMILY, ConventionFamily, detect_family, load_family, normalise_text
from app.templates.parsers.docx_prescan import PreScanResult, RunSpan, extract_bracket_tokens, extract_brackets

# The grammar this compiler reads now lives in `app/conventions/*.yaml`, because
# a thousand templates from a dozen authoring teams share a handful of grammars
# and none of them should require code. These module names are the default
# family's patterns, kept so that existing importers -- and the QA gate in
# fill_engine, which looks for leftover instruction text -- keep working.
_DEFAULT = load_family(DEFAULT_FAMILY)
INCLUDE_RE = _DEFAULT.include_re

# The fallback for a family that declares no `instruction_shape_pattern`.
#
# The vocabulary belongs to the family, not to this module: one estate shouts
# "USE IF ON TEMPORARY ASSIGNMENT" above the clause, another parenthesises
# "(Include if part time)" inside it, a third writes Japanese. Hardcoding one
# estate's phrasing here would make every new customer a code change, which is
# exactly what the convention files exist to prevent. This remains only so a
# family predating the field still escalates rather than silently deleting.
FALLBACK_INSTRUCTION_SHAPE_RE = re.compile(
    r"only if"
    r"|^\s*(?:insert\s+)?for\b"
    r"|^\s*(?:use|include|always include|remove|delete|insert)\b"
    r"|\((?:include|remove|insert|delete|use)\b"
    r"|适用|경우에만|の場合",
    re.IGNORECASE,
)


def _instruction_shaped(family, text: str) -> bool:
    """Does this read like a branch instruction in this family's language?

    Matching compiles nothing. It raises W-MARKER-PARSE, which blocks approval
    and escalates the template to a model -- so the cost of a false positive is
    one reviewer acknowledgement, and the cost of a false negative is a
    paragraph deleted with no warning that anyone notices only by reading the
    finished letter.
    """
    pattern = getattr(family, "instruction_shape_re", None) or FALLBACK_INSTRUCTION_SHAPE_RE
    return bool(pattern.search(text))
FOR_ENTITY_FIRST_RE = _DEFAULT.for_entity_first_re
FOR_VALUE_FIRST_RE = _DEFAULT.for_value_first_re
_FOR_START_RE = _DEFAULT.for_start_re
GLOSS_RE = _DEFAULT.gloss_re
PUT_RE = _DEFAULT.put_re
HEADING_MAX_LEN = _DEFAULT.heading_max_len

# How much to trust a block's end boundary, by how it was found. This drives the
# manifest's overall confidence, which is what sorts the review queue -- so it is
# a named contract, not an inline expression: a reviewer's attention is a scarce
# resource and these numbers decide where it goes.
#
#   till_hint / next_marker  the template said where the block ends
#   inline_zone              bounded by the next instruction span on the line
#   table_exit               inferred from a contiguous table run ending
#   lookahead_cap            nothing said where it ends; we stopped guessing
BOUNDARY_CONFIDENCE = {
    "till_hint": 1.0,
    "next_marker": 1.0,
    "inline_zone": 1.0,
    "table_exit": 0.7,
    "lookahead_cap": 0.5,
}
UNKNOWN_BOUNDARY_CONFIDENCE = 0.5
UNSLOTTED_FIELD_CONFIDENCE = 0.3


# Control tokens: `[[IF x > 0]]`, `[[ELSE]]`, `[[ENDIF]]`, `[[PROMPT: ...]]`.
# These are scaffolding -- they tell the engine what to do and must never reach
# the reader. Locating them is exact string work, so it is done deterministically
# here rather than asked of a model.
CONTROL_TOKEN_RE = re.compile(r"\[\[[^\[\]]*\]\]")


def find_control_markers(prescan: PreScanResult) -> list[dict]:
    """Every control token in the template, as span-scoped removals.

    Returned as `{paragraph_index, span_index, remove}` rather than whole-
    paragraph deletions because a marker often shares its line with content that
    must survive: `[[IF address_line2 is not blank]]<address_line2>[[ENDIF]]`
    has to keep the address and lose both markers. The fill engine drops the
    paragraph anyway if removing the markers leaves it empty, which handles the
    markers that sit on their own line.
    """
    markers: list[dict] = []
    for span in prescan.spans:
        found = CONTROL_TOKEN_RE.findall(span.text or "")
        if found:
            markers.append({
                "paragraph_index": span.paragraph_index,
                "span_index": span.span_index,
                "remove": found,
            })
    return markers


def _slug(text: str) -> str:
    """NFKC first, so `＜Ｎａｍｅ＞` and `<Name>` slug to the same key.

    Without it a CJK master's fullwidth Latin headers and placeholders are
    different codepoints from their ASCII twins, and exact-slug binding -- the
    rule that keeps `first_name` from colliding with `colleague_first_name` --
    fails to match anything at all rather than matching the wrong thing.
    """
    # `[\W_]+` and not `[^a-z0-9]+`: the ASCII-only class deleted every CJK
    # character, so `<姓名>` and `<部門>` both slugged to the fallback `"field"`
    # and collided into one manifest entry. `\w` is Unicode-aware in Python 3, so
    # this keeps CJK and accented Latin while producing byte-identical slugs for
    # the ASCII names the existing goldens were compiled from.
    s = re.sub(r"[\W_]+", "_", normalise_text(text).strip().lower(), flags=re.UNICODE).strip("_")
    return s[:60] or "field"


def _clean_condition_value(value: str, family: ConventionFamily = _DEFAULT) -> str:
    """Strip the author's gloss and the participle that qualifies the entity.

    `(e.g. Change Job with Recruitment)` explains the value to a human reader and
    is not part of it. `For transactions initiated with recruitment` likewise
    names the value as the prepositional phrase; the spreadsheet column holds
    only "With Recruitment", so keeping the participle compiles a literal no
    source value can ever equal and the branch silently never fires.

    Both rules are narrow, the instruction's original wording is kept in the
    condition's `compiled_from`, and anything that still fails to match a source
    value is surfaced for a human in Bind rather than guessed at.
    """
    cleaned = normalise_text(value or "")
    if family.gloss_re is not None:
        cleaned = family.gloss_re.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(":：").strip()
    if family.leading_qualifier_re is not None:
        cleaned = family.leading_qualifier_re.sub("", cleaned).strip()
    return cleaned


def parse_condition_instruction(text: str, family: ConventionFamily = _DEFAULT) -> tuple[str | None, str, str] | None:
    """`(till_hint, condition_field_text, condition_value)` for an instruction.

    Accepts every dialect the family declares -- the explicit `Include the
    following text only if the X is Y:` an author writes for an assembly tool,
    and the terser `For Y colleagues:` they write for a human. Returns None when
    the text is an instruction this family cannot read, which is a real answer:
    the caller records lower confidence rather than guessing.
    """
    stripped = normalise_text(text or "").strip()
    if not stripped:
        return None

    if family.include_re is not None:
        m = family.include_re.search(stripped)
        if m:
            g = family.include_groups
            value = _clean_condition_value(m.group(g["value"]), family)
            if value:
                till = m.group(g["till"]) if g.get("till") else None
                return till, m.group(g["field"]).strip(), value

    # `For ...` is anchored, because an unanchored match would read the word "for"
    # out of the middle of ordinary instruction prose. But an inline zone hands
    # this the previous branch's trailing instruction glued to the front of the
    # next one -- "Refer to Contact Matrix For transactions initiated without
    # recruitment ..." -- so retry from each place the instruction could start.
    # Position 0 is always tried: a CJK marker has no leading keyword to anchor
    # on -- `全职员工适用：` names the value first and the entity second, with
    # nothing before them -- so a family may legitimately declare no start
    # pattern at all. For English the patterns still require a leading `for`, so
    # offering position 0 costs nothing there.
    starts = [0]
    if family.for_start_re is not None:
        starts += [m.start() for m in family.for_start_re.finditer(stripped)]
    ordered = (
        (family.for_entity_first_re, family.for_entity_first_groups),
        (family.for_value_first_re, family.for_value_first_groups),
    )
    for start in dict.fromkeys(starts):
        candidate = stripped[start:]
        for pattern, groups in ordered:
            entity_group, value_group = groups["entity"], groups["value"]
            if pattern is None:
                continue
            fm = pattern.match(candidate)
            if fm:
                value = _clean_condition_value(fm.group(value_group), family)
                if value:
                    return None, family.entity_field(fm.group(entity_group)), value
    return None


def _norm_words(text: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9\s]", "", text.lower()).split() if len(w) > 2}


def _looks_like_heading(text: str, family: ConventionFamily = _DEFAULT) -> bool:
    t = text.strip()
    if not t or len(t) > family.heading_max_len:
        return False
    if t.endswith((".", ",", ";")):
        return False
    if "<" in t or "«" in t:
        return False
    return True


@dataclass
class CompiledField:
    id: str
    type: str
    slots: list = field(default_factory=list)
    required: bool = False
    source_hint: str | None = None
    value_rule: str | None = None
    compiled_from: str | None = None


@dataclass
class CompiledCondition:
    id: str
    expression: str
    keeps_blocks: list = field(default_factory=list)
    compiled_from: str = ""
    confidence: float = 0.8


@dataclass
class CompiledBlock:
    id: str
    start_paragraph: int
    end_paragraph: int
    boundary_method: str
    contains_fields: list = field(default_factory=list)
    # Span-scoped blocks. A paragraph range cannot express "keep this run, drop
    # that one, both on the same line", which is what an inline switch needs --
    # and falling back to deleting the paragraph takes the sentence's static text
    # with it. When these are set the block covers only spans
    # [start_span, end_span] of start_paragraph; when they are None the block
    # behaves exactly as it always has.
    start_span: int | None = None
    end_span: int | None = None


@dataclass
class CompiledManifest:
    fields: list
    conditions: list
    blocks: list
    delete_always: list  # list of {paragraph_index, span_index}
    mergefield_paragraphs: list
    hyperlink_paragraphs: list
    confidence: float
    compiled_by: str
    prescan_summary: dict
    notes: list = field(default_factory=list)
    # Named, per-paragraph reasons a human should look at this template before
    # approving it. Any entry blocks auto-onboarding.
    warnings: list = field(default_factory=list)
    # Which convention family read this template, and whether its language needs
    # the postposition pass. Recorded on the manifest so a fill reproduces the
    # compile's decisions without re-detecting them from the document.
    family: str = DEFAULT_FAMILY
    particles: bool = False


# What a human is asked to look at before a template is approved, and why.
#
# A single confidence float says "0.87" and tells an operator nothing about what
# to check. These say which paragraph, what was found there, and what the engine
# will do about it -- which is the difference between an onboarding funnel a
# person can run at a thousand templates and one where every file needs an
# engineer. Any warning present blocks auto-approval; each needs an explicit
# disposition recorded on the manifest.
WARNING_CATALOG = {
    "W-HL-GAP": "An unmarked fragment sits inside an instruction phrase. The zone rule deletes it with the instruction; confirm it is not content.",
    "W-MARKER-PARSE": "An instruction run looks like a branch marker but does not match this family's grammar. Its block will not compile.",
    "W-MIXED-SYNTAX": "A placeholder uses a delimiter pair this family does not declare. It will not be filled.",
    "W-FIELD-CODE": "A placeholder sits inside a live Word field. It is filled and then flattened; confirm the field was not doing something else.",
    "W-DUP-STATIC": "Two blocks carry near-identical static text. Confirm the duplication is intended rather than a conditional copy that lost its marker.",
    "W-NESTED-COND": "A branch marker sits inside another branch's block. Nesting is not modelled; the inner block's extent is a guess.",
    "W-UNSLOTTED-FIELD": "A field was compiled with no place in the document to write it.",
}


@dataclass
class ProfilerWarning:
    code: str
    paragraph_index: int
    detail: str
    evidence: str = ""

    @property
    def message(self) -> str:
        return WARNING_CATALOG.get(self.code, "")


def _profiler_warnings(prescan: PreScanResult, family: ConventionFamily, spans_by_para: dict,
                       inline_zones: dict, markers: list, blocks: list, fields: dict) -> list[ProfilerWarning]:
    warnings: list[ProfilerWarning] = []

    # W-HL-GAP: a static fragment stranded between two instruction runs. This is
    # the client-annotation defect that leaked "initiated" into a live letter.
    for p_idx, branches in inline_zones.items():
        ordered = [s for s in spans_by_para.get(p_idx, []) if not s.in_hyperlink]
        reds = [k for k, s in enumerate(ordered) if s.color == "red" and s.text.strip()]
        if not reds:
            continue
        for k in range(reds[0], reds[-1] + 1):
            s = ordered[k]
            if s.color == "black" and s.text.strip():
                warnings.append(ProfilerWarning("W-HL-GAP", p_idx, f"{s.text.strip()[:60]!r} is unmarked inside the instruction zone", s.text.strip()[:120]))

    marker_span_keys = {(p, s.span_index) for p, s, _ in markers}
    for p_idx, spans in spans_by_para.items():
        for s in spans:
            text = (s.text or "").strip()
            if not text or s.in_hyperlink:
                continue

            # W-MARKER-PARSE: reads like an instruction, does not compile as one.
            if (s.color == "red" and (p_idx, s.span_index) not in marker_span_keys
                    and p_idx not in inline_zones
                    and _instruction_shaped(family, normalise_text(text))
                    and not (family.put_re and family.put_re.match(text))):
                warnings.append(ProfilerWarning("W-MARKER-PARSE", p_idx, "instruction did not match the family grammar", text[:120]))

            # W-MIXED-SYNTAX: a delimiter pair this family does not declare.
            declared = {(d.open, d.close) for d in family.delimiters}
            for opener, closer in (("«", "»"), ("＜", "＞"), ("《", "》"), ("【", "】"), ("{{", "}}"), ("[", "]")):
                if opener in text and closer in text and (opener, closer) not in declared:
                    if opener == "«":
                        continue  # MERGEFIELDs are handled structurally, not as text
                    warnings.append(ProfilerWarning("W-MIXED-SYNTAX", p_idx, f"undeclared delimiter pair {opener}...{closer}", text[:120]))
                    break

    # W-FIELD-CODE: a bracket placeholder living inside a live Word field.
    mergefield_paras = {mf.paragraph_index for mf in prescan.mergefields}
    for p_idx in sorted(mergefield_paras):
        for s in spans_by_para.get(p_idx, []):
            if extract_brackets(s.text, family):
                warnings.append(ProfilerWarning("W-FIELD-CODE", p_idx, "bracket placeholder shares a paragraph with a MERGEFIELD", s.text.strip()[:120]))
                break

    # W-NESTED-COND: a marker inside another block's extent.
    for p_idx, _span, _parsed in markers:
        for b in blocks:
            if b.start_span is None and b.start_paragraph <= p_idx <= b.end_paragraph:
                warnings.append(ProfilerWarning("W-NESTED-COND", p_idx, f"marker sits inside block {b.id}", ""))
                break

    # W-DUP-STATIC: two blocks whose text is near-identical.
    def block_text(b) -> str:
        if b.start_span is not None:
            return ""
        return " ".join(
            s.text for j in range(b.start_paragraph, b.end_paragraph + 1)
            for s in spans_by_para.get(j, []) if s.color == "black"
        ).strip()

    seen: dict[str, str] = {}
    for b in blocks:
        text = re.sub(r"\s+", " ", block_text(b))[:400]
        if len(text) < 60:
            continue
        if text in seen and seen[text] != b.id:
            warnings.append(ProfilerWarning("W-DUP-STATIC", b.start_paragraph, f"static text duplicates block {seen[text]}", text[:120]))
        seen.setdefault(text, b.id)

    for f in fields.values():
        if not f.slots:
            warnings.append(ProfilerWarning("W-UNSLOTTED-FIELD", -1, f"field {f.id!r} has no slot in the document", f.id))

    return warnings


def _find_block_end(start_idx: int, till_hint: str | None, spans_by_para: dict, table_flags: set, next_marker_idx: int | None, n_paragraphs: int, family: ConventionFamily = _DEFAULT) -> tuple[int, str]:
    max_lookahead = 60
    limit = (next_marker_idx - 1) if next_marker_idx is not None else min(start_idx + max_lookahead, n_paragraphs - 1)

    if till_hint:
        hint_words = _norm_words(till_hint)
        for j in range(start_idx, min(start_idx + max_lookahead, n_paragraphs)):
            for s in spans_by_para.get(j, []):
                for b in extract_brackets(s.text, family):
                    if hint_words and hint_words <= _norm_words(b) or _norm_words(b) <= hint_words:
                        return j, "till_hint"

    end = start_idx
    entered_table = False
    for j in range(start_idx, limit + 1):
        in_table = j in table_flags
        if in_table:
            entered_table = True
            end = j
            continue
        if entered_table:
            break  # just exited a contiguous table run -- stop here
        if j > start_idx:
            texts = " ".join(s.text for s in spans_by_para.get(j, []))
            if _looks_like_heading(texts, family):
                break
        end = j
    method = "next_marker" if next_marker_idx is not None else ("table_exit" if entered_table else "lookahead_cap")
    return end, method


def _resolve_inline_zone(spans: list[RunSpan], family: ConventionFamily = _DEFAULT) -> tuple[list[dict], list[int]] | None:
    """Branches and unconditional span deletions for a paragraph whose switch is inline.

    The instruction ZONE runs from the first instruction span to the last one.
    Inside it, consecutive non-placeholder spans are joined and read as one
    instruction, and each placeholder is governed by the instruction that most
    recently preceded it. Everything in the zone that is not a governed
    placeholder is scaffolding and goes.

    Joining before parsing is what makes this survive real annotation. A client
    master had the word "initiated" left unmarked in the middle of two
    instruction phrases; per-span parsing sees three fragments, matches none of
    them, and leaks `letter to initiated APAC Recruitment Operations initiated .`
    into the letter. Joined, the instruction reads whole.

    Returns None when the paragraph is not an inline switch, which is the common
    case: a marker alone on its line governs the paragraphs that follow it.
    """
    ordered = [s for s in spans if not s.in_hyperlink]
    instruction_positions = [k for k, s in enumerate(ordered) if s.color == "red" and s.text.strip()]
    if not instruction_positions:
        return None
    zone_start, zone_end = instruction_positions[0], instruction_positions[-1]

    branches: list[dict] = []
    deletes: list[int] = []
    pending: dict | None = None
    k = zone_start
    while k <= zone_end:
        if ordered[k].color == "blue":
            if pending is not None:
                pending["value_spans"].append(ordered[k].span_index)
            k += 1
            continue
        segment = []
        while k <= zone_end and ordered[k].color != "blue":
            segment.append(ordered[k])
            k += 1
        deletes.extend(s.span_index for s in segment)
        parsed = parse_condition_instruction("".join(s.text for s in segment), family)
        if parsed:
            pending = {
                "condition": parsed,
                "value_spans": [],
                "compiled_from": " ".join(" ".join(s.text.split()) for s in segment).strip(),
            }
            branches.append(pending)
        else:
            pending = None

    branches = [b for b in branches if b["value_spans"]]
    if not branches:
        return None

    # A marker alone on its line is a paragraph-range block, not an inline
    # switch, and misreading one as the other would scope its content to a single
    # run. Require what an inline switch actually looks like: static text outside
    # the zone -- the sentence the switch is embedded in -- or a choice between
    # two or more branches on the one line.
    has_outside_static = any(
        (k < zone_start or k > zone_end) and s.color == "black" and s.text.strip()
        for k, s in enumerate(ordered)
    )
    if not has_outside_static and len(branches) < 2:
        return None
    return branches, deletes


def compile_manifest(prescan: PreScanResult, family: ConventionFamily | None = None) -> CompiledManifest:
    family = family or detect_family(prescan)
    spans_by_para: dict[int, list[RunSpan]] = {}
    for s in prescan.spans:
        spans_by_para.setdefault(s.paragraph_index, []).append(s)

    fields: dict[str, CompiledField] = {}
    delete_always: list[dict] = []
    markers: list[tuple[int, RunSpan, tuple]] = []

    def get_or_create_field(key: str, ftype: str, **kw) -> CompiledField:
        if key not in fields:
            fields[key] = CompiledField(id=key, type=ftype, **kw)
        return fields[key]

    # ---- pass 0: paragraphs whose switch lives inside the line ----
    inline_zones: dict[int, list[dict]] = {}
    for p_idx in sorted(spans_by_para):
        zone = _resolve_inline_zone(spans_by_para[p_idx], family)
        if zone is None:
            continue
        branches, zone_deletes = zone
        inline_zones[p_idx] = branches
        for span_index in zone_deletes:
            # `scope: span` and not a whole-paragraph deletion: the line carries
            # the static text the switch is embedded in.
            delete_always.append({"paragraph_index": p_idx, "span_index": span_index, "scope": "span"})

    # ---- pass 1: classify every non-hyperlink coloured span ----
    pending_hint: str | None = None
    for p_idx in sorted(spans_by_para):
        for s in spans_by_para[p_idx]:
            if s.in_hyperlink or not s.text.strip():
                continue
            if s.color == "black":
                continue
            # Instructions in an inline zone were consumed above, spans and all.
            # Placeholders still fall through: they are ordinary fields, and the
            # zone decides only whether they survive.
            if p_idx in inline_zones and s.color == "red":
                continue

            parsed = parse_condition_instruction(s.text, family) if s.color == "red" else None
            if parsed:
                markers.append((p_idx, s, parsed))
                delete_always.append({"paragraph_index": s.paragraph_index, "span_index": s.span_index})
                continue

            brackets = extract_bracket_tokens(s.text, family)
            if brackets:
                for token, b in brackets:
                    fid = _slug(b)
                    f = get_or_create_field(fid, "string", source_hint=pending_hint)
                    f.slots.append({"kind": f"{s.color}_placeholder", "text": token, "paragraph_index": s.paragraph_index, "span_index": s.span_index})
                # any leading/trailing prose around the bracket in a RED span is instruction text -> delete
                if s.color == "red":
                    remainder = family.bracket_re().sub("", s.text).strip()
                    if remainder:
                        delete_always.append({"paragraph_index": s.paragraph_index, "span_index": s.span_index, "partial": True})
                pending_hint = None
                continue

            if s.color == "red":
                pm = family.put_re.match(s.text.strip()) if family.put_re else None
                if pm:
                    pending_hint = pm.group(1)
                delete_always.append({"paragraph_index": s.paragraph_index, "span_index": s.span_index})

    # ---- pass 2: mergefields ----
    for mf in prescan.mergefields:
        fid = _slug(mf.code)
        f = get_or_create_field(fid, "currency", source_hint=f"mergefield:{mf.code}")
        f.slots.append({"kind": "mergefield", "code": mf.code, "paragraph_index": mf.paragraph_index})

    # ---- pass 3: resolve condition markers -> blocks (marker-to-marker / table-aware / till-hint) ----
    table_flags = prescan.table_paragraph_indices
    conditions_by_key: dict[str, CompiledCondition] = {}
    blocks: list[CompiledBlock] = []

    def register_condition(cond_field_text: str, cond_value: str, block_id: str, compiled_from: str) -> None:
        cond_key = f"{_slug(cond_field_text)}::{_slug(cond_value)}"
        if cond_key not in conditions_by_key:
            conditions_by_key[cond_key] = CompiledCondition(
                id=f"cond_{_slug(cond_field_text)}_{_slug(cond_value)}",
                expression=f"{_slug(cond_field_text)} == '{cond_value.strip()}'",
                compiled_from=compiled_from,
            )
        conditions_by_key[cond_key].keeps_blocks.append(block_id)

    for i, (p_idx, span, m) in enumerate(markers):
        till_hint, cond_field_text, cond_value = m
        next_marker_idx = markers[i + 1][0] if i + 1 < len(markers) else None
        start = p_idx + 1
        end, method = _find_block_end(start, till_hint, spans_by_para, table_flags, next_marker_idx, len(prescan.paragraphs), family)

        block_id = f"blk_{len(blocks)}_{_slug(cond_field_text)}_{_slug(cond_value)}"
        contains = []
        for j in range(start, end + 1):
            for s in spans_by_para.get(j, []):
                for b in extract_brackets(s.text, family):
                    contains.append(_slug(b))
        blocks.append(CompiledBlock(id=block_id, start_paragraph=start, end_paragraph=end, boundary_method=method, contains_fields=sorted(set(contains))))

        register_condition(cond_field_text, cond_value, block_id, span.text.strip())

    # ---- pass 4: inline switches -> span-scoped blocks ----
    for p_idx in sorted(inline_zones):
        for branch in inline_zones[p_idx]:
            _till, cond_field_text, cond_value = branch["condition"]
            value_spans = branch["value_spans"]
            block_id = f"blk_{len(blocks)}_{_slug(cond_field_text)}_{_slug(cond_value)}"
            contains = []
            for s in spans_by_para.get(p_idx, []):
                if s.span_index in value_spans:
                    contains.extend(_slug(b) for b in extract_brackets(s.text, family))
            blocks.append(CompiledBlock(
                id=block_id,
                start_paragraph=p_idx,
                end_paragraph=p_idx,
                boundary_method="inline_zone",
                contains_fields=sorted(set(contains)),
                start_span=min(value_spans),
                end_span=max(value_spans),
            ))
            register_condition(cond_field_text, cond_value, block_id, branch["compiled_from"])

    field_confidences = [1.0 if f.slots else UNSLOTTED_FIELD_CONFIDENCE for f in fields.values()]
    block_confidences = [BOUNDARY_CONFIDENCE.get(b.boundary_method, UNKNOWN_BOUNDARY_CONFIDENCE) for b in blocks]
    overall = round(sum(field_confidences + block_confidences) / max(1, len(field_confidences) + len(block_confidences)), 2) if (field_confidences or block_confidences) else 1.0

    return CompiledManifest(
        fields=[asdict(f) for f in fields.values()],
        conditions=[asdict(c) for c in conditions_by_key.values()],
        blocks=[asdict(b) for b in blocks],
        delete_always=delete_always,
        mergefield_paragraphs=sorted({mf.paragraph_index for mf in prescan.mergefields}),
        hyperlink_paragraphs=sorted({h.paragraph_index for h in prescan.hyperlinks}),
        confidence=overall,
        compiled_by="rule_based",
        family=family.id,
        particles=family.particles,
        warnings=[asdict(w) | {"message": w.message} for w in _profiler_warnings(
            prescan, family, spans_by_para, inline_zones, markers, blocks, fields)],
        prescan_summary={
            "paragraph_count": len(prescan.paragraphs),
            "blue_spans": sum(1 for s in prescan.spans if s.color == "blue" and s.text.strip()),
            "red_spans": sum(1 for s in prescan.spans if s.color == "red" and s.text.strip() and not s.in_hyperlink),
            "mergefields": len(prescan.mergefields),
            "hyperlinks": len(prescan.hyperlinks),
            "conditions_found": len(markers) + sum(len(b) for b in inline_zones.values()),
            "inline_switches": len(inline_zones),
            "uses_highlight_markup": prescan.uses_highlight_markup,
        },
    )


REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "field_renames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old_id": {"type": "string"},
                    "new_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["old_id", "new_id", "reason"],
                "additionalProperties": False,
            },
        },
        "condition_corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition_id": {"type": "string"},
                    "expression": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["condition_id", "expression", "reason"],
                "additionalProperties": False,
            },
        },
        "block_corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "end_paragraph": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["block_id", "end_paragraph", "reason"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["field_renames", "condition_corrections", "block_corrections", "notes"],
    "additionalProperties": False,
}

REFINE_SYSTEM = (
    "You review auto-compiled document-template manifests. The rule layer has already "
    "identified, deterministically and correctly, which runs are coloured, where the "
    "MERGEFIELDs are, and what the paragraph indices are -- do not second-guess any of "
    "that. Your job is the part rules cannot do: interpreting what the template author's "
    "prose MEANS, in whatever language it was written. "
    "Correct a field id only when the rule-based slug is genuinely unclear. Correct a "
    "condition only when the expression misreads the instruction it was compiled from. "
    "Correct a block boundary only when the paragraph text shows the block plainly ends "
    "somewhere else. Never invent fields, conditions, or blocks that are not in the draft, "
    "and return empty arrays when the draft is already correct."
)


def refine_with_llm(manifest: CompiledManifest, raw_text_context: str, paragraphs: list[str] | None = None, *, llm_policy=None) -> CompiledManifest:
    """Second compiler pass over the parts rules cannot do.

    The rule layer handles everything language-independent (colours, brackets,
    MERGEFIELDs, paragraph indices). What it cannot do is read instruction prose
    -- `INCLUDE_RE` and `PUT_RE` are English-only, so a German or Japanese
    template yields zero conditions from rules alone. That interpretation is
    exactly what a model is for, and it runs once per template rather than once
    per document.

    Never raises and never returns a worse manifest than it was given: the
    rule-based result is always a valid fallback, so any failure is a silent
    no-op rather than a broken compile.
    """
    from app.llm.provider import get_llm_provider, llm_configured

    if not llm_configured("compile"):
        # The rule-based manifest this was handed is real, deterministic work,
        # so returning it is correct -- but the skipped step has to be visible
        # in the UI rather than looking like the refiner ran and found nothing.
        manifest.notes = [*(manifest.notes or []), "LLM refinement skipped: no language model is configured for compiling."]
        return manifest

    # `llm_policy` is threaded from the request rather than resolved here: this
    # module has no session and no tenant. Passing it on to `get_llm_provider`
    # is what makes the residency check reach the compile paths, which it did
    # not when the only enforcement lived in `llm.boundary`. Leaving it None
    # raises rather than silently sending in-region data elsewhere.
    provider = get_llm_provider("Manifest refinement", policy=llm_policy)

    numbered = ""
    if paragraphs:
        numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(paragraphs) if text.strip())[:20000]

    prompt = (
        "Review this auto-compiled template manifest.\n\n"
        f"FIELDS: {json.dumps([{'id': f['id'], 'type': f['type'], 'source_hint': f.get('source_hint')} for f in manifest.fields], ensure_ascii=False)}\n\n"
        f"CONDITIONS: {json.dumps(manifest.conditions, ensure_ascii=False)}\n\n"
        f"BLOCKS: {json.dumps([{k: b[k] for k in ('id', 'start_paragraph', 'end_paragraph', 'boundary_method')} for b in manifest.blocks], ensure_ascii=False)}\n\n"
        f"TEMPLATE PARAGRAPHS (index-prefixed):\n{numbered or raw_text_context[:20000]}"
    )

    result = provider.structured(system=REFINE_SYSTEM, prompt=prompt, schema=REFINE_SCHEMA, purpose="compile")
    if result.data is None:
        return manifest

    renames = {r["old_id"]: r["new_id"] for r in result.data.get("field_renames", [])}
    known_ids = {f["id"] for f in manifest.fields}
    for f in manifest.fields:
        if f["id"] in renames:
            f["id"] = renames[f["id"]]
    # Conditions and blocks reference field ids by name, so a rename has to
    # propagate or the manifest silently stops resolving.
    for block in manifest.blocks:
        block["contains_fields"] = [renames.get(fid, fid) for fid in block.get("contains_fields", [])]

    by_condition = {c["id"]: c for c in manifest.conditions}
    for correction in result.data.get("condition_corrections", []):
        cond = by_condition.get(correction["condition_id"])
        if cond is not None:
            cond["expression"] = correction["expression"]
            cond["refined_by_llm"] = correction["reason"]

    by_block = {b["id"]: b for b in manifest.blocks}
    for correction in result.data.get("block_corrections", []):
        block = by_block.get(correction["block_id"])
        # A boundary that runs backwards would delete nothing or everything --
        # reject rather than trust it.
        if block is not None and correction["end_paragraph"] >= block["start_paragraph"]:
            block["end_paragraph"] = correction["end_paragraph"]
            block["boundary_method"] = "llm_refined"
            block["refined_by_llm"] = correction["reason"]

    if renames or result.data.get("condition_corrections") or result.data.get("block_corrections"):
        manifest.compiled_by = f"llm_refined:{result.model}"
    manifest.notes = result.data.get("notes", [])
    return manifest
