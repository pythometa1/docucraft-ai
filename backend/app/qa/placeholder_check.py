"""Did any of the template's scaffolding survive into the finished letter?

Three rows of the §17 per-generation QA table are the same question asked of
different notations -- "Placeholder remains", "Unexpected instruction text
remains", and the MERGEFIELD case behind them both. All three describe a
document that reached a person with the template's own machinery printed in it:
`<Colleague First Name>` where a name should be, `[[ENDIF]]` at the end of a
clause, `Put the salary from the source file` in the middle of a paragraph, or
Word's own «FIELD_CODE» because the merge never ran.

Two of these gates used to be unreliable in ways that were invisible from the
outside, and the shape of this module is a direct consequence.

The MERGEFIELD gate searched the document's visible text for the word
"MERGEFIELD". That string is never in the visible text: Word displays the
cached *result* of a field, so the check could essentially never fire and a
letter could ship with raw field codes in it and be reported clean. The
instruction lives in `<w:instrText>`, and that is what is read here.

The bracket and control-token gates read `Document.paragraphs`, which returns
only top-level body paragraphs and does not descend into tables -- so the
remuneration table, the part of an offer letter a reader checks first, sat
outside QA's field of view entirely. Every check here walks `<w:t>` across the
whole body instead, which covers table cells, nested tables and text boxes.
"""

import re
from dataclasses import dataclass, field

from app.compiler.rule_compiler import CONTROL_TOKEN_RE
from app.qa.policy import (
    CONTROL_TOKEN_REMAINS,
    INSTRUCTION_TEXT_REMAINS,
    PLACEHOLDER_REMAINS,
    UNRESOLVED_MERGEFIELD,
    QaFinding,
    QaPolicy,
    findings_for,
)
from app.templates.parsers.docx_prescan import W_NS, extract_brackets

# Prose addressed to whoever assembles the letter, surviving into the letter
# itself. Its presence means a block was never resolved.
#
# This was one pattern -- "include the following text|section" -- and it matched
# almost nothing these templates actually write. A real generated offer letter
# shipped carrying "(Remove all table once used)" and the gate stayed silent,
# because the estate writes its instructions in two other shapes entirely.
#
# The first is a parenthesised imperative: "(Include if working part time
# hours)", "(Remove all table once used)", "(E.G. Monday (7.2 hours)". The word
# boundary after the verb is what separates the imperative from the same verb
# inflected as prose: it is the difference between "(Include if part time)" and
# "(included in your remuneration package)", "(includes superannuation)",
# "(used for reference only)". Those are ordinary contract parentheticals, and
# blocking a letter on one would teach the reviewer to wave the gate through.
INSTRUCTION_PAREN_RE = re.compile(
    r"\((?:include|remove|insert|delete|use|choose|select|e\.?g\.?)\b[^)]{0,140}\)",
    re.IGNORECASE,
)

# The second is a shouted line standing alone above the clause it governs:
# "USE IF ON TEMPORARY ASSIGNMENT", "INCLUDE IF COLLEAGUE TYPE IS FIXED TERM".
# Matched only when the line is overwhelmingly upper-case, because "Use of
# company property" is a heading and "Include your employee number" could be an
# instruction to the colleague rather than to the author.
INSTRUCTION_SHOUT_RE = re.compile(
    r"^\s*(?:use|include|always include|remove|delete|insert)\b[^.]{0,120}$",
    re.IGNORECASE | re.MULTILINE,
)

#: How upper-case a line must be to read as shouted scaffolding rather than prose.
SHOUT_RATIO = 0.8

# Kept for callers that imported it, and still true: this phrasing is an
# instruction wherever it appears.
INCLUDE_RE = re.compile(r"include the following (?:text|section)", re.IGNORECASE)


def _is_shouted(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > SHOUT_RATIO


def instruction_text_in(text: str) -> list[str]:
    """Every instruction line the rendered document still carries.

    Verified against the six golden letters in tests/fixtures: zero matches on
    all of them, and one match -- the real leak -- on the offer letter that
    shipped with it. A gate that fires on correct documents gets overridden, and
    an overridden gate protects nothing, so the false-positive rate is the
    number that matters here rather than the recall.
    """
    found = [m.group(0).strip() for m in INSTRUCTION_PAREN_RE.finditer(text)]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and _is_shouted(stripped) and INSTRUCTION_SHOUT_RE.match(stripped):
            found.append(stripped)
    if INCLUDE_RE.search(text):
        found.append("include the following text")
    # Deduplicated but order-preserving: the same instruction repeated in six
    # places is one defect, and a reviewer reads the first example.
    return list(dict.fromkeys(found))


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


@dataclass
class PlaceholderScan:
    """What the finished document still carries. Lists, not counts, so a note
    can name the offending tokens rather than only how many there were."""

    leftover_brackets: list = field(default_factory=list)
    unresolved_mergefields: list = field(default_factory=list)
    leftover_control_tokens: list = field(default_factory=list)
    instruction_text_found: bool = False
    #: The instructions themselves, so the finding can name one instead of
    #: telling a reviewer only that something is wrong somewhere.
    instruction_texts: tuple = ()

    @property
    def clean(self) -> bool:
        return not (
            self.leftover_brackets
            or self.unresolved_mergefields
            or self.leftover_control_tokens
            or self.instruction_text_found
        )


def document_text(body_el) -> str:
    """Every `<w:t>` in the body, newline-joined, tables and all.

    Newlines rather than spaces: a token split across two paragraphs is not one
    token, and joining without a separator would invent placeholders that are
    not there.
    """
    return "\n".join(t.text or "" for t in body_el.iter(_q("t")))


def unresolved_mergefields(body_el) -> list:
    """MERGEFIELD instructions still present in the rendered document.

    Resolving a mergefield deletes its whole complex-field run sequence, so a
    surviving `instrText` is by construction one the fill never reached.
    """
    return sorted({
        (el.text or "").replace("MERGEFIELD", "").replace("\\* MERGEFORMAT", "").strip()
        for el in body_el.iter(_q("instrText"))
        if "MERGEFIELD" in (el.text or "")
    })


def scan(body_el) -> PlaceholderScan:
    """Read the rendered body once and report every kind of leftover scaffolding."""
    text = document_text(body_el)
    return PlaceholderScan(
        leftover_brackets=extract_brackets(text),
        unresolved_mergefields=unresolved_mergefields(body_el),
        leftover_control_tokens=CONTROL_TOKEN_RE.findall(text),
        instruction_text_found=bool(instruction_text_in(text)),
        instruction_texts=tuple(instruction_text_in(text)),
    )


def findings(body_el, policy: QaPolicy) -> list[QaFinding]:
    """The §17 scaffolding gates, in the order a reviewer reads them.

    Only the first five offenders of each kind are named. A document that leaks
    forty placeholders has one defect, not forty, and a note long enough to fill
    a screen is a note people stop reading.
    """
    found = scan(body_el)
    out: list[QaFinding] = []
    out += findings_for(
        PLACEHOLDER_REMAINS,
        [f"Leftover placeholder brackets: {found.leftover_brackets[:5]}"] if found.leftover_brackets else [],
        policy,
    )
    out += findings_for(
        UNRESOLVED_MERGEFIELD,
        [f"Unresolved mergefields left in the document: {found.unresolved_mergefields[:5]}"]
        if found.unresolved_mergefields
        else [],
        policy,
    )
    out += findings_for(
        CONTROL_TOKEN_REMAINS,
        [f"Leftover control tokens: {found.leftover_control_tokens[:5]}"] if found.leftover_control_tokens else [],
        policy,
    )
    out += findings_for(
        INSTRUCTION_TEXT_REMAINS,
        [f"Leftover instruction text: {t}" for t in found.instruction_texts[:5]],
        policy,
    )
    return out
