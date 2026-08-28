"""Did the values the record supplied actually reach the letter?

Every other gate in this package asks what the template left behind -- a
bracket, a control token, an instruction addressed to whoever assembles the
document. All of them read the output for things that should not be there.
None of them reads it for things that should.

That asymmetry shipped a letter. The template writes its logic as English prose
headers -- "USE IF ON TEMPORARY ASSIGNMENT", "(Include if the colleague is
working part time hours)" -- sitting above the content they govern. The compiler
read those headers as instruction text to delete and emitted a `delete_always`
entry for the whole paragraph, which took the governed content with it. The
result told a part-time colleague they were "employed on a full time basis", and
lost the offer sentence, the date, the hours of work and nine other paragraphs.
Eight fields the user had filled in resolved to real values and reached nothing.
`qa_passed` was True, because the document was clean of leftovers: the engine
had deleted the paragraph before the fill ran, so there was no placeholder left
to find, no mergefield left unresolved and no instruction left printed. A
document can be immaculate and empty.

The rule here is the mirror image of the rest of the package: a field that
resolved to a non-empty value must have that value findable in the rendered
document. If it is not, the letter is missing content somebody supplied.

Overlap with `value_lineage_check`, stated plainly so the two are not read as
one check twice. `required_value_missing` fires when a field resolved to
*nothing* and the manifest said that was fatal; this one fires only when a field
resolved to *something*. The two are disjoint by construction and neither can
report the other's defect. `branch_selection` is closer -- it also watches for
absence -- but it reasons at block granularity over branch sets, so it sees a
switch that kept zero arms and is blind to a paragraph that no condition ever
governed. The `delete_always` paragraph above is exactly that paragraph.

Three ways this check could accuse a correct document, and what is done about
each. A false blocking finding stops a real letter, and a gate people learn to
override protects nothing.

  * The block was dropped on purpose. A letter that selected "with recruitment"
    correctly contains no without-recruitment email address. So a field is only
    expected to appear when at least one of its slots is *not* covered by a
    block in `drop_block_ids` -- paragraph-scoped or span-scoped, matched the
    same way the renderer matches them.

  * The string in the record is not the string on the page. `78450` renders as
    `$78,450.00` through `value_format`, so comparing against the source record
    would accuse every currency, date and percent field in the estate. What is
    compared here is the value the *renderer resolved* -- the post-`format_value`
    string it actually wrote into the run -- which needs no re-derivation of
    anybody's formatting rules. The only transformation applied after that write
    is whitespace: the inline-switch repair collapses runs of spaces and strips a
    leading one, and babel puts a non-breaking space inside some locales'
    currency forms. Both sides are therefore whitespace-normalised before the
    substring test, and nothing else is normalised -- folding case or stripping
    punctuation would start hiding real substitutions.

  * A condition variable is read and never printed. `transaction_type` decides
    which branch survives and appears nowhere in any letter, correctly. Those
    fields carry no slots, so a field with no slot is skipped. (An unplaceable
    field is `manifest_validation`'s `field_without_slot`, not a content loss.)

Two things this deliberately does not do, because under-reporting is the safe
direction here and silently over-reporting is not:

  * The `REMOVE_SENTENCE` exclusion is paragraph-granular, not sentence-granular.
    When a missing field under that policy deletes its sentence, any other
    field's slot in the same paragraph is excused even if it sat in a different
    sentence that survived. That misses a real loss in a two-sentence paragraph;
    the alternative is blocking a letter for a deletion its own manifest asked
    for.

  * Presence is checked against the whole document, not against the paragraph
    the slot was in. Paragraphs are renumbered by every drop, so requiring a
    positional match would fire on documents that are entirely correct. A value
    that moved is not a value that was lost.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from app.generation.missing_policy import REMOVE_SENTENCE, field_on_missing
from app.qa.policy import QaFinding, QaPolicy, findings_for
from app.templates.parsers.docx_prescan import W_NS

#: Registered by the QA wiring, not here. A field resolved to a value and the
#: rendered document does not contain it.
RESOLVED_VALUE_ABSENT = "resolved_value_absent"

#: How much of a lost value a note quotes. Enough to recognise a salary or a
#: name; short enough that eight of these still fit on a screen.
VALUE_EXCERPT = 60


def _q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


@dataclass(frozen=True)
class LostValue:
    field_id: str
    value: str
    source: str
    #: Template paragraph indices of the slots that were *not* dropped -- where
    #: the value was supposed to land. Empty when the manifest records no
    #: position for any of them.
    expected_paragraphs: tuple


def normalise(text: str) -> str:
    """Collapse every run of whitespace to one space and trim the ends.

    `str.split()` treats U+00A0 and U+202F as whitespace, which is what makes
    this work on babel's currency output.
    """
    return " ".join(text.split())


def paragraph_texts(body_el) -> list:
    """One string per `<w:p>` in the body, tables and text boxes included.

    Per paragraph rather than per `<w:t>`, because a value can be split across
    runs and joining them is the only way to find it again. Never across two
    sibling paragraphs: concatenating the whole body into one string would let
    a name ending one paragraph and a surname starting the next satisfy a check
    for "Anna Smith" that the document does not actually pass.
    """
    return ["".join(t.text or "" for t in p.iter(_q("t"))) for p in body_el.iter(_q("p"))]


def resolved(field_values: Mapping, field_id: str) -> tuple:
    """`(value, source)` for one field, tolerant of a caller passing bare values.

    The renderer holds `{field_id: (value, source)}` and also builds a
    `{field_id: value}` view of it for the overflow gate. Unpacking the second
    shape as though it were the first is not an error that raises -- a
    two-character value would unpack into two characters -- so the shape is
    tested rather than assumed.
    """
    entry = field_values.get(field_id)
    if isinstance(entry, tuple) and len(entry) == 2:
        return entry
    return entry, "unrecorded"


def slot_is_dropped(slot: Mapping, dropped_blocks: Iterable[Mapping]) -> bool:
    """Whether a dropped block covers this slot, so its absence is intended.

    Matched exactly as the renderer matches it. A block with a `start_span` is
    an inline switch and covers a span range on one paragraph; every other block
    covers a paragraph range. A mergefield slot records no span index, and the
    renderer likewise only skips it for a paragraph-scoped drop.

    A slot whose paragraph the manifest does not record is not provably dropped,
    so it is reported as live. This check can only excuse an absence it can
    prove was deliberate.
    """
    p_idx = slot.get("paragraph_index")
    if p_idx is None:
        return False
    s_idx = slot.get("span_index")
    for b in dropped_blocks:
        start_p = b.get("start_paragraph")
        if start_p is None:
            continue
        if b.get("start_span") is not None:
            if s_idx is None or start_p != p_idx:
                continue
            if b["start_span"] <= s_idx <= b.get("end_span", b["start_span"]):
                return True
        elif start_p <= p_idx <= b.get("end_paragraph", start_p):
            return True
    return False


def sentence_removal_paragraphs(fields: Iterable[Mapping], field_values: Mapping) -> set:
    """Paragraphs where a missing `REMOVE_SENTENCE` field deleted a sentence.

    Anything else on those lines may have gone with it, legitimately, because
    the manifest asked for the deletion. Derived from the manifest and the
    resolved values rather than taken as a parameter: `field_on_missing` is a
    pure function of the field, and a missing value is the only way a field
    resolves to None.
    """
    removed = set()
    for f in fields:
        value, _source = resolved(field_values, f.get("id"))
        if value is not None or field_on_missing(f) != REMOVE_SENTENCE:
            continue
        for slot in f.get("slots") or ():
            p_idx = slot.get("paragraph_index")
            if p_idx is not None:
                removed.add(p_idx)
    return removed


def lost_values(
    body_el,
    manifest: Mapping,
    field_values: Mapping,
    drop_block_ids: Collection,
) -> list:
    """Every field that resolved to a value the rendered document does not carry.

    One entry per field, in manifest order, however many slots the field has:
    the reviewer has one thing to look for.
    """
    fields = manifest.get("fields") or ()
    dropped_blocks = [b for b in (manifest.get("blocks") or ()) if b.get("id") in drop_block_ids]
    excused = sentence_removal_paragraphs(fields, field_values)
    haystack = [normalise(text) for text in paragraph_texts(body_el)]

    lost = []
    for f in fields:
        fid = f.get("id")
        value, source = resolved(field_values, fid)
        needle = normalise(str(value)) if value is not None else ""
        if not needle:
            # No value, or one that renders as nothing. A field that resolved to
            # nothing is `required_value_missing`'s to report, and reporting it
            # here too would put two notes on one defect.
            continue

        live = [
            slot
            for slot in (f.get("slots") or ())
            if not slot_is_dropped(slot, dropped_blocks) and slot.get("paragraph_index") not in excused
        ]
        if not live:
            # Either the field is a condition variable with no slot at all, or
            # every place it would have been printed was removed on purpose.
            continue

        if any(needle in text for text in haystack):
            continue

        lost.append(LostValue(
            field_id=fid,
            value=str(value),
            source=source,
            expected_paragraphs=tuple(sorted(
                {s["paragraph_index"] for s in live if s.get("paragraph_index") is not None}
            )),
        ))
    return lost


def _where(paragraphs: tuple) -> str:
    if not paragraphs:
        return "a slot whose paragraph the manifest does not record"
    if len(paragraphs) == 1:
        return f"template paragraph {paragraphs[0]}"
    return "template paragraphs " + ", ".join(str(p) for p in paragraphs)


def content_loss_failures(
    body_el,
    manifest: Mapping,
    field_values: Mapping,
    drop_block_ids: Collection,
) -> list:
    """The reviewer-facing sentence for each lost value.

    Names the field, the value and where it should have appeared, so the fix is
    "look at that paragraph in the template" rather than "reproduce it under a
    debugger".
    """
    return [
        f"Field {lost.field_id!r} resolved to {lost.value[:VALUE_EXCERPT]!r} ({lost.source}) but that "
        f"value appears nowhere in the rendered document. It was placed at {_where(lost.expected_paragraphs)}, "
        f"which no dropped block covers, so the content was deleted rather than omitted by a condition."
        for lost in lost_values(body_el, manifest, field_values, drop_block_ids)
    ]


def findings(
    body_el,
    manifest: Mapping,
    field_values: Mapping,
    drop_block_ids: Collection,
    policy: QaPolicy,
) -> list[QaFinding]:
    """The generation-time content-loss gate.

    `field_values` is the renderer's own `{field_id: (value, source)}` -- the
    formatted strings it wrote -- and `drop_block_ids` the set of blocks its
    conditions decided against. Both exist at the call site already; taking them
    rather than the source record is what keeps this check from re-deriving
    formatting decisions it would get wrong.
    """
    if not policy.runs(RESOLVED_VALUE_ABSENT):
        return []
    return findings_for(
        RESOLVED_VALUE_ABSENT,
        content_loss_failures(body_el, manifest, field_values, drop_block_ids),
        policy,
    )
