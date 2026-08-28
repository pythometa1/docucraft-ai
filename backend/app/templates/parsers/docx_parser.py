"""Heading-based section-tree extraction for imported DOCX templates (spec §7.3).

Simplification vs. the full spec: we do not read raw OOXML for SDT/content-control
or sectPr manipulation here -- we support the `heading` and `jinja` template kinds
(covering the HR-letter case end-to-end: jinja fields + heading sections), which is
what the actual mapping wizard (`{employee_name}` style vars) exercises. `sdt`
(content-control) parsing is a documented extension point, not implemented in this
MVP -- see docs/BACKEND_SPEC.md §7.2.
"""

import hashlib
import re
from dataclasses import dataclass, field

import docx
from docx.oxml.ns import qn

JINJA_VAR_RE = re.compile(r"\{\{?\s*([a-zA-Z_][a-zA-Z0-9_ ]{1,40})\s*\}?\}")

HEADING_STYLE_RE = re.compile(r"^Heading\s*([1-6])$", re.IGNORECASE)
# Style *ids* are language-independent -- OOXML stores `Heading1` regardless of
# the UI language Word was running in.
HEADING_STYLE_ID_RE = re.compile(r"^Heading\s*([1-6])$", re.IGNORECASE)

# Display names of the built-in heading styles in the languages this estate is
# most likely to meet. True built-ins keep their canonical English `w:name` in
# styles.xml, so this is a safety net for author-defined styles that merely look
# like headings -- not the primary signal.
LOCALIZED_HEADING_PREFIXES = (
    "überschrift", "uberschrift",   # de
    "titre",                        # fr
    "título", "titulo",             # es / pt
    "titolo",                       # it
    "kop",                          # nl
    "rubrik",                       # sv / da
    "otsikko",                      # fi
    "nagłówek", "naglowek",         # pl
    "заголовок",                    # ru / uk
    "başlık", "baslik",             # tr
    "見出し",                        # ja
    "标题", "標題",                  # zh
    "제목",                          # ko
    "عنوان",                        # ar
)


def _outline_level(paragraph) -> int | None:
    """Read `w:outlineLvl` from the paragraph, then from its style definition.

    This is the signal Word itself uses to build a table of contents, so it is
    the most trustworthy structural marker when style naming is unreliable.
    """
    holders = []
    p_pr = paragraph._p.find(qn("w:pPr"))
    if p_pr is not None:
        holders.append(p_pr)
    style_el = getattr(paragraph.style, "element", None)
    if style_el is not None:
        style_pr = style_el.find(qn("w:pPr"))
        if style_pr is not None:
            holders.append(style_pr)
    for holder in holders:
        lvl = holder.find(qn("w:outlineLvl"))
        if lvl is not None:
            try:
                return int(lvl.get(qn("w:val")))
            except (TypeError, ValueError):
                return None
    return None


def heading_level(paragraph) -> int | None:
    """Return 1-6 when this paragraph is a heading, else None.

    Matching on the English style *name* alone is not safe for a multi-language
    template estate. Word stores the canonical English `w:name` ("heading 1") for
    true built-ins even when authored in a localized UI, but a template carrying
    author-defined styles can report "Überschrift 1" -- and a missed heading is
    not a small error: the document parses to zero sections and becomes invisible
    to the section-mapping engine entirely.

    Signals are tried in order of reliability: style id, explicit outline level,
    then the style name in English or a known localization.
    """
    style = paragraph.style
    if style is not None:
        style_id = (getattr(style, "style_id", "") or "").strip()
        match = HEADING_STYLE_ID_RE.match(style_id)
        if match:
            return int(match.group(1))

        name = (style.name or "").strip()
        match = HEADING_STYLE_RE.match(name)
        if match:
            return int(match.group(1))

        lowered = name.casefold()
        if any(lowered.startswith(prefix) for prefix in LOCALIZED_HEADING_PREFIXES):
            digits = re.search(r"([1-6])", name)
            if digits:
                return int(digits.group(1))

    level = _outline_level(paragraph)
    if level is not None and 0 <= level <= 5:
        return level + 1  # w:outlineLvl is 0-based
    return None


@dataclass
class ParsedSection:
    level: int
    title: str
    section_path: str
    order_index: int
    start_el: int
    end_el: int
    example_text: str
    fillable: bool
    fingerprint: str


@dataclass
class ParsedTemplate:
    kind: str
    sections: list[ParsedSection] = field(default_factory=list)
    jinja_vars: list[str] = field(default_factory=list)


def _fingerprint(level: int, title: str, parent_fp: str) -> str:
    normalized = title.strip().lower()
    return hashlib.sha256(f"{parent_fp}|{level}|{normalized}".encode()).hexdigest()


def parse_docx_template(path: str) -> ParsedTemplate:
    document = docx.Document(path)
    paragraphs = document.paragraphs

    # Detect jinja-style variables across the whole document body text.
    full_text = "\n".join(p.text for p in paragraphs)
    jinja_vars = sorted(set(m.group(0) for m in JINJA_VAR_RE.finditer(full_text)))

    open_stack: list[dict] = []  # {level, title, start_el, texts: []}
    closed: list[dict] = []

    def close_from(level: int, end_el: int):
        while open_stack and open_stack[-1]["level"] >= level:
            node = open_stack.pop()
            node["end_el"] = end_el
            closed.append(node)

    for i, p in enumerate(paragraphs):
        level = heading_level(p)
        if level is not None:
            close_from(level, i - 1)
            open_stack.append({"level": level, "title": p.text.strip() or f"Untitled section {i}", "start_el": i, "texts": []})
        else:
            if open_stack:
                open_stack[-1]["texts"].append(p.text)

    close_from(0, len(paragraphs) - 1)
    closed.sort(key=lambda n: n["start_el"])

    # number siblings per level to build section_path, e.g. "1", "1.1", "1.2", "2"
    counters: dict[int, int] = {}
    parent_stack: list[tuple[int, str]] = []  # (level, path)
    sections: list[ParsedSection] = []
    fp_stack: list[tuple[int, str]] = [(0, "root")]

    for order_index, node in enumerate(closed):
        level = node["level"]
        # pop deeper/equal levels from parent_stack
        while parent_stack and parent_stack[-1][0] >= level:
            parent_stack.pop()
        while fp_stack and fp_stack[-1][0] >= level:
            fp_stack.pop()

        counters[level] = counters.get(level, 0) + 1
        for deeper in [l for l in counters if l > level]:
            counters[deeper] = 0

        prefix = parent_stack[-1][1] + "." if parent_stack else ""
        number = f"{prefix}{counters[level]}"
        section_path = f"{number} {node['title']}"
        parent_stack.append((level, number))

        parent_fp = fp_stack[-1][1]
        fp = _fingerprint(level, node["title"], parent_fp)
        fp_stack.append((level, fp))

        example_text = "\n".join(t for t in node["texts"] if t.strip())[:500]
        lowered = node["title"].strip().lower()
        fillable = lowered not in {"table of contents", "cover page", "signature"}

        sections.append(
            ParsedSection(
                level=level,
                title=node["title"],
                section_path=section_path,
                order_index=order_index,
                start_el=node["start_el"],
                end_el=node["end_el"],
                example_text=example_text,
                fillable=fillable,
                fingerprint=fp,
            )
        )

    if jinja_vars and sections:
        kind = "mixed"
    elif jinja_vars:
        kind = "jinja"
    else:
        kind = "heading"

    return ParsedTemplate(kind=kind, sections=sections, jinja_vars=jinja_vars)
