"""The §18 reproducibility SLO, checked rather than asserted.

"Reproducibility | 100% | Identical inputs produce an identical output after
archive timestamps are normalised." Before `app.generation.reproducibility` that
line was a claim nobody could test: `tests/golden.py` compares canonicalised XML
precisely because two renders of the same letter used to differ in every zip
entry header, and it says so in its own docstring.

The failure these tests pin is an audit one rather than a rendering one. §17
requires the template and manifest hashes in every document's lineage so a
document can be reproduced from them; if the reproduction differs from the
original by bytes that nobody can account for, the only honest answer an auditor
gets is "trust our XML comparison". Two of these tests would have passed before
the module existed only because they compare the wrong thing -- so the pair
matters: identical input must give identical bytes, *and* a changed record must
still change them, or the normaliser is just flattening documents into sameness.
"""

from __future__ import annotations

import time
import zipfile
from datetime import date

import pytest
from lxml import etree

from app.compiler.rule_compiler import compile_manifest
from app.generation import docx_renderer as fill_engine
from app.generation.docx_renderer import fill_template
from app.generation.reproducibility import (
    COMPRESSION,
    CORE_PROPERTIES_PART,
    FIXED_DOCPROPS_TIMESTAMP,
    FIXED_ZIP_TIMESTAMP,
    NotAPackage,
    content_digest,
    entry_order,
    normalisation_failures,
    normalise_core_properties,
    normalise_docx,
)
from app.generation.source_ingestion import extract_records
from app.generation.source_resolver import apply_binding, suggest_bindings
from app.qa.layout_integrity import docprops_failures, structural_failures
from app.templates.parsers.docx_prescan import prescan

PINNED_DATE = date(2026, 8, 5)


@pytest.fixture(autouse=True)
def _pin_generation_date(monkeypatch):
    """The template prints the generation date, so an unpinned clock would make
    two renders differ for a reason that has nothing to do with the archive."""
    monkeypatch.setattr(fill_engine, "_today", lambda: PINNED_DATE)


@pytest.fixture(scope="module")
def hospira(fixtures_dir):
    template = fixtures_dir / "templates" / "hospira_offer.docx"
    compiled = compile_manifest(prescan(str(template)))
    manifest = {
        "fields": compiled.fields, "conditions": compiled.conditions,
        "blocks": compiled.blocks, "delete_always": compiled.delete_always,
    }
    columns, records = extract_records(str(fixtures_dir / "records" / "colleagues.xlsx"), "xlsx")
    bindings = suggest_bindings(manifest, columns).as_field_bindings()
    return template, manifest, bindings, {r["Colleague Type"]: r for r in records}


def _render(hospira, out, colleague_type="Full Time"):
    template, manifest, bindings, records = hospira
    return fill_template(str(template), str(out), manifest,
                         apply_binding(records[colleague_type], bindings), locale="en_AU")


# ------------------------------------------------------- the SLO itself ----
def test_two_renders_of_the_same_record_are_byte_identical(hospira, tmp_path):
    """The §18 SLO. Two renders a second apart used to differ in every zip entry
    header, which made the digest in the audit trail a record of when the file
    was written rather than of what it contains."""
    first, second = tmp_path / "first.docx", tmp_path / "second.docx"
    _render(hospira, first)
    # More than a second, because DOS zip timestamps have two-second resolution:
    # a faster test would pass on a renderer that normalises nothing.
    time.sleep(1.1)
    _render(hospira, second)

    assert first.read_bytes() == second.read_bytes()
    assert content_digest(str(first)) == content_digest(str(second))


def test_a_different_record_still_produces_a_different_file(hospira, tmp_path):
    """The other half of the claim. A normaliser that flattened the archive into
    sameness would satisfy the SLO and destroy the product: two employees on
    different salaries would hash identically and the lineage would be worthless."""
    full_time, part_time = tmp_path / "ft.docx", tmp_path / "pt.docx"
    _render(hospira, full_time, "Full Time")
    _render(hospira, part_time, "Part Time")

    assert full_time.read_bytes() != part_time.read_bytes()
    assert content_digest(str(full_time)) != content_digest(str(part_time))


def test_the_rendered_archive_carries_no_write_clock(hospira, tmp_path):
    """Named per-entry so a regression says *which* piece of metadata came back
    rather than only that the bytes moved."""
    out = tmp_path / "letter.docx"
    _render(hospira, out)

    assert normalisation_failures(str(out)) == []
    with zipfile.ZipFile(out) as archive:
        infos = archive.infolist()
        assert [i.filename for i in infos] == entry_order([i.filename for i in infos])
        assert {i.date_time for i in infos} == {FIXED_ZIP_TIMESTAMP}
        assert {i.compress_type for i in infos} == {COMPRESSION}


def test_normalising_does_not_change_a_single_document_part(hospira, tmp_path):
    """The one failure that would make this module worse than not having it: a
    normaliser that reproduces something other than what was rendered."""
    out = tmp_path / "letter.docx"
    _render(hospira, out)
    before = {}
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            before[name] = archive.read(name)

    normalise_docx(str(out))

    with zipfile.ZipFile(out) as archive:
        assert set(archive.namelist()) == set(before)
        for name, raw in before.items():
            assert archive.read(name) == raw, f"{name} was rewritten by re-normalisation"


def test_the_render_still_passes_the_static_region_gate(hospira, tmp_path):
    """Pinning the docProps clocks makes `docProps/core.xml` differ from the
    template's by construction. A gate that read that as an unauthorised edit
    would block every document the renderer produced."""
    out = tmp_path / "letter.docx"
    result = _render(hospira, out)

    assert structural_failures(str(hospira[0]), str(out)) == []
    assert result.qa_passed, result.qa_notes


# ----------------------------------------------------- the docProps part ----
def test_document_properties_lose_their_clocks_but_keep_their_author():
    """Erasing the author or the revision would be destroying evidence that came
    from the approved template, not removing noise."""
    raw = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<cp:coreProperties '
        b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        b'xmlns:dcterms="http://purl.org/dc/terms/">'
        b"<dc:creator>Priya Sharma</dc:creator><cp:revision>38</cp:revision>"
        b"<dcterms:created>2026-07-03T11:07:00Z</dcterms:created>"
        b"<dcterms:modified>2026-08-26T09:14:55Z</dcterms:modified>"
        b"<cp:lastPrinted>2026-08-01T00:00:00Z</cp:lastPrinted>"
        b"</cp:coreProperties>"
    )
    root = etree.fromstring(normalise_core_properties(raw))
    stamps = [e.text for e in root
              if etree.QName(e).localname in ("created", "modified", "lastPrinted")]

    assert stamps == [FIXED_DOCPROPS_TIMESTAMP] * 3
    assert root.findtext("{http://purl.org/dc/elements/1.1/}creator") == "Priya Sharma"
    assert normalise_core_properties(normalise_core_properties(raw)) == \
        normalise_core_properties(raw)


def test_only_the_clock_is_forgiven_in_the_document_properties(tmp_path):
    """Two packages differing only in their timestamps compare clean; differing
    in anything else does not."""
    head = (
        b'<?xml version="1.0"?><cp:coreProperties '
        b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        b'xmlns:dcterms="http://purl.org/dc/terms/">'
    )

    def package(path, creator, created):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                CORE_PROPERTIES_PART,
                head + b"<dc:creator>" + creator + b"</dc:creator>"
                + b"<dcterms:created>" + created + b"</dcterms:created></cp:coreProperties>",
            )
        return str(path)

    same_author_new_clock = (
        package(tmp_path / "a.docx", b"Approved Author", b"2026-07-03T11:07:00Z"),
        package(tmp_path / "b.docx", b"Approved Author", b"2026-08-26T09:14:55Z"),
    )
    assert docprops_failures(*same_author_new_clock) == []

    rewritten_author = (
        same_author_new_clock[0],
        package(tmp_path / "c.docx", b"Somebody Else", b"2026-07-03T11:07:00Z"),
    )
    failures = docprops_failures(*rewritten_author)
    assert len(failures) == 1 and CORE_PROPERTIES_PART in failures[0]


def test_a_template_without_core_properties_does_not_look_like_tampering(tmp_path):
    """python-docx synthesises the part on save. Reporting the writer's own
    addition as an unauthorised one would block every such template."""
    template, output = tmp_path / "t.docx", tmp_path / "o.docx"
    with zipfile.ZipFile(template, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<document/>")
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<document/>")
        archive.writestr(CORE_PROPERTIES_PART, b"<cp/>")

    assert structural_failures(str(template), str(output)) == []


# ------------------------------------------------------------- refusals ----
def test_a_file_that_is_not_a_package_is_refused_loudly(tmp_path):
    """Silently leaving a non-package alone would mean the caller believes it
    normalised a document it never opened."""
    junk = tmp_path / "not.docx"
    junk.write_bytes(b"this is not a zip archive")

    with pytest.raises(NotAPackage):
        normalise_docx(str(junk))
    with pytest.raises(NotAPackage):
        normalisation_failures(str(junk))


def test_malformed_document_properties_stop_the_normalisation(tmp_path):
    """Shipping the file unnormalised would mean asserting a reproducibility
    guarantee it does not meet."""
    broken = tmp_path / "broken.docx"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(CORE_PROPERTIES_PART, b"<cp:coreProperties><unclosed>")

    with pytest.raises(NotAPackage):
        normalise_docx(str(broken))
    assert broken.exists(), "the original must survive a refused normalisation"


def test_a_duplicate_entry_name_is_refused(tmp_path):
    """Which of two same-named entries a reader picks is reader-defined, so the
    file is ambiguous before it is irreproducible."""
    ambiguous = tmp_path / "dupe.docx"
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("word/document.xml", b"<a/>")
        archive.writestr("word/document.xml", b"<b/>")

    with pytest.raises(NotAPackage, match="duplicate"):
        normalise_docx(str(ambiguous))


def test_content_types_is_written_first_whatever_order_it_arrives_in():
    """OPC readers seek the central directory, but Word writes the content-type
    map first and streaming readers expect to find it without seeking."""
    assert entry_order(["word/document.xml", "[Content_Types].xml", "_rels/.rels"])[0] == \
        "[Content_Types].xml"
    assert entry_order(["b.xml", "a.xml"]) == ["a.xml", "b.xml"]


def test_an_unnormalised_archive_names_every_way_it_is_irreproducible(tmp_path):
    """The operator-facing half. "Two renders gave different digests" is not an
    answer anybody can act on; "every entry carries the write clock" is."""
    written_now = tmp_path / "raw.docx"
    with zipfile.ZipFile(written_now, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("word/document.xml", b"<document/>")
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr(
            CORE_PROPERTIES_PART,
            b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            b'metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/">'
            b"<dcterms:created>2026-08-26T09:14:55Z</dcterms:created></cp:coreProperties>",
        )

    failures = normalisation_failures(str(written_now))
    assert any("writer's order" in f for f in failures)
    assert any("write clock" in f for f in failures)
    assert any("compression" in f for f in failures)
    assert any("host platform" in f for f in failures)
    assert any("still carries a real clock" in f for f in failures)

    normalise_docx(str(written_now))
    assert normalisation_failures(str(written_now)) == []


def test_a_failed_normalisation_leaves_no_half_written_debris(tmp_path, monkeypatch):
    """The staged file is named `.normalising` and sits in the storage
    directory. Leaving one behind fills that directory with things that look
    like half a document to anything listing it."""
    import os

    import app.generation.reproducibility as reproducibility

    package = tmp_path / "letter.docx"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("word/document.xml", b"<document/>")
    original = package.read_bytes()

    def refuse(_src, _dst):
        raise OSError("cross-device link")

    monkeypatch.setattr(reproducibility.os, "replace", refuse)
    with pytest.raises(OSError):
        normalise_docx(str(package))

    assert package.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".normalising"] == []
    assert os.path.exists(package)


def test_a_zip_extra_field_is_reported_as_a_source_of_drift(tmp_path):
    """Extra fields are where a writer records host mtimes and uids at
    sub-second precision -- invisible in any part listing, and enough on their
    own to make two identical renders hash differently."""
    package = tmp_path / "extra.docx"
    with zipfile.ZipFile(package, "w") as archive:
        info = zipfile.ZipInfo("word/document.xml", date_time=FIXED_ZIP_TIMESTAMP)
        info.compress_type = COMPRESSION
        info.create_system = 0
        info.extra = b"\x55\x54\x05\x00\x03\x00\x00\x00\x00"
        archive.writestr(info, b"<document/>")

    assert any("extra field" in f for f in normalisation_failures(str(package)))
    normalise_docx(str(package))
    assert normalisation_failures(str(package)) == []


def test_malformed_document_properties_in_the_output_are_a_gate_failure(tmp_path):
    """The gate normalises both sides to compare them. A part that will not
    parse cannot be normalised, and reporting it clean would mean the comparison
    silently never happened."""
    template, output = tmp_path / "t.docx", tmp_path / "o.docx"
    with zipfile.ZipFile(template, "w") as archive:
        archive.writestr(CORE_PROPERTIES_PART, b"<cp/>")
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(CORE_PROPERTIES_PART, b"<cp><unclosed>")

    failures = docprops_failures(str(template), str(output))
    assert len(failures) == 1 and "well-formed" in failures[0]


def test_an_organisation_may_route_the_structural_gate_to_review(tmp_path):
    """Same rule as every other check: the manifest's `qa_policy` decides what a
    finding costs, and there is no way to switch the check off entirely."""
    from app.qa.layout_integrity import findings
    from app.qa.policy import STATIC_REGION_CHANGED, QaPolicy

    off = QaPolicy(severities={STATIC_REGION_CHANGED: "blocking"}, enabled=frozenset())

    assert findings("nowhere.docx", "nowhere.docx", off) == []
