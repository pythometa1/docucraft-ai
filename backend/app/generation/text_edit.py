"""Editing a finished letter's words without rebuilding the letter.

The editor this serves is deliberately narrow: it changes text and nothing else.
That narrowness is the whole design, and it exists because the obvious
alternative has already shipped here as a §20 CRITICAL defect.

The obvious alternative is a rich-text editor over an HTML rendering, saved back
through `assemble_from_html`. That function constructs a brand-new document from
`<p>` tags. HTML has no representation for a Word section break, a header/footer
pair, a numbering definition or a table cell's borders, so the save writes back a
file that reads similarly and is structurally different -- at the same blob path,
with no way back. `renderers.is_html_editable` refuses it for exactly this
reason, and this module exists so the refusal does not also mean "no editing".

What happens instead is what `docx_renderer.fill_template` already does: reopen
the file, re-derive spans with the same structural algorithm the compiler used,
and set the text of one span. Every other byte of the package is untouched, so
`layout_integrity` keeps passing by construction rather than by inspection.

The coordinates are the ones the rest of the system already speaks --
`(paragraph_index, span_index)` -- so an edit, a manifest slot and a QA finding
all point at the same run.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field

import docx
from lxml import etree

from app.templates.parsers.docx_prescan import (
    W_NS,
    RunSpan,
    _classify_color,
    _run_style_key,
    _run_text,
    _walk_paragraphs,
)


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


class EditRejected(ValueError):
    """An edit that cannot be applied to this document as written."""


@dataclass
class EditableSpan:
    """One run of text the editor may rewrite, with where it lives."""

    paragraph_index: int
    span_index: int
    text: str
    in_table: bool = False
    #: "black" | "blue" | "red" -- carried through so a reviewer can see which
    #: runs were filled values rather than static prose. Not editable state.
    role: str = "black"

    def as_dict(self) -> dict:
        return {
            "paragraph_index": self.paragraph_index,
            "span_index": self.span_index,
            "text": self.text,
            "in_table": self.in_table,
            "role": self.role,
        }


@dataclass
class EditableParagraph:
    paragraph_index: int
    spans: list[EditableSpan] = field(default_factory=list)
    in_table: bool = False

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    def as_dict(self) -> dict:
        return {
            "paragraph_index": self.paragraph_index,
            "in_table": self.in_table,
            "text": self.text,
            "spans": [s.as_dict() for s in self.spans],
        }


def _live_spans(document) -> tuple[list, dict]:
    """Paragraphs and their spans, derived exactly as the fill engine derives them.

    Not `prescan()`: that opens the file a second time and hands back elements
    from a throwaway tree. The whole point is that these are the real elements
    about to be mutated, addressed by the same coordinates the compiler used.
    """
    body = document.element.body
    paragraphs, table_flags = _walk_paragraphs(body)
    spans_by_paragraph: dict[int, list[RunSpan]] = {}

    for p_idx, p in enumerate(paragraphs):
        run_els = []
        for child in p:
            tag = etree.QName(child).localname
            if tag == "r":
                run_els.append(child)
            elif tag == "hyperlink":
                # Skipped rather than merged: a hyperlink's text is part of the
                # link, and rewriting it through this path would leave the
                # relationship pointing somewhere the words no longer describe.
                continue

        span_index = 0
        current: RunSpan | None = None
        for run in run_els:
            # A complex field (MERGEFIELD) is not a span -- its runs belong to
            # the field, and editing one of them corrupts the field structure.
            if run.find(_q("fldChar")) is not None or run.find(_q("instrText")) is not None:
                current = None
                continue
            text = _run_text(run)
            if not text:
                continue
            style = _run_style_key(run)
            role = _classify_color(style[0], style[3])
            if current is not None and current.color == role:
                current.text += text
                current.run_elements.append(run)
            else:
                current = RunSpan(
                    paragraph_index=p_idx, span_index=span_index,
                    color=role, text=text, run_elements=[run],
                )
                spans_by_paragraph.setdefault(p_idx, []).append(current)
                span_index += 1
    return paragraphs, spans_by_paragraph


def read_document(path: str) -> list[EditableParagraph]:
    """Every paragraph of a finished letter, as editable spans.

    Empty paragraphs are kept. They carry spacing a reader sees, and an editor
    that silently renumbers around them would make every coordinate below the
    first blank line point one line too high.
    """
    document = docx.Document(path)
    paragraphs, spans_by_paragraph = _live_spans(document)
    _p, table_flags = _walk_paragraphs(document.element.body)

    out: list[EditableParagraph] = []
    for index in range(len(paragraphs)):
        in_table = bool(table_flags[index]) if index < len(table_flags) else False
        out.append(EditableParagraph(
            paragraph_index=index,
            in_table=in_table,
            spans=[
                EditableSpan(index, s.span_index, s.text, in_table, s.color)
                for s in spans_by_paragraph.get(index, [])
            ],
        ))
    return out


# A control character has no business in a letter and several will not survive
# the XML serialiser.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Tab, newline and carriage return are refused separately, with their own
# message, because they are the ones a person or a model will actually send.
# Word does not store them as text: a line break is `<w:br/>` and a tab is
# `<w:tab/>`. Put one in a `<w:t>` and Word renders nothing at all, so the edit
# appears to save and the words silently disappear.
#
# Found by asking the model to rewrite a salutation: it returned a three-line
# block with `\n` between the lines, and the validator waved it through.
_BREAK_RE = re.compile(r"[\t\n\r]")

MAX_SPAN_CHARS = 20000


def validate_run_text(text: str) -> str:
    """Public name for the run-text rules, so every writer goes through them.

    The template studio and the co-pilot both write into runs, and both have to
    obey the same limits as the document editor -- most of all the one about tabs
    and line breaks, which Word stores as elements rather than characters, so a
    run containing one renders as nothing and the words silently vanish.
    """
    return _validate(text)


def _validate(text: str) -> str:
    if not isinstance(text, str):
        raise EditRejected("Replacement text must be a string.")
    if len(text) > MAX_SPAN_CHARS:
        raise EditRejected(
            f"Replacement text is {len(text)} characters; the limit for one run is {MAX_SPAN_CHARS}."
        )
    if _CONTROL_RE.search(text):
        raise EditRejected("Replacement text contains a control character.")
    if _BREAK_RE.search(text):
        raise EditRejected(
            "Replacement text contains a tab or line break. Word stores those as elements rather "
            "than as text, so one written into a run renders as nothing and the words vanish. "
            "Edit one paragraph at a time."
        )
    return text


def apply_edits(source_path: str, output_path: str, edits: list[dict]) -> dict:
    """Write `source_path` to `output_path` with each span's text replaced.

    `edits` is `[{paragraph_index, span_index, text}]`. Every one must name a
    span that exists; an edit that misses is an error rather than a silent
    no-op, because a save that reports success and changed nothing is the
    failure this whole module is careful about.

    Returns a summary suitable for a version's change note.
    """
    if not edits:
        raise EditRejected("No edits were supplied.")

    shutil.copyfile(source_path, output_path)
    document = docx.Document(output_path)
    _paragraphs, spans_by_paragraph = _live_spans(document)

    applied, unchanged = [], []
    for edit in edits:
        p_index = edit.get("paragraph_index")
        s_index = edit.get("span_index")
        if not isinstance(p_index, int) or not isinstance(s_index, int):
            raise EditRejected("Each edit needs an integer paragraph_index and span_index.")
        text = _validate(edit.get("text", ""))

        span = next(
            (s for s in spans_by_paragraph.get(p_index, []) if s.span_index == s_index),
            None,
        )
        if span is None:
            raise EditRejected(
                f"No editable run at paragraph {p_index}, span {s_index}. The document may have "
                f"changed since it was opened -- reload it and try again."
            )
        if span.text == text:
            unchanged.append({"paragraph_index": p_index, "span_index": s_index})
            continue
        applied.append({
            "paragraph_index": p_index, "span_index": s_index,
            "before": span.text, "after": text,
        })
        _write_span(span, text)

    if not applied:
        raise EditRejected("Every edit matched the text already in the document; nothing to save.")

    document.save(output_path)
    return {
        "applied": applied,
        "unchanged": unchanged,
        "summary": _summarise(applied),
    }


def _write_span(span: RunSpan, text: str) -> None:
    """Put `text` in the span's first run and empty the rest.

    The same shape as `docx_renderer._set_span_text`, and deliberately not an
    import of it: that one also strips the markup colour, which is right when
    filling a template and wrong here -- a reviewer editing a filled value has
    no reason to have its formatting changed underneath them.
    """
    if not span.run_elements:
        return
    first = span.run_elements[0]
    t_el = first.find(_q("t"))
    if t_el is None:
        t_el = etree.SubElement(first, _q("t"))
    t_el.text = text
    t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for extra in span.run_elements[1:]:
        for t in extra.findall(_q("t")):
            extra.remove(t)


def _summarise(applied: list[dict]) -> str:
    if len(applied) == 1:
        one = applied[0]
        return (
            f"Edited paragraph {one['paragraph_index']}: "
            f"{one['before'][:40]!r} -> {one['after'][:40]!r}"
        )
    paragraphs = sorted({a["paragraph_index"] for a in applied})
    shown = ", ".join(str(p) for p in paragraphs[:6])
    more = f" and {len(paragraphs) - 6} more" if len(paragraphs) > 6 else ""
    return f"Edited {len(applied)} run(s) across paragraph(s) {shown}{more}"


def structural_diff(before_path: str, after_path: str) -> list[str]:
    """Anything but `word/document.xml` that changed, canonicalised.

    The guarantee this module makes is that a text edit is a text edit. Stating
    it is cheap and checking it is cheaper than trusting it, so the edit endpoint
    runs this before it commits and refuses a save that moved anything else.
    """
    import zipfile

    def parts(path):
        out = {}
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.startswith("docProps/"):
                    continue
                raw = z.read(name)
                try:
                    out[name] = etree.tostring(etree.fromstring(raw), method="c14n")
                except (etree.C14NError, etree.XMLSyntaxError):
                    out[name] = raw
        return out

    a, b = parts(before_path), parts(after_path)
    return sorted(
        name for name in set(a) | set(b)
        # `[Content_Types].xml` differs only by element ordering after a
        # python-docx round trip, which is not a change to the document.
        if name not in ("word/document.xml", "[Content_Types].xml") and a.get(name) != b.get(name)
    )
