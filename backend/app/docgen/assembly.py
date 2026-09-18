"""Section text as document blocks: the layout half of an export.

Shared by every module that writes a deliverable from approved section text --
CMC's dossiers and the Safety module's periodic reports. Extracted from
`app.cmc.export` when the second module needed it, rather than copied, and
`app.cmc.export` keeps its names for these as aliases.

Nothing here reads the database. What a table contains is a question for the
module that owns the data; this file only decides where the table goes.
"""

import re

from app.docgen.markers import TABLE_MARKER_RE
from app.templates import blueprint as bp


def split_on_tables(content: str) -> list:
    """The content as an alternating list of ("text", str) and ("table", key)
    parts, in the order they appear.

    Splitting rather than substituting because a table is a block, not a
    string: it becomes a real w:tbl in the document, and a marker replaced by
    text would produce a paragraph that merely looks like one.
    """
    parts = []
    cursor = 0
    for match in TABLE_MARKER_RE.finditer(content or ""):
        text = content[cursor:match.start()]
        if text.strip():
            parts.append(("text", text.strip("\n")))
        parts.append(("table", match.group(1)))
        cursor = match.end()
    tail = (content or "")[cursor:]
    if tail.strip():
        parts.append(("text", tail.strip("\n")))
    return parts


def paragraph_texts(text: str) -> list:
    """A run of prose as its paragraphs.

    Blank lines separate paragraphs; a single newline inside one is a wrapped
    line in somebody's editor rather than a new paragraph, so it becomes a
    space. Getting this backwards produces a document of one-line paragraphs
    that looks broken in Word.
    """
    out = []
    for chunk in re.split(r"\n\s*\n", text or ""):
        line = " ".join(part.strip() for part in chunk.splitlines() if part.strip())
        if line:
            out.append(line)
    return out


def paragraphs(text: str) -> list:
    """Blueprint paragraphs for a run of prose."""
    return [bp.paragraph([bp.segment("static", line)]) for line in paragraph_texts(text)]
