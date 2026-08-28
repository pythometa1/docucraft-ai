"""Deterministic pre-scan for the Template Compiler (docs/TEMPLATE_COMPILER_RESEARCH.md
§4.1 step 1). Never guesses meaning -- only mechanically inventories: which runs
are which colour, where MERGEFIELDs live, where the hyperlinks are, and a stable
paragraph index every downstream anchor is expressed in terms of. The LLM/rule
compiler (manifest_compiler.py) is the only layer that interprets what any of
this *means*.
"""

import re
from dataclasses import dataclass, field

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W_NS}

BLUE_RGBS = {"0000FF", "0070C0", "0563C1"}  # common "placeholder blue" variants
RED_RGBS = {"FF0000", "C00000", "E00000"}
BRACKET_RE = re.compile(r"<([^<>]{1,80})>")

# Word's highlighter is the other half of this convention. Some clients annotate
# with the highlighter rather than font colour -- green for an instruction to
# whoever assembles the letter, yellow for a value to be filled in -- and reading
# only `w:color` makes those files look like one uniform block of static text.
# Measured on a real client master: 0 fields and 0 conditions from a document
# that carries 25 of one and 3 of the other, because every run classified black.
#
# The two conventions carry the same three roles, so they collapse onto the same
# three names the rest of the pipeline already speaks. Nothing downstream needs
# to know which one a template used.
HIGHLIGHT_ROLES = {
    "green": "red",       # Word's UI calls this "Bright Green"
    "brightgreen": "red",
    "yellow": "blue",
}


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


@dataclass
class RunSpan:
    """One or more adjacent same-style `w:r` runs, merged (report §1.4/§11:
    'fragmented runs split placeholders across XML runs')."""

    paragraph_index: int
    span_index: int
    color: str  # "blue" | "red" | "black"
    text: str
    run_elements: list = field(default_factory=list)  # underlying lxml <w:r> elements, in order
    in_hyperlink: bool = False


@dataclass
class MergeField:
    paragraph_index: int
    code: str  # e.g. "LAB__FT_SALARY__38_HR_"
    field_elements: list = field(default_factory=list)  # [begin_fldChar_run, instr_run, separate_fldChar_run, result_run(s)..., end_fldChar_run]
    in_table: bool = False


@dataclass
class HyperlinkInfo:
    paragraph_index: int
    text: str
    target: str


@dataclass
class PreScanResult:
    paragraphs: list  # lxml <w:p> elements, in document order (incl. table cells)
    spans: list[RunSpan]
    mergefields: list[MergeField]
    hyperlinks: list[HyperlinkInfo]
    table_paragraph_indices: set[int]
    # True when any run carried a highlight this scanner treats as markup. The
    # fill engine strips those highlights from the letter -- they are annotation
    # for the template author, not formatting the recipient should ever see --
    # and this flag keeps it from touching a template that never used them.
    uses_highlight_markup: bool = False


def _classify_color(rgb: str | None, highlight: str | None = None) -> str:
    """The run's role: "red" instruction, "blue" placeholder, "black" static.

    Font colour is checked first, so a template already using the red/blue
    convention behaves exactly as it did before highlights were understood --
    including one that highlights for emphasis on top of a coloured run.
    """
    if rgb:
        rgb = rgb.upper()
        if rgb in BLUE_RGBS:
            return "blue"
        if rgb in RED_RGBS:
            return "red"
    if highlight:
        role = HIGHLIGHT_ROLES.get(highlight.lower())
        if role:
            return role
    return "black"


def _run_style_key(run_el) -> tuple:
    rpr = run_el.find(_q("rPr"))
    color_el = rpr.find(_q("color")) if rpr is not None else None
    color = color_el.get(_q("val")) if color_el is not None else None
    hl_el = rpr.find(_q("highlight")) if rpr is not None else None
    # Part of the key, not just an output: two adjacent runs that differ only by
    # highlight are an instruction next to its value, and merging them into one
    # span would erase the boundary the compiler needs.
    highlight = hl_el.get(_q("val")) if hl_el is not None else None
    bold = rpr is not None and rpr.find(_q("b")) is not None
    italic = rpr is not None and rpr.find(_q("i")) is not None
    return (color, bold, italic, highlight)


def _run_text(run_el) -> str:
    return "".join(t.text or "" for t in run_el.findall(_q("t")))


def _walk_paragraphs(body_el):
    """Flatten the body into document-order paragraphs, descending into table
    cells, tagging which ones live inside a table (mergefields in this template
    class live in table cells, per the research doc's finding)."""
    out = []
    table_flags = []

    def walk(el, in_table):
        tag = etree.QName(el).localname
        if tag == "p":
            out.append(el)
            table_flags.append(in_table)
            return
        if tag == "tbl":
            for tr in el.findall(_q("tr")):
                for tc in tr.findall(_q("tc")):
                    for child in tc:
                        walk(child, True)
            return
        # sectPr and other body-level non-content elements: ignore

    for child in body_el:
        walk(child, False)

    return out, table_flags


def prescan(docx_path: str) -> PreScanResult:
    import docx

    document = docx.Document(docx_path)
    body_el = document.element.body
    paragraphs, table_flags = _walk_paragraphs(body_el)
    table_paragraph_indices = {i for i, f in enumerate(table_flags) if f}

    spans: list[RunSpan] = []
    mergefields: list[MergeField] = []
    hyperlinks: list[HyperlinkInfo] = []
    uses_highlight_markup = False

    rels = document.part.rels

    for p_idx, p in enumerate(paragraphs):
        # runs directly under the paragraph, and runs nested inside w:hyperlink wrappers
        run_els = []
        for child in p:
            tag = etree.QName(child).localname
            if tag == "r":
                run_els.append((child, False))
            elif tag == "hyperlink":
                rid = child.get(_q("id")) or child.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = rels[rid].target_ref if rid and rid in rels else None
                htext = "".join(_run_text(r) for r in child.findall(_q("r")))
                if htext.strip():
                    hyperlinks.append(HyperlinkInfo(paragraph_index=p_idx, text=htext, target=target or ""))
                for r in child.findall(_q("r")):
                    run_els.append((r, True))

        # --- detect MERGEFIELD complex-field sequences (begin/instrText/separate/result/end) ---
        i = 0
        n = len(run_els)
        consumed = set()
        while i < n:
            r, _hl = run_els[i]
            fld_char = r.find(_q("fldChar"))
            if fld_char is not None and fld_char.get(_q("fldCharType")) == "begin":
                instr_text = None
                result_runs = []
                j = i + 1
                state = "seeking_instr"
                seq = [r]
                while j < n:
                    rj, _ = run_els[j]
                    seq.append(rj)
                    instr_el = rj.find(_q("instrText"))
                    fc = rj.find(_q("fldChar"))
                    if instr_el is not None and state == "seeking_instr":
                        instr_text = (instr_text or "") + (instr_el.text or "")
                    elif fc is not None and fc.get(_q("fldCharType")) == "separate":
                        state = "collecting_result"
                    elif fc is not None and fc.get(_q("fldCharType")) == "end":
                        j += 1
                        break
                    elif state == "collecting_result":
                        result_runs.append(rj)
                    j += 1
                if instr_text and "MERGEFIELD" in instr_text:
                    code = instr_text.replace("MERGEFIELD", "").replace("\\* MERGEFORMAT", "").strip()
                    mergefields.append(MergeField(paragraph_index=p_idx, code=code, field_elements=seq, in_table=p_idx in table_paragraph_indices))
                for k in range(i, j):
                    consumed.add(k)
                i = j
                continue
            i += 1

        # --- merge adjacent same-style plain runs into spans ---
        span_idx = 0
        current = None
        for k, (r, is_hl) in enumerate(run_els):
            if k in consumed:
                current = None
                continue
            text = _run_text(r)
            if not text:
                continue
            style = _run_style_key(r)
            color = _classify_color(style[0], style[3])
            if style[3] and style[3].lower() in HIGHLIGHT_ROLES:
                uses_highlight_markup = True
            if current and current.color == color and current.in_hyperlink == is_hl:
                current.text += text
                current.run_elements.append(r)
            else:
                current = RunSpan(paragraph_index=p_idx, span_index=span_idx, color=color, text=text, run_elements=[r], in_hyperlink=is_hl)
                spans.append(current)
                span_idx += 1

    return PreScanResult(
        paragraphs=paragraphs,
        spans=spans,
        mergefields=mergefields,
        hyperlinks=hyperlinks,
        table_paragraph_indices=table_paragraph_indices,
        uses_highlight_markup=uses_highlight_markup,
    )


def extract_bracket_tokens(text: str, family=None) -> list[tuple[str, str]]:
    """`[(token_exactly_as_written, inner_text), ...]`.

    The token is returned verbatim rather than rebuilt as `f"<{inner}>"` because
    the fill engine replaces it by exact string match against the run's original
    text. A CJK master writes `＜姓名＞` with fullwidth brackets, and a rebuilt
    ASCII token would match nothing there -- the placeholder would survive into
    the letter while the manifest recorded it as filled.
    """
    pattern = family.bracket_re() if family is not None else BRACKET_RE
    out = []
    for m in pattern.finditer(text or ""):
        inner = next((g for g in m.groups() if g is not None), None)
        if inner is not None:
            out.append((m.group(0), inner))
    return out


def extract_brackets(text: str, family=None) -> list[str]:
    return [inner for _token, inner in extract_bracket_tokens(text, family)]
