"""An Extraction split into the pieces a section is allowed to cite.

Pure: no session, no filesystem, no embedding call. It takes the dataclasses
`app.csr.extraction` produced and returns dicts shaped like `CsrChunk` rows,
so the whole splitting policy can be tested without a database and stays the
same whatever the caller does with the rows.

The policy exists because retrieval only ever returns whole chunks, which
makes the chunk -- not the document -- the unit a writer cites:

* A TABLE is one chunk, never merged into surrounding prose and never split
  mid-row. Every chunk of a table repeats the table's header line and its
  column header row, because a half-table whose columns are unlabelled is a
  block of numbers nobody can attribute to an arm.
* NARRATIVE text splits on paragraph and sentence boundaries and overlaps,
  so a sentence carrying a result is not cut in half and stranded across two
  chunks that are retrieved separately.
* Nothing is ever split mid-word. A truncated number ("12.4" becoming "12.")
  reads as a real value.
"""

import re

TARGET_TOKENS = 1000
OVERLAP_TOKENS = 120
MAX_TABLE_TOKENS = 1200

#: Doc types whose pages are hard boundaries. A TLF page is one table plus
#: the footnotes belonging to THAT table; a chunk spanning the page break
#: attaches page 12's "excludes 3 patients" to page 13's table, and a footnote
#: read against the wrong table is a wrong number with a citation on it. A
#: safety narrative is one patient per document section for the same reason.
PAGE_LOCAL_TYPES = ("tlf", "narrative")

#: Longest line still plausible as a heading. Past this it is a sentence, and
#: a sentence stored as `section_hint` tells a reviewer nothing about where in
#: the protocol the chunk came from.
MAX_HEADING_CHARS = 80

#: Words a title-cased heading may leave lowercase.
_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
    "the", "to", "with", "versus", "vs",
}

_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

#: Room reserved in a split table's budget for the " (part 2 of 7)" marker, so
#: adding the marker cannot push a part past MAX_TABLE_TOKENS.
_PART_MARKER_TOKENS = 8


def estimate_tokens(text: str) -> int:
    """Characters over four, floor one.

    A heuristic on purpose: the real tokenizer belongs to whichever model the
    generation step uses, importing it here would put a model dependency in a
    pure function, and every budget in this module has slack for the error.
    Floor one so an empty string never reports a zero-cost chunk.
    """
    return max(1, len(text or "") // 4)


# ------------------------------------------------------------------- headings

def _is_all_caps(line: str) -> bool:
    letters = [char for char in line if char.isalpha()]
    return len(letters) >= 2 and all(char.isupper() for char in letters)


def _is_title_case(line: str) -> bool:
    words = line.split()
    if not 1 <= len(words) <= 12:
        return False
    significant = 0
    for word in words:
        core = word.strip("()[]{}:;,.-\"'")
        if not core or not core[0].isalpha():
            continue
        if core.lower() in _SMALL_WORDS:
            continue
        if not core[0].isupper():
            return False
        significant += 1
    return significant >= 1


def _looks_like_heading(line: str) -> bool:
    """A short line that announces a section rather than saying something.

    Numbered ("9.4.6 Blinding"), shouted ("ADVERSE EVENTS") or title-cased
    ("Selection of Study Population") -- the three ways every protocol,
    SAP and prior CSR in the corpus writes one. Trailing full stops disqualify
    the unnumbered forms, because "He Was Withdrawn." is a sentence.
    """
    stripped = (line or "").strip()
    if not stripped or len(stripped) > MAX_HEADING_CHARS:
        return False
    if _NUMBERED_HEADING.match(stripped):
        return True
    if stripped.endswith("."):
        return False
    return _is_all_caps(stripped) or _is_title_case(stripped)


# ------------------------------------------------------------ narrative units

class _Unit:
    """One paragraph, with the page it started on and the heading above it."""

    __slots__ = ("text", "page", "hint")

    def __init__(self, text: str, page, hint):
        self.text = text
        self.page = page
        self.hint = hint


def _split_oversized(text: str, limit_chars: int) -> list:
    """A paragraph too big for one chunk, cut at sentence then word boundaries.

    Never mid-word, at any size. A single word longer than the limit (a run-on
    subject id, a base64 blob in a converted PDF) is emitted whole and over
    budget on purpose: the budget has slack, a severed number does not.
    """
    pieces: list = []
    buffer = ""

    def flush():
        nonlocal buffer
        if buffer.strip():
            pieces.append(buffer.strip())
        buffer = ""

    for sentence in _SENTENCE_SPLIT.split(text):
        if not sentence.strip():
            continue
        if len(sentence) > limit_chars:
            flush()
            words: list = []
            length = 0
            for word in sentence.split():
                if words and length + 1 + len(word) > limit_chars:
                    pieces.append(" ".join(words))
                    words, length = [], 0
                words.append(word)
                length += (1 if length else 0) + len(word)
            if words:
                buffer = " ".join(words)
            continue
        if buffer and len(buffer) + 1 + len(sentence) > limit_chars:
            flush()
        buffer = f"{buffer} {sentence}".strip() if buffer else sentence
    flush()
    return pieces


def _units(extraction, page_local: bool) -> list:
    """Paragraphs in reading order, grouped into runs a chunk may span.

    The heading in effect carries ACROSS pages even when chunks may not: a
    protocol's "9.4.6 Blinding" heading sits at the bottom of one page and its
    prose runs onto the next, and dropping the hint at the page break labels
    that prose with nothing.
    """
    runs: list = []
    current: list = []
    hint = None
    for page in getattr(extraction, "pages", None) or ():
        text = getattr(page, "text", "") or ""
        number = getattr(page, "page", None)
        for block in _PARAGRAPH_SPLIT.split(text):
            lines = [line for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            # A heading on the block's FIRST line labels this block; one
            # further down labels what comes after it. Applying a trailing
            # heading to its own block would file the prose above it under
            # the section it introduces.
            if _looks_like_heading(lines[0]):
                hint = lines[0].strip()
            opening_hint = hint
            for line in lines[1:]:
                if _looks_like_heading(line):
                    hint = line.strip()
            paragraph = "\n".join(line.rstrip() for line in lines).strip()
            if not paragraph:
                continue
            # Split only what is actually oversized: the splitter rejoins on
            # single spaces, and a paragraph that fits should reach the chunk
            # with the line breaks the source gave it.
            pieces = ([paragraph] if len(paragraph) <= TARGET_TOKENS * 4
                      else _split_oversized(paragraph, TARGET_TOKENS * 4) or [paragraph])
            for piece in pieces:
                current.append(_Unit(piece, number, opening_hint))
        if page_local and current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _overlap_tail(text: str, tokens: int) -> str:
    """The last ~`tokens` worth of a chunk, starting at a clean boundary.

    Sentence boundary where the window contains one, word boundary otherwise.
    Starting the next chunk mid-word would put a fragment at the top of a
    retrieved passage, which is exactly where a model looks first.
    """
    limit = tokens * 4
    if len(text) <= limit:
        tail = text
    else:
        tail = text[-limit:]
        boundary = _SENTENCE_SPLIT.search(tail)
        if boundary is not None:
            tail = tail[boundary.end():]
        else:
            space = re.search(r"\s", tail)
            tail = tail[space.end():] if space else ""
    return tail.strip()


def _narrative_chunks(runs) -> list:
    chunks: list = []
    for run in runs:
        buffer: list = []
        chars = 0
        carry = ""
        carry_page = None

        def flush():
            nonlocal buffer, chars, carry, carry_page
            if not buffer:
                return
            own = "\n\n".join(unit.text for unit in buffer)
            content = f"{carry}\n\n{own}" if carry else own
            if content.strip():
                chunks.append({
                    # The page where the chunk's text BEGINS, which is the
                    # carried overlap's page when there is one -- a citation
                    # sending a reviewer one page late is a QC finding.
                    "page": carry_page if carry else buffer[0].page,
                    "section_hint": buffer[0].hint,
                    "is_table": False,
                    "table_id": None,
                    "content": content,
                    "token_count": estimate_tokens(content),
                })
            carry = _overlap_tail(own, OVERLAP_TOKENS)
            carry_page = buffer[-1].page
            buffer = []
            chars = 0

        for unit in run:
            if buffer and (chars + 2 + len(unit.text)) // 4 > TARGET_TOKENS:
                flush()
            buffer.append(unit)
            chars += (2 if chars else 0) + len(unit.text)
        flush()
    return chunks


# --------------------------------------------------------------- table chunks

def _render_row(cells) -> str:
    """One row as one pipe-delimited line.

    A cell containing a pipe is escaped rather than passed through: unescaped
    it fakes an extra column, every value after it shifts one place left, and
    a placebo number read under the active arm is the worst outcome this
    module can produce.
    """
    return " | ".join(str(cell or "").replace("|", r"\|") for cell in cells)


def _header_line(table) -> str:
    """The line that makes the table findable by id.

    Always first in the chunk, and repeated in every part of a split table,
    because "Table 14.2.1" is how the section prompt, the citation and the QC
    reviewer all refer to it. A table with neither id nor title says so rather
    than borrowing an id from anywhere.
    """
    table_id = (getattr(table, "table_id", None) or "").strip()
    title = (getattr(table, "title", None) or "").strip()
    if table_id and title:
        return f"Table {table_id} -- {title}"
    if table_id:
        return f"Table {table_id}"
    if title:
        return title
    return "Table (untitled)"


#: The words a post-text caption starts with. Stripped before a caption row is
#: compared with the header line, because the header line always renders the
#: word "Table" while the source may have written "Listing 16.2.1" or
#: "Figure 14.2.3" -- comparing the two verbatim finds no match, and the
#: caption row then survives AS the column header row, which is the exact
#: failure _drop_caption_rows exists to prevent.
_CAPTION_LABEL = re.compile(r"^(?:table|listing|figure)")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _caption_key(text: str) -> str:
    """A caption reduced to what identifies it: its id and title, no label."""
    return _CAPTION_LABEL.sub("", _squash(text))


def _restates(value: str, banner: str) -> bool:
    """True when a row says only what the header line already says.

    Anchored at one end rather than matched anywhere inside: a caption row is
    the whole banner, or just its id, or just its title, so it always shares a
    start or an end with it. A match floating in the middle of two short
    strings is a coincidence, and dropping on one costs a real row of results.
    """
    if not value or not banner:
        return False
    return (banner.startswith(value) or banner.endswith(value)
            or value.startswith(banner) or value.endswith(banner))


def _drop_caption_rows(rows, header_line: str) -> list:
    """Discard leading rows that only restate the caption already in the header.

    A TLF export puts "Table 14.2.1 Demographics" in a merged cell as row 0,
    which would otherwise be repeated in every part of a split table AS its
    column header row -- leaving the real column names on part 1 only, and the
    numbers in parts 2 and 3 sitting under a title instead of under an arm.
    The id and the title routinely arrive as two separate rows, so this loops
    rather than dropping only row 0.
    """
    banner = _caption_key(header_line)
    while len(rows) > 1:
        distinct = {cell for cell in rows[0] if cell}
        if len(distinct) != 1:
            break
        if not _restates(_caption_key(distinct.pop()), banner):
            break
        rows = rows[1:]
    return rows


def _table_chunks(table) -> list:
    rows = [[str(cell or "").strip() for cell in (row or ())]
            for row in (getattr(table, "rows", None) or ())]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []

    header_line = _header_line(table)
    rows = _drop_caption_rows(rows, header_line)
    column_header = _render_row(rows[0])
    prefix = f"{header_line}\n{column_header}"
    body = [_render_row(row) for row in rows[1:]]

    budget = (MAX_TABLE_TOKENS - _PART_MARKER_TOKENS - estimate_tokens(prefix)) * 4
    groups: list = []
    current: list = []
    length = 0
    for line in body:
        # A row never splits, so a row wider than the whole budget takes a
        # part to itself and goes over. An over-long row is readable; a row
        # cut in half is a set of values under the wrong headings.
        if current and length + 1 + len(line) > max(budget, 1):
            groups.append(current)
            current, length = [], 0
        current.append(line)
        length += (1 if length else 0) + len(line)
    if current or not groups:
        groups.append(current)

    total = len(groups)
    chunks: list = []
    for index, group in enumerate(groups, start=1):
        marker = f" (part {index} of {total})" if total > 1 else ""
        content = "\n".join([f"{header_line}{marker}", column_header, *group]).strip()
        if not content:
            continue
        chunks.append({
            "page": getattr(table, "page", None),
            "section_hint": (getattr(table, "title", None) or header_line)[:MAX_HEADING_CHARS],
            "is_table": True,
            "table_id": getattr(table, "table_id", None),
            "content": content,
            "token_count": estimate_tokens(content),
        })
    return chunks


# ------------------------------------------------------------------ entry point

def chunk_extraction(extraction, *, doc_type: str) -> list:
    """`CsrChunk`-shaped dicts for one extracted document.

    Prose first in page order, then the tables. Not interleaved: a table
    lifted out of a spreadsheet has no page to interleave AT, and ordering
    chunks by a page number half of them do not have would be an invented
    sequence rather than the document's own.
    """
    kind = (doc_type or "").strip().lower()
    chunks = _narrative_chunks(_units(extraction, page_local=kind in PAGE_LOCAL_TYPES))
    for table in getattr(extraction, "tables", None) or ():
        chunks.extend(_table_chunks(table))
    return [chunk for chunk in chunks if chunk["content"].strip()]
