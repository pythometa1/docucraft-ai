"""The editable half of a template: a document body the emitter can write.

`app.manifests.models.ManifestEnvelope` is the contract *about* a document and
cannot be the thing an author edits, for two reasons that are in the code rather
than in anybody's opinion. A `StaticObject` carries `text_hash`, not text
(`semantic_model.py`), and no `.docx` can be emitted from a hash. And
`ManifestEnvelope.__post_init__` requires a non-empty `template_version_id`,
which a template being written from scratch does not yet have.

So a blueprint is two things side by side: this body, and the typed objects from
`app.templates.semantic_model`. Publishing emits both projections from them -- a
real `.docx`, and a manifest -- and neither is the source of truth for the other.

The body's shape is dictated entirely by what
`app.templates.parsers.docx_prescan.prescan` can read back, because the whole
design rests on one property:

    blueprint -> emit -> prescan -> compile -> blueprint'      and    blueprint' == blueprint

An authored template is then indistinguishable from a well-formed legacy one, so
it re-enters the existing pipeline with no special case anywhere: same
pre-scanner, same compiler, same fill engine, same QA gates. Anything this
module models that the pre-scanner cannot recover would break that property
silently, which is why the model is smaller than a word processor's and stops
exactly where `prescan` stops.

Two consequences of that rule are worth stating, because both look like
omissions and neither is.

**Paragraph indices are not stored.** They are derived by `walk_paragraphs`,
which reproduces `docx_prescan._walk_paragraphs` -- document order, descending
into table cells row by row. A stored index is a number that goes stale the
moment a paragraph is inserted above it, silently, with every anchor below it
still reporting a clean resolve. The same reasoning is why `semantic_model`
replaced `(paragraph_index, span_index)` pairs with composite anchors.

**Bold and italic belong to a segment, not to a range inside one.** The
pre-scanner merges adjacent runs into one span on colour and hyperlink state
alone -- `_run_style_key` computes bold and italic but the merge condition does
not test them -- so "Dear " plain followed by "John" bold comes back as a single
span whatever the emitter wrote. A body that modelled them as two segments would
therefore produce a document that reads back as one, and the round-trip property
would fail on a formatting nicety. The span is the unit of formatting everywhere
downstream, so it is the unit here too, and `normalise_body` reports every
distinction the merge costs rather than dropping it quietly.
"""

from app.templates.parsers.docx_prescan import BLUE_RGBS, RED_RGBS


# ---- segment roles ----

STATIC = "static"
PLACEHOLDER = "placeholder"
INSTRUCTION = "instruction"
MERGEFIELD = "mergefield"
HYPERLINK = "hyperlink"

SEGMENT_ROLES = (STATIC, PLACEHOLDER, INSTRUCTION, MERGEFIELD, HYPERLINK)

#: What `docx_prescan._classify_color` will answer for each role's emitted run.
#: This is the contract the emitter is written against: get it wrong and a
#: placeholder reads back as static text, which is a letter that ships with
#: `<Annual Salary>` printed in it.
#:
#: HYPERLINK maps to "black" on purpose. Word's own hyperlink colour is 0563C1,
#: which is *in* `BLUE_RGBS`, so a link styled the way Word styles it would
#: classify as a placeholder and the compiler would try to fill it. Emitted links
#: therefore carry no `w:color` at all and are told apart by `in_hyperlink`,
#: which the pre-scanner tracks separately and the fill engine already protects.
ROLE_COLOR = {
    STATIC: "black",
    PLACEHOLDER: "blue",
    INSTRUCTION: "red",
    HYPERLINK: "black",
}

#: The exact RGB written for each coloured role. Both are the first entry of the
#: pre-scanner's own set, so what is written is by construction inside what is
#: read. A literal here that drifted from those sets would produce a template
#: that compiles to nothing at all, and the compile would report success.
PLACEHOLDER_RGB = "0000FF"
INSTRUCTION_RGB = "FF0000"

ROLE_RGB = {PLACEHOLDER: PLACEHOLDER_RGB, INSTRUCTION: INSTRUCTION_RGB}

# Checked at import, and raised rather than asserted because `python -O` strips
# an assert and this is not a debugging aid. If either literal ever falls out of
# the pre-scanner's sets, every template this module emits compiles to an empty
# manifest -- and the compile reports success, because a document with no
# placeholders in it is a perfectly valid document with no placeholders in it.
if PLACEHOLDER_RGB not in BLUE_RGBS or INSTRUCTION_RGB not in RED_RGBS:
    raise ImportError(
        f"emitted colours have drifted from the pre-scanner's: {PLACEHOLDER_RGB} must be in "
        f"{sorted(BLUE_RGBS)} and {INSTRUCTION_RGB} in {sorted(RED_RGBS)}")

#: A classified colour name back to the RGB that produces it. Used only to
#: reproduce a colour a customer's own file already carried -- a segment read
#: from a legacy document records what it was, and emitting it writes that back.
#: Links this codebase *authors* carry no colour at all (see ROLE_COLOR), because
#: Word's own link blue is inside `BLUE_RGBS` and would compile as a placeholder.
RGB_BY_COLOUR = {"blue": PLACEHOLDER_RGB, "red": INSTRUCTION_RGB, "black": None}


def observed_colour(seg: dict) -> str:
    """The colour this segment will read back as.

    A segment read from a legacy file records the colour it actually had, which
    for a hyperlink is whatever the author gave it -- a real client offer letter
    has a red one. Everything else is defined *by* its colour, so the role
    answers for it.
    """
    return seg.get("colour") or ROLE_COLOR[seg["role"]]


#: Roles that become a `RunSpan`. A MERGEFIELD does not: the pre-scanner's
#: complex-field state machine consumes its whole run sequence into a
#: `MergeField` and resets the span accumulator, so a merge field sitting between
#: two static segments leaves two static spans rather than one merged one.
SPAN_ROLES = frozenset({STATIC, PLACEHOLDER, INSTRUCTION, HYPERLINK})


class BlueprintError(ValueError):
    """A body that cannot be emitted, or was not built by this module."""


# ---- the body ----

def empty_body() -> dict:
    return {"blocks": [], "sect_pr_from": None}


def paragraph(segments=(), *, style: str | None = None) -> dict:
    return {"kind": "paragraph", "style": style, "segments": [dict(s) for s in segments]}


def table(rows) -> dict:
    """`rows` is a list of rows, each a list of cells, each a list of blocks.

    Nested exactly the way `_walk_paragraphs` descends, so a cell may itself hold
    a table and the walk order still matches without a special case.
    """
    return {"kind": "table", "rows": [[list(cell) for cell in row] for row in rows]}


def segment(role: str, text: str = "", **extra) -> dict:
    if role not in SEGMENT_ROLES:
        raise BlueprintError(f"unknown segment role {role!r}; expected one of {list(SEGMENT_ROLES)}")
    out = {"role": role, "text": text}
    out.update(extra)
    return out


def merge_key(seg: dict) -> tuple:
    """What the pre-scanner will merge two adjacent segments on.

    Colour and hyperlink state, and nothing else -- see this module's docstring.
    A MERGEFIELD has no key because it is not a span; returning a unique object
    for it keeps it from ever comparing equal to its neighbours.
    """
    role = seg.get("role")
    if role == MERGEFIELD:
        return (MERGEFIELD, id(seg))
    return (ROLE_COLOR[role], role == HYPERLINK, seg.get("target") or "")


# ---- walking, in the pre-scanner's order ----

def walk_paragraphs(body: dict):
    """`(paragraph_index, paragraph_block, in_table)`, in `prescan`'s order.

    Mirrors `docx_prescan._walk_paragraphs`: body children in document order,
    descending into a table row by row and cell by cell. The index is derived
    here and stored nowhere, which is what keeps it from going stale.
    """
    out = []

    def walk(blocks, in_table):
        for block in blocks:
            kind = block.get("kind")
            if kind == "paragraph":
                out.append((len(out), block, in_table))
            elif kind == "table":
                for row in block.get("rows") or ():
                    for cell in row:
                        walk(cell, True)
            else:
                raise BlueprintError(
                    f"unknown block kind {kind!r}; expected 'paragraph' or 'table'")

    walk(body.get("blocks") or (), False)
    return out


def paragraph_texts(body: dict) -> list[str]:
    """One string per paragraph, summed over its spans.

    Matches `app.compiler.mapping_agent._paragraph_texts`, which sums `RunSpan`
    text and therefore excludes merge fields, and is the text every anchor in
    `semantic_model` is resolved against.
    """
    return [
        "".join(s.get("text") or "" for s in block["segments"] if s.get("role") in SPAN_ROLES)
        for _index, block, _in_table in walk_paragraphs(body)
    ]


def table_paragraph_indices(body: dict) -> set:
    return {i for i, _block, in_table in walk_paragraphs(body) if in_table}


def emits(seg: dict) -> bool:
    """Whether this segment is written into the document at all.

    `emit: False` is how a cleaned template carries an author instruction the
    compile removes: the segment stays in the body so the editor can show it and
    the author can put it back, and it is not written to the file. It therefore
    produces no run, and a run with no text produces no span -- so a segment that
    does not emit must not be counted when predicting span indices, or the
    prediction is one too many for every span after it in the paragraph.
    """
    return seg.get("emit") is not False


def emitted_spans(segments) -> list:
    """The spans this paragraph will actually have, in order.

    `span_plan` answers "where does this segment sit in the body"; this answers
    "what will the pre-scanner find in the file". They differ whenever something
    is marked not to emit, and not only by one:

        static "Signed "  |  instruction "(remove me)"  |  static " by hand"

    Drop the instruction and the two statics become adjacent runs of the same
    colour, which Word stores -- and `prescan` reads -- as **one** span. The
    empty run left behind does not separate them either, because a run with no
    text is skipped without breaking the span it sits in. So removing one
    instruction can cost two spans, and a prediction that only subtracted the
    instruction would be one too many for the rest of the paragraph.

    That is why this merges rather than filters. The body still holds all three
    segments, because the editor has to show the instruction and the author has
    to be able to put it back.
    """
    return _merge_adjacent([seg for seg in segments if emits(seg)])[0]


def span_plan(segments) -> list:
    """`(position_in_segments, span_index)` for every segment that becomes a span.

    Only correct on a normalised paragraph: it assumes no two adjacent
    span-producing segments share a merge key, which is precisely what
    `normalise_body` establishes.
    """
    out, span_index = [], 0
    for position, seg in enumerate(segments):
        if seg.get("role") in SPAN_ROLES:
            out.append((position, span_index))
            span_index += 1
    return out


# ---- normalisation: the guard that makes one segment mean one span ----

def _merge_adjacent(segments) -> tuple:
    """`(merged, notes)` -- adjacent segments Word would store as one run, joined.

    The single definition of the merge rule. `normalise_with_notes` applies it to
    the body so one segment means one span; `emitted_spans` applies it to the
    emitting subset so a prediction about the file accounts for neighbours that
    became adjacent when something between them was dropped. Two copies of this
    would be two answers to the same question.
    """
    merged: list = []
    notes: list = []
    for seg in segments:
        role = seg.get("role")
        if role not in SEGMENT_ROLES:
            raise BlueprintError(
                f"unknown segment role {role!r}; expected one of {list(SEGMENT_ROLES)}")
        if role != MERGEFIELD and not (seg.get("text") or ""):
            continue
        if merged and merge_key(merged[-1]) == merge_key(seg):
            previous = merged[-1]
            for attribute in ("bold", "italic"):
                if bool(previous.get(attribute)) != bool(seg.get(attribute)):
                    notes.append(
                        f"{attribute} on {seg.get('text')!r} was dropped: it sits directly beside "
                        f"{previous.get('text')!r}, which Word stores as the same run, so the two "
                        "cannot carry different emphasis")
            previous["text"] = (previous.get("text") or "") + (seg.get("text") or "")
            continue
        merged.append(dict(seg))
    return merged, notes


def normalise_with_notes(body: dict) -> tuple:
    """`(body, notes)` -- merged so that one segment yields exactly one span.

    This is the single most load-bearing function in the authoring path. Two
    adjacent segments the pre-scanner would merge are *one* span in the emitted
    document, so a body that still holds them as two describes a document that
    does not exist: every `span_index` after them is one too high, and the
    manifest's slots point one span to the left for the rest of the paragraph.
    Normalising before writing makes "one segment, one span" true by
    construction rather than by hope.

    Empty segments are dropped for the same reason -- `prescan` skips a run with
    no text, so a segment that emitted one would be counted here and absent
    there.

    `notes` names every distinction the merge cost. A merge only ever loses bold
    or italic, because those are the only attributes that differ within a merge
    key; it is reported rather than silently applied so an author is told that
    the emphasis they asked for cannot survive this document model.
    """
    notes = []

    def normalise_blocks(blocks):
        return [normalise_block(b) for b in blocks]

    def normalise_block(block):
        kind = block.get("kind")
        if kind == "table":
            return {**block, "rows": [[normalise_blocks(cell) for cell in row]
                                      for row in block.get("rows") or ()]}
        if kind != "paragraph":
            raise BlueprintError(
                f"unknown block kind {kind!r}; expected 'paragraph' or 'table'")

        merged, merge_notes = _merge_adjacent(block.get("segments") or ())
        notes.extend(merge_notes)
        return {**block, "segments": merged}

    return ({**body, "blocks": normalise_blocks(body.get("blocks") or ())}, notes)


def normalise_body(body: dict) -> dict:
    return normalise_with_notes(body)[0]


def is_normalised(body: dict) -> bool:
    return normalise_body(body) == body
