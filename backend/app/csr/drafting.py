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
import logging

from app.csr.ich_e3 import source_types_for
from app.docgen.markers import (
    MISSING_VALUE, DraftingFailed, DraftResult, _one_line, parse_citations,
    parse_data_needed,
)
from app.llm.provider import get_llm_provider

log = logging.getLogger(__name__)

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

def _header_value(study_metadata: dict, name: str) -> str:
    for key in _HEADER_FIELDS[name]:
        value = (study_metadata or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return MISSING_VALUE


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
        log.warning("CSR draft of Section %s failed: %s", section_number,
                    result.error or "no structured reply")
        raise DraftingFailed(
            f"The AI draft of Section {section_number} could not be produced right now. "
            "Please try again.")
    content = (result.data.get("content") or "").strip()
    if not content:
        raise DraftingFailed(
            f"The AI returned an empty draft for Section {section_number}. Please try again.")

    return DraftResult(
        content=content,
        model=result.model,
        citations=parse_citations(content, source_map),
        data_needed=parse_data_needed(content),
        # Stamped explicitly. The shared dataclass has no default to inherit,
        # deliberately: a module that forgot to say which prompt wrote a draft
        # would record None rather than quietly claiming another module's.
        prompt_version=PROMPT_VERSION,
    )
