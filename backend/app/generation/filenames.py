"""What a downloaded file is called, and how that name travels in a header.

A filename is the first thing a recipient reads, and it goes wherever the
file goes -- an email attachment, a regulator's portal, a shared drive. The
old names were `{project}_{project#}_{document#}_{language}.docx`: the name of
the sender's internal workspace and two sequential database counters, which
say how the product is organised and roughly how many documents the tenant
has produced. Neither is about the document.

So a document is named after what it is:

    <Document type> - <YYYY-MM-DD> - <8-char id>[ - <language>].<ext>
    Offer Letter - 2026-09-19 - 3f9a1c2b.docx

The id is the first eight hex characters of the document's random UUID --
enough to tell two letters of one day apart, and not a counter. The language
is added only when it is not English, because that is when two otherwise
identical names need telling apart.

`content_disposition` is the one way a name reaches a header: an ASCII
fallback plus RFC 5987 `filename*` with every reserved character
percent-encoded. Starlette's own `FileResponse(filename=...)` leaves "/"
unencoded, which some clients read as a path.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

#: Characters no filesystem or header should receive: path separators,
#: Windows-reserved punctuation, quotes and control characters.
_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_SPACE_RE = re.compile(r"\s+")

MAX_STEM = 120
DEFAULT_TYPE = "Document"


def safe_filename(name: str, *, fallback: str = DEFAULT_TYPE) -> str:
    """`name` with everything a filesystem or a header would trip on removed.

    Separators become a space rather than vanishing, so "3.2.A / 3.2.R" stays
    two readable tokens. Leading and trailing dots and spaces go, because
    Windows drops them and a name beginning with "." is hidden elsewhere.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = _UNSAFE_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip(" .-")
    stem, dot, ext = text.rpartition(".")
    if dot and 0 < len(ext) <= 5 and stem:
        return f"{stem[:MAX_STEM].rstrip(' .')}.{ext}"
    return text[:MAX_STEM].rstrip(" .") or fallback


def _is_english(language: str | None) -> bool:
    code = (language or "en").strip().lower()
    return code in ("", "english") or code == "en" or code.startswith("en-") \
        or code.startswith("en_")


def _utc_date(created_at) -> str:
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        # Stored naive by SQLite; every writer here stores UTC.
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")


def document_filename(*, document_type: str | None, created_at, document_id: str,
                      language: str | None, ext: str) -> str:
    """`<Document type> - <YYYY-MM-DD> - <8-char id>[ - <language>].<ext>`."""
    kind = safe_filename((document_type or "").strip(), fallback=DEFAULT_TYPE)
    short = re.sub(r"[^0-9a-fA-F]", "", document_id or "")[:8].lower() or "00000000"
    parts = [kind, _utc_date(created_at), short]
    if not _is_english(language):
        parts.append(safe_filename(language or "", fallback=""))
    stem = " - ".join(p for p in parts if p)
    return safe_filename(f"{stem}.{ext.lstrip('.')}")


def generated_document_filename(db, document, ext: str = "docx") -> str:
    """The name of one generated document, from its project's document type."""
    from app.models import Project

    project = db.get(Project, document.project_id) if document.project_id else None
    return document_filename(
        document_type=getattr(project, "document_type", None),
        created_at=document.created_at, document_id=document.id,
        language=document.language, ext=ext)


def unique_name(name: str, taken: set) -> str:
    """`name`, or `name (2)`, `name (3)`... -- whichever is not in `taken`.

    Adds the result to `taken`. A zip with two entries of one name silently
    keeps one of them.
    """
    candidate = name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    counter = 2
    while candidate in taken:
        candidate = f"{stem} ({counter})" + (f".{ext}" if ext else "")
        counter += 1
    taken.add(candidate)
    return candidate


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """A Content-Disposition value any client reads the same way.

    `filename=` carries an ASCII-only fallback for old clients; `filename*=`
    carries the real name, UTF-8 and percent-encoded with nothing left safe --
    "/" included.
    """
    name = safe_filename(filename)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = safe_filename(ascii_name.replace("%", " "), fallback="download")
    _stem, dot, ext = name.rpartition(".")
    if dot and not ascii_name.endswith(f".{ext}"):
        # The whole stem was non-ASCII; keep the extension so the fallback
        # still opens in the right program.
        ascii_name = f"download.{ext.encode('ascii', 'ignore').decode() or 'bin'}"
    return (f'{disposition}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name, safe='')}")
