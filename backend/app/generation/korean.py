"""Korean postposition agreement — deterministic, not a model call.

Korean postpositions alternate on whether the preceding syllable ends in a final
consonant (받침, batchim): 이/가, 을/를, 은/는, 과/와, 으로/로. A template cannot
know which form to print, because the deciding syllable is the value that gets
inserted at generation time. Authors therefore write the alternation explicitly
after the placeholder — `<성명>이(가)` — and this pass resolves it once the value
is in place.

The rule is arithmetic on the Hangul syllable block, not a language model:
precomposed syllables occupy U+AC00..U+D7A3 laid out as
`(initial × 21 + medial) × 28 + final`, so a syllable has a batchim exactly when
`(code - 0xAC00) % 28 != 0`. There is no ambiguity to resolve and no confidence
to score, so putting an LLM anywhere near it would only add a failure mode.
"""

from __future__ import annotations

import re

HANGUL_START, HANGUL_END = 0xAC00, 0xD7A3
_JONGSEONG_COUNT = 28
_RIEUL_JONGSEONG = 8  # ㄹ, the one final consonant 으로/로 treats as vowel-like

# Written as `이(가)`, `을(를)`, or with a slash. Both are in live use.
ALTERNATION_RE = re.compile(r"(이|을|은|과|으로)\s*[（(/]\s*(가|를|는|와|로)\s*[）)]?")

PAIRS = {
    ("이", "가"): "이/가",
    ("을", "를"): "을/를",
    ("은", "는"): "은/는",
    ("과", "와"): "과/와",
    ("으로", "로"): "으로/로",
}


def _final_consonant(word: str) -> int | None:
    """Index of the syllable's final consonant, 0 for none, None if not Hangul."""
    for ch in reversed(word.strip()):
        if ch.isspace():
            continue
        code = ord(ch)
        if HANGUL_START <= code <= HANGUL_END:
            return (code - HANGUL_START) % _JONGSEONG_COUNT
        return None
    return None


def has_batchim(word: str) -> bool:
    final = _final_consonant(word)
    return bool(final)


def choose(word: str, consonant_form: str, vowel_form: str) -> str:
    """The form that agrees with `word`.

    A value that does not end in Hangul — a Latin name, a number, an email
    address — has no batchim to read. The vowel form is chosen as the
    conventional default and the caller flags it, because "Alex이" and "Alex가"
    are both wrong for some readers and only a human can say which the client
    prefers.
    """
    final = _final_consonant(word)
    if final is None:
        return vowel_form
    if (consonant_form, vowel_form) == ("으로", "로"):
        # ㄹ-final behaves as if there were no batchim: 서울로, not 서울으로.
        return vowel_form if final in (0, _RIEUL_JONGSEONG) else consonant_form
    return consonant_form if final else vowel_form


def resolve(text: str) -> tuple[str, list[str]]:
    """Rewrite every `이(가)`-style alternation in `text` against what precedes it.

    Returns the rewritten text and the list of values whose script could not
    decide the form, for the decision report.
    """
    ambiguous: list[str] = []

    def replace(m: re.Match) -> str:
        consonant_form, vowel_form = m.group(1), m.group(2)
        if (consonant_form, vowel_form) not in PAIRS:
            return m.group(0)
        preceding = text[: m.start()]
        word = preceding.rstrip()
        if not word:
            return m.group(0)
        if _final_consonant(word) is None:
            ambiguous.append(word[-12:])
        return choose(word, consonant_form, vowel_form)

    return ALTERNATION_RE.sub(replace, text or ""), ambiguous
