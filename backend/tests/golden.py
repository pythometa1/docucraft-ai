"""Golden-file comparison for generated .docx documents.

The product's claim is that identical input yields identical output. That claim
is only worth anything if something checks it, and checking it is less obvious
than it looks: a .docx is a zip, zip entries carry modification timestamps, and
python-docx rewrites the whole archive on save. Raw bytes therefore differ
between two runs that produced *identical documents*, so a naive
`open(a,'rb').read() == open(b,'rb').read()` fails constantly and gets deleted
by the third engineer who trips over it.

So compare the XML parts, canonicalised (C14N), part by part. That is stable
across runs, ignores attribute ordering and namespace-prefix churn that carry no
meaning, and -- because it reports *which part* differs -- points at the failure
instead of saying "the bytes changed".

Each golden directory holds two files:

    output.docx           the binary, used for the assertion
    document.xml.c14n     the canonicalised main part, checked in for humans

The second is not read by any test. It exists so a reviewer can see a text diff
of what a golden update actually changed, because "binary file differs" in a
pull request is indistinguishable between a deliberate improvement and a
regression that silently corrupts every document from this template.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from lxml import etree

# The parts that carry document meaning. Deliberately excludes docProps/, whose
# created/modified timestamps change on every save and would make every golden
# fail for reasons that have nothing to do with the document.
_MEANINGFUL_SUFFIXES = (".xml", ".rels")
_IGNORED_PREFIXES = ("docProps/",)

MAIN_PART = "word/document.xml"


def _canonical(raw: bytes) -> bytes:
    """Canonicalised where possible, raw bytes where not.

    Real client documents carry parts c14n refuses: a SharePoint-managed .docx
    ships `customXml/item*.xml` content-type schemas that bind namespace
    prefixes as `ct:_=""`, and lxml raises C14NError rather than serialising
    them. Falling back to the raw bytes keeps the part in the comparison instead
    of dropping it — an unmodified part round-trips byte-identically anyway, so
    the check stays just as strict for everything the engine does not touch.
    """
    try:
        return etree.tostring(etree.fromstring(raw), method="c14n")
    except (etree.C14NError, etree.XMLSyntaxError):
        return raw


def docx_xml_parts(path: str | Path) -> dict[str, bytes]:
    """Every meaningful XML part of a .docx, canonicalised."""
    with zipfile.ZipFile(str(path)) as archive:
        return {
            name: _canonical(archive.read(name))
            for name in sorted(archive.namelist())
            if name.endswith(_MEANINGFUL_SUFFIXES) and not name.startswith(_IGNORED_PREFIXES)
        }


def assert_docx_equal(actual: str | Path, expected: str | Path) -> None:
    """Fail with the name of the first differing part, not just 'bytes differ'."""
    a, b = docx_xml_parts(actual), docx_xml_parts(expected)
    if a.keys() != b.keys():
        missing = sorted(b.keys() - a.keys())
        added = sorted(a.keys() - b.keys())
        raise AssertionError(f"part sets differ -- missing: {missing}, unexpected: {added}")
    for name in a:
        if a[name] != b[name]:
            raise AssertionError(
                f"part differs: {name}\n"
                f"  (run `pytest --update-goldens` to accept, and explain why in the PR)"
            )


def assert_matches_golden(actual: str | Path, golden_dir: str | Path, *, update: bool) -> None:
    """Compare against a golden directory, or regenerate it under --update-goldens."""
    actual, golden_dir = Path(actual), Path(golden_dir)
    expected = golden_dir / "output.docx"

    if update or not expected.exists():
        golden_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(actual, expected)
        # The reviewable copy: pretty-printed so a diff is readable line by line.
        parts = docx_xml_parts(actual)
        if MAIN_PART in parts:
            tree = etree.fromstring(parts[MAIN_PART])
            (golden_dir / "document.xml.c14n").write_bytes(
                etree.tostring(tree, pretty_print=True)
            )
        if not update:
            raise AssertionError(
                f"golden did not exist and was created at {golden_dir}. "
                "Review it, then re-run."
            )
        return

    assert_docx_equal(actual, expected)
