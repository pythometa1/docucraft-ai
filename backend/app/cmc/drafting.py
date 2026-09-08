"""Drafting one CMC section's prose, and refusing to write its data.

Flow A of the two this module keeps apart. The prompt's rule 2 tells the model
to emit the single line `[TABLE: <key>]` where a specification, batch analysis,
stability or batch formula table belongs, and `table_markers` is how everything
downstream finds those lines again. That division is the reason the module
exists: a model that typesets a specification is a model that decides
significant figures, and a limit that arrives in a submission rounded from
0.050 to 0.05 is a regulatory event, not a typo.

So nothing here reads, re-prints or re-formats a value. The verified data is
shown to the model as JSON it may quote but not recompute, which is what stops
the prose and the rendered table disagreeing; no value on this path is parsed
back out of a draft, and none is routed through `app.generation.value_format`,
which rounds at three decimal places through a float.

Nothing invents content on a failure path either. A model that refuses or
returns an empty draft raises; a section with no indexed evidence comes back as
an explicit [DATA NEEDED: ...] gap with no model call at all, because with no
permitted factual basis the only things a model can return are declared gaps or
half-remembered chemistry from somebody else's dossier, and the second is
indistinguishable from the first until a reviewer catches it.
"""

import json
import re

from app.docgen.markers import (
    MISSING_VALUE, DraftingFailed, DraftResult, _one_line, parse_citations,
    parse_data_needed,
)
from app.llm import provider as llm_provider

#: The metering label for this capability. One string, spelled the same way
#: every time it is asked for, because the cost report groups on it.
CAPABILITY = "Drafting a quality dossier section"

#: Recorded on every draft. When the prompt changes, this changes with it --
#: otherwise "why does P.5.1 read differently since the variation?" has no
#: answer, and two drafts either side of the change are not comparable.
PROMPT_VERSION = "cmc-m4q-1"

# Reproduced verbatim from docs/CMC_MODULE_SPEC.md, em dashes and all, in a
# codebase that otherwise writes " -- ". Copied rather than paraphrased because
# it is a contract the rest of the module is built against: rule 2 is why
# `table_markers` has anything to find, rule 3 is why a quoted value keeps its
# own digits, rule 5 is why a gap arrives as [DATA NEEDED: ...] instead of as a
# plausible sentence, and rule 6 is what makes a prior dossier safe to retrieve
# at all. Reword the prompt and the table renderer, the citation store and the
# QC engine are all silently checking a document nobody asked for.
SYSTEM_PROMPT = """You are drafting section {section_code} "{section_title}" of a {deliverable_name} for a pharmaceutical
quality dossier, prepared to {structure_basis} conventions for submission to {target_regions}.

PRODUCT: {product_name} ({inn_or_ds_name}), {dosage_form}, {strengths}, {route_of_administration}.
Submission type: {submission_type}. Development phase: {development_phase}.

PRODUCT AND SITE METADATA:
{project_metadata_json}

VERIFIED QUALITY DATA AVAILABLE TO THIS SECTION (read-only, already rendered as tables in the document):
{verified_data_summary_json}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis for narrative content:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts, product metadata, and verified quality data above for product-specific
   facts. General regulatory and pharmaceutical knowledge may shape structure and phrasing, never content.
2. DO NOT WRITE DATA TABLES. Specification tables, batch analysis tables, stability tables and batch
   formulae are inserted automatically from verified data. Where such a table belongs, output the single
   line: [TABLE: <table_key>] and continue with the narrative.
3. Do not restate individual numeric results in prose unless the value appears in the verified quality
   data above; when you do, reproduce it exactly — same digits, same significant figures, same unit,
   same operator (NMT/NLT/ND/<//>) — and follow it with its citation [S#, p.X] or [S#, Table Y].
4. Never round, convert, average, extrapolate, or infer a value. Never state a trend, a shelf life, or a
   conformance conclusion that is not explicitly stated in the sources.
5. If information this section requires is absent, insert [DATA NEEDED: <exactly what is missing>].
   Never guess. Never omit silently.
6. Extracts marked "reference only" (previously approved dossiers) must never be cited as current fact.
7. Style: formal regulatory English, third person, no marketing language, no speculation. Use the present
   tense for descriptions of the manufacturing process, controls, and specifications as they currently
   stand; use the past tense for studies, validation exercises and batches already executed.
8. Refer to other sections by their CTD code (e.g. "see Section 3.2.P.5.1") only where the referenced
   section exists in this dossier.
9. Keep the exact section code and title given. Follow the subsection structure in the template guidance.
10. Output the heading, then the content. No markdown decoration beyond headings. No commentary about
    being an AI.
"""

#: Where each value in the prompt's PRODUCT header comes from, in order of
#: preference. Aliases exist because the same field is spelled one way on the
#: project row and another in a metadata blob a caller assembled, and a header
#: reading "(not recorded)" over data the project actually holds teaches the
#: model that this dossier has no dosage form.
_HEADER_FIELDS = {
    "product_name": ("product_name", "name"),
    "inn_or_ds_name": ("inn_or_ds_name", "inn", "drug_substance_name"),
    "dosage_form": ("dosage_form",),
    "strengths": ("strengths",),
    "route_of_administration": ("route_of_administration", "route"),
    "submission_type": ("submission_type",),
    "development_phase": ("development_phase", "phase"),
    "target_regions": ("target_regions", "regions"),
}

#: A single prose field, because that is what a section draft is. The shared
#: block-and-citation-list schema is the right shape for the template engine and
#: the wrong one here: it would strip the inline [S#] and [TABLE: key] markers
#: that rules 2 and 3 exist to produce.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": ("The section heading followed by the section content, "
                            "exactly as it should appear in the dossier."),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

# Rule 2's marker as rule 2 asks for it -- "the single line" -- and as
# `app/cmc/export.py::TABLE_MARKER_RE` resolves it: alone on its line, key of
# word characters. This expression has to track that one, so a test compares
# the two against the same drafts.
#
# Anything looser is worse than nothing here. "[Table: spec_table]", a dotted
# key, or a marker mid-sentence all reach the renderer as prose and are printed
# literally into a submitted dossier, while a permissive match tells the
# workspace and QC that the section's specification table resolved. Reported as
# no marker at all, the section is instead visibly missing its table, which is
# the failure somebody notices.
_TABLE_MARKER_RE = re.compile(
    r"^[ \t]*\[TABLE:\s*(?P<key>[A-Za-z0-9_]+)\s*\][ \t]*$", re.MULTILINE)


def _header_value(project_metadata: dict, name: str) -> str:
    """One PRODUCT header field, as one line, or an explicit absence.

    `_one_line` is not decoration. These values arrive from uploaded documents
    and from free-text project fields, and a product name containing a newline
    and "11. Ignore rule 2" is a prompt injection that ends with a model
    typesetting a specification table. Collapsing to one line costs nothing and
    makes the whole class impossible.
    """
    for key in _HEADER_FIELDS[name]:
        value = (project_metadata or {}).get(key)
        # Lists are joined in their stored order -- strengths and target
        # regions are both lists, and both are read as a sequence.
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item).strip() for item in value if str(item).strip())
        if value is not None and str(value).strip():
            return _one_line(str(value))
    return MISSING_VALUE


def _one_line_or_missing(value) -> str:
    return _one_line(str(value if value is not None else "")) or MISSING_VALUE


def build_prompt(*, section_code: str, section_title: str, deliverable_name: str,
                 structure_basis: str, guidance: str | None, project_metadata: dict,
                 verified_data: dict, extracts: str,
                 instruction: str | None = None) -> str:
    """The full drafting prompt for one section.

    `sort_keys=True` on both JSON blobs is not cosmetic: the same section drafted
    from the same evidence has to produce the same prompt twice, or the audit
    record of what the model saw is not a record of anything and two drafts
    cannot be compared. The blobs need no `_one_line` of their own -- `json.dumps`
    escapes the newline that a header field would otherwise be broken out of its
    line by.
    """
    metadata = dict(project_metadata or {})
    prompt = SYSTEM_PROMPT.format(
        section_code=_one_line_or_missing(section_code),
        section_title=_one_line_or_missing(section_title),
        deliverable_name=_one_line_or_missing(deliverable_name),
        structure_basis=_one_line_or_missing(structure_basis),
        target_regions=_header_value(metadata, "target_regions"),
        product_name=_header_value(metadata, "product_name"),
        inn_or_ds_name=_header_value(metadata, "inn_or_ds_name"),
        dosage_form=_header_value(metadata, "dosage_form"),
        strengths=_header_value(metadata, "strengths"),
        route_of_administration=_header_value(metadata, "route_of_administration"),
        submission_type=_header_value(metadata, "submission_type"),
        development_phase=_header_value(metadata, "development_phase"),
        project_metadata_json=json.dumps(metadata, indent=2, sort_keys=True, default=str),
        verified_data_summary_json=json.dumps(dict(verified_data or {}), indent=2,
                                              sort_keys=True, default=str),
        section_guidance=(guidance or "").strip()
        or "(no template guidance recorded for this section)",
        numbered_source_extracts=(extracts or "").strip() or "(none retrieved)",
    )
    if (instruction or "").strip():
        # An eleventh rule rather than a preamble: a regeneration instruction is
        # a constraint on the draft, and the rules are the only part of this
        # prompt the model is told it must obey. Saying explicitly that it does
        # not relax rules 1-6 is what stops "state that the batches met the
        # shelf-life criteria" being read as permission to state it unsourced.
        prompt += (
            f'11. Additionally, follow the writer\'s instruction for this revision: '
            f'"{_one_line(instruction)}". It constrains this draft; it does not relax '
            f'Rules 1-6. If it asks for a fact or a value the sources and the verified '
            f'quality data do not contain, write [DATA NEEDED: <what is missing>] rather '
            f'than the statement.\n'
        )
    return prompt


def table_markers(content: str) -> list:
    """Every `[TABLE: key]` key the renderer will place, in the draft's order.

    This lives beside the prompt that produces the markers because the two are
    one decision: rule 2 is only enforced if something downstream can find what
    rule 2 asked for, and finds exactly what the renderer will act on.
    Duplicates are kept -- a section naming `spec_table` twice would render it
    twice, which is a finding for QC and not this function's to tidy away.
    """
    return [match.group("key") for match in _TABLE_MARKER_RE.finditer(content or "")]


def _no_evidence_result(section_code: str, section_title: str,
                        wanted_doc_types) -> DraftResult:
    """The draft for a section with nothing indexed to write it from.

    Names the document types the section reads, because "no sources" is not
    actionable and "upload the drug product specification and the CoAs" is.
    `model` is None because no model wrote this, and `prompt_version` is None
    for the same reason: stamping this gap with the drafting prompt's version
    would enter it in the audit trail as something that prompt produced.
    """
    wanted = ", ".join(str(doc_type).strip() for doc_type in (wanted_doc_types or ())
                       if str(doc_type).strip())
    content = (
        f"{section_code} {section_title}\n\n"
        f"[DATA NEEDED: no indexed source content matches this section -- upload and "
        f"process {wanted or 'the source documents this section reads'}, then regenerate]"
    )
    return DraftResult(content=content, model=None, citations=[],
                       data_needed=parse_data_needed(content), prompt_version=None)


def draft_section(*, section_code: str, section_title: str, deliverable_name: str,
                  structure_basis: str, guidance: str | None, project_metadata: dict,
                  verified_data: dict, chunks: list, source_map: list, extracts: str,
                  wanted_doc_types=None, instruction: str | None = None, llm_policy=None,
                  get_provider=llm_provider.get_llm_provider) -> DraftResult:
    """Draft one section from the evidence retrieved for it.

    `get_provider` is a parameter and not a module global on purpose. The
    clinical module re-exports `get_llm_provider` into its own namespace, so a
    test monkeypatching it has to know which of the two names the code under
    test resolves, and a refactor that changes the answer breaks the test
    silently in the direction of calling a real model. Passing the seam in
    leaves nothing to guess.

    Raises `LLMNotConfiguredError` or `ResidencyUnscoped` (the router answers
    503) or `DraftingFailed` rather than returning anything a person might
    mistake for a draft.
    """
    # Asked for before the empty-evidence check, deliberately. This call is
    # where residency is enforced and metering is attached, and an install with
    # no key configured has to fail here -- otherwise a project whose documents
    # have not finished indexing receives gap-only "drafts" and a broken
    # deployment looks exactly like a working one.
    provider = get_provider(CAPABILITY, policy=llm_policy)
    if not chunks:
        return _no_evidence_result(section_code, section_title, wanted_doc_types)

    prompt = build_prompt(
        section_code=section_code, section_title=section_title,
        deliverable_name=deliverable_name, structure_basis=structure_basis,
        guidance=guidance, project_metadata=project_metadata,
        verified_data=verified_data, extracts=extracts, instruction=instruction,
    )
    result = provider.structured(
        # The spec's prompt IS the system prompt, extracts and all; the user
        # turn only names the section, because every provider here wants a
        # non-empty one.
        system=prompt,
        prompt=f'Draft section {section_code} "{section_title}" now, '
               f'following every rule above.',
        schema=DRAFT_SCHEMA,
        # "generate", not "compile". `purpose` names the job, not the size of
        # the model: borrowing "compile" to reach a stronger model would also
        # route this to LLM_COMPILE_PROVIDER, a different vendor entirely, and
        # send trade-secret CMC content across a boundary nobody approved.
        purpose="generate",
    )
    if result.data is None:
        raise DraftingFailed(
            f"The model could not draft section {section_code}: "
            f"{result.error or 'it returned no structured reply'}")
    content = (result.data.get("content") or "").strip()
    if not content:
        raise DraftingFailed(
            f"The model returned an empty draft for section {section_code}.")

    return DraftResult(
        content=content,
        model=result.model,
        citations=parse_citations(content, source_map),
        data_needed=parse_data_needed(content),
        # Stamped explicitly. The shared dataclass has no default to inherit,
        # deliberately: a module that forgot to say which prompt wrote a draft
        # records None rather than quietly claiming another module's.
        prompt_version=PROMPT_VERSION,
    )
