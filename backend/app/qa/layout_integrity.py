"""The letter is still a valid Office package, and still the same one.

The text gates read what the document says; this reads what it *is*. §17 lists
"Static PDF/DOCX region unexpectedly changes -> Block + flag template/render
issue", and for a DOCX produced by OOXML surgery that is a statement about the
package: a rendering bug that removed a relationship, corrupted a part or
dropped `styles.xml` produces a file Word refuses to open, or silently re-styles
into something that no longer looks like the approved template. No amount of
checking the prose would notice either.

The engine's contract is narrow enough to make this a total check rather than a
sampled one. `word/document.xml` is the only part it is allowed to rewrite, so
every other part is compared against the template and any difference is a defect
by definition -- there is no legitimate reason for `header1.xml`, `styles.xml`,
`numbering.xml` or the relationship graph to move. That is a stronger guarantee
than the visual diff §15 lists as async work, and much cheaper: it needs no
renderer, no page images and no tolerance threshold.

What it cannot see is the layout those parts describe. A body edit that is
structurally perfect and pushes the signature block onto a fourth page passes
here; `overflow_check` covers the part of that which is computable without
rendering.

The PDF half of the same §17 row is a different measurement of the same claim.
§12 gives the overlay renderer a contract as narrow as the DOCX engine's -- "the
PDF renderer should modify only approved dynamic regions. Static text, page
geometry, logos and unaffected elements remain untouched" -- and that contract is
checkable exactly because it is narrow. Page geometry is four numbers per page
and a rotation; static text is every show-text operation whose origin does not
fall inside an approved region. Both are compared before and after, and any
difference is a defect by definition rather than by threshold.

Geometry is compared to half a point rather than exactly. PDF coordinates are
real numbers that survive a parse-and-rewrite as decimal strings, and a renderer
that re-emits 595.276 as 595.2760000000001 has not moved the page. Half a point
is a quarter of a millimetre: far below anything a reader or a printer can see,
and far above float noise.
"""

import math
import zipfile
from dataclasses import dataclass

from app.generation.reproducibility import (
    CORE_PROPERTIES_PART,
    NORMALISED_PARTS,
    NotAPackage,
    normalise_core_properties,
)
from app.qa.document_diff import PackageDiff, diff_packages
from app.qa.policy import (
    STATIC_REGION_CHANGED,
    VALUE_OVERFLOWS_REGION,
    QaFinding,
    QaPolicy,
    findings_for,
)
from app.templates.semantic_model import PageRegion

#: The only part the fill engine rewrites, and therefore the only part exempt
#: from the comparison. Adding a name here widens what the engine may silently
#: change, so it is a deliberate list rather than a pattern.
EDITABLE_PARTS = frozenset({"word/document.xml"})


def failures_from_diff(diff: PackageDiff) -> list:
    """Turn a package diff into reviewer-facing notes, worst structure first.

    Order matters: a dropped part explains a hundred changed ones, so it is read
    first. Only the first five names of each kind are listed -- the sixth adds no
    information a person can act on.
    """
    failures: list = []
    if diff.dropped:
        failures.append(f"Parts dropped from the package: {diff.dropped[:5]}")
    if diff.added:
        failures.append(f"Parts added to the package: {diff.added[:5]}")
    for name in diff.changed:
        failures.append(f"Part changed that the engine must not touch: {name}")
    for name, message in diff.malformed:
        failures.append(f"{name} is not well-formed XML: {message}")
    if diff.unreadable is not None:
        failures.append(f"Rendered file is not a readable Office package: {diff.unreadable}")
    return failures


def _core_properties(path: str) -> bytes | None:
    with zipfile.ZipFile(path) as archive:
        if CORE_PROPERTIES_PART not in archive.namelist():
            return None
        return archive.read(CORE_PROPERTIES_PART)


def docprops_failures(template_path: str, output_path: str) -> list:
    """`docProps/core.xml`, compared with the clocks the writer pins taken out.

    The part cannot simply be exempted. `reproducibility.normalise_docx` pins its
    `dcterms:created` / `dcterms:modified` to a constant, so a byte comparison
    against the template reports a difference on every document ever rendered --
    but exempting the whole part would let a renderer rewrite the author, the
    title or the revision of an approved template with nothing noticing. So the
    same normalisation is applied to both sides and what is left is compared: the
    clocks are ignored *because* the writer is known to set them, and everything
    else in the part is still gated.

    A template with no core properties is not a failure. python-docx synthesises
    the part on save, which is an addition the writer is entitled to make and the
    reason `structural_failures` drops it from the added-parts list.
    """
    try:
        before, after = _core_properties(template_path), _core_properties(output_path)
    except (zipfile.BadZipFile, OSError):
        # An unreadable package is already the loudest line in the report;
        # saying it a second time here buries the more informative first one.
        return []

    if before is None or after is None:
        return []
    try:
        if normalise_core_properties(before) != normalise_core_properties(after):
            return [
                f"Part changed that the engine must not touch: {CORE_PROPERTIES_PART} "
                "(compared with the render timestamps normalised, so this is a real edit to the "
                "author, title or revision the template carried)"
            ]
    except NotAPackage as exc:
        return [str(exc)]
    return []


def structural_failures(template_path: str, output_path: str) -> list:
    """Every way the rendered package differs from the template it was cut from.

    Empty means the engine touched nothing it was not permitted to touch and the
    body it rewrote still parses.
    """
    diff = diff_packages(
        template_path,
        output_path,
        ignore_parts=EDITABLE_PARTS | NORMALISED_PARTS,
        require_well_formed=("word/document.xml",),
    )
    # A normalised part the template did not have is the writer creating it, not
    # the engine smuggling one in -- python-docx synthesises `docProps/core.xml`
    # for any template that ships without one. Only *added* is filtered: a
    # normalised part the output has lost is still a dropped part.
    diff.added = [name for name in diff.added if name not in NORMALISED_PARTS]
    return failures_from_diff(diff) + docprops_failures(template_path, output_path)


def findings(template_path: str, output_path: str, policy: QaPolicy) -> list[QaFinding]:
    """The §17 static-region gate.

    A dropped part, an added part, an edited part and an unreadable package are
    all reported under one check name: they are one failure mode -- the renderer
    changed something outside its contract -- and an operator who accepts one of
    them as a warning is accepting all of them.
    """
    if not policy.runs(STATIC_REGION_CHANGED):
        return []
    return findings_for(STATIC_REGION_CHANGED, structural_failures(template_path, output_path), policy)


# --------------------------------------------------------------------------
# The PDF half of the same §17 row.
# --------------------------------------------------------------------------

#: Half a point -- a quarter of a millimetre. Below the tolerance of any printer
#: and any reader, and far above the noise of re-serialising a decimal.
PAGE_DIMENSION_TOLERANCE = 0.5

#: Text positions are compared at hundredths of a point. Tighter than the page
#: tolerance because a static line that has moved at all has moved for a reason,
#: and the reason is always the renderer touching something it should not have.
CHUNK_POSITION_TOLERANCE = 0.01

#: Derived from the tolerance rather than written beside it, so the two cannot
#: be changed independently and quietly stop agreeing.
_CHUNK_PLACES = max(0, -math.floor(math.log10(CHUNK_POSITION_TOLERANCE)))

#: How many differing chunks one report names. A page whose static text all
#: moved has one cause, and listing four hundred lines hides it.
MAX_REPORTED_CHUNKS = 5


@dataclass(frozen=True)
class PageGeometry:
    """The geometry of one page, as §12 requires it to be validated.

    Defined here rather than in the renderer on purpose: the gate owns the shape
    of what it compares, so a renderer cannot quietly stop reporting a dimension
    by changing its own return type.
    """

    page: int          # 1-based, as a bbox anchor numbers pages
    width: float
    height: float
    rotation: int

    def differs_from(self, other: "PageGeometry") -> str | None:
        """The human-readable difference, or None when the pages are the same size."""
        if self.rotation != other.rotation:
            return f"rotation {self.rotation} -> {other.rotation}"
        if (abs(self.width - other.width) > PAGE_DIMENSION_TOLERANCE
                or abs(self.height - other.height) > PAGE_DIMENSION_TOLERANCE):
            return (f"{self.width:.3f} x {self.height:.3f} pt -> "
                    f"{other.width:.3f} x {other.height:.3f} pt")
        return None


@dataclass(frozen=True)
class TextChunk:
    """One show-text operation, located on the page it was drawn on.

    `x` and `y` are the device-space origin of the operation -- where the first
    glyph's baseline starts -- because that is the coordinate a stored bounding
    box addresses. Which region a chunk belongs to is decided by that origin and
    nothing else, so a long line that starts outside an approved region is static
    text even where it happens to run through one.
    """

    page: int
    x: float
    y: float
    text: str
    font_size: float

    def inside(self, box) -> bool:
        x0, y0, x1, y1 = box
        return x0 <= self.x <= x1 and y0 <= self.y <= y1

    @property
    def key(self) -> tuple:
        """What makes two chunks the same chunk, at the compared precision."""
        return (self.page, round(self.x, _CHUNK_PLACES), round(self.y, _CHUNK_PLACES), self.text)

    def describe(self) -> str:
        return f"page {self.page} at ({self.x:.2f}, {self.y:.2f}): {self.text!r}"


def _boxes_by_page(approved_regions) -> dict:
    boxes: dict = {}
    for region in approved_regions:
        if not isinstance(region, PageRegion):
            raise TypeError(
                f"an approved region must be a semantic_model.PageRegion, got "
                f"{type(region).__name__}; the manifest's region model is the one the anchor "
                "resolver already uses, and a second one beside it would drift")
        boxes.setdefault(region.page, []).append((region.x0, region.y0, region.x1, region.y1))
    return boxes


def static_chunks(chunks, approved_regions) -> tuple:
    """Every chunk whose origin lies outside every approved dynamic region.

    This is the definition of "static" the §12 contract turns on: the renderer is
    permitted inside the approved boxes and nowhere else, so everything outside
    them must survive the render untouched.
    """
    boxes = _boxes_by_page(approved_regions)
    return tuple(c for c in chunks if not any(c.inside(b) for b in boxes.get(c.page, ())))


def pdf_geometry_failures(before, after) -> list:
    """Page count and page dimensions, before against after.

    A page-count change is reported alone. Once the pages have been renumbered
    every dimension comparison after it is meaningless, and printing forty of
    them buries the one line that explains all forty.
    """
    before, after = tuple(before), tuple(after)
    if len(before) != len(after):
        return [
            f"Page count changed: the approved PDF has {len(before)} page(s), the generated one "
            f"has {len(after)}. An overlay renderer must never add or remove a page."
        ]
    failures = []
    for was, now in zip(before, after):
        difference = was.differs_from(now)
        if difference is not None:
            failures.append(f"Page {was.page} geometry changed: {difference}")
    return failures


def pdf_static_failures(before_chunks, after_chunks, approved_regions) -> list:
    """Static text that moved, vanished or appeared outside an approved region.

    Compared as sets of (page, position, text) rather than in order, because the
    order of operations in a content stream is the writer's business and carries
    no meaning a reader can see. Position and text are the whole of what a reader
    gets, so they are the whole of what is compared.
    """
    was = {c.key: c for c in static_chunks(before_chunks, approved_regions)}
    now = {c.key: c for c in static_chunks(after_chunks, approved_regions)}

    failures = []
    lost = [was[k] for k in was.keys() - now.keys()]
    gained = [now[k] for k in now.keys() - was.keys()]
    if lost:
        failures.append(
            "Static text lost from the approved PDF: "
            + "; ".join(sorted(c.describe() for c in lost)[:MAX_REPORTED_CHUNKS])
        )
    if gained:
        failures.append(
            "Text appeared outside every approved dynamic region: "
            + "; ".join(sorted(c.describe() for c in gained)[:MAX_REPORTED_CHUNKS])
        )
    return failures


def pdf_findings(
    *,
    before_geometry,
    after_geometry,
    before_chunks,
    after_chunks,
    approved_regions,
    overflows=(),
    policy: QaPolicy,
) -> list[QaFinding]:
    """The §17 gate for an overlay render.

    Geometry and static text share one check name because they are one failure
    mode -- "the renderer changed something outside its contract" -- and an
    operator who accepts a moved page as a warning is accepting a rewritten
    paragraph as one too. Overflow is separate: §17 gives it its own row and its
    own remedy, "block or route to exception workflow".
    """
    findings = findings_for(
        STATIC_REGION_CHANGED,
        pdf_geometry_failures(before_geometry, after_geometry)
        + pdf_static_failures(before_chunks, after_chunks, approved_regions),
        policy,
    )
    return findings + findings_for(VALUE_OVERFLOWS_REGION, list(overflows), policy)
