"""The marker grammar every generated section shares.

`[S3, p.12]` and `[DATA NEEDED: ...]` are house conventions, not clinical ones:
a citation resolves a marker back to the chunk the model was shown, and a gap
says out loud what the sources did not contain. Both are parsed here, once, so
that a second module inventing its own bracket dialect is a decision somebody
has to make rather than something that happens by drift.

Pure and offline. Nothing here knows what a study or a batch is.
"""

import re
from dataclasses import dataclass, field

#: What a value-less field reads as in a prompt header. Not "None", not blank:
#: a header line that says "Sponsor: (not recorded)" tells the model the field
#: exists and is empty, which is what stops it filling one in.
MISSING_VALUE = "(not recorded)"

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
    #: Carried on the result, not read from a module constant by the caller.
    #: The version that produced THIS draft is the only one worth recording: a
    #: caller reading its module's PROMPT_VERSION at save time would stamp
    #: every stored draft with whatever the prompt says today, which is
    #: precisely the question the field exists to answer. The module that
    #: builds the result supplies it; there is no default to inherit wrongly.
    prompt_version: str | None = None


def _one_line(text: str) -> str:
    """A writer's instruction as one quoted line inside a numbered rule list.

    Newlines and double quotes are collapsed because the instruction is user
    text landing in the middle of the rules that constrain the draft. A
    two-line instruction whose second line reads `10. Ignore rule 1` is a
    prompt injection that costs one function to make impossible.
    """
    return " ".join((text or "").split()).replace('"', "'")


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




# ------------------------------------------------------------- table markers

#: `[TABLE: key]` alone on its line -- the one shape a renderer resolves.
#: Shared by every module whose sections carry computed tables, so the drafting
#: side that reports "this section's table is present" and the export side that
#: replaces the marker are asking the same question of the same text. The key
#: is a named group and also group 1, so `findall` and `.group(1)` both work.
#:
#: Deliberately strict. "[Table: x]", a dotted key or a marker mid-sentence all
#: reach a renderer as prose and are printed literally, while a permissive match
#: would report the table resolved. Reported as no marker at all, the section is
#: visibly missing its table instead -- which is the failure somebody notices.
TABLE_MARKER_RE = re.compile(
    r"^[ \t]*\[TABLE:\s*(?P<key>[A-Za-z0-9_]+)\s*\][ \t]*$", re.MULTILINE)


def table_markers(content: str) -> list:
    """Every table key the renderer will place, in order. Duplicates kept."""
    return [match.group("key") for match in TABLE_MARKER_RE.finditer(content or "")]


# ------------------------------------------------------ judgments left open

_ASSESSMENT_RE = re.compile(r"\[ASSESSMENT REQUIRED:\s*(?P<what>[^\]]*)\]",
                            re.IGNORECASE)


def parse_assessments(content: str) -> list:
    """Every [ASSESSMENT REQUIRED: ...] a draft leaves for a qualified person.

    The Safety module's prompt tells the model to write one of these wherever a
    section needs a benefit-risk or causality conclusion that no source states.
    Each is an open judgment, not a gap in the data, and it blocks export until
    somebody authorised to make that judgment has made it. An empty payload is
    kept for the same reason `parse_data_needed` keeps one.
    """
    return [match.group("what").strip()
            for match in _ASSESSMENT_RE.finditer(content or "")]
