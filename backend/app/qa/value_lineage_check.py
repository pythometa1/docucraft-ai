"""Did every value the letter promises actually arrive, and did exactly one
version of each conditional clause survive?

Two §17 rows, and they fail in opposite directions.

"Required source value missing -> Block document" is the cheap one to state and
the expensive one to get wrong. The fill engine writes a resolved value into a
run; a field that resolved to nothing writes an empty string, which is
indistinguishable in the finished .docx from a sentence that never had a
placeholder. The only durable record that a value was expected at all is the
`FieldLineage` the fill emits, so that is what this check reads -- and it
recomputes the verdict from the recorded `source` and `on_missing` rather than
trusting the `blocking` flag the renderer wrote. A gate that only repeats the
decision it is auditing is not a gate; recomputing means a QA re-run over a
stored document's lineage can disagree with the renderer that produced it, and
say so.

The branch invariant is the other direction. Every other gate in this package
checks that scaffolding *went*; this one checks that content *stayed*. Where a
template offers mutually exclusive branches over one field -- Full time / Part
time / Fixed Term -- and the record supplies a value for that field, exactly one
of them must survive. Zero means the letter is missing a section; more than one
means it contradicts itself. Both have shipped from this pipeline looking clean:
an inverted condition once dropped a section from 14 of 20 letters, and a switch
the compiler could not read deleted two paragraphs of a client's letter, both
with `qa_passed` True, because nothing was watching for absence.
"""

import re
from collections.abc import Iterable, Mapping

from app.expressions.token_parser import _normalise_scalar
from app.generation.missing_policy import BLOCK
from app.qa.policy import (
    BRANCH_SELECTION,
    REQUIRED_VALUE_MISSING,
    QaFinding,
    QaPolicy,
    findings_for,
)

# `field == 'literal'`, which is what the compiler emits for one arm of a branch
# set. Anything richer is left alone rather than guessed at.
#
# This lives here rather than in the renderer because the branch invariant is
# its only real consumer; `app/generation/source_resolver.py` imports it through
# the renderer's re-export, which is why that alias still exists.
EQUALITY_CONDITION_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*==\s*'(.*)'\s*$")


def blocks_document(source: str, on_missing: str) -> bool:
    """Whether one field's absence is fatal to this document.

    Deliberately total and deliberately dumb: a field that resolved is never
    blocking whatever its policy says, and a field that did not is blocking only
    when the manifest asked for BLOCK. Everything subtler about absence -- what
    counts as missing, what an unreadable policy string falls back to -- was
    already decided in `missing_policy`, and re-deciding it here is how two
    modules end up disagreeing about the same field.
    """
    return source == "missing" and on_missing == BLOCK


def missing_value_note(field_id: str) -> str:
    """The one wording for this failure, so tooling can match it in one place."""
    return f"Required field '{field_id}' has no value in the source record."


def missing_required_failures(field_lineage: Iterable[Mapping]) -> list:
    """Every required field that the source record did not supply.

    Reported once per field id even when a field has several slots and therefore
    several lineage rows: the reviewer has one thing to fix.
    """
    seen, failures = set(), []
    for entry in field_lineage:
        fid = entry.get("field_id")
        if fid in seen:
            continue
        if blocks_document(entry.get("source") or "", entry.get("on_missing") or ""):
            seen.add(fid)
            failures.append(missing_value_note(fid))
    return failures


def branch_count_failures(
    manifest: dict,
    source_record: dict,
    condition_lineage: list,
    drop_block_ids: set,
) -> list:
    """Branch sets that did not resolve to exactly one surviving branch.

    Read twice, from two different places, because neither reading is complete.

    The first groups the manifest's conditions by the field they test, which
    catches an inverted or misread expression. The second reads the document's
    own structure -- two or more span-scoped blocks on one paragraph are a
    switch by construction -- and catches the case the grouping cannot: a branch
    whose condition is missing from the manifest is governed by nothing, so it is
    kept, and the letter names both recruitment teams in one sentence.
    """
    verdicts = {c["condition_id"]: c["result"] for c in condition_lineage}
    groups: dict[str, list[tuple[str, str]]] = {}
    for cond in manifest["conditions"]:
        m = EQUALITY_CONDITION_RE.match(cond.get("expression", ""))
        if m:
            groups.setdefault(m.group(1), []).append((cond["id"], m.group(2)))

    failures = []
    for field_id, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        record_value = source_record.get(field_id)
        if record_value in (None, ""):
            continue  # nothing selects a branch, so no branch is expected
        survivors = [value for cid, value in members if verdicts.get(cid)]

        # How many branches SHOULD survive is the number of conditions testing
        # the record's own value -- not one.
        #
        # One field commonly governs several independent switches. This contract
        # keys three on `colleague_type`: which opening paragraph, which hours
        # clause, and which remuneration table. A full-time colleague must keep
        # exactly one branch of each, so three survive out of seven, and the
        # letter is correct. Expecting one blocked every document from a
        # correctly-compiled template, and the more conditions a compiler finds
        # the more certainly it misfires -- so it punished the compiler for
        # reading the template better.
        #
        # The check it is really making survives intact: an expression that is
        # inverted or misread fires on the wrong value, and the count then stops
        # matching. What is dropped is only the assumption that one field means
        # one switch.
        expected = sum(
            1 for _cid, value in members
            if _normalise_scalar(value) == _normalise_scalar(record_value)
        )
        offered = ", ".join(repr(v) for _cid, v in members)
        if expected == 0:
            # The record names a value no branch offers, so every branch is
            # dropped and the letter is missing the section this switch exists to
            # choose. Counting survivors against `expected` alone would call that
            # agreement -- nought expected, nought survived -- which is exactly
            # the silence this gate was written to break.
            failures.append(
                f"Branch selection on '{field_id}': no branch offers the record's value "
                f"{record_value!r}, so all {len(members)} were dropped and the letter is missing "
                f"this section. Template offers {offered}."
            )
        elif len(survivors) != expected:
            failures.append(
                f"Branch selection on '{field_id}': {len(survivors)} of {len(members)} branches "
                f"survived; {expected} match the record. Record value {record_value!r}; "
                f"template offers {offered}."
            )

    inline_by_paragraph: dict[int, list[dict]] = {}
    for b in manifest["blocks"]:
        if b.get("start_span") is not None:
            inline_by_paragraph.setdefault(b["start_paragraph"], []).append(b)

    for p_idx, members in sorted(inline_by_paragraph.items()):
        if len(members) < 2:
            continue
        survivors = [b["id"] for b in members if b["id"] not in drop_block_ids]
        if len(survivors) != 1:
            failures.append(
                f"Inline switch in paragraph {p_idx}: {len(survivors)} of {len(members)} branches "
                f"survived (expected exactly 1). Surviving: {survivors or 'none'}."
            )
    return failures


def required_value_findings(field_lineage: Iterable[Mapping], policy: QaPolicy) -> list[QaFinding]:
    """The §17 "Required source value missing" gate, re-read from the lineage."""
    return findings_for(REQUIRED_VALUE_MISSING, missing_required_failures(field_lineage), policy)


def branch_findings(
    manifest: dict,
    source_record: dict,
    condition_lineage: list,
    drop_block_ids: set,
    policy: QaPolicy,
) -> list[QaFinding]:
    if not policy.runs(BRANCH_SELECTION):
        return []
    return findings_for(
        BRANCH_SELECTION,
        branch_count_failures(manifest, source_record, condition_lineage, drop_block_ids),
        policy,
    )
