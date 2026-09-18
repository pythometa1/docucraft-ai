"""The checks that decide whether a CSR draft can be trusted.

Pure functions over text and already-resolved citations -- no database, no
file reads, no model call -- because a QC result has to be reproducible in
three places at once: on the reviewer's Issues tab, in the export gate, and in
a test. A check that needed a session could not run in all three, and a gate
that disagrees with the screen it is gating is worse than no gate.

The bias throughout is toward flagging. A false flag costs a medical writer
one glance; an unsourced number that reaches a regulator costs the submission.
So a sentence is guilty until a citation marker appears inside it, and a cited
number stays unverified until that exact value is found in the chunk its
marker resolves to -- "the source probably says so" is not a QC result.

Nothing here ever repairs a draft, and nothing here ever fills a blank. Every
check either returns an explicit finding or stays silent; a check that quietly
supplied a plausible number to make itself pass is the single failure this
module exists to make impossible.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Iterable

#: Finding codes. Constants and not literals at the call sites, because the
#: frontend Issues tab and the export gate both branch on them; a typo'd code
#: silently downgrades a blocking finding to an unknown one nobody renders.
UNCITED_NUMBER = "UNCITED_NUMBER"
NUMBER_NOT_IN_SOURCE = "NUMBER_NOT_IN_SOURCE"
CITATION_UNRESOLVED = "CITATION_UNRESOLVED"
COUNT_DISAGREEMENT = "COUNT_DISAGREEMENT"
DATA_NEEDED = "DATA_NEEDED"


@dataclass
class QcFinding:
    """One thing wrong with a draft, in a shape the API can serialise as-is.

    `detail` carries the evidence (the sentence, the marker, the values) so the
    editor can jump to the offending text; keep everything in it JSON-safe --
    this dataclass crosses the wire, and a Decimal in there is a 500.
    """

    code: str
    section_number: str | None
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "section_number": self.section_number,
            "message": self.message,
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------- the grammar

#: A numeric token as a CSR writes one: 12.3, 1,234, 45%, -2.5, 0.001.
#:
#: The two lookbehinds are the whole point. The first refuses a digit run that
#: continues a word or a longer number, so the "12" of "p.12" and the "1" of
#: "S1" are not numbers to be cited -- they are the citation. The second
#: refuses the tail of a hyphenated word, so "COVID-19" is a disease and not a
#: 19 somebody forgot to source.
#:
#: The remaining two exclusions the spec asks for -- markers and heading
#: numbers -- are not expressible here without making this unreadable, so
#: every public entry point blanks those regions first (`_prepare`, `_mask`).
#: Blanking preserves offsets, which is what keeps sentence boundaries and
#: number-to-word distances honest after the masking.
NUMBER_RE = re.compile(
    r"""
    (?<![\w.,])                        # not continuing a word or a longer number
    (?<![A-Za-z]-)                     # not the "19" of COVID-19
    [-+]?
    (?: \d{1,3} (?: ,\d{3} )+ | \d+ )  # thousands-grouped first, else plain
    (?: \.\d+ )?
    %?
    """,
    re.VERBOSE,
)

#: The same shape anchored, for deciding whether a string IS a number rather
#: than finding numbers inside prose.
_NUMERIC_SHAPE_RE = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")

#: An inline citation as the generation prompt mandates: [S3, p.12],
#: [S1, Table 14.2.1]. Deliberately loose after the source number -- a model
#: that writes "[S3, page 12]" has still cited, and treating that sentence as
#: uncited would train writers to ignore the check.
CITATION_MARKER_RE = re.compile(r"\[\s*S\d+[^\[\]]*\]")

#: The declared gap. Case-insensitive because the rule is enforced on a model.
DATA_NEEDED_RE = re.compile(r"\[\s*DATA\s+NEEDED\s*:?\s*([^\[\]]*)\]", re.IGNORECASE)

#: A capitalised acronym, 2-6 characters, with an optional plural "s" so the
#: "AEs" that a safety section is written in is still one AE. Longer all-caps
#: words (OBJECTIVES, CONFIDENTIAL) fall outside the length and exclude
#: themselves; everything six characters and under has to be carried by the
#: stoplist below, ICH E3's own heading words included.
ABBREVIATION_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5})s?\b")

#: Where an abbreviation gets defined: "Adverse Event (AE)".
_DEFINITION_RE = re.compile(r"\(([A-Z][A-Z0-9]{1,5})s?\)")

#: All-caps English that is not an abbreviation to define. These reach the
#: scanner from headings and emphasis, and a Section 4 list padded with "AND"
#: and "TOTAL" is one a reviewer stops trusting -- and therefore stops reading
#: for the real omissions. CSR, AE, SAE, IEC and friends are deliberately
#: absent: they are exactly what Section 4 is for.
ABBREVIATION_STOPLIST = frozenset({
    "AS", "AT", "BE", "BY", "DO", "IF", "IN", "IS", "IT", "NO", "OF", "ON",
    "OR", "SO", "TO", "UP", "WE",
    "AGE", "ALL", "AND", "ANY", "ARE", "BUT", "CAN", "DAY", "DUE", "END",
    "FOR", "HAD", "HAS", "ITS", "MAY", "NEW", "NOR", "NOT", "ONE", "OUR",
    "OUT", "PER", "SIX", "TEN", "THE", "TWO", "USE", "VIA", "WAS", "WHY",
    "YES", "YET",
    "ALSO", "BEEN", "BOTH", "EACH", "ELSE", "FROM", "HAVE", "INTO", "LESS",
    "MORE", "MOST", "MUST", "NONE", "ONLY", "OVER", "PART", "SUCH", "THAN",
    "THAT", "THEN", "THIS", "USED", "WERE", "WHEN", "WITH",
    "AFTER", "DRAFT", "EVERY", "FINAL", "MEANS", "NOTES", "OTHER", "SHALL",
    "SINCE", "STUDY", "TABLE", "THEIR", "THESE", "THOSE", "THREE", "TITLE",
    "TOTAL", "UNDER", "UNTIL", "WHERE", "WHICH", "WHILE", "WHOSE",
    "ABOVE", "BELOW", "DATA", "PAGE", "NOTE", "NEEDED", "REPORT", "SHOULD",
    "BEFORE", "DURING", "EITHER", "FIGURE", "NUMBER", "WITHIN",
    # ICH E3 heading and table words short enough to survive the length rule.
    # Without these, Section 4 opens with DESIGN, ETHICS and SAFETY, and a
    # reviewer who meets three non-abbreviations at the top of the list stops
    # reading it before reaching the omission it exists to show.
    "DESIGN", "DOSE", "DOSES", "ETHICS", "GRAPHS", "GROUP", "GROUPS", "LIST",
    "LISTS", "MEAN", "PHASE", "PLAN", "RESULT", "REVIEW", "SAFETY", "SITE",
    "SITES", "TABLES", "TRIAL", "VISIT", "VISITS",
    # Roman numerals a CSR uses for phases and grades. "IV" is left out on
    # purpose: in a safety section it is far more often "intravenous".
    "II", "III", "VII", "VIII", "IX", "XI", "XII", "XIII", "XIV", "XV",
})

#: Words skipped when matching an expansion to its acronym, so that
#: "Case Report Form (CRF)" and "Committee for Medicinal Products (CMP)"
#: both resolve. The expansion is never allowed to START with one of these.
_EXPANSION_SKIP = frozenset({"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with"})

#: Tokens whose full stop does not end a sentence. Without these, "e.g." and
#: "[S1, p. 4]" split one sentence into two, and the half without the marker
#: gets reported as uncited -- a false flag on correctly cited prose, which is
#: the fastest way to get a QC panel switched off.
_ABBREVIATIONS = frozenset({
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "ca.", "approx.", "no.", "nos.",
    "p.", "pp.", "fig.", "figs.", "tab.", "al.", "dr.", "prof.", "mr.",
    "mrs.", "ms.", "st.", "inc.", "ltd.", "co.", "corp.", "u.s.", "u.k.",
    "ph.d.", "sec.", "min.", "max.",
})

_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*[ \t]+")
_TRAILING_TOKEN_RE = re.compile(r"\S+$")
_WORD_RE = re.compile(r"[A-Za-z]+")

#: A line that opens with its own section number: "9.4.6 Blinding", "4 List of
#: Abbreviations". Prompt rule 6 makes the model emit these, and counting a
#: heading number as an unsourced fact would put a finding on every section in
#: the report.
_HEADING_LINE_RE = re.compile(r"^([ \t]*)(\d+(?:\.\d+)*\.?)([ \t]+)(\S.*)$")


# ------------------------------------------------------------------ masking

def _blank(match: re.Match) -> str:
    """Same length, no content -- offsets survive so distances stay true."""
    return " " * (match.end() - match.start())


def _mask(pattern: re.Pattern, text: str) -> str:
    return pattern.sub(_blank, text)


def _mask_heading_number(line: str) -> str:
    """Blank a leading section number, and only a leading section number.

    "120 patients were enrolled" opens with a number too, so the number alone
    cannot decide. A heading is followed by a title: it starts with a capital
    and does not end in sentence punctuation. Prose that opens with a count
    reads "120 patients were ..." -- lower case, full stop -- and keeps its
    number, which is the case that must never be lost.
    """
    match = _HEADING_LINE_RE.match(line)
    if match is None:
        return line
    rest = match.group(4)
    if not rest[:1].isupper():
        return line
    if rest.rstrip().endswith((".", "!", "?")):
        return line
    start, end = match.start(2), match.end(2)
    return line[:start] + " " * (end - start) + line[end:]


def _prepare(content: str) -> str:
    """Heading numbers blanked; everything else, including markers, intact."""
    return "\n".join(_mask_heading_number(line) for line in (content or "").split("\n"))


# ---------------------------------------------------------------- sentences

def _ends_in_abbreviation(line: str, stop_index: int) -> bool:
    match = _TRAILING_TOKEN_RE.search(line[:stop_index + 1])
    if match is None:
        return False
    token = match.group(0).lstrip("([{\"'").lower()
    if token in _ABBREVIATIONS:
        return True
    # "J. Smith": a lone initial is a name, not the end of a thought.
    return bool(re.fullmatch(r"[a-z]\.", token))


def split_sentences(text: str) -> list[str]:
    """Split into sentences, tolerating "e.g.", "p. 4" and decimals.

    Decimals need no special case: "12.3" has no space after the stop, and a
    boundary requires one. A line break is treated as a hard boundary -- a
    bulleted disposition list carries one fact per line, and letting a
    citation on the next line excuse the number on this one is how an
    uncited count gets through.
    """
    sentences: list[str] = []
    for line in (text or "").split("\n"):
        start = 0
        for match in _SENTENCE_END_RE.finditer(line):
            if _ends_in_abbreviation(line, match.start()):
                continue
            piece = line[start:match.end()].strip()
            if piece:
                sentences.append(piece)
            start = match.end()
        tail = line[start:].strip()
        if tail:
            sentences.append(tail)
    return sentences


# ------------------------------------------------------------------ numbers

def normalize_number(text: str) -> str | None:
    """"12.3%" -> "12.3", "1,234" -> "1234", "12.30" -> "12.3"; None if not numeric.

    This is what makes number-to-source comparison honest. A TLF prints
    "1,234" where the draft writes "1234" and prints "45.0%" where the draft
    writes "45%"; comparing the raw strings would report a mismatch on a
    correctly cited number, and a QC panel that cries wolf gets ignored.

    Decimal, not float, and the shape is gated before parsing: Decimal happily
    builds "NaN" and "1e5", and a NaN that compares equal to nothing would
    turn every verification into a silent pass.
    """
    if text is None:
        return None
    candidate = str(text).strip()
    if candidate.endswith("%"):
        candidate = candidate[:-1].rstrip()
    if not _NUMERIC_SHAPE_RE.match(candidate):
        return None
    try:
        value = Decimal(candidate.replace(",", ""))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if value == 0:
        return "0"  # "-0.0" and "0" are the same count of patients
    # format(..., "f"), never str(normalize()): Decimal("120").normalize()
    # prints 1.2E+2, and scientific notation in a QC detail reads as a bug.
    return format(value.normalize(), "f")


def _numbers_in(text: str) -> list[str]:
    return [match.group(0) for match in NUMBER_RE.finditer(text)]


def _uncited_sentences(content: str) -> list[tuple[str, list[str]]]:
    """(sentence, numbers) for every sentence carrying numbers but no marker."""
    out: list[tuple[str, list[str]]] = []
    for sentence in split_sentences(_prepare(content)):
        if CITATION_MARKER_RE.search(sentence):
            continue
        # A number inside "[DATA NEEDED: Table 14.1.1]" is the writer naming
        # what is missing. Flagging it would punish the one behaviour the
        # prompt's rule 3 is trying to produce.
        scannable = _mask(DATA_NEEDED_RE, sentence)
        numbers = _numbers_in(scannable)
        if numbers:
            out.append((sentence, numbers))
    return out


def uncited_numbers(content: str) -> list[str]:
    """Every numeric token standing in a sentence that cites nothing.

    Sentence-level and not clause-level on purpose: prompt rule 2 asks for a
    citation immediately after each number, but writers move markers to the
    end of the sentence and that is still a sourced claim.
    """
    return [number for _sentence, numbers in _uncited_sentences(content) for number in numbers]


def verify_citation_values(
    citations: list[dict],
    chunk_text_by_id: dict[str, str],
) -> list[QcFinding]:
    """Check that each cited value is actually in the chunk it points at.

    Acceptance criterion 5: an altered number must raise a mismatch. Comparison
    is against the source's numeric TOKENS, normalised, rather than a substring
    of its text -- "120" is a substring of "1120", and a check that passes on
    an off-by-a-thousand count is worse than no check.

    A marker resolving to no chunk is its own finding: an unverifiable citation
    looks like a verified one on the page, and that is the more dangerous of
    the two failures.
    """
    findings: list[QcFinding] = []
    normalized_chunks: dict[str, set[str]] = {}
    for citation in citations or ():
        marker = citation.get("marker")
        chunk_id = citation.get("chunk_id")
        source = chunk_text_by_id.get(chunk_id) if chunk_id else None
        if source is None:
            findings.append(QcFinding(
                code=CITATION_UNRESOLVED,
                section_number=None,
                message=f"Citation {marker} does not resolve to a source chunk.",
                detail={"marker": marker, "chunk_id": chunk_id},
            ))
            continue

        raw_value = citation.get("cited_value")
        if raw_value is None or not str(raw_value).strip():
            continue  # a marker on prose, with no number of its own to check
        cited_value = str(raw_value).strip()

        wanted = normalize_number(cited_value)
        if wanted is None:
            # A non-numeric cited value (a table id, a quoted term) still has
            # to be in the source; substring is the only honest test for it.
            found = cited_value.lower() in " ".join(source.split()).lower()
        else:
            if chunk_id not in normalized_chunks:
                normalized_chunks[chunk_id] = {
                    normalized
                    for token in _numbers_in(source)
                    if (normalized := normalize_number(token)) is not None
                }
            found = wanted in normalized_chunks[chunk_id]

        if not found:
            findings.append(QcFinding(
                code=NUMBER_NOT_IN_SOURCE,
                section_number=None,
                message=(
                    f"Citation {marker} claims {cited_value}, which does not "
                    f"appear in the cited source."
                ),
                detail={"marker": marker, "cited_value": cited_value, "chunk_id": chunk_id},
            ))
    return findings


# --------------------------------------------------------- headline N counts

#: The patient counts that must agree wherever the report states them.
HEADLINE_COUNTS = ("screened", "enrolled", "randomized", "treated", "completed", "discontinued")

#: Both spellings fold onto one key, so a "randomised" in 10.1 still disagrees
#: with a "randomized" in 11.1 -- the disagreement is about the patients, and
#: it would be absurd for a dictionary variant to hide it.
_CONCEPT_RE = re.compile(
    r"\b(screened|enrolled|randomi[sz]ed|treated|completed|discontinued)\b",
    re.IGNORECASE,
)

#: How far from the word a count may sit. Backwards is generous ("120 patients
#: with confirmed disease were enrolled"); forwards is tight, because it only
#: has to reach across "enrolled: " and "randomized (N=".
_LOOKBACK = 60
_LOOKAHEAD = 20

#: A word that turns the number after it into a document location rather than
#: a count of patients. "Table 14.1.1 gives the number enrolled" and "were
#: enrolled (Table 14.1.1)" both put a table id exactly where the count-finder
#: looks, and 14.1 recorded as an enrolled N then disagrees with the real 120
#: in the next section -- an invented disagreement, which is the one outcome
#: _count_beside exists to prevent.
_REFERENCE_LABEL_RE = re.compile(
    r"\b(?:tables?|figures?|fig|listings?|sections?|appendix|annex"
    r"|pages?|p|pp)\.?\s*$",
    re.IGNORECASE,
)


def _canonical_concept(word: str) -> str:
    lowered = word.lower()
    return "randomized" if lowered.startswith("randomi") else lowered


def _paren_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for index, char in enumerate(text):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            spans.append((stack.pop(), index))
    return spans


def _inside(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in spans)


def _reference_labelled(text: str, numbers: list[tuple[int, int, str]]) -> set[int]:
    """Start offsets of the numbers that name a table, figure or section."""
    return {
        start
        for start, _end, _token in numbers
        if _REFERENCE_LABEL_RE.search(text[:start])
    }


def _count_beside(
    word_start: int,
    word_end: int,
    numbers: list[tuple[int, int, str]],
    parens: list[tuple[int, int]],
    labelled: set[int],
) -> str | None:
    """The count belonging to a disposition word, or None.

    A number in front of the word wins over a nearer one behind it, because
    English puts it there: in "120 were screened and 100 were enrolled" the
    nearest number to "screened" is 100, and pairing those two would invent a
    disagreement out of a correct sentence.

    Percentages and parenthesised numbers are never headline counts -- "60
    (75.0%) patients completed" states one N and two derived figures -- and
    neither is a number introduced by "Table" or "Section", which is a place
    in the report and not a number of people.
    """
    before = [
        (word_start - end, token)
        for start, end, token in numbers
        if end <= word_start
        and word_start - end <= _LOOKBACK
        and not token.endswith("%")
        and not _inside(start, parens)
        and start not in labelled
    ]
    if before:
        return normalize_number(min(before)[1])
    after = [
        (start - word_end, token)
        for start, end, token in numbers
        if start >= word_end
        and start - word_end <= _LOOKAHEAD
        and not token.endswith("%")
        and start not in labelled
    ]
    if after:
        return normalize_number(min(after)[1])
    return None


def extract_headline_counts(content: str) -> dict[str, list[str]]:
    """{"enrolled": ["120"], ...} -- the counts this section states.

    Values come back normalised, so a section writing "1,234" and one writing
    "1234" are recorded as the same number and cross-section consistency does
    not fire on a comma.
    """
    prepared = _mask(DATA_NEEDED_RE, _mask(CITATION_MARKER_RE, _prepare(content)))
    found: dict[str, list[str]] = {}
    for sentence in split_sentences(prepared):
        numbers = [(m.start(), m.end(), m.group(0)) for m in NUMBER_RE.finditer(sentence)]
        if not numbers:
            continue
        parens = _paren_spans(sentence)
        labelled = _reference_labelled(sentence, numbers)
        for word in _CONCEPT_RE.finditer(sentence):
            value = _count_beside(word.start(), word.end(), numbers, parens, labelled)
            if value is None:
                continue
            bucket = found.setdefault(_canonical_concept(word.group(1)), [])
            if value not in bucket:
                bucket.append(value)
    return found


def _section_sort_key(section_number: str):
    return [
        (0, int(piece)) if piece.isdigit() else (1, piece)
        for piece in str(section_number).split(".")
    ]


def cross_section_consistency(
    counts_by_section: dict[str, dict[str, list[str]]],
) -> list[QcFinding]:
    """Report any headline count the report states two different ways.

    Silence means agreement or absence: one value, however many sections
    repeat it, is consistent. Two values for the same concept is a finding
    even when they come from one section, because "120 randomized" and "118
    randomized" in the same draft is the defect, not a subtlety of scope.
    """
    per_concept: dict[str, dict[str, list[str]]] = {}
    for section_number, counts in (counts_by_section or {}).items():
        for concept, values in (counts or {}).items():
            sections = per_concept.setdefault(concept, {})
            bucket = sections.setdefault(section_number, [])
            for value in values or ():
                if value not in bucket:
                    bucket.append(value)

    findings: list[QcFinding] = []
    for concept in sorted(per_concept):
        by_section = {s: v for s, v in per_concept[concept].items() if v}
        distinct = {value for values in by_section.values() for value in values}
        if len(distinct) < 2:
            continue
        ordered = sorted(by_section, key=_section_sort_key)
        spelled = "; ".join(f"{s} says {', '.join(by_section[s])}" for s in ordered)
        findings.append(QcFinding(
            code=COUNT_DISAGREEMENT,
            section_number=None,
            message=f"The number {concept} disagrees across sections: {spelled}.",
            detail={
                "concept": concept,
                "sections": ordered,
                "values_by_section": {s: list(by_section[s]) for s in ordered},
            },
        ))
    return findings


# -------------------------------------------------------------- declared gaps

def data_needed_payloads(content: str) -> list[str]:
    """What each [DATA NEEDED: ...] in the draft says is missing."""
    return [match.group(1).strip() for match in DATA_NEEDED_RE.finditer(content or "")]


def data_needed_findings(section_number, payloads) -> list[QcFinding]:
    """One finding per distinct declared gap; these block export.

    A gap with an empty payload still counts. Dropping it because the model
    forgot to say what was missing would let a section with a hole in it
    present as complete, which is the exact outcome acceptance criterion 3
    forbids.
    """
    findings: list[QcFinding] = []
    seen: set[str] = set()
    for payload in payloads or ():
        text = str(payload).strip() or "unspecified"
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        where = f"Section {section_number}" if section_number else "The draft"
        findings.append(QcFinding(
            code=DATA_NEEDED,
            section_number=section_number,
            message=f"{where} needs data that the sources do not contain: {text}",
            detail={"payload": text},
        ))
    return findings


# ------------------------------------------------------------ abbreviations

def _expansion_before(text: str, paren_start: int, abbreviation: str) -> str | None:
    """The words in front of "(AE)" that spell it, or None.

    Matched on initials rather than a dictionary, so a compound-specific
    acronym from this study's protocol is expanded as readily as a standard
    one. When no window of words spells the acronym, the answer is None and
    the writer fills it in -- guessing an expansion would put a definition
    into Section 4 that the report never made.
    """
    head = text[:paren_start]
    if head[-1:] not in (" ", "\t"):
        return None
    words = list(_WORD_RE.finditer(head))
    letters = [character.upper() for character in abbreviation if character.isalpha()]
    if not words or not letters:
        return None
    # Allow a few extra words in the window so skipped joiners ("of", "and")
    # still leave enough initials to match.
    for take in range(len(letters), min(len(words), len(letters) + 4) + 1):
        window = words[-take:]
        if window[0].group(0).lower() in _EXPANSION_SKIP:
            continue
        initials = [
            word.group(0)[0].upper()
            for word in window
            if word.group(0).lower() not in _EXPANSION_SKIP
        ]
        if initials == letters:
            return head[window[0].start():window[-1].end()]
    return None


def collect_abbreviations(texts: Iterable[str]) -> list[dict]:
    """Every acronym the report uses, with its expansion where one was written.

    This is CSR Section 4. `expansion` is None when the report never spelled
    the acronym out, which is precisely the omission Section 4 exists to
    surface; the first expansion found wins, because a second, different one
    is an editorial decision and not this module's to make.
    """
    counts: Counter = Counter()
    expansions: dict[str, str] = {}
    for text in texts or ():
        # Markers are masked first, or "[S1, p.4]" contributes an "S1".
        cleaned = _mask(DATA_NEEDED_RE, _mask(CITATION_MARKER_RE, str(text or "")))
        for match in ABBREVIATION_RE.finditer(cleaned):
            token = match.group(1)
            if token in ABBREVIATION_STOPLIST:
                continue
            counts[token] += 1
        for match in _DEFINITION_RE.finditer(cleaned):
            token = match.group(1)
            if token in ABBREVIATION_STOPLIST or token in expansions:
                continue
            expansion = _expansion_before(cleaned, match.start(), token)
            if expansion:
                expansions[token] = expansion
    return [
        {"abbreviation": token, "expansion": expansions.get(token), "count": counts[token]}
        for token in sorted(counts)
    ]


# ---------------------------------------------------------- the per-section run

def section_findings(
    *,
    section_number,
    content: str,
    citations: list[dict],
    chunk_text_by_id: dict[str, str],
) -> list[QcFinding]:
    """Every check that can be answered from one section's own draft.

    Cross-section consistency is deliberately not here: it needs the whole
    report, and running it per section would report a disagreement against
    nothing.
    """
    findings: list[QcFinding] = []
    for sentence, numbers in _uncited_sentences(content or ""):
        findings.append(QcFinding(
            code=UNCITED_NUMBER,
            section_number=section_number,
            message=f"No citation for {', '.join(numbers)} in: {sentence}",
            detail={"numbers": list(numbers), "sentence": sentence},
        ))
    findings.extend(
        replace(finding, section_number=section_number)
        for finding in verify_citation_values(citations or [], chunk_text_by_id or {})
    )
    findings.extend(data_needed_findings(section_number, data_needed_payloads(content or "")))
    return findings
