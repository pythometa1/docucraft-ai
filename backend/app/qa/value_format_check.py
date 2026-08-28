"""Does the finished sentence read like something a person would sign?

The other gates in this package each read one half of the document. The
scaffolding checks ask whether the template's machinery went; the lineage
checks ask whether the record's values arrived; `layout_integrity` asks whether
the package is still the approved one. None of them reads the sentence that the
template and the record produce *together*, and that join is where this defect
lives:

    "Your base salary will be $AUD 76,800 per annum per annum."

Two faults in eleven words, on a letter this pipeline reported as
`qa_passed: True`. The template writes "$<Base Salary> per annum". The record
supplied "AUD 76,800 per annum". Neither half is wrong on its own -- a template
is right to carry the currency marker and the unit its sentence needs, and a
source system is right to say what its number means -- so no check that inspects
one half in isolation can see it. Only the rendered text can, which is why this
check runs on the generated body rather than on the manifest or the record.

Three shapes, all of them the same defect: something the sentence already said
gets said again when the value lands.

  * A unit repeated -- "per annum per annum", "per week per week", and the
    cross-notation form "per annum p.a." where the template and the value spell
    the same unit differently.
  * A currency marker repeated -- "$AUD", "$ USD", "$$76,800", "INR INR
    12,00,000". The template supplies a symbol, the value arrives carrying a
    code, and the amount ends up marked twice.
  * A word repeated -- "to to", "the the".

Two check names rather than one, because severity is a property of the name and
these failures do not deserve the same one.

`value_format_doubled` covers the units and the currency markers. No template
author writes "per annum per annum" and no source system means "$AUD" -- these
strings exist only because two halves were joined, so they are always the
engine's fault and always wrong on the page. Blocking is the right cost.

`doubled_word` is different, and pretending otherwise would wreck the check.
The template this defect was found in contains "offer to to the position", and
has contained it since before this pipeline existed. It is the customer's typo,
faithfully reproduced. Blocking a correct letter on it teaches reviewers that
this gate cries wolf, and a gate people override by habit protects nothing. So:

  * when the source template is available, a doubling the template already
    contains is not reported at all -- only the ones the fill *added*, counted
    per repeated word, are; and
  * when it is not available, the doubling is still reported but the note says
    so in as many words, because silence here would miss the real ones.

Nothing in the word list is clever. English legitimately repeats a few words
("had had", "that that") and this estate's own colleagues work in Wagga Wagga
and Woy Woy, so those are exempt; everything else adjacent and identical is
reported. A false positive on a name or a place is cheap to produce and
expensive in trust.

What this check does not do, stated plainly:

  * It does not repair anything. "$AUD 76,800" could be rewritten to "AUD
    76,800" mechanically, and that is precisely the kind of silent correction
    that makes a wrong number look deliberate. The reviewer is told; the
    document is not edited.
  * It does not report "AUD $76,800" or "$76,800 AUD". Both are conventional --
    the INR letters in this suite write "INR 12,00,000.00 per annum", and
    Australian usage writes "AUD $76,800" -- and a blocking finding on ordinary
    house style is worse than the miss.
  * It reads visible text only. A doubling split across two paragraphs is not a
    doubling on the page, so paragraph text is the unit of comparison.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.qa.policy import QaFinding, QaPolicy, findings_for
from app.templates.parsers.docx_prescan import W_NS, _walk_paragraphs

#: Doubled units and doubled currency markers: composition artifacts that cannot
#: occur in a hand-written letter. Intended to be registered blocking.
VALUE_FORMAT_DOUBLED = "value_format_doubled"

#: Adjacent identical words. Intended to be registered as a warning: it can be
#: the customer's typo rather than the engine's, and it says which when it can.
DOUBLED_WORD = "doubled_word"

# Units that mean the same thing spelled differently. Grouped, so that the
# template's "per annum" followed by a value's "p.a." is caught as the one
# defect it is rather than passing because the two strings differ.
UNIT_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("per annum", "per year", "p.a."),
    ("per month", "per calendar month"),
    ("per fortnight",),
    ("per week",),
    ("per day",),
    ("per hour",),
)

# Deliberately not in the list: "p.m." (a time of day), "pa" and "pw" (too many
# ordinary words), "annually" and "weekly" (adverbs that sit legitimately beside
# a unit -- "paid weekly, calculated per week" is correct prose).

CURRENCY_SYMBOLS = "$£€¥₹"

# ISO codes this estate's templates and source systems actually emit. Matched
# case-sensitively: every one of these is written upper-case in a letter, and
# lower-casing them would put ordinary words ("cad", "sek") into the pattern.
CURRENCY_CODES = (
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD",
    "IDR", "INR", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN",
    "SEK", "SGD", "THB", "USD", "VND", "ZAR",
)

# Words that repeat legitimately. The first two are ordinary English; the rest
# are place names an Australian colleague record supplies as a work location,
# which is exactly the kind of value that lands in a letter.
LEGITIMATE_REPEATS = frozenset({
    "had", "that",
    "wagga", "woy", "walla", "baden", "bora", "pago",
})

#: Characters allowed between the two halves of a doubling. Whitespace because
#: runs and tabs are joined verbatim; a comma because "per annum, per annum" is
#: the same defect wearing punctuation. A full stop is *not* here: "...per
#: annum. Per annum figures follow" is two sentences, not one doubling.
_SEPARATOR = r"[,\s]+"

#: How much of the paragraph goes into the note on each side of the match. Enough
#: for a reviewer to find the line by eye in Word without opening a debugger.
CONTEXT_CHARS = 45


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _phrase(phrase: str) -> str:
    """A literal phrase as a pattern whose spaces tolerate any whitespace run."""
    return r"\s+".join(re.escape(word) for word in phrase.split())


def _alternation(phrases: Sequence[str]) -> str:
    # Longest first, so "per calendar month" is preferred over "per month".
    return "|".join(_phrase(p) for p in sorted(phrases, key=len, reverse=True))


UNIT_RES = tuple(
    re.compile(
        rf"(?<![A-Za-z])(?:{_alternation(group)}){_SEPARATOR}(?:{_alternation(group)})(?![A-Za-z])",
        re.IGNORECASE,
    )
    for group in UNIT_SYNONYMS
)

_CODES = "|".join(CURRENCY_CODES)
_SYM = f"[{re.escape(CURRENCY_SYMBOLS)}]"

CURRENCY_RES = (
    # "$AUD 76,800" -- the one that shipped. A symbol immediately followed by a
    # code marks the amount twice, and no house style writes it that way.
    re.compile(rf"{_SYM}\s*(?:{_CODES})(?![A-Za-z])"),
    # "$$76,800": template symbol, value symbol.
    re.compile(rf"{_SYM}\s*{_SYM}"),
    # "INR INR 12,00,000": template code, value code. Reported here rather than
    # as a doubled word because a repeated currency code is never a typo in
    # prose -- it is two systems both naming the currency.
    re.compile(rf"(?<![A-Za-z])({_CODES})\s+\1(?![A-Za-z])"),
    # The reverse, but only where it reads wrong. "AUD $76,800" is conventional
    # and is left alone; "AUD $" with no amount behind it is a stranded marker.
    re.compile(rf"(?<![A-Za-z])(?:{_CODES})\s*{_SYM}(?!\s*\d)"),
)

DOUBLED_WORD_RE = re.compile(r"(?<![\w'’-])([A-Za-z]{2,})\s+\1(?![\w'’-])", re.IGNORECASE)


@dataclass(frozen=True)
class Doubling:
    """One repetition, located well enough to be fixed."""

    kind: str  # "unit" | "currency" | "word"
    text: str  # what is doubled, as it appears in the document
    paragraph: int  # index into `_walk_paragraphs`, the pipeline's paragraph numbering
    context: str
    #: For words only. True == the fill added it, False == the template already
    #: had it (and it is therefore not reported), None == no template to compare.
    introduced_by_fill: bool | None = None
    #: How many times the source template contains this same doubling.
    template_occurrences: int | None = None


def paragraph_texts(body_el) -> list[str]:
    """Every paragraph's visible text, runs joined the way Word draws them.

    Not `placeholder_check.document_text`. That joins every `<w:t>` with a
    newline, which is right for finding a token inside one run and wrong here:
    a filled value is its own run, so "$" + "AUD 76,800" + " per annum" is three
    runs, and newline-joining them hides the very adjacency this check exists to
    find. Tabs and line breaks become whitespace so that a doubling separated by
    one is still visible, and so that two words either side of a tab do not fuse
    into a word that was never on the page.
    """
    texts = []
    paragraphs, _table_flags = _walk_paragraphs(body_el)
    for p_el in paragraphs:
        parts = []
        for el in p_el.iter():
            if el.tag == _q("t"):
                parts.append(el.text or "")
            elif el.tag == _q("tab"):
                parts.append("\t")
            elif el.tag in (_q("br"), _q("cr")):
                parts.append("\n")
        texts.append("".join(parts))
    return texts


def _context(text: str, start: int, end: int) -> str:
    """The match with its neighbours, whitespace collapsed onto one line."""
    left = text[max(0, start - CONTEXT_CHARS):start]
    right = text[end:end + CONTEXT_CHARS]
    prefix = "..." if start - CONTEXT_CHARS > 0 else ""
    suffix = "..." if end + CONTEXT_CHARS < len(text) else ""
    return prefix + " ".join((left + text[start:end] + right).split()) + suffix


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _non_overlapping(matches: list) -> list:
    """One finding per distinct problem: "$$AUD" is one mess, not two.

    Matches from different patterns can cover the same characters, and a
    reviewer reading two notes about one string has to work out that they are
    one string.
    """
    kept: list = []
    for match in sorted(matches, key=lambda m: (m.start(), -m.end())):
        if kept and match.start() < kept[-1].end():
            continue
        kept.append(match)
    return kept


def format_doublings(paragraphs: Sequence[str]) -> list[Doubling]:
    """Doubled units and doubled currency markers, in document order."""
    found: list[Doubling] = []
    for index, text in enumerate(paragraphs):
        matches = [m for pattern in UNIT_RES for m in pattern.finditer(text)]
        units = {(m.start(), m.end()) for m in matches}
        matches += [m for pattern in CURRENCY_RES for m in pattern.finditer(text)]
        for match in _non_overlapping(matches):
            found.append(
                Doubling(
                    kind="unit" if (match.start(), match.end()) in units else "currency",
                    text=_collapse(match.group(0)),
                    paragraph=index,
                    context=_context(text, match.start(), match.end()),
                )
            )
    return found


def _template_repeat_counts(paragraphs: Sequence[str]) -> Counter:
    counts: Counter = Counter()
    for text in paragraphs:
        for match in DOUBLED_WORD_RE.finditer(text):
            counts[match.group(1).lower()] += 1
    return counts


def word_doublings(
    paragraphs: Sequence[str],
    template_paragraphs: Sequence[str] | None = None,
) -> list[Doubling]:
    """Adjacent identical words the fill is answerable for.

    When `template_paragraphs` is given, a repeated word is reported only from
    the occurrence after the template's own count of it -- the template's
    "offer to to the position" is the customer's, and the letter that copies it
    once has added nothing. A doubling the fill introduced somewhere else takes
    the count above the template's and is reported.

    That accounting is deliberately per repeated word rather than per location.
    Paragraphs are deleted during the fill, so template and letter do not share
    a paragraph numbering, and matching by position would report every doubling
    in any letter whose optional clauses were dropped.
    """
    template_counts = _template_repeat_counts(template_paragraphs) if template_paragraphs is not None else None
    seen: Counter = Counter()
    found: list[Doubling] = []
    for index, text in enumerate(paragraphs):
        # A repeated currency code is already reported, with the right severity,
        # by `format_doublings`. Two notes on one defect is one note too many.
        claimed = [(m.start(), m.end()) for m in _non_overlapping(
            [m for pattern in CURRENCY_RES for m in pattern.finditer(text)]
        )]
        for match in DOUBLED_WORD_RE.finditer(text):
            word = match.group(1).lower()
            if word in LEGITIMATE_REPEATS:
                continue
            if any(match.start() < end and start < match.end() for start, end in claimed):
                continue
            seen[word] += 1
            if template_counts is None:
                introduced, occurrences = None, None
            else:
                occurrences = template_counts[word]
                if seen[word] <= occurrences:
                    continue
                introduced = True
            found.append(
                Doubling(
                    kind="word",
                    text=_collapse(match.group(0)),
                    paragraph=index,
                    context=_context(text, match.start(), match.end()),
                    introduced_by_fill=introduced,
                    template_occurrences=occurrences,
                )
            )
    return found


def _format_note(d: Doubling) -> str:
    label = "unit" if d.kind == "unit" else "currency marker"
    return (
        f"Doubled {label} {d.text!r} in paragraph {d.paragraph} of the generated document: "
        f"\"{d.context}\""
    )


def _word_note(d: Doubling) -> str:
    if d.template_occurrences is None:
        origin = (
            "; the source template was not compared, so this may be a typo in the template "
            "rather than something the fill introduced"
        )
    elif d.template_occurrences == 0:
        origin = "; not present in the source template, so the fill introduced it"
    else:
        origin = (
            f"; the source template contains this doubling {d.template_occurrences} time(s) and "
            "the letter contains more, so at least one was introduced by the fill"
        )
    return (
        f"Doubled word {d.text!r} in paragraph {d.paragraph} of the generated document{origin}: "
        f"\"{d.context}\""
    )


def format_failures(paragraphs: Sequence[str]) -> list[str]:
    return [_format_note(d) for d in format_doublings(paragraphs)]


def word_failures(
    paragraphs: Sequence[str],
    template_paragraphs: Sequence[str] | None = None,
) -> list[str]:
    return [_word_note(d) for d in word_doublings(paragraphs, template_paragraphs)]


def findings(body_el, policy: QaPolicy, template_body_el=None) -> list[QaFinding]:
    """Both doubling gates over the rendered body.

    `template_body_el` is the *source* template's body -- at the call site,
    `docx.Document(template_path).element.body`. Passing it is what lets the
    word gate tell the customer's typo apart from the engine's; omitting it
    leaves the gate running and honest about not knowing which it found.
    """
    paragraphs = paragraph_texts(body_el)
    template_paragraphs = paragraph_texts(template_body_el) if template_body_el is not None else None

    out: list[QaFinding] = []
    if policy.runs(VALUE_FORMAT_DOUBLED):
        out += findings_for(VALUE_FORMAT_DOUBLED, format_failures(paragraphs), policy)
    if policy.runs(DOUBLED_WORD):
        out += findings_for(DOUBLED_WORD, word_failures(paragraphs, template_paragraphs), policy)
    return out
