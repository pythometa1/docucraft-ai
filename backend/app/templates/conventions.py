"""Convention families -- the grammar a set of client masters share.

A family is selected by what a template *looks like*, never by which template it
is. That distinction is the whole scaling story: a thousand masters from a dozen
authoring teams collapse onto a handful of grammars, so onboarding a conforming
template costs a profile run and a golden fixture rather than a code change. A
non-conforming one costs one YAML file in `app/conventions/`, which then covers
every future template written the same way.

The patterns in `en_annotated.yaml` were module constants in `manifest_compiler`
until this file existed, and were moved verbatim.
`test_conventions_and_safety.py::test_the_default_family_reads_the_english_grammar`
asserts they still read the strings taken from the real client masters, so the
extraction is provably a refactor rather than a rewrite.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

CONVENTIONS_DIR = Path(__file__).resolve().parent.parent / "conventions"
DEFAULT_FAMILY = "en_annotated"


def normalise_text(text: str) -> str:
    """NFKC, which is what makes one family config serve mixed-width authoring.

    A CJK master writes its placeholders and colons fullwidth -- `＜姓名＞`, `：`,
    `３` -- and those are different codepoints from their ASCII twins, so a
    delimiter pattern written in ASCII matches nothing at all. NFKC folds the
    compatibility forms (fullwidth Latin and digits, ideographic space, fullwidth
    punctuation) onto their canonical equivalents, so the same pattern reads both.
    Applied to slugs and to instruction text before matching; never to the text
    written into a letter, which stays exactly as the template had it.
    """
    return unicodedata.normalize("NFKC", text or "")


@dataclass(frozen=True)
class Delimiter:
    open: str
    close: str
    kind: str = "angle"


@dataclass
class ConventionFamily:
    id: str
    language: str = "en"
    description: str = ""
    delimiters: list = field(default_factory=list)

    include_re: re.Pattern | None = None
    include_groups: dict = field(default_factory=lambda: {"till": 1, "field": 2, "value": 3})
    for_entity_first_re: re.Pattern | None = None
    for_value_first_re: re.Pattern | None = None
    for_start_re: re.Pattern | None = None
    # Which capture group holds which half. English puts the entity noun first
    # in one form and last in the other; Chinese puts the value first in both.
    # Naming the groups keeps the word order a property of the language rather
    # than an assumption baked into the parser.
    for_entity_first_groups: dict = field(default_factory=lambda: {"entity": 1, "value": 2})
    for_value_first_groups: dict = field(default_factory=lambda: {"entity": 2, "value": 1})

    entity_field_suffix: str = "_type"
    entity_field_overrides: dict = field(default_factory=dict)

    gloss_re: re.Pattern | None = None
    leading_qualifier_re: re.Pattern | None = None
    put_re: re.Pattern | None = None

    # Text that READS like a branch instruction in this family's language.
    # Matching raises W-MARKER-PARSE, which blocks approval and escalates the
    # template to a model; it compiles nothing by itself.
    #
    # A family property, not a module constant, because the vocabulary is the
    # thing that varies between customers. One estate shouts "USE IF ON
    # TEMPORARY ASSIGNMENT" above the clause, another parenthesises "(Include if
    # part time)" inside it, a third writes neither. Hardcoding one estate's
    # phrasing in the compiler makes the next customer a code change; declaring
    # it here makes them a YAML file, which is the whole point of families.
    instruction_shape_re: re.Pattern | None = None
    instruction_phrases: list = field(default_factory=list)
    heading_max_len: int = 60
    # Korean postposition agreement; see services/korean.py. Off everywhere else,
    # because the pass rewrites text and must never run on a language whose
    # authors did not ask for it.
    particles: bool = False

    def bracket_re(self) -> re.Pattern:
        """One alternation over every delimiter pair this family declares.

        Built from the family rather than hardcoded so a CJK master's `＜...＞`
        and `《...》` are found by the same single pass that finds `<...>`.
        """
        parts = []
        for d in self.delimiters:
            o, c = re.escape(d.open), re.escape(d.close)
            parts.append(rf"{o}([^{o}{c}\n]{{1,80}}){c}")
        return re.compile("|".join(parts) if parts else r"<([^<>\n]{1,80})>")

    def entity_field(self, noun: str) -> str:
        n = normalise_text(noun).strip().lower()
        if n in self.entity_field_overrides:
            return self.entity_field_overrides[n]
        return f"{n[:-1] if n.endswith('s') else n}{self.entity_field_suffix}"


def _compile(pattern: str | None, **subs) -> re.Pattern | None:
    if not pattern:
        return None
    for key, value in subs.items():
        pattern = pattern.replace("{" + key + "}", value)
    return re.compile(pattern, re.IGNORECASE)


def _from_dict(data: dict) -> ConventionFamily:
    entity_nouns = data.get("entity_nouns", "")
    return ConventionFamily(
        id=data["id"],
        language=data.get("language", "en"),
        description=data.get("description", ""),
        delimiters=[Delimiter(**d) for d in data.get("delimiters", [])],
        include_re=_compile(data.get("include_pattern")),
        include_groups=data.get("include_groups", {"till": 1, "field": 2, "value": 3}),
        for_entity_first_re=_compile(data.get("for_entity_first_pattern"), entity_nouns=entity_nouns),
        for_value_first_re=_compile(data.get("for_value_first_pattern"), entity_nouns=entity_nouns),
        for_start_re=_compile(data.get("for_start_pattern")),
        for_entity_first_groups=data.get("for_entity_first_groups") or {"entity": 1, "value": 2},
        for_value_first_groups=data.get("for_value_first_groups") or {"entity": 2, "value": 1},
        entity_field_suffix=data.get("entity_field_suffix", "_type"),
        entity_field_overrides={k.lower(): v for k, v in (data.get("entity_field_overrides") or {}).items()},
        gloss_re=_compile(data.get("gloss_pattern")),
        leading_qualifier_re=_compile(data.get("leading_qualifier_pattern")),
        put_re=_compile(data.get("put_pattern")),
        instruction_shape_re=_compile(data.get("instruction_shape_pattern")),
        instruction_phrases=list(data.get("instruction_phrases") or []),
        heading_max_len=int(data.get("heading_max_len", 60)),
        particles=bool(data.get("particles", False)),
    )


@lru_cache(maxsize=None)
def load_family(family_id: str = DEFAULT_FAMILY) -> ConventionFamily:
    path = CONVENTIONS_DIR / f"{family_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No convention family '{family_id}' in {CONVENTIONS_DIR}")
    return _from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def all_families() -> tuple:
    return tuple(sorted(p.stem for p in CONVENTIONS_DIR.glob("*.yaml")))


def detect_family(prescan, sample_text: str = "") -> ConventionFamily:
    """Pick the family whose grammar the template actually speaks.

    Scored on instruction matches rather than on language detection: a bilingual
    master, or an English template a Chinese team has partly localised, belongs
    to whichever grammar reads its markers. Ties fall back to the default, which
    is also what an estate of one language will always resolve to.
    """
    text = sample_text or "\n".join(
        s.text for s in getattr(prescan, "spans", []) if getattr(s, "color", "") == "red"
    )
    text = normalise_text(text)

    best, best_score = load_family(DEFAULT_FAMILY), 0
    for family_id in all_families():
        family = load_family(family_id)
        score = sum(
            1 for pattern in (family.include_re, family.for_entity_first_re, family.for_value_first_re)
            if pattern is not None and pattern.search(text)
        )
        if score > best_score:
            best, best_score = family, score
    return best
