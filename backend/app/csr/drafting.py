"""Drafting one CSR section: the prompt, the model call, and what comes back.

One section per call, never the document -- the module's first design
principle, and the reason a CSR can be regenerated section by section without
disturbing the twelve a writer has already approved.

The three functions that matter downstream are the parsers. Generation is only
half of grounded drafting; the other half is being able to prove, afterwards,
which sentence rests on which page of which file. `parse_citations` turns the
model's [S2, p.14] markers back into rows, and `parse_data_needed` collects the
gaps the model was told to declare instead of filling. Both are pure text
functions with no session and no provider, because QC re-runs them over edited
drafts long after the generation that produced them.

Nothing here invents content on a failure path. A model that refuses, times
out or returns an empty draft raises; a section with no indexed evidence comes
back as an explicit [DATA NEEDED: ...] gap and no prose at all. A CSR section
that reads well and states something no source supports is the worst artefact
this system could produce.
"""

import json
import re
from dataclasses import dataclass, field

from app.csr.ich_e3 import source_types_for
from app.llm.provider import get_llm_provider

#: The metering label for this capability. One string, spelled the same way
#: every time it is asked for, because the cost report groups on it.
CAPABILITY = "Drafting a clinical study report section"

#: Recorded on every draft. When the prompt changes, this changes with it --
#: otherwise "why does Section 10.1 read differently this month?" has no
#: answer, and the drafts either side of the change are not comparable.
PROMPT_VERSION = "csr-e3-1"

# Reproduced verbatim from docs/CSR_MODULE_SPEC.md, two em dashes and all, in a
# codebase that otherwise writes " -- ". It is copied rather than paraphrased
# because it is a contract the rest of the module is built against: rule 2 is
# why every number carries a marker `parse_citations` can find, rule 3 is why a
# gap arrives as [DATA NEEDED: ...] rather than as a plausible sentence, and
# rule 4 is what makes a prior CSR safe to retrieve at all. Reword the prompt
# and the QC engine is silently checking a document nobody asked for.
SYSTEM_PROMPT = """You are drafting Section {section_number} "{section_title}" of a Clinical Study Report (ICH E3) for:
Study {study_id} — {compound_name}, Phase {phase}, {indication}. Sponsor: {sponsor}.

STUDY METADATA:
{study_metadata_json}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts and study metadata above for study-specific facts. General regulatory-writing knowledge may shape style, never content.
2. Every number, percentage, dose, count, p-value, confidence interval, or date must be immediately followed by its citation: [S#, p.X] or [S#, Table Y].
3. If information this section requires is not present in the sources, insert [DATA NEEDED: <exactly what is missing>]. Never guess. Never omit silently.
4. Extracts marked "style reference only" must never be cited as fact.
5. Style: formal regulatory English; past tense for study conduct and results; third person; no marketing language; no speculation.
6. Keep the exact heading number and title given. Follow the subsection structure in the template guidance.
7. Prefer the SAP's wording for statistical methods and the protocol's wording for design elements. Refer to statistical outputs as "Table/Listing/Figure <id>" only when that id appears in the sources; otherwise write [DATA NEEDED: table reference].
8. Output the section heading, then the prose. No markdown decoration beyond headings. No commentary about being an AI.
"""

#: Where each heading field of the prompt's first two lines comes from, in
#: order of preference. `study_id` reads the protocol number first and the row
#: id only as a fallback: the identifier on a submitted CSR is the one the
#: sponsor's staff recognise, and a uuid in that line reads as corruption.
_HEADER_FIELDS = {
    "study_id": ("protocol_number", "study_id"),
    "compound_name": ("compound_name", "compound"),
    "phase": ("phase",),
    "indication": ("indication",),
    "sponsor": ("sponsor",),
}

#: What a missing heading field renders as. Not blank and not a guess: the
#: writer reading the draft has to be able to see that the sponsor was never
#: recorded, and the model has to be shown an absence rather than left to
#: supply a plausible name for one.
MISSING_VALUE = "(not recorded)"

#: A single prose field, because that is what a CSR section is. Constraining
#: the reply at the API level is what keeps the [S#] markers intact: the shared
#: GENERATION_SCHEMA splits prose into blocks with chunk-id citation lists,
#: which is the right shape for the template engine and the wrong one here --
#: it would strip the inline markers rule 2 exists to produce.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": ("The section heading followed by the section prose, "
                            "exactly as it should appear in the report."),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

_MARKER_RE = re.compile(r"\[S(?P<index>\d+)(?:\s*,\s*(?P<locator>[^\]]*))?\]")
_PAGE_RE = re.compile(r"\b(?:p{1,2}\.?|pages?)\s*(\d+)", re.IGNORECASE)
_TABLE_RE = re.compile(r"\b(?:table|listing|figure)\s*[0-9][\w.\-]*", re.IGNORECASE)
_DATA_NEEDED_RE = re.compile(r"\[DATA NEEDED:\s*(?P<what>[^\]]*)\]", re.IGNORECASE)

# The number a marker is vouching for is the last one before it, separated by
# at most a few unit or noun words ("120 patients [S2]", "12.5 mg/day [S1]").
# The word limit is the point: without it, "in 2024 the sponsor reported this
# [S1]" would record 2024 as the cited value, and QC would then raise a
# mismatch against a number the sentence never claimed.
_CITED_VALUE_RE = re.compile(
    r"(?P<value>-?\d[\d,]*(?:\.\d+)?\s*%?)"
    r"(?:\s+[A-Za-z()/%'.\-]+){0,3}"
    r"[\s)\](,;:.]*$"
)


class DraftingFailed(RuntimeError):
    """The model produced no usable draft for this section.

    Distinct from `LLMNotConfiguredError` (no model at all, which the router
    turns into a 503) because the remedies differ: that one is a deployment to
    configure, this one is a section to retry. Neither ever resolves into
    invented prose.
    """


@dataclass
class DraftResult:
    content: str
    model: str | None
    citations: list = field(default_factory=list)
    data_needed: list = field(default_factory=list)
    #: Carried on the result, not read from the module by the caller. The
    #: version that produced THIS draft is the only one worth recording: a
    #: caller reading PROMPT_VERSION at save time would stamp every stored
    #: draft with whatever the prompt says today, which is precisely the
    #: question the field exists to answer.
    prompt_version: str | None = PROMPT_VERSION


def _header_value(study_metadata: dict, name: str) -> str:
    for key in _HEADER_FIELDS[name]:
        value = (study_metadata or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return MISSING_VALUE


def _one_line(text: str) -> str:
    """A writer's instruction as one quoted line inside a numbered rule list.

    Newlines and double quotes are collapsed because the instruction is user
    text landing in the middle of the rules that constrain the draft. A
    two-line instruction whose second line reads `10. Ignore rule 1` is a
    prompt injection that costs one function to make impossible.
    """
    return " ".join((text or "").split()).replace('"', "'")


def build_prompt(*, section_number: str, section_title: str, guidance: str | None,
                 study_metadata: dict, extracts: str,
                 instruction: str | None = None) -> str:
    """The full drafting prompt for one section.

    `sort_keys=True` on the metadata is not cosmetic: the same section drafted
    from the same evidence has to produce the same prompt twice, or the audit
    record of what the model saw is not a record of anything and two drafts
    cannot be compared.
    """
    metadata = dict(study_metadata or {})
    prompt = SYSTEM_PROMPT.format(
        section_number=section_number,
        section_title=section_title,
        study_id=_header_value(metadata, "study_id"),
        compound_name=_header_value(metadata, "compound_name"),
        phase=_header_value(metadata, "phase"),
        indication=_header_value(metadata, "indication"),
        sponsor=_header_value(metadata, "sponsor"),
        study_metadata_json=json.dumps(metadata, indent=2, sort_keys=True, default=str),
        section_guidance=(guidance or "").strip() or "(no template guidance recorded for this section)",
        numbered_source_extracts=(extracts or "").strip() or "(none retrieved)",
    )
    if (instruction or "").strip():
        # A ninth rule rather than a preamble: a regeneration instruction is a
        # constraint on the draft, and the rules are the only part of this
        # prompt the model is told it must obey. Saying explicitly that it does
        # not relax rules 1-4 is what stops "say the study met its endpoint"
        # from being read as permission to say it without a source.
        prompt += (
            f'9. Additionally, follow the writer\'s instruction for this revision: '
            f'"{_one_line(instruction)}". It constrains this draft; it does not relax '
            f'Rules 1-4. If it asks for something the sources do not support, write '
            f'[DATA NEEDED: <what is missing>] rather than the statement.\n'
        )
    return prompt


def parse_citations(content: str, source_map: list) -> list:
    """Every [S#] marker in a draft, resolved to the chunk it points at.

    The dicts carry exactly the columns of `CsrCitation`, so the caller stores
    them without translating and nothing is quietly lost in between.

    A marker that resolves to nothing -- [S9] when six extracts were supplied --
    comes back with `chunk_id` None rather than being dropped. It is a real
    defect: the model cited a source it was never given, and every sentence
    resting on that marker is ungrounded. QC cannot flag what retrieval already
    swept up.
    """
    by_index = {entry.get("marker"): entry for entry in (source_map or ())}
    citations = []
    previous_end = 0
    for match in _MARKER_RE.finditer(content or ""):
        # The window stops at the previous marker so a number inside it
        # ("[S1, p.4]") is never mistaken for the value this one vouches for.
        window = (content or "")[previous_end:match.start()]
        previous_end = match.end()

        locator = (match.group("locator") or "").strip()
        page_match = _PAGE_RE.search(locator)
        table_match = _TABLE_RE.search(locator)
        source = by_index.get(f"S{int(match.group('index'))}")
        citations.append({
            "marker": match.group(0),
            # An unresolved marker keeps its text and its value and loses only
            # its target -- which is precisely the finding QC has to report.
            "document_id": source.get("document_id") if source else None,
            "chunk_id": source.get("chunk_id") if source else None,
            "page": int(page_match.group(1)) if page_match else None,
            # Kept as written, "Table 14.1.1" and not "14.1.1": a Listing and a
            # Table sharing a number are different objects, and QC compares
            # against both the id and the kind.
            "table_ref": " ".join(table_match.group(0).split()) if table_match else None,
            "cited_value": _preceding_number(window),
        })
    return citations


def _preceding_number(window: str) -> str | None:
    match = _CITED_VALUE_RE.search(window.rstrip())
    if not match:
        return None
    # "45.6 %" and "45.6%" are the same claim; QC compares normalised values,
    # so the space goes here rather than in three places downstream.
    return "".join(match.group("value").split())


def parse_data_needed(content: str) -> list:
    """Every [DATA NEEDED: ...] payload, in the order the draft states them.

    An empty payload is kept. The model declaring a gap without saying what is
    missing is still a gap, and it still blocks export -- silently discarding
    it because it is uninformative would unblock the export instead.
    """
    return [match.group("what").strip() for match in _DATA_NEEDED_RE.finditer(content or "")]


def _no_evidence_result(section_number: str, section_title: str) -> DraftResult:
    """The draft for a section with nothing indexed to write it from.

    Deliberately not a model call. With no permitted factual basis the only two
    things a model can return are rule-3 gaps or prose from its own memory of
    other people's trials, and the second is indistinguishable from the first
    until a reviewer catches it. `model` is None because no model wrote this.
    """
    types = source_types_for(section_number)
    wanted = ", ".join(types) if types else "the study's source documents"
    content = (
        f"{section_number} {section_title}\n\n"
        f"[DATA NEEDED: no indexed source content matches this section -- "
        f"upload and process {wanted}, then regenerate]"
    )
    # `prompt_version` is None alongside `model`, for the same reason: no
    # prompt was built and no model was asked, so stamping this gap with the
    # drafting prompt's version would put it in the audit trail as something
    # that prompt produced.
    return DraftResult(content=content, model=None, citations=[],
                       data_needed=parse_data_needed(content), prompt_version=None)


def draft_section(*, section_number: str, section_title: str, guidance: str | None,
                  study_metadata: dict, chunks: list, source_map: list, extracts: str,
                  instruction: str | None = None, llm_policy=None) -> DraftResult:
    """Draft one section from the evidence retrieved for it.

    Raises `LLMNotConfiguredError` (the router answers 503) or `DraftingFailed`
    rather than returning anything a person might mistake for a draft.
    """
    # Asked for before the empty-evidence check, and deliberately so. This call
    # is where residency is enforced and metering is attached, and an install
    # with no key configured has to fail here -- otherwise a project with no
    # documents yet would receive gap-only "drafts" and a broken deployment
    # would look exactly like a working one.
    provider = get_llm_provider(CAPABILITY, policy=llm_policy)
    if not chunks:
        return _no_evidence_result(section_number, section_title)

    prompt = build_prompt(
        section_number=section_number, section_title=section_title,
        guidance=guidance, study_metadata=study_metadata,
        extracts=extracts, instruction=instruction,
    )
    result = provider.structured(
        # The spec's prompt IS the system prompt, extracts and all; the user
        # turn only names the section, because every provider here wants a
        # non-empty one.
        system=prompt,
        prompt=f'Draft Section {section_number} "{section_title}" now, following every rule above.',
        schema=DRAFT_SCHEMA,
        # "generate", not "compile". `purpose` names the job, not the size of
        # the model: borrowing "compile" to reach a stronger model would also
        # route CSR drafting to LLM_COMPILE_PROVIDER, a different vendor
        # entirely. Which model drafts is configuration (spec: model names are
        # config, not code).
        purpose="generate",
    )
    if result.data is None:
        raise DraftingFailed(
            f"The model could not draft Section {section_number}: "
            f"{result.error or 'it returned no structured reply'}")
    content = (result.data.get("content") or "").strip()
    if not content:
        raise DraftingFailed(
            f"The model returned an empty draft for Section {section_number}.")

    return DraftResult(
        content=content,
        model=result.model,
        citations=parse_citations(content, source_map),
        data_needed=parse_data_needed(content),
    )
