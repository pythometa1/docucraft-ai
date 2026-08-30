"""Read an existing `.docx` into a blueprint body.

This is the front half of the loop the product was missing: a legacy template
goes in, the compiler says what it means, and what comes back out is something a
person can edit rather than a manifest they can only argue with.

The structure is taken from one `prescan` of the file rather than re-derived, so
the reader cannot disagree with the pre-scanner about what a span is. That
matters more than it looks: the merge-field state machine, the run-merge rule and
the hyperlink lift are each subtle, and a second implementation of any of them
would drift and be wrong in a way nothing detects until a letter is wrong. The
same discipline is why `docx_renderer.fill_template` re-derives spans against the
live tree with the pre-scanner's own algorithm instead of trusting a stored copy.

What this reader does add is **structure**, which `PreScanResult` flattens away.
It reports `table_paragraph_indices` as a set, so a caller knows a paragraph is
*in* a table but not which table, row or cell -- and a body that cannot say that
cannot be written back out. So the walk here recurses the same way
`_walk_paragraphs` does, keeping the tree instead of discarding it, and the
paragraph counter it maintains is the pre-scanner's index by construction rather
than by a lookup that could mismatch.

**What is deliberately not modelled.** A run holding only `<w:br/>` or `<w:tab/>`
carries no `w:t`, so `prescan` skips it and so does this reader -- the body has
no way to say "line break" because nothing downstream has one either
(`text_edit._validate` refuses `\\t` and `\\n` in a run for exactly this reason:
Word stores them as elements, not characters, and a run containing one renders as
nothing). This is why a blueprint read from a legacy file publishes by copying
that file and mutating it in place, never by re-emitting it from the body: the
body is a faithful model of the *text and its roles*, and a lossy model of
everything else. `notes` names what was passed over so the loss is visible rather
than assumed.
"""

import docx

from app.templates import blueprint as bp
from app.templates.parsers.docx_prescan import _q, prescan

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _style_name(paragraph_el, document) -> str | None:
    """The paragraph's style as a *name*, not the id stored in the XML.

    `w:pStyle` holds an id (`Heading1`); python-docx's `add_paragraph(style=...)`
    wants a name (`Heading 1`). Resolving here means a body read from one file
    can be written to another without a lookup table nobody maintains. An
    unresolvable id yields None -- a paragraph in the default style, which is
    what an unknown style would have rendered as anyway.
    """
    ppr = paragraph_el.find(_q("pPr"))
    style_el = ppr.find(_q("pStyle")) if ppr is not None else None
    style_id = style_el.get(_q("val")) if style_el is not None else None
    if not style_id:
        return None
    for style in document.styles:
        if getattr(style, "style_id", None) == style_id:
            return style.name
    return style_id


def _hyperlink_target(run_el, paragraph_el, rels) -> str:
    """The destination of the link this run sits in, or "" if it is not in one.

    Read from the relationship rather than matched against
    `PreScanResult.hyperlinks` by text, because two links in one paragraph can
    share their text -- "see here ... and here" -- and matching on it would send
    a reader to the wrong page.
    """
    node = run_el
    while node is not None and node is not paragraph_el:
        if node.tag == _q("hyperlink"):
            rid = node.get(_q("id")) or node.get(f"{{{R_NS}}}id")
            if rid and rid in rels:
                return rels[rid].target_ref or ""
            return ""
        node = node.getparent()
    return ""


def _run_ordinals(paragraph_el) -> dict:
    """Every `w:r` in this paragraph, mapped to its position in document order.

    Spans and merge fields both know which runs they own but neither knows where
    it sits relative to the other, and `$«SALARY» per annum` has to come back in
    that order rather than as two static segments followed by a field. Keyed on
    the element object: while `prescan`'s result is alive it holds a reference to
    every run, and lxml hands back the same proxy for a node that already has
    one, so identity is exact here. It would not be across two independent
    passes -- `docx_renderer` documents that trap -- which is why this map is
    built and consumed without letting the scan go.
    """
    return {run: index for index, run in enumerate(paragraph_el.iter(_q("r")))}


def _segments_for(paragraph_el, spans, mergefields, rels, notes) -> list:
    """The paragraph's segments, in document order."""
    ordinals = _run_ordinals(paragraph_el)
    entries = []

    for span in spans:
        if not span.run_elements:
            continue
        at = ordinals.get(span.run_elements[0])
        if at is None:
            # A span whose runs are not in this paragraph cannot be placed, and
            # guessing a position would silently reorder the sentence.
            notes.append(
                f"paragraph {span.paragraph_index}: a span could not be located and was skipped")
            continue
        if span.in_hyperlink:
            target = _hyperlink_target(span.run_elements[0], paragraph_el, rels)
            # The colour is recorded, not assumed. A link this codebase writes
            # carries none -- Word's own link blue is inside `BLUE_RGBS` and
            # would compile as a placeholder -- but a link in a customer's file
            # carries whatever its author gave it, and a real client offer letter
            # has a red one. Discarding that would make the body disagree with
            # the file it was read from.
            entries.append((at, bp.segment(bp.HYPERLINK, span.text, target=target,
                                           colour=span.color)))
        else:
            role = {"blue": bp.PLACEHOLDER, "red": bp.INSTRUCTION}.get(span.color, bp.STATIC)
            entries.append((at, bp.segment(role, span.text)))

    for field in mergefields:
        if not field.field_elements:
            continue
        at = ordinals.get(field.field_elements[0])
        if at is None:
            notes.append(f"merge field {field.code!r} could not be located and was skipped")
            continue
        entries.append((at, bp.segment(bp.MERGEFIELD, "", code=(field.code or "").strip())))

    return [seg for _at, seg in sorted(entries, key=lambda pair: pair[0])]


def read_body(path: str) -> tuple:
    """`(body, notes)` for an existing `.docx`.

    The body comes back already normalised, because it was read from a document
    that has already been through the merge rule -- the pre-scanner did the
    merging, so no two adjacent segments can be ones Word would store as one run.
    """
    scan = prescan(path)
    # The structure is walked over **the tree `prescan` itself parsed**, reached
    # through one of the paragraphs it returned. Opening the file a second time
    # would build a second tree, and then every run element the scan holds is a
    # different object from the one this walk sees: `_run_ordinals` matches
    # nothing, every span is skipped, and the body comes back as a document full
    # of empty paragraphs. That is exactly what the check at the end of this
    # function caught the first time it ran.
    #
    # `document` is still opened, but only for `rels` and `styles`, which are
    # keyed by string id and therefore say the same thing about the same file
    # whichever tree asked.
    document = docx.Document(path)
    rels = document.part.rels
    notes: list = []

    if not scan.paragraphs:
        return {"blocks": [], "sect_pr_from": None}, notes
    body_el = scan.paragraphs[0].getroottree().getroot().find(_q("body"))

    spans_by_paragraph: dict = {}
    for span in scan.spans:
        spans_by_paragraph.setdefault(span.paragraph_index, []).append(span)
    fields_by_paragraph: dict = {}
    for field in scan.mergefields:
        fields_by_paragraph.setdefault(field.paragraph_index, []).append(field)

    counter = {"index": 0}

    def read_blocks(container_el) -> list:
        blocks = []
        for child in container_el:
            tag = child.tag
            if tag == _q("p"):
                index = counter["index"]
                counter["index"] += 1
                blocks.append(bp.paragraph(
                    _segments_for(child, spans_by_paragraph.get(index, ()),
                                  fields_by_paragraph.get(index, ()), rels, notes),
                    style=_style_name(child, document),
                ))
            elif tag == _q("tbl"):
                rows = [
                    [read_blocks(tc) for tc in tr.findall(_q("tc"))]
                    for tr in child.findall(_q("tr"))
                ]
                blocks.append(bp.table(rows))
            # sectPr, bookmarks and other body-level non-content: the walk
            # `prescan` performs ignores them, so counting them here would put
            # every paragraph index out by one against the manifest.
        return blocks

    body = {"blocks": read_blocks(body_el), "sect_pr_from": None}

    # The reader's own check, run every time rather than only in a test. If the
    # text or the table membership disagrees with the pre-scan, the body is not a
    # model of this document and every anchor lifted against it addresses the
    # wrong place.
    from app.compiler.mapping_agent import _paragraph_texts

    if bp.paragraph_texts(body) != _paragraph_texts(scan):
        raise bp.BlueprintError(
            f"the body read from {path} does not reproduce the document's paragraph text; "
            "it cannot be used to address anything in it")
    if bp.table_paragraph_indices(body) != scan.table_paragraph_indices:
        raise bp.BlueprintError(
            f"the body read from {path} disagrees with the pre-scan about which paragraphs "
            "sit inside a table")

    return body, notes
