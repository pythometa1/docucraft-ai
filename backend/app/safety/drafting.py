"""Drafting one periodic safety report section's prose -- and nothing that a
person has to decide.

The model writes sentences. It does not count (§2's second principle: every
figure arrives in `confirmed_data` already computed, and a section that holds a
table emits `[TABLE: key]` where it belongs), it does not assign seriousness,
expectedness or causality (principle one), and it does not conclude that a
signal exists or that the benefit-risk balance has changed. Where a section
needs such a judgment and no source states it, the prompt makes the model write
`[ASSESSMENT REQUIRED: ...]` instead -- an open question for a qualified person,
which blocks export until one has answered it.

**Nothing identifying reaches the model.** Retrieval reads `pv_chunks`, which
are built from masked text only, and the baseline is a previous draft rather
than a source document. That is the design. This module adds a check anyway:
before any call, the free-text parts of the prompt -- extracts, baseline text,
the writer's instruction -- are scanned with the same detector the masking pass
uses, and a confident identifier refuses the call outright. §2's fourth
principle is "never a model call on un-masked text", and a rule stated once
upstream is a rule one refactor away from not holding.

The system prompt is §8's, reproduced byte for byte; `tests/test_safety_drafting`
extracts it from `docs/SAFETY_MODULE_SPEC.md` and compares. It is a contract
the rest of the module is built against: rule 2 is what the export resolves,
rule 5 is what QC's eighth blocker looks for, rule 6 is why the confirmed data
arrives already split into interval and cumulative, and rule 8 is the backstop
behind the masking.
"""

import json
import logging
import re

from app.docgen.markers import (
    MISSING_VALUE, DraftingFailed, DraftResult, _one_line, parse_assessments,
    parse_citations, parse_data_needed, table_markers,
)
from app.llm import provider as llm_provider
from app.safety import deident

log = logging.getLogger(__name__)

CAPABILITY = "Drafting a periodic safety report section"
NARRATIVE_CAPABILITY = "Drafting an ICSR case narrative"

#: Recorded on every draft; changes when the prompt does.
PROMPT_VERSION = "pv-m6-2"
NARRATIVE_PROMPT_VERSION = "pv-icsr-m6-1"

__all__ = ["SYSTEM_PROMPT", "NARRATIVE_PROMPT", "build_prompt", "draft_section",
           "draft_narrative", "IdentifierInPrompt", "table_markers",
           "parse_assessments", "PROMPT_VERSION", "OUTPUT_RULES", "prompt_artifacts"]

SYSTEM_PROMPT = """You are drafting section {section_code} "{section_title}" of a {deliverable_name} — a pharmacovigilance
periodic safety document prepared to {structure_basis} conventions for {target_regions}.

PRODUCT: {product_name} ({inn}), MAH {mah_name}. IBD {ibd}. DIBD {dibd}.
REPORTING INTERVAL: {period_start} to {period_end}. DATA LOCK POINT: {data_lock_point}.
REFERENCE SAFETY INFORMATION IN FORCE FOR THIS REPORT: {rsi_label} version {rsi_version}, effective
{rsi_effective_date}. MedDRA version {meddra_version}.

PRODUCT AND PERIOD METADATA:
{report_metadata_json}

CONFIRMED SAFETY DATA AVAILABLE TO THIS SECTION (read-only, already rendered as tables in the document):
{confirmed_data_summary_json}

BASELINE TEXT FROM THE PREVIOUS APPROVED REPORT FOR THIS SECTION (may be reused where still accurate;
verify against this interval's data before retaining any statement):
{baseline_section_text}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis for narrative content:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts, report metadata, baseline text and confirmed safety data above for
   product-specific facts. General pharmacovigilance and medical knowledge may shape structure and
   phrasing, never content.
2. DO NOT WRITE TABULATIONS OR LINE LISTINGS. Summary tabulations, line listings, exposure tables,
   the signal overview and the safety-concern table are inserted automatically from confirmed data.
   Where such a table belongs, output the single line: [TABLE: <table_key>] and continue.
3. DO NOT COUNT, TOTAL, SUBTRACT OR ESTIMATE CASES, EVENTS, RATES OR EXPOSURE. Every figure you state
   must appear verbatim in the confirmed safety data above; reproduce it exactly and follow it with its
   citation [S#, p.X] or [S#, Table Y].
4. DO NOT ASSIGN OR ALTER seriousness, seriousness criteria, expectedness/listedness, or causality.
   Report only what the confirmed data records. Do not describe an event as "listed", "unlisted",
   "related" or "unrelated" unless that determination is present in the confirmed data.
5. DO NOT CONCLUDE that a signal exists, is refuted, or is causally associated with the product, and do
   not state a change to the benefit-risk balance, unless that conclusion is explicitly stated in the
   sources. Where the section requires such an evaluation and no sourced conclusion exists, write
   [ASSESSMENT REQUIRED: <the specific judgment the qualified person must make>].
6. Distinguish INTERVAL data from CUMULATIVE data explicitly in every sentence where a figure appears.
   Never merge the two. Never carry an interval figure forward from the baseline text.
7. If information this section requires is absent, insert [DATA NEEDED: <exactly what is missing>].
   Never guess. Never omit silently.
8. NEVER include patient identifiers, reporter names, investigator names, site names, exact dates of
   birth, or any identifying detail. Refer to cases by case identifier only.
9. Baseline text is a starting point, not a source of current fact. Any statement carried forward must
   be consistent with this interval's confirmed data; where it is not, rewrite it and note the change.
10. Style: formal regulatory English, third person, past tense for events and actions in the interval,
    present tense for the current state of knowledge. No speculation, no promotional language.
11. Keep the exact section code and title given. Output the heading, then the content. No markdown
    decoration beyond headings. No commentary about being an AI.
"""

#: Appended after §8's rules, which stay byte for byte as the spec gives them.
#: Written because drafts came back quoting the prompt to the reader: a PBRER
#: export printed "[CONFIRMED SAFETY DATA: table_totals]" twelve times and
#: "[Product and period metadata]" four, and called the report a "report
#: workspace". The input block headings and the JSON keys are how the model is
#: briefed; none of them is a word a regulator should read.
OUTPUT_RULES = """
OUTPUT FORMAT:
- The only square-bracketed tokens the content may contain are citations [S#, p.X] / [S#, Table Y],
  [TABLE: <table_key>] lines, [DATA NEEDED: ...] and [ASSESSMENT REQUIRED: ...]. Write nothing else in
  square brackets.
- Never name or echo the headings of the inputs above (for example "confirmed safety data", "product
  and period metadata", "source extracts", "baseline text", "template guidance") and never write a
  data key such as table_totals or case_counts. State the figure or fact itself, with its citation.
- Never refer to the software, the drafting process, a "workspace", a "prompt" or "the data provided";
  write as the author of the report.
"""

#: The headings of the blocks `SYSTEM_PROMPT` and `NARRATIVE_PROMPT` brief the
#: model with. A bracket opening with one of these is the model quoting its
#: brief, not writing the report.
PROMPT_BLOCK_LABELS = (
    "CONFIRMED SAFETY DATA", "PRODUCT AND PERIOD METADATA", "BASELINE TEXT",
    "TEMPLATE GUIDANCE", "SOURCE EXTRACTS", "REFERENCE SAFETY INFORMATION",
    "REPORTING INTERVAL", "DATA LOCK POINT", "CASE RECORD", "RULES", "OUTPUT FORMAT",
)

#: The bracketed forms a draft is meant to contain. Citations are resolved or
#: stripped at export, table markers are replaced by tables, and the two gap
#: markers block export until a person answers them -- so none of them can
#: reach a finished document by accident, and none is reported here.
_INTENDED_BRACKET_RE = re.compile(
    r"^\s*(?:S\d|TABLE\s*:|DATA NEEDED|ASSESSMENT REQUIRED)", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,200})\]")
#: A data key: lower-case words joined by underscores, the shape of every key
#: in the confirmed-data summary and every table key.
_DATA_KEY_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def prompt_artifacts(text: str) -> list:
    """Every bracketed token in `text` that quotes the model's brief.

    Two shapes: a bracket that opens with an input block's heading
    ("[CONFIRMED SAFETY DATA: table_totals]", "[Product and period
    metadata]"), and a bracket holding a data key ("[table_totals]"). The
    intended markers -- citations, `[TABLE: key]`, `[DATA NEEDED: ...]`,
    `[ASSESSMENT REQUIRED: ...]` -- are never reported: they are handled by the
    export and by their own QC blockers, and must keep reaching them.
    """
    found = []
    for match in _BRACKET_RE.finditer(text or ""):
        inner = match.group(1)
        if _INTENDED_BRACKET_RE.match(inner):
            continue
        heading = inner.strip().upper()
        if any(heading.startswith(label) for label in PROMPT_BLOCK_LABELS) \
                or _DATA_KEY_RE.search(inner):
            found.append(match.group(0))
    return found


#: §8 describes the narrative prompt rather than giving it, so this is written
#: to its description: the fixed order, every clause traceable to a case field,
#: no interpretation, no identifiers, and a declared gap for every absent E2B
#: element.
NARRATIVE_PROMPT = """You are writing the case narrative for individual case safety report {case_id} for
{product_name}. The narrative is a structured summary of the case record below and nothing else.

CASE RECORD (the only permitted source; every statement must come from a field in it):
{case_record_json}

RULES:
1. Write in exactly this order, one paragraph each: patient demographics; relevant medical history
   and concomitant medications; the suspect product with dose, route and dates; event onset with
   dates and course; treatment given; outcome; dechallenge and rechallenge; the reporter's and the
   company's causality assessments as recorded; relevant laboratory data; follow-up status.
2. Every clause must be traceable to a field of the case record. Do not interpret, infer, explain
   mechanism, or add clinical context the record does not contain.
3. Where the record lacks an element the order calls for, write [DATA NEEDED: <the element>] in
   its place. Never omit it silently and never guess it.
4. Report seriousness, expectedness and causality exactly as the record states them. Do not assess
   them. If the record states none, say that none is recorded.
5. NEVER include a name, initials, date of birth, address, contact detail, site, investigator or
   any identifier. Refer to the patient as "the patient" and to the case by its case identifier.
   Tokens in square brackets such as [PATIENT-1a2b] are masked identifiers: never expand, guess or
   reproduce what they stand for.
6. Formal regulatory English, third person, past tense. No commentary about being an AI.
"""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": ("The section heading followed by the section content, "
                            "exactly as it should appear in the report."),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}


class IdentifierInPrompt(DraftingFailed):
    """The prompt contained something the detector is confident identifies a
    person. The call is refused rather than made: a name that reached a model
    provider's logs cannot be taken back."""


def _field(value) -> str:
    """One header value as one line, or an explicit absence.

    Collapsing to one line is the prompt-injection guard CMC's builder uses:
    a product name containing a newline and "12. Ignore rule 3" would otherwise
    append a rule. Dates render as ISO; lists join in stored order.
    """
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    text = _one_line(str(value if value is not None else ""))
    return text or MISSING_VALUE


def build_prompt(*, section_code: str, section_title: str, deliverable_name: str,
                 structure_basis: str, target_regions, product: dict, report: dict,
                 rsi: dict, confirmed_data: dict, baseline_text: str | None,
                 guidance: str | None, extracts: str,
                 instruction: str | None = None) -> str:
    """The full prompt for one section. Deterministic: `sort_keys=True` on the
    JSON blobs so the same evidence produces the same prompt, and the audit
    record of what the model saw is a record of something."""
    prompt = SYSTEM_PROMPT.format(
        section_code=_field(section_code),
        section_title=_field(section_title),
        deliverable_name=_field(deliverable_name),
        structure_basis=_field(structure_basis),
        target_regions=_field(target_regions),
        product_name=_field(product.get("product_name")),
        inn=_field(product.get("inn")),
        mah_name=_field(product.get("mah_name")),
        ibd=_field(product.get("ibd")),
        dibd=_field(product.get("dibd")),
        period_start=_field(report.get("period_start")),
        period_end=_field(report.get("period_end")),
        data_lock_point=_field(report.get("data_lock_point")),
        rsi_label=_field(rsi.get("label")),
        rsi_version=_field(rsi.get("version")),
        rsi_effective_date=_field(rsi.get("effective_date")),
        meddra_version=_field(report.get("meddra_version")),
        report_metadata_json=json.dumps({"product": product, "report": report},
                                        indent=2, sort_keys=True, default=str),
        confirmed_data_summary_json=json.dumps(dict(confirmed_data or {}), indent=2,
                                               sort_keys=True, default=str),
        baseline_section_text=(baseline_text or "").strip()
        or "(no previous approved report holds this section)",
        section_guidance=(guidance or "").strip()
        or "(no template guidance recorded for this section)",
        numbered_source_extracts=(extracts or "").strip() or "(none retrieved)",
    )
    if (instruction or "").strip():
        # A twelfth rule, stated as subordinate to the other eleven: an
        # instruction to "state that the benefit-risk balance is unchanged" is
        # a request for exactly the conclusion rule 5 forbids without a source.
        prompt += (
            f'\n12. Additionally, follow the writer\'s instruction for this revision: '
            f'"{_one_line(instruction)}". It constrains this draft; it does not relax '
            f'Rules 1-11. If it asks for a figure, a determination or a conclusion the '
            f'sources and the confirmed data do not contain, write [DATA NEEDED: ...] '
            f'or [ASSESSMENT REQUIRED: ...] rather than the statement.\n'
        )
    return prompt + OUTPUT_RULES


def _guard(*parts: str) -> None:
    """Refuse the call if the free text about to be sent names anybody.

    Only the parts that came from documents or people are scanned -- not the
    product header the organisation typed in, where a marketing authorisation
    holder called "... Institute" would otherwise trip the site pattern on
    every call. Only confident detections count, the same threshold as §11's
    leakage scan.
    """
    found = []
    for part in parts:
        found.extend(deident.scan(part or ""))
    if found:
        kinds = sorted({hit.identifier_type.replace("_", " ") for hit in found})
        raise IdentifierInPrompt(
            f"The text assembled for the model contains {len(found)} likely "
            f"identifier(s) ({', '.join(kinds)}). Nothing was sent. Resolve the "
            "de-identification queue, or remove the identifier from the baseline "
            "text or the instruction, and regenerate.")


def _no_evidence(section_code, section_title, wanted) -> DraftResult:
    """No indexed source matches: a declared gap, and no model call at all."""
    wanted_text = ", ".join(sorted(wanted or ())) or "the documents this section reads"
    content = (f"{section_code} {section_title}\n\n"
               f"[DATA NEEDED: no indexed source content matches this section -- upload, "
               f"de-identify and process {wanted_text}, then regenerate]")
    return DraftResult(content=content, model=None, citations=[],
                       data_needed=parse_data_needed(content), prompt_version=None)


def draft_section(*, section_code: str, section_title: str, deliverable_name: str,
                  structure_basis: str, target_regions, product: dict, report: dict,
                  rsi: dict, confirmed_data: dict, baseline_text: str | None,
                  guidance: str | None, chunks: list, source_map: list, extracts: str,
                  wanted_doc_types=None, instruction: str | None = None,
                  llm_policy=None,
                  get_provider=llm_provider.get_llm_provider) -> DraftResult:
    """Draft one section, or refuse.

    The provider is asked for first so that a deployment with no model
    configured fails here -- a broken install otherwise returns gap-only
    "drafts" and looks exactly like a working one. `get_provider` is a
    parameter so a test's stub is unambiguous (see `app.cmc.drafting`).

    A section with no evidence AND no baseline gets a declared gap with no model
    call. A section with a baseline but no new evidence is still drafted: a
    periodic report is written against its predecessor, and "nothing new this
    interval" is a real answer the baseline lets the model give.
    """
    provider = get_provider(CAPABILITY, policy=llm_policy)
    if not chunks and not (baseline_text or "").strip():
        return _no_evidence(section_code, section_title, wanted_doc_types)

    _guard(extracts, baseline_text, instruction)
    prompt = build_prompt(
        section_code=section_code, section_title=section_title,
        deliverable_name=deliverable_name, structure_basis=structure_basis,
        target_regions=target_regions, product=product, report=report, rsi=rsi,
        confirmed_data=confirmed_data, baseline_text=baseline_text,
        guidance=guidance, extracts=extracts, instruction=instruction)
    result = provider.structured(
        system=prompt,
        prompt=f'Draft section {section_code} "{section_title}" now, following every '
               f'rule above.',
        schema=DRAFT_SCHEMA, purpose="generate")
    if result.data is None:
        log.warning("Safety draft of section %s failed: %s", section_code,
                    result.error or "no structured reply")
        raise DraftingFailed(
            f"The AI draft of section {section_code} could not be produced right now. "
            "Please try again.")
    content = (result.data.get("content") or "").strip()
    if not content:
        raise DraftingFailed(
            f"The AI returned an empty draft for {section_code}. Please try again.")
    # The OUTPUT is scanned too. Rule 8 tells the model never to write an
    # identifier, and a model that expanded a masked token or recalled a name
    # from its training data has written one anyway.
    _guard(content)
    return DraftResult(content=content, model=result.model,
                       citations=parse_citations(content, source_map),
                       data_needed=parse_data_needed(content),
                       prompt_version=PROMPT_VERSION)


def draft_narrative(*, case_id: str, product_name: str, case_record: dict,
                    llm_policy=None,
                    get_provider=llm_provider.get_llm_provider) -> DraftResult:
    """One ICSR narrative, from the structured (and masked) case record."""
    provider = get_provider(NARRATIVE_CAPABILITY, policy=llm_policy)
    record_json = json.dumps(case_record, indent=2, sort_keys=True, default=str)
    _guard(record_json)
    prompt = NARRATIVE_PROMPT.format(case_id=_field(case_id),
                                     product_name=_field(product_name),
                                     case_record_json=record_json)
    result = provider.structured(
        system=prompt, prompt=f"Write the narrative for case {case_id} now.",
        schema=DRAFT_SCHEMA, purpose="generate")
    if result.data is None or not (result.data.get("content") or "").strip():
        log.warning("Narrative for case %s failed: %s", case_id,
                    getattr(result, "error", None) or "it returned nothing")
        raise DraftingFailed(
            f"The AI narrative for case {case_id} could not be produced right now. "
            "Please try again.")
    content = result.data["content"].strip()
    _guard(content)
    return DraftResult(content=content, model=result.model, citations=[],
                       data_needed=parse_data_needed(content),
                       prompt_version=NARRATIVE_PROMPT_VERSION)
