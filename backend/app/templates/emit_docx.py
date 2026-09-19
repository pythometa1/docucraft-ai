"""Write a blueprint body as a `.docx` the pre-scanner reads back unchanged.

This module is the inverse of `app.templates.parsers.docx_prescan`. That one
classifies a run by its colour -- blue is a placeholder, red is an instruction --
and this one writes those colours. Which means an emitted template is
*self-describing*: it is indistinguishable from a well-formed legacy template, so
`prescan`, the compiler, the fill engine and every QA gate accept it with no
special case for "we wrote this one ourselves". There is no second renderer to
keep in step, which is the entire point.

The pre-scanner is small but it is not obvious, and five of its mechanics decide
whether a round trip holds. Each is a rule here rather than a comment there:

1. **Adjacent runs merge into one span** when their colour and hyperlink state
   match. Two placeholder segments written side by side come back as *one* span,
   and every `span_index` after them in that paragraph is one too high -- so the
   manifest's slots address the span to their left for the rest of the paragraph
   and the fill writes values into the wrong text. `emit` refuses a body that has
   not been through `blueprint.normalise_body`, which makes one segment mean one
   span by construction.

2. **A run with no text is invisible.** `prescan` skips it without breaking the
   span it was in, so an empty run is not a separator and must never be written
   as though it were one. `normalise_body` drops empty segments.

3. **A merge field is not a span, and it resets the span accumulator.** Its whole
   `begin / instrText / separate / result / end` sequence is consumed into a
   `MergeField`, so static text either side of one stays two spans rather than
   merging into one. Emitting a merge field as anything less than that full
   sequence loses it entirely: `_mergefield_code` would find nothing and the
   compiler would report a template with no salary field in it.

4. **A hyperlink's runs are lifted out of the `w:hyperlink` wrapper** and flagged,
   and the flag is half the merge key. Links are emitted with no `w:color`
   because Word's own link colour, 0563C1, is *in* `BLUE_RGBS` -- a link styled
   the way Word styles it would classify as a placeholder and the compiler would
   try to fill it with data.

5. **Paragraph index is a document-order walk that descends into table cells.**
   So table paragraphs interleave with body ones and the index is not "the nth
   top-level paragraph". `blueprint.walk_paragraphs` reproduces that walk, and
   the emitter writes in exactly its order.

Finally the file is put through `app.generation.reproducibility.normalise_docx`,
so emitting the same blueprint twice produces the same bytes. Without it
`manifests.versioning.template_hash` records when a file was written rather than
what is in it, and the (template_hash, manifest_hash) pin that §6 rests on
becomes a value nobody can reproduce.
"""

from dataclasses import dataclass, field

import docx
from docx.oxml.ns import qn

from app.generation.reproducibility import normalise_docx
from app.templates.base_docx import BASE_DOCX
from app.templates.blueprint import (
    HYPERLINK, INSTRUCTION, MERGEFIELD, PLACEHOLDER, ROLE_RGB, SPAN_ROLES, STATIC,
    BlueprintError, RGB_BY_COLOUR, emits, emitted_spans, is_normalised, observed_colour,
    span_plan, walk_paragraphs,
)

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


class EmitError(BlueprintError):
    """The body cannot be written as a document."""


class RoundTripError(EmitError):
    """The emitted file does not read back as the body that produced it.

    Raised by `emit` against its own output. A blueprint that cannot survive its
    own pre-scan is one whose manifest addresses spans that are not there, and
    the failure is silent at every later stage: the compile succeeds, the
    approval succeeds, and the letters come out wrong.
    """


@dataclass
class EmitResult:
    path: str
    paragraph_count: int = 0
    span_count: int = 0
    mergefield_count: int = 0
    notes: list = field(default_factory=list)


# ---- low-level OOXML ----

def _run(text: str, *, rgb: str | None = None, bold: bool = False, italic: bool = False):
    """One `w:r`, with `xml:space="preserve"` so leading and trailing spaces live.

    Without the attribute Word collapses `"Dear "` to `"Dear"`, the pre-scanner
    reads back a paragraph whose text is a character short, and every anchor
    context hash taken over that paragraph stops matching.
    """
    run = docx.oxml.parse_xml(
        f'<w:r xmlns:w="{docx.oxml.ns.nsmap["w"]}"><w:t xml:space="preserve"/></w:r>')
    if rgb or bold or italic:
        rpr = run.makeelement(qn("w:rPr"), {})
        # `w:rPr` must be the first child of `w:r`; Word rejects the part outright
        # if it is not, and the failure is a file that will not open at all.
        run.insert(0, rpr)
        if bold:
            rpr.append(rpr.makeelement(qn("w:b"), {}))
        if italic:
            rpr.append(rpr.makeelement(qn("w:i"), {}))
        if rgb:
            rpr.append(rpr.makeelement(qn("w:color"), {qn("w:val"): rgb}))
    run.find(qn("w:t")).text = text
    return run


def _fld_char(kind: str):
    run = docx.oxml.parse_xml(f'<w:r xmlns:w="{docx.oxml.ns.nsmap["w"]}"/>')
    run.append(run.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): kind}))
    return run


def _instr_text(code: str):
    run = docx.oxml.parse_xml(f'<w:r xmlns:w="{docx.oxml.ns.nsmap["w"]}"/>')
    instr = run.makeelement(qn("w:instrText"), {qn("xml:space"): "preserve"})
    instr.text = f" MERGEFIELD {code} "
    run.append(instr)
    return run


def _mergefield_runs(code: str, result_text: str):
    """The full complex-field sequence the pre-scanner's state machine expects.

    `begin -> instrText -> separate -> result -> end`. The result run carries the
    placeholder text Word shows before the field is updated; it is inside the
    field, so it is consumed with the rest and never becomes a span of its own.
    """
    return [
        _fld_char("begin"),
        _instr_text(code),
        _fld_char("separate"),
        _run(result_text or f"«{code}»"),
        _fld_char("end"),
    ]


def _hyperlink(paragraph_el, rel_id: str, text: str, *, rgb: str | None = None):
    link = paragraph_el.makeelement(qn("w:hyperlink"), {f"{{{R_NS}}}id": rel_id})
    link.append(_run(text, rgb=rgb))
    return link


# ---- emission ----

def _write_paragraph(paragraph_el, block: dict, part):
    for seg in block.get("segments") or ():
        if not emits(seg):
            continue
        role = seg.get("role")
        if role == MERGEFIELD:
            code = (seg.get("code") or "").strip()
            if not code:
                raise EmitError("a mergefield segment carries no code, so nothing would be filled")
            for run in _mergefield_runs(code, seg.get("result_text") or ""):
                paragraph_el.append(run)
        elif role == HYPERLINK:
            target = seg.get("target") or ""
            if not target:
                raise EmitError(
                    f"hyperlink segment {seg.get('text')!r} carries no target; Word writes the "
                    "relationship, not the text, so a link with no target is not a link")
            rel_id = part.relate_to(target, HYPERLINK_REL, is_external=True)
            paragraph_el.append(_hyperlink(paragraph_el, rel_id, seg.get("text") or "",
                                           rgb=RGB_BY_COLOUR.get(seg.get("colour"))))
        else:
            paragraph_el.append(_run(
                seg.get("text") or "",
                rgb=ROLE_RGB.get(role),
                bold=bool(seg.get("bold")),
                italic=bool(seg.get("italic")),
            ))


def _write_blocks(container, blocks, document):
    """Append each block to `container` (the body, or a table cell).

    python-docx's `add_paragraph` / `add_table` only exist on `Document` and
    `_Cell`, and both append to their own element, so the container is addressed
    through whichever of those it is rather than through the raw element.
    """
    for block in blocks:
        kind = block.get("kind")
        if kind == "paragraph":
            para = container.add_paragraph(style=block.get("style") or None)
            _write_paragraph(para._p, block, document.part)
        elif kind == "table":
            rows = block.get("rows") or ()
            if not rows:
                raise EmitError("a table with no rows cannot be written")
            width = len(rows[0])
            if any(len(r) != width for r in rows):
                raise EmitError(
                    "every row of a table must have the same number of cells; Word has no "
                    "representation for a ragged grid")
            docx_table = container.add_table(rows=len(rows), cols=width)
            for row_index, row in enumerate(rows):
                for cell_index, cell_blocks in enumerate(row):
                    cell = docx_table.cell(row_index, cell_index)
                    # A new cell arrives holding one empty paragraph. Left in
                    # place it is a paragraph the walk counts and the body does
                    # not, so every index after it is off by one.
                    cell._tc.remove(cell.paragraphs[0]._p)
                    _write_blocks(cell, cell_blocks, document)
        else:
            raise EmitError(f"unknown block kind {kind!r}; expected 'paragraph' or 'table'")


def emit(body: dict, output_path: str) -> EmitResult:
    """Write `body` to `output_path` and verify it reads back unchanged.

    The body must already be normalised. Normalising here instead would mean
    emitting a document that does not match the body the caller holds -- their
    segment indices, and therefore every anchor lifted against them, would
    describe a different file from the one on disk.
    """
    if not is_normalised(body):
        raise EmitError(
            "emit needs a normalised body: adjacent segments Word stores as one run must be "
            "merged first, or one segment stops meaning one span and every slot after the "
            "merge addresses the wrong text. Call blueprint.normalise_body first.")

    # The neutral base, never `docx.Document()`: python-docx's own default
    # package names python-docx as the author and carries a thumbnail, a
    # custom-XML part and revision ids into every file a customer receives.
    document = docx.Document(BASE_DOCX)
    # A fresh python-docx document opens with no body paragraphs, so nothing has
    # to be removed here -- unlike a table cell, which does.
    _write_blocks(document, body.get("blocks") or (), document)
    document.save(output_path)
    normalise_docx(output_path)

    result = _verify(body, output_path)
    return EmitResult(path=output_path, **result)


def _verify(body: dict, path: str) -> dict:
    """Re-scan the emitted file and insist it says what the body says.

    This is the round-trip property asserted on every single emit rather than
    only in a test. The cost is one `prescan` of a file that is already in the
    page cache; the alternative is discovering a span-numbering slip during a
    batch, in a letter, after approval.
    """
    from app.templates.parsers.docx_prescan import prescan

    scan = prescan(path)
    expected = walk_paragraphs(body)

    if len(scan.paragraphs) != len(expected):
        raise RoundTripError(
            f"emitted {len(expected)} paragraphs but the file reads back with "
            f"{len(scan.paragraphs)}")

    spans_by_paragraph = {}
    for span in scan.spans:
        spans_by_paragraph.setdefault(span.paragraph_index, []).append(span)

    span_count = 0
    for index, block, _in_table in expected:
        segments = emitted_spans(block.get("segments") or ())
        plan = span_plan(segments)
        found = spans_by_paragraph.get(index, [])
        if len(found) != len(plan):
            raise RoundTripError(
                f"paragraph {index} declares {len(plan)} span(s) and reads back with "
                f"{len(found)}; adjacent segments Word stores as one run were not merged")
        for (position, span_index), span in zip(plan, found):
            seg = segments[position]
            if span.span_index != span_index:
                raise RoundTripError(
                    f"paragraph {index} segment {position} expected span index {span_index}, "
                    f"got {span.span_index}")
            if span.text != (seg.get("text") or ""):
                raise RoundTripError(
                    f"paragraph {index} span {span_index} reads {span.text!r}, "
                    f"expected {seg.get('text')!r}")
            wanted_colour = observed_colour(seg)
            if span.color != wanted_colour:
                raise RoundTripError(
                    f"paragraph {index} span {span_index} ({seg['role']}) reads back as "
                    f"{span.color!r}, expected {wanted_colour!r}")
            if span.in_hyperlink != (seg["role"] == HYPERLINK):
                raise RoundTripError(
                    f"paragraph {index} span {span_index} hyperlink state does not match")
            span_count += 1

    expected_codes = sorted(
        (s.get("code") or "").strip()
        for _i, block, _t in expected for s in block.get("segments") or ()
        if s.get("role") == MERGEFIELD and emits(s)
    )
    found_codes = sorted((mf.code or "").strip() for mf in scan.mergefields)
    if expected_codes != found_codes:
        raise RoundTripError(
            f"merge fields read back as {found_codes}, expected {expected_codes}")

    emitted_tables = {i for i, _b, in_table in expected if in_table}
    if scan.table_paragraph_indices != emitted_tables:
        raise RoundTripError(
            f"table paragraphs read back as {sorted(scan.table_paragraph_indices)}, "
            f"expected {sorted(emitted_tables)}")

    return {
        "paragraph_count": len(expected),
        "span_count": span_count,
        "mergefield_count": len(found_codes),
    }



# ---- the legacy path: mutate the customer's own file, never rebuild it ----

#: The only part of the package a text edit may touch.
DOCUMENT_PART = "word/document.xml"


def _replace_document_part(base_path: str, output_path: str, document_xml: bytes) -> None:
    """Copy the package entry for entry, substituting only `word/document.xml`.

    Not a python-docx round trip, and that is the whole point. Opening a real
    Word file with python-docx and saving it **drops parts**: measured on a
    client compensation letter, `word/_rels/{comments,endnotes,fontTable,
    footnotes}.xml.rels` all disappear, along with the package's directory
    entries. Those files are 123 bytes each and Word opens the result anyway,
    which is exactly what makes it dangerous -- the damage is invisible until
    something needs the relationship that is no longer declared.

    Writing the archive out from its own `ZipInfo` entries preserves names,
    order, timestamps and compression, so every part except the one being edited
    is the customer's original bytes.
    """
    import zipfile

    with zipfile.ZipFile(base_path) as source:
        entries = source.infolist()
        payloads = {info.filename: source.read(info.filename)
                    for info in entries if not info.filename.endswith("/")}

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target:
        for info in entries:
            if info.filename.endswith("/"):
                target.writestr(info, b"")
                continue
            data = document_xml if info.filename == DOCUMENT_PART else payloads[info.filename]
            target.writestr(info, data)


def emit_from_base(body: dict, base_path: str, output_path: str) -> EmitResult:
    """Write `body` by copying `base_path` and changing only its text.

    A template read from a customer's `.docx` must never be published by
    rebuilding it from the body. The body is a faithful model of the text and its
    roles and a lossy model of everything else -- section properties, headers,
    numbering definitions, cell borders, a run holding only a `<w:br/>` -- so
    re-rendering it would hand back a document that reads the same and lays out
    differently. That is the defect `text_edit.py` exists to prevent; this reuses
    its technique and tightens it, because `text_edit` writes through python-docx
    and this writes the archive itself.

    Structural edits are refused rather than half-applied. Inserting a paragraph
    into the body and then writing only text changes would produce a file that
    disagrees with the body about how many paragraphs it has, and every anchor
    below the insertion would address the wrong one. Until an operation
    vocabulary can insert into the real package, a body whose shape has diverged
    from its base is a save this function cannot honour, and saying so is better
    than appearing to.
    """
    from lxml import etree

    from app.generation.text_edit import _validate, _write_span
    from app.templates.parsers.docx_prescan import prescan
    from app.templates.read_docx import read_body

    base_body, notes = read_body(base_path)
    base_paragraphs = walk_paragraphs(base_body)
    paragraphs = walk_paragraphs(body)

    if len(paragraphs) != len(base_paragraphs):
        raise EmitError(
            f"the body has {len(paragraphs)} paragraph(s) and the base file has "
            f"{len(base_paragraphs)}; a template read from a file is published by editing that "
            "file, so its shape cannot change without an operation that can edit the package too")

    wanted: dict = {}
    for (index, block, _t), (_bi, base_block, _bt) in zip(paragraphs, base_paragraphs):
        plan = span_plan(block["segments"])
        base_plan = span_plan(base_block["segments"])
        if len(plan) != len(base_plan):
            raise EmitError(
                f"paragraph {index} has {len(plan)} span(s) and the base file has "
                f"{len(base_plan)}; segments cannot be added or removed by a text edit")
        for (position, span_index), (base_position, _bs) in zip(plan, base_plan):
            seg = block["segments"][position]
            base_seg = base_block["segments"][base_position]
            # An instruction the compile removes is written as an empty run,
            # which is what the fill engine does to a span-scoped `delete_always`.
            # Removing the run instead would renumber every span after it.
            text = "" if seg.get("emit") is False else (seg.get("text") or "")
            if text != (base_seg.get("text") or ""):
                wanted[(index, span_index)] = _validate(text)

    scan = prescan(base_path)
    for span in scan.spans:
        text = wanted.get((span.paragraph_index, span.span_index))
        if text is not None:
            _write_span(span, text)

    root = scan.paragraphs[0].getroottree().getroot() if scan.paragraphs else None
    if root is None:
        raise EmitError(f"{base_path} has no paragraphs to edit")
    _replace_document_part(base_path, output_path, etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True))

    from app.generation.text_edit import structural_diff

    moved = structural_diff(base_path, output_path)
    if moved:
        raise EmitError(
            "publishing this template changed parts of the package other than its text: "
            + ", ".join(moved)
            + ". The layout a customer approved is not something an edit may touch.")

    return EmitResult(path=output_path, notes=list(notes), **_verify(body, output_path))
