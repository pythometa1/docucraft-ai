"""A `.docx` from a client is an untrusted zip archive, not a document.

Every template in this system arrives by upload from outside the trust boundary,
and the pipeline's first act is to open it and walk its XML. That makes the
archive itself an attack surface, independent of anything in the document:

* **Path traversal.** An entry named `../../etc/cron.d/x` or `/etc/passwd`
  escapes the extraction directory. `zipfile.extractall` sanitises these, but
  code that joins entry names by hand does not, and neither does `unzip`.
* **Symlink entries.** A zip can store a symlink; extracting it and then writing
  "into" it writes wherever it points. This is the standard docx-toolchain
  escape and is why the entry mode is checked, not just the name.
* **Decompression bombs.** A few hundred kilobytes can inflate to gigabytes and
  take the worker with it. Both the per-entry and total inflated sizes are
  capped, and the compression ratio is checked before anything is read.
* **Entry-count floods.** Tens of thousands of tiny members exhaust file
  handles and CPU in the parser rather than in the unzip.

XML-level attacks (billion laughs, external entities) are handled separately by
lxml, whose defaults resolve no external entities and forbid DTD loading.

The check reads the central directory only -- it never decompresses -- so it is
cheap enough to run on every upload before any parser sees the file.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Generous next to a real template (the client masters here are 45-62 KB) and
# still far below anything that threatens a worker.
MAX_TOTAL_UNCOMPRESSED = 300 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED = 100 * 1024 * 1024
MAX_ENTRIES = 5_000
MAX_COMPRESSION_RATIO = 200

REQUIRED_MEMBERS = ("word/document.xml",)
_S_IFLNK = 0o120000


class UnsafePackageError(ValueError):
    """Actively dangerous: reject the upload outright. The message names the entry."""


class MalformedPackageError(ValueError):
    """Merely broken -- a truncated download, a .doc renamed to .docx, a PDF.

    Kept separate from `UnsafePackageError` because the two deserve opposite
    handling. A hostile archive must never be stored or parsed; a corrupt one is
    an ordinary user mistake, and the upload is still recorded with an error so
    the person who sent it can see what arrived and why it failed.
    """


@dataclass
class PackageReport:
    entries: int = 0
    total_uncompressed: int = 0
    largest_entry: int = 0
    rejected: list = field(default_factory=list)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # The unix mode lives in the top 16 bits of external_attr, and only for
    # archives written on unix (create_system 3). A zip written by Word never
    # sets it, so this is a strong signal rather than a heuristic.
    if info.create_system != 3:
        return False
    return (info.external_attr >> 16) & 0xF000 == _S_IFLNK


def _is_traversal(name: str) -> bool:
    if name.startswith("/") or name.startswith("\\"):
        return True
    if ":" in name.split("/")[0] and len(name.split("/")[0]) == 2:
        return True  # drive-letter absolute path from a Windows writer
    return any(part == ".." for part in name.replace("\\", "/").split("/"))


def inspect_package(path: str | Path, *, require_document: bool = True) -> PackageReport:
    """Validate the archive. Raises `UnsafePackageError`; returns a report otherwise."""
    report = PackageReport()
    try:
        archive = zipfile.ZipFile(str(path))
    except zipfile.BadZipFile as exc:
        raise MalformedPackageError(f"Not a readable Office package: {exc}") from exc

    with archive:
        infos = archive.infolist()
        report.entries = len(infos)
        if report.entries > MAX_ENTRIES:
            raise UnsafePackageError(f"Package has {report.entries} entries (limit {MAX_ENTRIES})")

        names = set()
        for info in infos:
            name = info.filename
            names.add(name)

            if _is_symlink(info):
                raise UnsafePackageError(f"Package contains a symlink entry: {name!r}")
            if _is_traversal(name):
                raise UnsafePackageError(f"Package entry escapes the extraction root: {name!r}")

            size = info.file_size
            report.total_uncompressed += size
            report.largest_entry = max(report.largest_entry, size)
            if size > MAX_ENTRY_UNCOMPRESSED:
                raise UnsafePackageError(f"Entry {name!r} inflates to {size} bytes (limit {MAX_ENTRY_UNCOMPRESSED})")
            if info.compress_size > 0 and size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise UnsafePackageError(
                    f"Entry {name!r} has a compression ratio of "
                    f"{size // max(1, info.compress_size)}:1 (limit {MAX_COMPRESSION_RATIO}:1)"
                )

        if report.total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            raise UnsafePackageError(
                f"Package inflates to {report.total_uncompressed} bytes (limit {MAX_TOTAL_UNCOMPRESSED})"
            )
        if require_document:
            missing = [m for m in REQUIRED_MEMBERS if m not in names]
            if missing:
                raise MalformedPackageError(f"Not a Word document -- missing {missing}")

    return report
