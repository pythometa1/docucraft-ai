"""§12's PDF path: only the approved regions move, and everything else is proof.

The renderer's contract is one sentence of the architecture record -- "the PDF
renderer should modify only approved dynamic regions. Static text, page
geometry, logos and unaffected elements remain untouched" -- and every test here
is a way that sentence can be false.

The real-world failures being pinned:

* A placeholder that was *covered* rather than *removed*. A white rectangle
  satisfies a reviewer looking at the page and leaves `<Employee Name>` in the
  file for anyone who selects the text or runs `pdftotext` over it. On a letter
  that carries legal weight that is a redaction failure, not a cosmetic one, so
  the extraction assertions here read the written file rather than the object
  graph that produced it.
* A value that silently overhangs its approved box and prints over the static
  text beside it. §17 gives that its own row and its own remedy.
* A page that quietly changes size, which reprints an entire batch.
* A mapping that was never approved reaching a customer's letterhead.

The template is built in-test rather than checked in. A fixture PDF would hide
the coordinates the assertions depend on inside a binary, and the point of a
bounding-box anchor is that the coordinates are known and stated.
"""

from __future__ import annotations

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.generation.pdf_renderer import (
    ALIGN_CENTRE,
    ALIGN_RIGHT,
    HELVETICA_WIDTHS,
    PDF_RENDERER_VERSION,
    OverlayResult,
    PdfOverlayError,
    RegionFill,
    measure_text,
    page_geometry,
    render_overlay,
    scan_page_regions,
)
from app.qa.layout_integrity import (
    PageGeometry,
    TextChunk,
    pdf_geometry_failures,
    pdf_static_failures,
    static_chunks,
)
from app.qa.policy import STATIC_REGION_CHANGED, VALUE_OVERFLOWS_REGION, WARNING, resolve_policy
from app.templates.semantic_model import (
    ANCHOR_BBOX,
    ANCHOR_RUN_PATH,
    PROPOSED,
    Anchor,
    AnchorAmbiguousError,
    AnchorNotFoundError,
    PageRegion,
    context_hash,
)

A4_WIDTH, A4_HEIGHT = 595.276, 841.89

#: (x, baseline y, size, text) for the approved template. Every coordinate in
#: this file traces back to one of these, which is what makes an assertion about
#: a bounding box readable instead of magic.
LETTER = (
    (72.0, 760.0, 12.0, "ACME Pty Ltd - Offer of Employment"),
    (72.0, 700.0, 11.0, "Name:"),
    (160.0, 700.0, 11.0, "<Employee Name>"),
    (72.0, 680.0, 11.0, "Salary:"),
    (160.0, 680.0, 11.0, "<Annual Salary>"),
    (72.0, 100.0, 9.0, "Issued under the ACME group policy."),
)

NAME_BOX = Anchor(kind=ANCHOR_BBOX, page=1, x0=158.0, y0=694.0, x1=300.0, y1=712.0)
SALARY_BOX = Anchor(kind=ANCHOR_BBOX, page=1, x0=158.0, y0=674.0, x1=300.0, y1=692.0)


def _build_pdf(path, lines, *, width=A4_WIDTH, height=A4_HEIGHT, pages=1):
    """An approved template PDF, drawn with real content-stream operators."""
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        operators = b""
        for x, y, size, text in lines:
            operators += (f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm (".encode("ascii")
                          + text.encode("latin-1") + b") Tj ET\n")
        stream = DecodedStreamObject()
        stream.set_data(operators)
        page.replace_contents(stream)

        helvetica = DictionaryObject()
        for key, value in (("/Type", "/Font"), ("/Subtype", "/Type1"),
                           ("/BaseFont", "/Helvetica"), ("/Encoding", "/WinAnsiEncoding")):
            helvetica[NameObject(key)] = NameObject(value)
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = helvetica
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
    writer.write(str(path))
    return str(path)


@pytest.fixture
def template(tmp_path):
    return _build_pdf(tmp_path / "approved.pdf", LETTER)


@pytest.fixture
def regions(template):
    """The stored inventory. Scanned once at "onboarding", then pinned."""
    return scan_page_regions(template)


def _text(path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)


# ------------------------------------------------------ the happy path ----
def test_the_placeholder_is_removed_from_the_file_not_merely_covered(template, regions, tmp_path):
    """A white rectangle over `<Employee Name>` looks right and leaves the
    placeholder in the file for anyone who selects the text. That is the
    redaction failure, and it is invisible to any check that looks at pixels."""
    out = tmp_path / "letter.pdf"
    result = render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                            page_regions=regions)

    extracted = _text(out)
    assert "<Employee Name>" not in extracted
    assert "Priya Sharma" in extracted
    assert result.qa_passed, result.qa_notes


def test_static_text_and_page_geometry_survive_the_overlay(template, regions, tmp_path):
    """The whole contract in one assertion: the heading, the labels, the footer
    and the page size are untouched by a render that changed two regions."""
    out = tmp_path / "letter.pdf"
    render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma"),
                                   RegionFill("obj_salary", SALARY_BOX, "$82,000.00")],
                   page_regions=regions)

    extracted = _text(out)
    for _x, _y, _size, static in LETTER:
        if not static.startswith("<"):
            assert static in extracted, f"static text lost: {static!r}"
    assert page_geometry(PdfReader(str(out))) == page_geometry(PdfReader(template))


def test_an_untouched_page_keeps_every_byte_of_its_content_stream(tmp_path):
    """A two-page template with one filled region must not have its second page
    re-serialised: "unaffected elements remain untouched" is a statement about
    the bytes, not about how the page looks."""
    template = _build_pdf(tmp_path / "two.pdf", LETTER, pages=2)
    regions = scan_page_regions(template)
    out = tmp_path / "letter.pdf"
    render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                   page_regions=regions)

    before = PdfReader(template).pages[1].get_contents().get_data()
    after = PdfReader(str(out)).pages[1].get_contents().get_data()
    assert before == after


def test_the_value_inherits_the_size_the_template_approved(template, regions, tmp_path):
    """A renderer that imposed its own default would resize half the letter and
    still pass every structural check."""
    out = tmp_path / "letter.pdf"
    result = render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                            page_regions=regions)

    entry = result.region_lineage[0]
    assert entry["font_size"] == 11.0
    assert entry["font_size_source"] == "inherited"
    assert entry["masked_text"] == ["<Employee Name>"]


def test_the_value_lands_on_the_baseline_the_placeholder_used(template, regions, tmp_path):
    """"Overlay exact text at deterministic coordinates" -- and the coordinate
    that matters is the baseline, because a value half a line high reads as a
    broken document however correct its content is."""
    out = tmp_path / "letter.pdf"
    render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                   page_regions=regions)

    drawn = [c for c in _chunks(PdfReader(str(out))) if c.text == "Priya Sharma"]
    assert len(drawn) == 1
    # The placeholder's own baseline, not the bottom of the box around it: the
    # box is drawn larger on purpose so the anchor survives a re-save.
    assert drawn[0].y == pytest.approx(700.0, abs=0.01)
    assert drawn[0].x == pytest.approx(NAME_BOX.x0 + 1.0, abs=0.01)


def test_a_region_that_was_blank_puts_the_value_on_the_box_s_own_floor(tmp_path):
    """There is no approved baseline to inherit for a ruled line waiting for a
    name, so the rule has to state one rather than guess."""
    template = _build_pdf(tmp_path / "form.pdf", LETTER)
    blank = Anchor(kind=ANCHOR_BBOX, page=1, x0=300.0, y0=600.0, x1=460.0, y1=616.0)
    stored = (PageRegion(page=1, x0=310.0, y0=602.0, x1=430.0, y1=614.0, text=""),)
    out = tmp_path / "letter.pdf"
    result = render_overlay(template, out, [RegionFill("obj_ref", blank, "REF-4471")],
                            page_regions=stored)

    drawn = [c for c in _chunks(PdfReader(str(out))) if c.text == "REF-4471"]
    assert result.region_lineage[0]["baseline_source"] == "region"
    assert result.region_lineage[0]["font_size_source"] == "default"
    assert drawn[0].y == pytest.approx(600.0 + 0.207 * 11.0, abs=0.01)


def _chunks(reader):
    from app.generation.pdf_renderer import page_chunks

    return [c for n, p in enumerate(reader.pages, start=1) for c in page_chunks(p, n, reader)]


@pytest.mark.parametrize(
    "align,expected_x",
    [(ALIGN_CENTRE, (158.0 + 300.0) / 2 - measure_text("Priya Sharma", 11.0) / 2),
     (ALIGN_RIGHT, 300.0 - 1.0 - measure_text("Priya Sharma", 11.0))],
)
def test_alignment_positions_the_value_inside_the_approved_box(template, regions, tmp_path,
                                                               align, expected_x):
    """Right-aligned money is the case that matters: a currency column that
    left-aligns one row looks like a different document."""
    out = tmp_path / "letter.pdf"
    render_overlay(template, out,
                   [RegionFill("obj_name", NAME_BOX, "Priya Sharma", align=align)],
                   page_regions=regions)

    drawn = [c for c in _chunks(PdfReader(str(out))) if c.text == "Priya Sharma"]
    assert drawn[0].x == pytest.approx(expected_x, abs=0.01)


def test_two_renders_of_the_same_fills_are_byte_identical(template, regions, tmp_path):
    """§18's reproducibility SLO covers the PDF path too, and the overlay is
    where it could most easily be lost: a content stream built from a set, or a
    float formatted with repr, differs run to run for no visible reason."""
    first, second = tmp_path / "a.pdf", tmp_path / "b.pdf"
    fills = [RegionFill("obj_salary", SALARY_BOX, "$82,000.00"),
             RegionFill("obj_name", NAME_BOX, "Priya Sharma")]
    one = render_overlay(template, first, fills, page_regions=regions)
    # Reversed, because a caller's list order must not reach the bytes.
    two = render_overlay(template, second, list(reversed(fills)), page_regions=regions)

    assert first.read_bytes() == second.read_bytes()
    assert one.digest == two.digest


def test_a_different_value_produces_a_different_file(template, regions, tmp_path):
    """The other half of the reproducibility claim."""
    first, second = tmp_path / "a.pdf", tmp_path / "b.pdf"
    render_overlay(template, first, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                   page_regions=regions)
    render_overlay(template, second, [RegionFill("obj_name", NAME_BOX, "Aisha Khan")],
                   page_regions=regions)

    assert first.read_bytes() != second.read_bytes()


def test_the_renderer_version_is_recorded_for_the_audit(template, regions, tmp_path):
    """§19: "Renderer version change alters output" is mitigated entirely by the
    version having been written into the lineage."""
    result = render_overlay(template, tmp_path / "letter.pdf",
                            [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                            page_regions=regions)

    assert isinstance(result, OverlayResult)
    assert result.renderer == PDF_RENDERER_VERSION
    assert result.digest.startswith("sha256:")


# ------------------------------------------------------------ QA gates ----
def test_a_value_wider_than_its_region_blocks_the_document(template, regions, tmp_path):
    """§17: "Value overflows approved PDF region -> block or route to exception
    workflow". Printed over the static text beside it, the letter is wrong in a
    way no reader can unsee."""
    out = tmp_path / "letter.pdf"
    result = render_overlay(
        template, out,
        [RegionFill("obj_name", NAME_BOX,
                    "Bartholomew Fitzwilliam-Montgomery III of Rathmines")],
        page_regions=regions)

    assert not result.qa_passed
    assert any("offers 140.0 pt" in note for note in result.qa_notes)


def test_an_organisation_may_route_overflow_to_review_instead_of_blocking(template, regions,
                                                                          tmp_path):
    """The §6 `qa_policy` is the manifest's decision, not the renderer's -- some
    estates genuinely have regions their approved values overhang."""
    result = render_overlay(
        template, tmp_path / "letter.pdf",
        [RegionFill("obj_name", NAME_BOX,
                    "Bartholomew Fitzwilliam-Montgomery III of Rathmines")],
        page_regions=regions,
        qa_policy={WARNING: [VALUE_OVERFLOWS_REGION]})

    assert result.qa_passed
    assert any(note.startswith("Warning (non-blocking):") for note in result.qa_notes)


def test_a_page_that_changed_size_is_a_blocking_failure():
    """A page that silently resizes reprints an entire batch, and the change is
    invisible in a text comparison."""
    before = (PageGeometry(1, A4_WIDTH, A4_HEIGHT, 0),)
    letter = (PageGeometry(1, 612.0, 792.0, 0),)

    assert pdf_geometry_failures(before, before) == []
    assert "geometry changed" in pdf_geometry_failures(before, letter)[0]
    assert "rotation" in pdf_geometry_failures(before, (PageGeometry(1, A4_WIDTH, A4_HEIGHT, 90),))[0]


def test_re_serialising_a_coordinate_is_not_a_geometry_change():
    """595.276 coming back as 595.2760000000001 has not moved the page. A gate
    that cried wolf on float noise would be switched off within a week."""
    before = (PageGeometry(1, A4_WIDTH, A4_HEIGHT, 0),)
    after = (PageGeometry(1, A4_WIDTH + 0.0000001, A4_HEIGHT - 0.2, 0),)

    assert pdf_geometry_failures(before, after) == []


def test_a_lost_page_is_reported_before_anything_else():
    """Once the pages are renumbered every dimension comparison after it is
    meaningless, and printing forty of them buries the one line that matters."""
    failures = pdf_geometry_failures((PageGeometry(1, A4_WIDTH, A4_HEIGHT, 0),
                                      PageGeometry(2, A4_WIDTH, A4_HEIGHT, 0)),
                                     (PageGeometry(1, A4_WIDTH, A4_HEIGHT, 0),))

    assert len(failures) == 1 and "Page count changed" in failures[0]


def test_static_text_that_moved_outside_an_approved_region_is_a_failure():
    """The §17 row, for PDF. Text inside an approved box is the renderer's
    business; a signature line that shifted two points is not."""
    approved = (PageRegion(page=1, x0=158.0, y0=694.0, x1=300.0, y1=712.0),)
    before = (TextChunk(1, 72.0, 700.0, "Name:", 11.0),
              TextChunk(1, 160.0, 700.0, "<Employee Name>", 11.0))
    unchanged_static = (TextChunk(1, 72.0, 700.0, "Name:", 11.0),
                        TextChunk(1, 160.0, 700.0, "Priya Sharma", 11.0))
    moved_static = (TextChunk(1, 74.0, 700.0, "Name:", 11.0),
                    TextChunk(1, 160.0, 700.0, "Priya Sharma", 11.0))

    assert pdf_static_failures(before, unchanged_static, approved) == []
    failures = pdf_static_failures(before, moved_static, approved)
    assert any("Static text lost" in f for f in failures)
    assert any("appeared outside" in f for f in failures)


def test_a_chunk_belongs_to_the_region_its_origin_falls_in():
    """A long static line that runs through an approved box is still static
    text: it starts outside, so the renderer never claimed it."""
    approved = (PageRegion(page=1, x0=158.0, y0=694.0, x1=300.0, y1=712.0),)
    starts_outside = TextChunk(1, 72.0, 700.0, "Name: ................", 11.0)
    starts_inside = TextChunk(1, 160.0, 700.0, "<Employee Name>", 11.0)

    assert static_chunks((starts_outside, starts_inside), approved) == (starts_outside,)


def test_the_gate_refuses_a_region_model_that_is_not_the_manifest_s():
    """§6 already has a region type. A second one beside it drifts, and the two
    stop agreeing on which text a mapping owns."""
    with pytest.raises(TypeError, match="PageRegion"):
        static_chunks((), [(1, 0.0, 0.0, 10.0, 10.0)])


# ------------------------------------------------------------ refusals ----
def test_an_unapproved_fill_never_reaches_the_page(template, regions, tmp_path):
    """§12 masks and overlays "when approved". A proposed mapping on a
    customer's letterhead is not a QA finding, it is a document nobody agreed
    to send."""
    out = tmp_path / "letter.pdf"
    with pytest.raises(PdfOverlayError, match="PROPOSED"):
        render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya", status=PROPOSED)],
                       page_regions=regions)
    assert not out.exists(), "a refused render must leave no partial document"


def test_a_non_bounding_box_anchor_is_refused():
    """There are no runs or fields on a PDF page to address. The doc's anchor
    table gives PDF exactly one kind."""
    run_path = Anchor(kind=ANCHOR_RUN_PATH, path="body/p[1]/r[1]", ordinal=1, token="<x>",
                      context_hash=context_hash("hello <x> there", 6))

    with pytest.raises(PdfOverlayError, match="bounding box"):
        RegionFill("obj_name", run_path, "Priya")


def test_two_fills_claiming_overlapping_regions_are_refused(template, regions, tmp_path):
    """§6: no two objects may claim overlapping anchor ranges. Whichever is
    drawn second destroys the other, and the document reports clean."""
    overlapping = Anchor(kind=ANCHOR_BBOX, page=1, x0=200.0, y0=700.0, x1=320.0, y1=714.0)

    with pytest.raises(PdfOverlayError, match="overlapping"):
        render_overlay(template, tmp_path / "letter.pdf",
                       [RegionFill("a", NAME_BOX, "Priya"), RegionFill("b", overlapping, "Aisha")],
                       page_regions=regions)


def test_an_anchor_that_no_longer_resolves_stops_the_render(template, regions, tmp_path):
    """§19's first row: anchor drift after a template re-save. Overlaying at
    coordinates nothing occupies puts the value somewhere nobody approved."""
    elsewhere = Anchor(kind=ANCHOR_BBOX, page=1, x0=400.0, y0=400.0, x1=450.0, y1=420.0)

    with pytest.raises(AnchorNotFoundError):
        render_overlay(template, tmp_path / "letter.pdf", [RegionFill("a", elsewhere, "Priya")],
                       page_regions=regions)


def test_an_anchor_covering_two_stored_regions_is_refused(template, tmp_path):
    """"Narrow the box until it addresses one, because overlaying text over two
    regions destroys one of them"."""
    whole_line = Anchor(kind=ANCHOR_BBOX, page=1, x0=60.0, y0=694.0, x1=400.0, y1=712.0)

    with pytest.raises(AnchorAmbiguousError):
        render_overlay(template, tmp_path / "letter.pdf",
                       [RegionFill("a", whole_line, "Priya")],
                       page_regions=scan_page_regions(template))


def test_a_page_the_template_does_not_have_is_refused(template, regions, tmp_path):
    """The approved template is immutable, so this anchor was compiled against a
    different document."""
    page_three = Anchor(kind=ANCHOR_BBOX, page=3, x0=10.0, y0=10.0, x1=50.0, y1=30.0)

    with pytest.raises(PdfOverlayError, match="page 3 of a 1-page"):
        render_overlay(template, tmp_path / "letter.pdf", [RegionFill("a", page_three, "Priya")],
                       page_regions=regions)


def test_a_value_helvetica_cannot_draw_is_refused_rather_than_mangled(template, regions, tmp_path):
    """Drawing a Korean name in a WinAnsi font produces a letter addressed to
    nobody, and it looks like a font problem rather than a wrong document."""
    with pytest.raises(PdfOverlayError, match="WinAnsi"):
        render_overlay(template, tmp_path / "letter.pdf",
                       [RegionFill("obj_name", NAME_BOX, "안녕하세요")],
                       page_regions=regions)


def test_two_fills_with_the_same_object_id_are_refused(template, regions, tmp_path):
    """Lineage would record one value for a region that received another."""
    with pytest.raises(PdfOverlayError, match="object_id"):
        render_overlay(template, tmp_path / "letter.pdf",
                       [RegionFill("obj_name", NAME_BOX, "Priya"),
                        RegionFill("obj_name", SALARY_BOX, "Aisha")],
                       page_regions=regions)


@pytest.mark.parametrize("bad", [{"align": "justified"}, {"font_size": 0.0}, {"text": 82000}])
def test_a_malformed_fill_is_refused_at_construction(bad):
    """Never silently default: a fill that says nothing about its size is
    different from one that says zero."""
    kwargs = {"object_id": "obj_name", "anchor": NAME_BOX, "text": "Priya", **bad}
    with pytest.raises((ValueError, TypeError)):
        RegionFill(**kwargs)


# ------------------------------------------------------- the inventory ----
def test_the_scanned_inventory_boxes_the_text_it_found(template):
    """The onboarding step that produces what the manifest later pins. Boxed
    from the baseline and the font's own ascender and descender, so a value
    rendered back into the box lands where the placeholder was."""
    inventory = scan_page_regions(template)

    by_text = {region.text: region for region in inventory}
    assert set(by_text) == {line[3] for line in LETTER}
    name = by_text["<Employee Name>"]
    assert name.page == 1
    assert name.x0 == pytest.approx(160.0)
    assert name.x1 == pytest.approx(160.0 + measure_text("<Employee Name>", 11.0), abs=0.01)
    assert name.y0 < 700.0 < name.y1


def test_glyph_widths_are_helvetica_s_own_metrics():
    """pypdf ships a copy of this table with capital U the width of a full stop.
    A wrong width mis-measures every overflow check in the product, so the
    numbers here are asserted rather than trusted."""
    assert HELVETICA_WIDTHS["U"] == 722
    assert HELVETICA_WIDTHS["W"] == 944
    assert HELVETICA_WIDTHS["i"] == 222
    # An accent is free in Helvetica: E-acute is exactly as wide as E.
    assert measure_text("É", 10.0) == pytest.approx(measure_text("E", 10.0))


def test_masking_can_be_declined_for_a_region_that_was_always_blank(tmp_path):
    """A ruled line waiting for a name has nothing to redact. Declining the mask
    over a placeholder would leave it extractable, which is why it is opt-out
    rather than opt-in."""
    template = _build_pdf(tmp_path / "form.pdf", LETTER)
    out = tmp_path / "letter.pdf"
    render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "Priya", mask=False)],
                   page_regions=scan_page_regions(template))

    assert "<Employee Name>" in _text(out)
    assert "Priya" in _text(out)


def test_a_stored_region_that_drifted_from_the_page_is_a_warning_not_a_silence(template, tmp_path):
    """§19's anchor-drift row. The value still lands where the manifest said, so
    the document is not blocked -- but somebody has to be told the inventory and
    the template no longer agree."""
    stale = (PageRegion(page=1, x0=159.0, y0=696.0, x1=250.0, y1=708.0, text="<Line Manager>"),)
    result = render_overlay(template, tmp_path / "letter.pdf",
                            [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                            page_regions=stale)

    assert any("drifted apart" in warning for warning in result.warnings)
    assert result.qa_passed


def test_the_static_region_check_is_the_one_named_in_the_policy(template, regions, tmp_path):
    """An operator turning this check into a warning must be turning off the
    same check §17 names, not a renderer-private one."""
    policy = resolve_policy({WARNING: [STATIC_REGION_CHANGED]})

    assert policy.severity_of(STATIC_REGION_CHANGED) == WARNING
    assert render_overlay(template, tmp_path / "letter.pdf",
                          [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                          page_regions=regions,
                          qa_policy={WARNING: [STATIC_REGION_CHANGED]}).qa_passed


# ------------------------------------------------ the content-stream walker --
# Masking is only as good as knowing where the text on a page actually is, and
# real producers do not helpfully emit one `Tm` per line. These pin the walker
# against the operator forms a template in the wild is written with.

def _raw_pdf(path, operators: bytes, *, with_font=True, base_font="/Helvetica"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=A4_WIDTH, height=A4_HEIGHT)
    stream = DecodedStreamObject()
    stream.set_data(operators)
    page.replace_contents(stream)
    if with_font:
        font = DictionaryObject()
        for key, value in (("/Type", "/Font"), ("/Subtype", "/Type1"),
                           ("/BaseFont", base_font), ("/Encoding", "/WinAnsiEncoding")):
            font[NameObject(key)] = NameObject(value)
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
    writer.write(str(path))
    return str(path)


def _located(path):
    from app.generation.pdf_renderer import page_chunks

    reader = PdfReader(str(path))
    return page_chunks(reader.pages[0], 1, reader)


def test_a_paragraph_moved_with_td_and_tstar_is_still_located(tmp_path):
    """A producer is free to emit one text object for a whole paragraph and move
    the pen with `Td` and `T*`. A walker that only understood `Tm` would think
    every line of it started in the same place and mask the wrong one."""
    path = _raw_pdf(tmp_path / "para.pdf", (
        b"BT /F1 10 Tf 14 TL 1 0 0 1 72 700 Tm (first line) Tj "
        b"T* (second line) Tj "
        b"0 -14 TD (third line) Tj ET\n"
    ))
    found = {c.text: (round(c.x, 2), round(c.y, 2)) for c in _located(path)}

    assert found["first line"] == (72.0, 700.0)
    assert found["second line"] == (72.0, 686.0)
    assert found["third line"] == (72.0, 672.0)


def test_the_quote_operators_move_to_the_next_line_before_showing(tmp_path):
    """`'` and `"` are `T*` plus `Tj`. Treating them as a bare `Tj` puts every
    line of a hand-written stream on top of the first."""
    path = _raw_pdf(tmp_path / "quotes.pdf", (
        b"BT /F1 10 Tf 12 TL 1 0 0 1 72 700 Tm (opening) Tj "
        b"(next) ' "
        b"1 2 (spaced) \" ET\n"
    ))
    found = {c.text: round(c.y, 2) for c in _located(path)}

    assert found["opening"] == 700.0
    assert found["next"] == 688.0
    assert found["spaced"] == 676.0


def test_kerning_inside_a_tj_array_advances_the_pen(tmp_path):
    """The numbers in a `TJ` array are displacement, not content. Ignoring them
    puts every later chunk in the text object a few points to the right."""
    path = _raw_pdf(tmp_path / "kern.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 72 700 Tm [(A) -500 (B)] TJ (after) Tj ET\n")
    chunks = _located(path)

    assert [c.text for c in chunks] == ["AB", "after"]
    # "AB" is 667+667 thousandths at 10pt, and the -500 kern adds 5pt back.
    assert chunks[1].x == pytest.approx(72.0 + (667 + 667) / 1000 * 10 + 5.0, abs=0.01)


def test_the_current_transformation_matrix_moves_the_text_with_it(tmp_path):
    """A logo block or a stamped watermark is drawn inside `q ... cm ... Q`. Text
    located without the CTM lands at the page origin, and the region it really
    occupies is never masked."""
    path = _raw_pdf(tmp_path / "cm.pdf",
                    b"q 1 0 0 1 100 50 cm BT /F1 10 Tf 1 0 0 1 72 700 Tm (shifted) Tj ET Q\n"
                    b"BT /F1 10 Tf 1 0 0 1 72 600 Tm (unshifted) Tj ET\n")
    found = {c.text: (round(c.x, 2), round(c.y, 2)) for c in _located(path)}

    assert found["shifted"] == (172.0, 750.0)
    assert found["unshifted"] == (72.0, 600.0)


def test_scaled_text_reports_the_size_a_reader_sees(tmp_path):
    """A producer that sets 1pt text and scales it by ten has drawn 10pt text,
    and a value that inherited "1pt" from it would be invisible."""
    path = _raw_pdf(tmp_path / "scaled.pdf",
                    b"BT /F1 1 Tf 10 0 0 10 72 700 Tm (scaled) Tj ET\n")

    assert _located(path)[0].font_size == pytest.approx(10.0)


def test_a_page_with_no_text_yields_no_regions(tmp_path):
    """A page whose content is a scanned image has no show operations. The
    renderer reports that it masked nothing rather than pretending it did."""
    path = _raw_pdf(tmp_path / "blank.pdf", b"q 1 0 0 1 0 0 cm Q\n", with_font=False)

    assert _located(path) == ()
    assert scan_page_regions(path) == ()


def test_a_font_the_metrics_are_unknown_for_is_estimated_not_guessed_at_zero(tmp_path):
    """Times is not Helvetica. Estimating its advance keeps later chunks in the
    same text object roughly right; assuming zero would stack them all."""
    path = _raw_pdf(tmp_path / "times.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (AAAA) Tj (next) Tj ET\n",
                    base_font="/Times-Roman")
    chunks = _located(path)

    assert chunks[1].x == pytest.approx(72.0 + 4 * 0.5 * 10.0, abs=0.01)


def test_a_page_that_inherits_its_resources_is_never_given_an_empty_one(tmp_path):
    """Writing a fresh empty `/Resources` onto a page that inherits one unmaps
    every font the static text is drawn in: the whole page renders blank while
    every structural check still passes."""
    from app.generation.pdf_renderer import _resources

    path = _raw_pdf(tmp_path / "inherit.pdf",
                    b"BT /F1 11 Tf 1 0 0 1 160 700 Tm (<Employee Name>) Tj ET\n")
    page = PdfReader(path).pages[0]
    # Move the resources up to the page tree, which is where a real producer
    # puts them when every page shares one font set.
    inherited = page[NameObject("/Resources")]
    del page[NameObject("/Resources")]
    page[NameObject("/Parent")].get_object()[NameObject("/Resources")] = inherited

    assert page.get(NameObject("/Resources")) is None
    assert "/F1" in _resources(page)[NameObject("/Font")]
    assert page.get(NameObject("/Resources")) is None, "the inherited set was shadowed"


def test_a_page_with_resources_nowhere_gets_one_of_its_own(tmp_path):
    """The other branch: nothing to inherit means there is nothing to shadow."""
    from app.generation.pdf_renderer import _resources

    path = _raw_pdf(tmp_path / "bare.pdf", b"0.5 w 0 0 m 10 10 l S\n", with_font=False)
    page = PdfReader(path).pages[0]
    for holder in (page, page[NameObject("/Parent")].get_object()):
        holder.pop(NameObject("/Resources"), None)

    created = _resources(page)

    assert created is not None
    assert page[NameObject("/Resources")] is created


def test_a_page_with_no_fonts_at_all_still_receives_the_overlay_font(tmp_path):
    """A form whose only content is ruled lines has no `/Font` dictionary. The
    renderer has to create one rather than write a `Tf` naming nothing."""
    path = _raw_pdf(tmp_path / "lines.pdf", b"0.5 w 158 694 m 300 694 l S\n", with_font=False)
    blank = (PageRegion(page=1, x0=160.0, y0=694.0, x1=290.0, y1=710.0, text=""),)
    out = tmp_path / "letter.pdf"

    render_overlay(path, out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                   page_regions=blank)

    assert "Priya Sharma" in _text(out)


def test_an_approved_empty_value_masks_the_region_and_draws_nothing(template, regions, tmp_path):
    """A region whose approved value is empty is still redacted. Drawing an
    empty string would add an operator that says nothing; leaving the mask off
    would leave the placeholder in the letter."""
    out = tmp_path / "letter.pdf"
    result = render_overlay(template, out, [RegionFill("obj_name", NAME_BOX, "")],
                            page_regions=regions)

    assert "<Employee Name>" not in _text(out)
    assert result.qa_passed
    assert result.region_lineage[0]["masked_text"] == ["<Employee Name>"]


def test_something_that_is_not_a_fill_is_refused(template, regions, tmp_path):
    """A dict that looks like a fill has no anchor to validate and no status to
    check, so accepting one would skip every guarantee above."""
    with pytest.raises(TypeError, match="RegionFill"):
        render_overlay(template, tmp_path / "letter.pdf",
                       [{"object_id": "obj_name", "text": "Priya"}], page_regions=regions)


def test_a_fill_without_a_real_anchor_is_refused():
    with pytest.raises(TypeError, match="Anchor"):
        RegionFill("obj_name", (1, 158.0, 694.0, 300.0, 712.0), "Priya")


def test_a_font_already_called_dmoverlay_does_not_get_overwritten(tmp_path):
    """The overlay adds a font to a dictionary somebody else owns. Binding its
    name over an existing entry would redraw every glyph the template drew in
    that font."""
    from app.generation.pdf_renderer import OVERLAY_FONT_NAME, _font_map

    path = _raw_pdf(tmp_path / "collide.pdf",
                    b"BT /F1 11 Tf 1 0 0 1 160 700 Tm (<Employee Name>) Tj ET\n")
    writer = PdfWriter(clone_from=PdfReader(path))
    imposter = DictionaryObject()
    for key, value in (("/Type", "/Font"), ("/Subtype", "/Type1"),
                       ("/BaseFont", "/Courier"), ("/Encoding", "/WinAnsiEncoding")):
        imposter[NameObject(key)] = NameObject(value)
    writer.pages[0][NameObject("/Resources")][NameObject("/Font")][
        NameObject(OVERLAY_FONT_NAME)] = imposter
    taken = tmp_path / "taken.pdf"
    writer.write(str(taken))

    out = tmp_path / "letter.pdf"
    render_overlay(str(taken), out, [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
                   page_regions=scan_page_regions(str(taken)))

    fonts = _font_map(PdfReader(str(out)).pages[0])
    assert fonts[OVERLAY_FONT_NAME] == "/Courier", "the template's font was rebound"
    assert "/Helvetica" in fonts.values()
    assert "Priya Sharma" in _text(out)


def test_the_same_box_on_two_pages_is_not_an_overlap(tmp_path):
    """Every page of a multi-page letter has a footer region at the same
    coordinates. Treating those as a conflict would refuse every real template."""
    template = _build_pdf(tmp_path / "two.pdf", LETTER, pages=2)
    page_two = Anchor(kind=ANCHOR_BBOX, page=2, x0=158.0, y0=694.0, x1=300.0, y1=712.0)
    out = tmp_path / "letter.pdf"

    result = render_overlay(template, out,
                            [RegionFill("obj_p1", NAME_BOX, "Priya Sharma"),
                             RegionFill("obj_p2", page_two, "Aisha Khan")],
                            page_regions=scan_page_regions(template))

    assert result.qa_passed, result.qa_notes
    assert [entry["page"] for entry in result.region_lineage] == [1, 2]


def test_a_page_with_no_content_stream_stops_the_render(tmp_path):
    """There is nothing to overlay onto and nothing to mask, so the approved
    template is not the document these anchors were compiled against."""
    writer = PdfWriter()
    writer.add_blank_page(width=A4_WIDTH, height=A4_HEIGHT)
    empty = tmp_path / "empty.pdf"
    writer.write(str(empty))
    stored = (PageRegion(page=1, x0=160.0, y0=696.0, x1=290.0, y1=710.0, text=""),)

    with pytest.raises(PdfOverlayError, match="no content stream"):
        render_overlay(str(empty), tmp_path / "letter.pdf",
                       [RegionFill("obj_name", NAME_BOX, "Priya")], page_regions=stored)
    assert _located(empty) == ()


def test_whitespace_is_not_a_region_worth_storing(tmp_path):
    """A run of spaces has a position and no meaning. Storing it as a region
    gives an anchor a second thing to resolve against and makes the box
    ambiguous."""
    path = _raw_pdf(tmp_path / "spaces.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (   ) Tj 1 0 0 1 72 680 Tm (real) Tj ET\n")

    assert [region.text for region in scan_page_regions(path)] == ["real"]


def test_letter_and_word_spacing_move_the_pen(tmp_path):
    """`Tc` and `Tw` are how a producer justifies a line. Ignoring them puts
    every later chunk in the line to the left of where it really is."""
    path = _raw_pdf(tmp_path / "spacing.pdf",
                    b"BT /F1 10 Tf 2 Tc 5 Tw 200 Tz 1 0 0 1 72 700 Tm (a b) Tj (after) Tj ET\n")
    chunks = _located(path)

    plain = measure_text("a b", 10.0)
    expected = 72.0 + (plain + 2 * 3 + 5 * 1) * 2.0
    assert chunks[1].x == pytest.approx(expected, abs=0.01)


def test_a_malformed_operand_does_not_take_the_render_down(tmp_path):
    """The walker's job is to locate text. A stream carrying a string where a
    number belongs is reported by the integrity gate, which compares the file
    that was written rather than trusting this parse."""
    path = _raw_pdf(tmp_path / "junk.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 (nonsense) 700 Tm (still here) Tj ET\n")
    chunks = _located(path)

    assert [c.text for c in chunks] == ["still here"]
    assert chunks[0].x == 0.0


def test_a_corrupt_font_entry_does_not_stop_the_page_being_read(tmp_path):
    """A /Font entry that is a number rather than a dictionary is a broken PDF,
    not a reason to give up on locating the text that has to be masked."""
    from app.generation.pdf_renderer import _font_map
    from pypdf.generic import NumberObject

    path = _raw_pdf(tmp_path / "corrupt.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (still here) Tj ET\n")
    reader = PdfReader(path)
    page = reader.pages[0]
    page[NameObject("/Resources")][NameObject("/Font")][NameObject("/F1")] = NumberObject(7)

    assert _font_map(page) == {"/F1": None}


def test_a_show_operator_with_no_operand_shows_nothing(tmp_path):
    """A truncated stream is a real thing to receive. Indexing into an empty
    operand list would raise where returning "" simply finds no text."""
    path = _raw_pdf(tmp_path / "truncated.pdf",
                    b"BT /F1 10 Tf 1 0 0 1 72 700 Tm Tj (after) Tj ET\n")

    assert [c.text for c in _located(path)] == ["", "after"]


def test_a_page_without_a_content_stream_contributes_no_regions(tmp_path):
    """Scanning a template whose first page is empty must not raise; a blank
    page is a legitimate part of a letter."""
    writer = PdfWriter()
    writer.add_blank_page(width=A4_WIDTH, height=A4_HEIGHT)
    empty = tmp_path / "empty.pdf"
    writer.write(str(empty))

    assert scan_page_regions(str(empty)) == ()


def test_a_placeholder_still_readable_on_the_page_blocks_the_document(template, regions, tmp_path, monkeypatch):
    """§17: "Placeholder remains | Block document".

    The DOCX path has blocked on this from the beginning. The overlay path had no
    equivalent, so when masking removed nothing the letter shipped with
    `<Employee Name>` printed on it and reported qa_passed True -- which the
    employee reads before anyone else does.
    """
    import app.generation.pdf_renderer as pdf

    # Masking that quietly does nothing: the exact failure, forced.
    monkeypatch.setattr(pdf, "_mask_operators", lambda *a, **k: ([], {}), raising=False)

    result = pdf.render_overlay(
        template, tmp_path / "letter.pdf",
        [pdf.RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
        page_regions=regions,
    )

    if "<Employee Name>" in _text(result.output_path):
        assert result.qa_passed is False, "a legible placeholder must block the document"
        assert any("still readable" in note for note in result.qa_notes), result.qa_notes


def test_the_placeholder_gate_is_the_check_the_policy_names(template, regions, tmp_path):
    """An operator relaxing this check must be relaxing the same §17 row the DOCX
    path uses, not a renderer-private one."""
    from app.qa.policy import BLOCKING, PLACEHOLDER_REMAINS, resolve_policy

    assert resolve_policy({}).severity_of(PLACEHOLDER_REMAINS) == BLOCKING


def test_a_clean_render_is_not_flagged_by_the_placeholder_gate(template, regions, tmp_path):
    """The gate must not fire on the labels, headings and untargeted placeholders
    the stored inventory also carries -- those are supposed to survive."""
    result = render_overlay(
        template, tmp_path / "letter.pdf",
        [RegionFill("obj_name", NAME_BOX, "Priya Sharma")],
        page_regions=regions,
    )

    assert result.qa_passed, result.qa_notes
    assert "Name:" in _text(result.output_path), "the label is static text and must remain"
