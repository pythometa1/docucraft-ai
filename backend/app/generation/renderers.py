"""Which renderer produced a document, and whether its output can be edited as HTML.

Two separate needs meet here.

The first is §19's "renderer version change alters output": a regression has to
be attributable to a specific renderer release rather than argued about, so the
name and version are written into every document version's lineage.

The second is the §20 CRITICAL defect. The editor's save path used to decide
whether a document was safe to rebuild from HTML by asking whether it *had* any
HTML -- but the legacy generation path stores an HTML preview alongside a
document whose real layout came from Word template surgery. The guard therefore
never fired on the one path it existed to protect, and Save quietly replaced a
letter that had letterhead, tables, headers and hyperlinks with a new document
built out of `<p>` tags. Keying the decision off how the file was actually
produced is the difference between a guard and a comment.
"""

# Anchored OOXML surgery on a copy of the approved template (fill_engine).
OOXML_FILL = "ooxml_fill/1.0"

# Legacy draft path: python-docx paragraph replacement inside a copy of the
# template. Positional rather than anchored, but the template is still the
# substrate, so its layout must not be rebuilt from HTML either.
DOCX_TEMPLATE_ASSEMBLY = "docx_template_assembly/1.0"

# A document built from HTML in the first place. This one, and only this one,
# can be rebuilt from HTML without losing anything that was not already absent.
HTML_ASSEMBLY = "html_assembly/1.0"

#: Renderers whose output is HTML all the way down, so an HTML save is lossless.
HTML_EDITABLE = frozenset({HTML_ASSEMBLY})


def is_html_editable(renderer: str | None) -> bool:
    """Whether saving HTML over this document's blob preserves its meaning.

    `None` -- a row written before the renderer was recorded -- is deliberately
    *not* editable. Failing closed costs a legacy HTML document its in-app
    editing until someone re-generates it; failing open costs a filled template
    its layout, silently, at the same blob path, with no way back.
    """
    return renderer in HTML_EDITABLE
