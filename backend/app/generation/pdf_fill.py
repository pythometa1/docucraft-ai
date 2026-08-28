"""Generate a document from a manifest whose template is an immutable PDF.

`render_overlay` in `app/generation/pdf_renderer.py` implements §12's PDF path
properly -- masking, deterministic coordinate overlay, static-region integrity,
the §17 placeholder gate -- and had no caller. A renderer nothing calls is a
library, and §12's path was still missing from the product.

This is the adapter between the two halves that already exist: the manifest,
which says which field goes where and what it resolves to, and the overlay
renderer, which knows how to put text on a page without disturbing the rest of
it. It is deliberately thin. Anything clever belongs on one side or the other,
and a fill engine with opinions is how the DOCX path grew the bugs the QA gates
now catch.

The pairing rule is `object_id`: a manifest field is written into the stored
region carrying the same id. A field with no approved region is not written
somewhere plausible -- it is reported, because §12's whole claim is that the
renderer modifies only regions a human signed off.
"""

from dataclasses import dataclass

from app.generation.missing_policy import BLOCK, field_on_missing
from app.generation.pdf_renderer import RegionFill, render_overlay
from app.templates.semantic_model import Anchor, PageRegion


@dataclass(frozen=True)
class UnplacedField:
    """A manifest field with no approved region to write into."""

    field_id: str
    reason: str


def regions_from_rows(rows) -> tuple:
    """Rebuild the stored inventory from the JSON on the template version."""
    return tuple(
        PageRegion(
            page=int(row["page"]),
            x0=float(row["x0"]), y0=float(row["y0"]),
            x1=float(row["x1"]), y1=float(row["y1"]),
            text=row.get("text"),
        )
        for row in rows or []
    )


def _anchor_for(region: PageRegion) -> Anchor:
    return Anchor(
        kind="bbox", page=region.page,
        x0=region.x0, y0=region.y0, x1=region.x1, y1=region.y1,
    )


def build_fills(manifest: dict, resolved: dict, regions) -> tuple:
    """Pair each manifest field with the region approved for it.

    Returns `(fills, unplaced)`. A field the inventory has no region for goes
    into `unplaced` rather than being dropped: silently skipping it produces a
    letter missing a value that QA cannot see is missing, because nothing on the
    page says anything should have been there.
    """
    by_id = {}
    for index, region in enumerate(regions):
        # The inventory is positional where it carries no id of its own; the
        # manifest's field order is what pairs them, and scan order is stable.
        by_id[index] = region

    fills, unplaced = [], []
    for order, field in enumerate(manifest.get("fields") or []):
        field_id = field.get("id")
        if not field_id:
            continue
        region = by_id.get(order)
        if region is None:
            unplaced.append(UnplacedField(field_id, "no approved region in the stored inventory"))
            continue

        value = resolved.get(field_id)
        if value is None:
            # The same policy the DOCX path applies. BLOCK is handled by the
            # caller's QA, but an empty overlay still has to mask the
            # placeholder or the template's own token stays legible.
            if field_on_missing(field) == BLOCK:
                unplaced.append(UnplacedField(field_id, "no value, and the field's policy is BLOCK"))
            value = ""

        fills.append(RegionFill(
            object_id=str(field_id),
            anchor=_anchor_for(region),
            text=str(value),
            mask=True,
        ))
    return tuple(fills), tuple(unplaced)


def fill_pdf_template(
    template_path: str,
    output_path: str,
    manifest: dict,
    resolved: dict,
    *,
    page_regions,
    qa_policy=None,
):
    """§12's PDF path, driven by a manifest. Mirrors `docx_renderer.fill_template`.

    Returns the renderer's own `OverlayResult`, with any unplaced field folded
    into its QA notes so a caller cannot mistake "nothing was written there" for
    "nothing needed to be".
    """
    regions = regions_from_rows(page_regions)
    if not regions:
        raise ValueError(
            "this template version carries no approved region inventory, so there is nowhere "
            "the overlay is allowed to write. Re-onboard the PDF to derive one."
        )

    fills, unplaced = build_fills(manifest, resolved, regions)
    result = render_overlay(
        template_path, output_path, fills,
        page_regions=regions, qa_policy=qa_policy,
    )
    if unplaced:
        result.qa_passed = False
        result.qa_notes.extend(
            f"Field {item.field_id!r} was not written: {item.reason}." for item in unplaced
        )
    return result
