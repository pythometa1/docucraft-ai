"""Two renders of the same input, byte for byte -- the SLO nobody could assert.

§18 puts Reproducibility at 100%: "Identical inputs produce an identical output
after archive timestamps are normalised". It then explains, in the same section,
why this build did not meet it. "A DOCX is a ZIP archive, and default archive
metadata embeds creation timestamps, so two functionally identical renders will
differ byte-for-byte unless the writer normalises them. Fix the timestamps and
ordering in the renderer, and reproducibility becomes something you can assert
in an audit rather than something you have to explain away."

That is not pedantry, and the explaining-away had already started. §6's lock
semantics say a document "can be reproduced from that pair plus the source
record alone", and §17 requires the pair in every document's lineage -- but the
only reproduction check this codebase could run was a *semantic* one.
`tests/golden.py` says so in its own docstring: a naive byte comparison "fails
constantly and gets deleted by the third engineer who trips over it". So the
answer to "is this the file we issued?" was "canonicalise the XML parts and
trust the comparison" rather than "the digests match", which is a materially
weaker thing to put in front of an auditor.

Three things vary between two renders of identical input, and none of them is
document content:

  * Every zip entry header carries the wall-clock second it was written. Two
    renders a second apart differ in every entry.
  * Entry order comes from the writer's walk of the package's relationship
    graph. Stable in practice, guaranteed by nothing.
  * `docProps/core.xml` carries `dcterms:created` / `dcterms:modified`, and
    python-docx *synthesises* that part with `modified = now` for any template
    that does not already ship one -- so the render clock lands inside the
    document for exactly the templates nobody thought to check.

The normaliser therefore rewrites the envelope: fixed entry timestamps, entries
in a declared order, one compression setting, and the document-properties clocks
pinned to a constant. Nothing under `word/` is touched. A normaliser that
altered a byte of the document would be reproducing something other than what
was rendered, which is the one failure that would make this module worse than
having none.

What it does not promise: deflate is deterministic for a given zlib build, not
across every build that has ever existed. Two renders on one deployment are
identical, which is what the SLO is about; comparing across a library upgrade is
what §19's "renderer version recorded in lineage" row exists for.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile

from lxml import etree

#: 1980-01-01T00:00:00 -- the DOS epoch, and the earliest instant a zip entry
#: header can encode. Chosen because it is the one timestamp no reader can
#: mistake for information: a document dated the day the format was invented is
#: obviously a normalised constant rather than a claim about when it was made.
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: The same instant in the W3CDTF form `docProps/core.xml` uses.
FIXED_DOCPROPS_TIMESTAMP = "1980-01-01T00:00:00Z"

#: One compression setting for every entry. Word does not care which, and the
#: point is that the renderer never chooses per-entry.
COMPRESSION = zipfile.ZIP_DEFLATED
COMPRESS_LEVEL = 9

#: MS-DOS/FAT, which is what Word itself writes. The value python-docx leaves
#: behind is the *host* platform (3 == Unix), so a package written on a
#: developer's Mac and one written by a Linux worker differ in every entry
#: header for no reason a reader can act on.
CREATE_SYSTEM = 0
EXTERNAL_ATTR = 0

CORE_PROPERTIES_PART = "docProps/core.xml"

#: The only part whose *content* the normaliser rewrites. The QA gate reads this
#: name from here rather than keeping its own copy: two lists of "what the
#: writer is allowed to change" that drift apart is how a static-region check
#: starts passing over a real edit.
NORMALISED_PARTS = frozenset({CORE_PROPERTIES_PART})

_DCTERMS_NS = "http://purl.org/dc/terms/"
_COREPROPS_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

#: Every element of `docProps/core.xml` that carries a clock. `lastPrinted` is
#: in the list because a template that was ever printed carries one, and a
#: renderer that pinned two of the three would still emit a document whose bytes
#: depended on the machine it ran on.
TIMESTAMP_ELEMENTS = (
    f"{{{_DCTERMS_NS}}}created",
    f"{{{_DCTERMS_NS}}}modified",
    f"{{{_COREPROPS_NS}}}lastPrinted",
)

#: OPC readers locate parts through the central directory, so entry order is
#: free to be anything -- but `[Content_Types].xml` first is what Word writes and
#: what streaming readers expect to find without seeking, so the declared order
#: keeps it there and sorts the rest.
_FIRST_ENTRY = "[Content_Types].xml"


class NotAPackage(ValueError):
    """The path handed to the normaliser is not a readable Office package."""


def entry_order(names) -> list:
    """The declared order: `[Content_Types].xml`, then every other name sorted.

    Sorted rather than "as written", because "as written" is the writer's walk
    of the relationship graph and that is precisely the thing this module
    refuses to depend on.
    """
    rest = sorted(n for n in names if n != _FIRST_ENTRY)
    return ([_FIRST_ENTRY] if _FIRST_ENTRY in set(names) else []) + rest


def normalise_core_properties(raw: bytes) -> bytes:
    """`docProps/core.xml` with every clock pinned to the fixed instant.

    Only the timestamp elements are touched: author, title and revision are
    document content that came from the approved template, and a normaliser that
    quietly erased them would be destroying evidence rather than removing noise.

    Idempotent, which is what makes it usable as a comparison key -- the QA gate
    applies it to the template and to the output and compares the results.
    """
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError as exc:
        raise NotAPackage(
            f"{CORE_PROPERTIES_PART} is not well-formed XML ({exc}); the package cannot be "
            "normalised, and shipping it unnormalised would mean asserting a reproducibility "
            "guarantee this file does not meet"
        ) from exc

    for tag in TIMESTAMP_ELEMENTS:
        for element in root.iter(tag):
            element.text = FIXED_DOCPROPS_TIMESTAMP

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _read_entries(path: str) -> list:
    try:
        with zipfile.ZipFile(path) as archive:
            return [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    except (zipfile.BadZipFile, OSError) as exc:
        raise NotAPackage(f"{path} is not a readable Office package: {exc}") from exc


def normalise_docx(path: str) -> str:
    """Rewrite `path` in place so identical input yields identical bytes.

    Replaces the archive rather than editing it: zip entry headers cannot be
    patched in place without rewriting everything after them, and a half-written
    archive left behind by a crash is a corrupt deliverable. The temp file is
    created beside the target so the final `os.replace` is atomic on the same
    filesystem.

    Returns the path, so a caller can chain it onto a save.
    """
    entries = _read_entries(path)
    by_name = dict(entries)
    if len(by_name) != len(entries):
        # Two entries with one name: which one a reader picks is reader-defined,
        # so the file is ambiguous before it is irreproducible.
        raise NotAPackage(f"{path} contains duplicate entry names and is ambiguous to any reader")

    for part in NORMALISED_PARTS:
        if part in by_name:
            by_name[part] = normalise_core_properties(by_name[part])

    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, staged = tempfile.mkstemp(dir=directory, suffix=".normalising")
    os.close(handle)
    try:
        with zipfile.ZipFile(staged, "w", compression=COMPRESSION, compresslevel=COMPRESS_LEVEL) as out:
            for name in entry_order(by_name):
                info = zipfile.ZipInfo(filename=name, date_time=FIXED_ZIP_TIMESTAMP)
                info.compress_type = COMPRESSION
                info.create_system = CREATE_SYSTEM
                info.external_attr = EXTERNAL_ATTR
                info.internal_attr = 0
                # Extra fields are where a writer records host mtimes and uids at
                # sub-second precision. Emptied rather than normalised: nothing a
                # reader needs lives there.
                info.extra = b""
                info.comment = b""
                out.writestr(info, by_name[name], compress_type=COMPRESSION,
                             compresslevel=COMPRESS_LEVEL)
        os.replace(staged, path)
    except BaseException:
        # Leaving the staged file behind would fill the storage directory with
        # debris that looks like half a document to anything that lists it.
        if os.path.exists(staged):
            os.unlink(staged)
        raise
    return path


def content_digest(path: str) -> str:
    """`sha256:<hex>` over the whole file -- the audit's answer to "is this it?".

    Worth having only because of `normalise_docx`. Before it, this digest
    changed every second and could not be recorded in lineage as anything other
    than a note about when the file was written.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def normalisation_failures(path: str) -> list:
    """Every way `path` still depends on the clock or the writer's walk order.

    Empty means the archive metadata carries nothing a second render could
    change. Used by the tests, and available to an operator asking why two
    renders they believed identical produced different digests.
    """
    failures: list = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if names != entry_order(names):
                failures.append(
                    f"Entries are in the writer's order, not the declared one: {names[:5]}")
            for info in infos:
                if info.date_time != FIXED_ZIP_TIMESTAMP:
                    failures.append(
                        f"{info.filename} carries the write clock {info.date_time} "
                        f"rather than the fixed {FIXED_ZIP_TIMESTAMP}")
                if info.compress_type != COMPRESSION:
                    failures.append(
                        f"{info.filename} uses compression {info.compress_type}, not {COMPRESSION}")
                if info.create_system != CREATE_SYSTEM:
                    failures.append(
                        f"{info.filename} records host platform {info.create_system}, "
                        f"so the bytes depend on which machine rendered it")
                if info.extra:
                    failures.append(f"{info.filename} carries a zip extra field: {info.extra!r}")
            for part in sorted(NORMALISED_PARTS):
                if part in set(names) and archive.read(part) != normalise_core_properties(
                    archive.read(part)
                ):
                    failures.append(f"{part} still carries a real clock")
    except (zipfile.BadZipFile, OSError) as exc:
        raise NotAPackage(f"{path} is not a readable Office package: {exc}") from exc
    return failures
