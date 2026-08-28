"""Fields the manifest declares that no document could ever print.

Every other gate in this package reads a finished letter. This one reads the
manifest, needs no source record and no render, and can therefore say "this
template is broken" the instant compilation ends -- before a data-template
column is generated for the field, before anyone types a value into it, and
before the first letter goes out.

The failure it exists for. This estate writes its conditional logic as English
prose above the content it governs -- `USE IF ON TEMPORARY ASSIGNMENT`,
`(Include if the colleague is working part time hours)`. The compiler is meant
to read those headers as conditions; where it cannot, it falls back to reading
them as instruction text and deleting them, and its idea of "them" ran past the
header and swallowed the content paragraph underneath. On the offer-letter
manifest that shipped, 30 paragraphs were marked `delete_always` and 8 of 26
declared fields ended up with every slot inside one of them: the compiler
declared the field, the data-template generator gave it a spreadsheet column,
the payroll team filled it in, and the letter told a part-time colleague she was
"employed on a full time basis" because the paragraph that would have said
otherwise no longer existed. `qa_passed` was True. It was true of every gate we
had, because a paragraph that is not in the document cannot fail a check that
reads the document.

The contradiction is visible in the manifest alone: a field with a slot is a
promise that a value will be printed somewhere, and a paragraph in
`delete_always` is a promise that nowhere is where it will be printed. Both
promises in one manifest is a compile error we were not raising.

Two refinements, both of which change the answer on real manifests, and both of
which exist to stop this gate blocking letters that work. A gate that fires on a
correct document gets overridden, and an overridden gate protects nothing.

  * A paragraph a block covers is not gone. `blocks` are the branch machinery:
    the renderer rebuilds those paragraphs and keeps whichever branch the record
    selects, so a `delete_always` entry inside a block's range removes the
    instruction, not the line. On the production manifest, paragraph 273 is in
    `delete_always` and is covered by both arms of the recruitment switch
    (`blk_0_transaction_type_with_recruitment` / `..._without_recruitment`), and
    the two fields living there do reach the reader. The same thing is
    reproducible in this repo: compile `templates/compensation_letter.docx` by
    rules alone and `address_line2` looks orphaned; the stored manifest at
    `fixtures/manifests/compensation.json`, which has the blocks, shows it
    covered by `blk_0_show_address_line2` and printing normally. Ignoring blocks
    would have reported 10 orphans instead of 8 on the manifest that was
    actually broken, and would have blocked a template that works.

  * A `delete_always` entry is not always a paragraph deletion. Three of its
    shapes -- `partial`, `remove` and `scope: "span"` -- edit a span and leave
    the line standing, and `app/generation/docx_renderer.py` skips exactly those
    three when it builds `drop_paragraph_indices`. The rule is mirrored here
    rather than approximated, because the difference is not academic:
    `hospira_offer.docx` paragraph 197 reads `Please put collegue first and last
    name from source file  <First Name> <Last Name>` and carries a `partial`
    entry that strips the instruction and keeps the placeholders. Counting it as
    a paragraph deletion reports `first_name` and `last_name` as orphans on a
    template whose three golden letters all sign off "Olivia Williams".

Deliberately out of scope: a field that only *some* records can reach, because
the branch that contains it was not selected. That is a per-document question
and `branch_selection` in `value_lineage_check` already asks it. This check asks
the compile-time one -- could any document, under any record, contain this
field -- and answers it with no data at all.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.qa.policy import QaFinding

#: Registered by the QA wiring, not here. Renaming it invalidates any manifest
#: whose `qa_policy` named it, exactly as for the names in `policy.REGISTRY`.
ORPHANED_FIELD = "orphaned_field"

#: How many paragraph numbers one finding spells out before it starts counting.
#: A field orphaned in twenty paragraphs is one defect with one cause, and a
#: note that fills a screen is a note people stop reading.
MAX_PARAGRAPHS_NAMED = 5


@dataclass(frozen=True)
class Orphan:
    field_id: str
    #: Ascending, de-duplicated: the paragraphs this field's slots sit in, all of
    #: which are unreachable. Kept alongside `slot_count` because two slots in
    #: one paragraph is a different thing for a reviewer to look at than two
    #: slots in two paragraphs.
    paragraphs: tuple
    slot_count: int


def _index(value) -> int | None:
    """`value` as a paragraph index, or None when it is not one.

    `bool` is excluded because `True` is an `int` in Python and would address
    paragraph 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def deletes_whole_paragraph(entry: Mapping) -> bool:
    """Whether this `delete_always` entry removes the line or only edits it.

    The three exceptions are the renderer's, not this module's: `partial` is an
    instruction sharing a span with a placeholder that still has to be filled,
    `remove` is a list of literal control tokens cut out of a span whose
    remaining text survives, and `scope: "span"` is one arm of an inline switch
    being blanked while the static text around it stays. See the
    `drop_paragraph_indices` loop in `app/generation/docx_renderer.py`, which
    skips the same three; `app/compiler/llm_compiler.py` re-derives it too when
    it advances a block start past the scaffolding above it.
    """
    return not (entry.get("partial") or entry.get("remove") or entry.get("scope") == "span")


def block_paragraphs(manifest: dict) -> set[int]:
    """Every paragraph index any block covers, start and end inclusive.

    A block with an unreadable range is read as generously as its numbers allow
    -- reversed endpoints are normalised, one endpoint covers itself -- rather
    than as covering nothing. `validate_manifest` already refuses to approve a
    manifest with `block_range_inverted` or `block_without_range`; piling a
    second, wrong finding on top of the real one only teaches a reviewer that
    this gate is noisy.
    """
    covered: set[int] = set()
    for block in manifest.get("blocks") or ():
        bounds = [i for i in (_index(block.get("start_paragraph")), _index(block.get("end_paragraph"))) if i is not None]
        if bounds:
            covered.update(range(min(bounds), max(bounds) + 1))
    return covered


def unreachable_paragraphs(manifest: dict) -> set[int]:
    """The paragraphs no generated document can contain, whatever the record.

    The complement of this -- a `reachable_paragraphs` set -- is not returnable:
    reachability is the default, so it would have to enumerate every paragraph
    of the template, and the manifest does not carry the template's paragraph
    count. Any bound this module invented would be a guess, and the paragraph
    just past the guess would read as deleted. The unreachable set is finite,
    exact, and derived only from what the manifest actually states, so that is
    what callers get; `p not in unreachable_paragraphs(manifest)` is the
    reachability predicate.
    """
    covered = block_paragraphs(manifest)
    dead: set[int] = set()
    for entry in manifest.get("delete_always") or ():
        if not deletes_whole_paragraph(entry):
            continue
        idx = _index(entry.get("paragraph_index"))
        if idx is not None and idx not in covered:
            dead.add(idx)
    return dead


def orphans(manifest: dict) -> list[Orphan]:
    """Declared fields whose every slot sits in an unreachable paragraph.

    Two kinds of field are passed over, for opposite reasons.

    A field with no slots at all is not orphaned. Those are the condition
    variables -- `colleague_type`, `transaction_type` -- which are bound and read
    and legitimately have no placeholder anywhere. `validate_manifest` reports
    the ones that are genuinely a compiler failure to place a name, under
    `field_without_slot`; reporting them again here would put two findings on one
    defect and make this gate look like it fires on healthy manifests.

    A field with a slot whose `paragraph_index` the manifest does not state is
    not orphaned either. Its location is unknown, unknown is not proven
    unreachable, and this check accuses nothing it cannot show.

    Manifest order is preserved: it is the order the compiler emitted the fields
    and the order a reviewer reads them, and it is stable across runs.
    """
    dead = unreachable_paragraphs(manifest)
    found: list[Orphan] = []
    for field in manifest.get("fields") or ():
        slots = field.get("slots") or ()
        if not slots:
            continue
        indices = [_index(slot.get("paragraph_index")) for slot in slots]
        if any(idx is None or idx not in dead for idx in indices):
            continue  # one slot this check cannot prove dead is enough to keep the field
        found.append(Orphan(
            field_id=str(field.get("id")),
            paragraphs=tuple(sorted(set(indices))),
            slot_count=len(indices),
        ))
    return found


def orphan_note(orphan: Orphan) -> str:
    """The one wording for this failure, so tooling can match it in one place."""
    named = ", ".join(str(p) for p in orphan.paragraphs[:MAX_PARAGRAPHS_NAMED])
    if len(orphan.paragraphs) > MAX_PARAGRAPHS_NAMED:
        named += f" and {len(orphan.paragraphs) - MAX_PARAGRAPHS_NAMED} more"
    where = f"paragraph {named}" if len(orphan.paragraphs) == 1 else f"paragraphs {named}"
    slots = "its only slot is" if orphan.slot_count == 1 else f"all {orphan.slot_count} of its slots are"
    return (
        f"Field '{orphan.field_id}' can never appear in any document: {slots} in {where}, "
        f"which the manifest deletes unconditionally and no block restores -- recompile "
        f"the instruction above that paragraph into a condition, or drop the field."
    )


def orphaned_fields(manifest: dict) -> list[QaFinding]:
    """The compile-time gate: one finding per field the template cannot print.

    Takes no `QaPolicy`. This runs once against a manifest, not once per
    generated document, and the manifest whose `qa_policy` would decide the
    severity is the same manifest under examination -- a broken compile deciding
    how loudly to report itself. Severity is the caller's to route.
    """
    return [QaFinding(check=ORPHANED_FIELD, detail=orphan_note(o)) for o in orphans(manifest)]
