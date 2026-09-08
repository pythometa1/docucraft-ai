"""Turn one uploaded source file into pages and tables, and nothing else.

The whole CSR pipeline is downstream of this module, so it holds one line:
what comes out was IN the file. No summarising, no repair, no filling of a
blank cell with a plausible zero -- a fabricated number that reaches a chunk
becomes a fabricated number with a citation on it, which is the one defect a
medical writer cannot catch by reading the draft.

Two consequences shape the code:

* Tables keep their grid. A table flattened into prose is not a citable
  source, so every extractor returns rows as rows and lets the chunker render
  them; `table_identity` recovers the ICH id ("14.2.1") so that
  "Table 14.2.1 Demographics" stays findable BY its id, which is how a writer
  and a QC reviewer both refer to it.
* Optional readers are imported INSIDE the function that needs them.
  PyMuPDF and striprtf are not installed here, and an import at module scope
  would take the entire CSR router down at startup over a file type nobody in
  this deployment uploads. A missing reader is one file type refused with the
  pip package named in the message, not an outage.
"""

import csv
import importlib
import os
import re
from dataclasses import dataclass, field

#: An ICH table id is dotted ("14.2.1"): a bare "Table 14" is a section
#: cross-reference far more often than it is a post-text table, and a wrong
#: id is worse than none because it makes the WRONG table retrievable.
_IDENTITY_RE = re.compile(
    r"(?:table|listing|figure)\s+([0-9]+(?:\.[0-9]+)+)", re.IGNORECASE)

#: How many leading rows may carry the table's own caption. TLF exports put
#: the id on row 0 and the title on row 1; past row 2 we are reading data,
#: and a subject id that happens to look like "14.2.1" must not become the id.
_IDENTITY_SCAN_ROWS = 3

#: Longest caption still readable as a table's name. Past this it is the
#: sentence that happened to precede the table in the source document.
_MAX_CAPTION_CHARS = 120

#: Suffix -> what the file actually is, for uploads stored under a hash with
#: the extension stripped. The suffix wins when it names a known type; the
#: browser-supplied mime type is only consulted when it does not.
_MIME_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}

#: Legacy binary formats with no pure-python reader. Named separately so the
#: refusal can say what to do instead -- "unsupported" alone sends the user
#: back to re-upload the same .doc and fail again.
_LEGACY_HINTS = {
    ".doc": "save it as .docx",
    ".xls": "save it as .xlsx",
    ".ppt": "save it as .pdf",
    ".pptx": "save it as .pdf",
}


class UnsupportedSource(Exception):
    """The file type has no extractor. Refused, never silently skipped."""


class ExtractorUnavailable(Exception):
    """The optional reader for this file type is not installed.

    Distinct from UnsupportedSource on purpose: this one is fixed by an
    install, so the message names the pip package and the caller can say so.
    """


@dataclass
class ExtractedTable:
    table_id: str | None
    title: str | None
    page: int | None
    rows: list = field(default_factory=list)


@dataclass
class ExtractedPage:
    page: int
    text: str


@dataclass
class Extraction:
    pages: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    page_count: int = 0


# ------------------------------------------------------------ table identity

def _clean_cell(value) -> str:
    """One cell as one line of text.

    Newlines inside a cell are collapsed because the chunker renders one row
    per line: a cell that keeps its newline splits its row in half, and half a
    row of numbers under no column headings is a source nobody can cite.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_rows(rows) -> list:
    """Cells to stripped strings, fully empty leading/trailing rows and empty
    trailing columns dropped. Spreadsheet exports carry a wide margin of blank
    cells; kept, they become columns of nothing that dilute the embedding."""
    grid = [[_clean_cell(cell) for cell in (row or ())] for row in rows or ()]
    while grid and not any(grid[0]):
        grid.pop(0)
    while grid and not any(grid[-1]):
        grid.pop()
    width = 0
    for row in grid:
        for index, cell in enumerate(row):
            if cell:
                width = max(width, index + 1)
    if not width:
        return []
    return [list(row[:width]) + [""] * (width - len(row[:width])) for row in grid]


def _tidy_title(text: str) -> str | None:
    """Strip the separators a caption puts between the id and the title.

    "Table 14.2.1: Demographics", "... -- Demographics" and "... . Demographics"
    are the same title written three ways, and three spellings of one title
    means three tables to a reader scanning the draft.
    """
    title = re.sub(r"^[\s:.\-\u2013\u2014]+", "", text or "").strip()
    title = re.sub(r"\s+", " ", title).strip(" .;:,-")
    return title or None


def table_identity(rows, *, caption: str | None) -> tuple:
    """`(table_id, title)` for a table, from its caption or its own first rows.

    Returns `(None, title)` when nothing in the text carries an id, and
    `(None, None)` when there is no usable title either -- an id is never
    guessed from position or from the surrounding tables, because a table
    filed under a neighbour's id is retrieved in place of that neighbour.
    """
    scanned: list = []
    if caption:
        scanned.append(str(caption))
    for row in list(rows or ())[:_IDENTITY_SCAN_ROWS]:
        cells = [_clean_cell(cell) for cell in (row or ())]
        # De-duplicated: a merged caption cell repeats across the row in most
        # exports, and "Table 14.2.1 Table 14.2.1 Table 14.2.1" is not a title.
        seen: list = []
        for cell in cells:
            if cell and cell not in seen:
                seen.append(cell)
        if seen:
            scanned.append(" ".join(seen))

    for index, text in enumerate(scanned):
        match = _IDENTITY_RE.search(text)
        if not match:
            continue
        title = _tidy_title(text[match.end():])
        if not title:
            # TLF exports routinely put the id on one line and the title on
            # the next; an id with no title is harder to recognise in a draft
            # than one carrying the words the writer knows it by.
            for follower in scanned[index + 1:]:
                if _IDENTITY_RE.search(follower):
                    break
                title = _tidy_title(follower)
                if title and len(title) > _MAX_CAPTION_CHARS:
                    title = None
                if title:
                    break
        return match.group(1), title

    # No id anywhere. A caption is a title; a row of the table is not --
    # naming a table after its own column headings would put "Subject Age Sex"
    # in the chunk's header line, where a reader expects the table's name. A
    # caption too long to be a caption is prose that happened to sit above the
    # table, and is dropped rather than printed as a title.
    title = _tidy_title(caption) if caption else None
    return None, title if title and len(title) <= _MAX_CAPTION_CHARS else None


# --------------------------------------------------------- optional readers

def _require(candidates, package: str):
    """Import the first available module name, or refuse naming the install.

    A tuple of names because PyMuPDF ships as both `pymupdf` and `fitz`
    depending on its age: the modern name is tried first because importing the
    legacy `fitz` alias prints a deprecation warning on every upload, and the
    old name is still tried so a deployment pinned to an older build is not
    told the library is missing.
    """
    if isinstance(candidates, str):
        candidates = (candidates,)
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ExtractorUnavailable(
        f"{package} is not installed -- run `pip install {package}` to read this file type")


# ------------------------------------------------------------- the extractors

def _pdf_captions(text: str) -> list:
    """Caption lines on a page, top to bottom.

    PyMuPDF's table finder returns the grid but not the line above it, which
    is where a TLF puts "Table 14.1.1 Demographics". Recovering the caption
    from the page text is what keeps the id on the table.
    """
    return [line.strip() for line in (text or "").splitlines()
            if _IDENTITY_RE.search(line or "")]


def _pdf_tables(page, number: int, captions) -> list:
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return []
    try:
        found = finder()
        candidates = getattr(found, "tables", None)
        if candidates is None:
            candidates = list(found)
    except Exception:
        # Broad on purpose: table finding is the newest and least stable part
        # of PyMuPDF, it raises differently in every build, and a page whose
        # grid cannot be read is still a page whose TEXT we want. Losing the
        # document over one unreadable table is the worse trade.
        return []

    extracted: list = []
    for table in candidates or ():
        try:
            rows = _clean_rows(table.extract())
        except Exception:
            continue
        if not rows:
            continue
        extracted.append(rows)

    # Zipped by position only when the counts agree. Captions and tables both
    # run down the page, so equal counts pair reliably; unequal counts mean we
    # do not know which caption belongs to which table, and a table wearing
    # its neighbour's id is worse than a table with no id.
    paired = captions if len(captions) == len(extracted) else [None] * len(extracted)
    out: list = []
    for rows, caption in zip(extracted, paired):
        table_id, title = table_identity(rows, caption=caption)
        out.append(ExtractedTable(table_id=table_id, title=title, page=number, rows=rows))
    return out


def _extract_pdf(path: str) -> Extraction:
    fitz = _require(("pymupdf", "fitz"), "PyMuPDF")
    pages: list = []
    tables: list = []
    document = fitz.open(path)
    try:
        for index, page in enumerate(document, start=1):
            text = page.get_text() or ""
            pages.append(ExtractedPage(page=index, text=text))
            tables.extend(_pdf_tables(page, index, _pdf_captions(text)))
    finally:
        document.close()
    return Extraction(pages=pages, tables=tables, page_count=len(pages))


def _extract_docx(path: str) -> Extraction:
    docx = _require("docx", "python-docx")
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)

    # Walking the body in document order, rather than reading `document.tables`
    # on its own, is what gives each table the paragraph immediately above it.
    # In a Word TLF that paragraph IS the caption, and without it the table
    # loses the id it is cited by.
    tables: list = []
    caption = None
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            line = Paragraph(child, document).text.strip()
            if line:
                caption = line
        elif tag == "tbl":
            rows = _clean_rows(
                [[cell.text for cell in row.cells] for row in Table(child, document).rows])
            if rows:
                table_id, title = table_identity(rows, caption=caption)
                # page=1 means "this document", not "Word's page 1": .docx
                # stores no pagination, it is computed by the renderer, and a
                # page number we invented would print inside a citation.
                tables.append(ExtractedTable(
                    table_id=table_id, title=title, page=1, rows=rows))
            caption = None

    return Extraction(pages=[ExtractedPage(page=1, text=text)], tables=tables, page_count=1)


def _extract_rtf(path: str) -> Extraction:
    striprtf = _require("striprtf.striprtf", "striprtf")
    text = striprtf.rtf_to_text(_read_text(path)) or ""
    return Extraction(pages=[ExtractedPage(page=1, text=text)], tables=[], page_count=1)


def _extract_xlsx(path: str) -> Extraction:
    openpyxl = _require("openpyxl", "openpyxl")
    # data_only: a cell holding "=SUM(B2:B9)" is a formula, not a count, and
    # the formula text cited as a number of subjects is a fabrication. Where
    # the workbook carries no cached result the cell reads blank, which is the
    # honest answer.
    book = openpyxl.load_workbook(path, data_only=True, read_only=True)
    tables: list = []
    try:
        for sheet in book.worksheets:
            rows = _clean_rows(sheet.iter_rows(values_only=True))
            if not rows:
                continue
            table_id, title = table_identity(rows, caption=sheet.title)
            # page=None, not 0: a worksheet has no page, and "p.0" in a
            # citation is a number a reviewer would try to look up.
            tables.append(ExtractedTable(
                table_id=table_id, title=title or sheet.title, page=None, rows=rows))
    finally:
        book.close()
    return Extraction(pages=[], tables=tables, page_count=0)


def _extract_csv(path: str) -> Extraction:
    text = _read_text(path)
    # Sniffed, because a semicolon-delimited European export read with a comma
    # delimiter parses as one column: every number lands in the first cell and
    # the table looks empty rather than looking broken.
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except (csv.Error, IndexError):
        pass
    rows = _clean_rows(list(csv.reader(text.splitlines(), delimiter=delimiter)))
    if not rows:
        return Extraction(pages=[], tables=[], page_count=0)
    stem = os.path.splitext(os.path.basename(path))[0]
    table_id, title = table_identity(rows, caption=stem)
    return Extraction(
        pages=[],
        tables=[ExtractedTable(table_id=table_id, title=title or stem, page=None, rows=rows)],
        page_count=0,
    )


def _extract_text(path: str) -> Extraction:
    return Extraction(
        pages=[ExtractedPage(page=1, text=_read_text(path))], tables=[], page_count=1)


def _read_text(path: str) -> str:
    """Decode with replacement rather than raising.

    utf-8-sig because an Excel-exported CSV starts with a BOM, and a BOM glued
    to the first header cell stops "Table 14.1.1" matching anything. Undecodable
    bytes become the replacement character: a visibly damaged word is honest,
    where dropping the line silently loses a row of results.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return handle.read()


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".rtf": _extract_rtf,
    ".xlsx": _extract_xlsx,
    ".csv": _extract_csv,
    ".txt": _extract_text,
    ".md": _extract_text,
}


def extract(path: str, *, mime_type: str | None = None) -> Extraction:
    """Read `path` into pages and tables.

    Raises UnsupportedSource for a type with no extractor and
    ExtractorUnavailable when the reader for a supported type is not
    installed. Neither is ever downgraded to an empty Extraction: a document
    that silently produced no chunks is one a writer generates against and
    only discovers is missing when a section comes back thin.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in _EXTRACTORS:
        # The mime type is the fallback, not the authority: uploads land in
        # storage under a generated name that may have lost its extension,
        # while browsers send "application/octet-stream" for half of these.
        suffix = _MIME_SUFFIXES.get((mime_type or "").split(";")[0].strip().lower(), suffix)
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        hint = _LEGACY_HINTS.get(suffix)
        detail = f" -- {hint}" if hint else ""
        raise UnsupportedSource(
            f"{suffix or 'this file'} cannot be read as a CSR source{detail}. "
            f"Supported: {', '.join(sorted(_EXTRACTORS))}")
    return extractor(path)
