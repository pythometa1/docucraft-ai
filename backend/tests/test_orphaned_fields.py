"""The compile-time gate: a field the manifest declares that no letter can print.

Two things are being defended here, and the second one is the harder of the two.

The first is detection. The offer-letter manifest that shipped marked 30
paragraphs `delete_always` -- the instruction headers and, wrongly, the content
paragraphs beneath them -- and 8 of its 26 fields ended up with every slot inside
one. `on_a_part_time_basis` had a spreadsheet column, a filled-in value, and
nowhere in any document to go. `test_the_shipped_defect_is_caught` is that
manifest in miniature.

The second is silence on templates that work, which is why most of this file is
negatives. Two of them are real manifests compiled from real client masters, and
each one is a false positive the obvious version of this check actually produces:
`hospira_offer.docx` paragraph 197 (a `partial` deletion that strips instruction
prose and keeps the placeholders) and the compensation manifest's paragraph 32
(a `delete_always` line rebuilt by a block). A blocking gate that fires on a
correct letter gets overridden, and once it is overridden it protects nothing --
so a false positive here is a worse defect than a miss.
"""

import dataclasses

import pytest

from app.qa.orphaned_fields import (
    MAX_PARAGRAPHS_NAMED,
    ORPHANED_FIELD,
    deletes_whole_paragraph,
    orphaned_fields,
    orphans,
    unreachable_paragraphs,
)


def _slot(paragraph_index, span_index=0):
    return {"kind": "red_placeholder", "text": "<x>", "paragraph_index": paragraph_index, "span_index": span_index}


def _manifest(**overrides):
    """A healthy manifest: one field in a live paragraph, one deleted header.

    Paragraph 14 is an instruction header the compiler correctly deleted;
    paragraph 15 is the content beneath it and survives. This is what the
    offer-letter template looks like when compilation goes right.
    """
    manifest = {
        "fields": [{"id": "colleague_name", "type": "string", "slots": [_slot(15)]}],
        "conditions": [],
        "blocks": [],
        "delete_always": [{"paragraph_index": 14, "span_index": 0}],
    }
    manifest.update(overrides)
    return manifest


# ----------------------------------------------------------------- positives
def test_the_shipped_defect_is_caught():
    """The compiler swallowed the content paragraph along with its header.

    Paragraph 15 carried `(Include if the colleague is working part time hours)`
    and the sentence it governed. Both went into `delete_always`, so
    `on_a_part_time_basis` was declared, columned, filled -- and unprintable.
    """
    manifest = _manifest(
        fields=[{"id": "on_a_part_time_basis", "slots": [_slot(15)]}],
        delete_always=[{"paragraph_index": 14, "span_index": 0}, {"paragraph_index": 15, "span_index": 0}],
    )
    findings = orphaned_fields(manifest)

    assert len(findings) == 1
    assert findings[0].check == ORPHANED_FIELD
    # A reviewer must be able to act without opening a debugger: the field it is
    # about and the paragraph to go and look at, both in the note.
    assert "on_a_part_time_basis" in findings[0].detail
    assert "paragraph 15" in findings[0].detail


def test_every_orphan_is_reported_once_and_only_orphans_are():
    """The real manifest had eight of these at once, and one live field between
    them. Reporting the live field, or reporting one orphan twice, would both
    make the note untrustworthy."""
    manifest = _manifest(
        fields=[
            {"id": "on_a_part_time_basis", "slots": [_slot(15), _slot(15, 2)]},
            {"id": "colleague_name", "slots": [_slot(11)]},
            {"id": "secondment_duration", "slots": [_slot(27)]},
        ],
        delete_always=[{"paragraph_index": p, "span_index": 0} for p in (14, 15, 26, 27)],
    )
    reported = [o.field_id for o in orphans(manifest)]

    assert reported == ["on_a_part_time_basis", "secondment_duration"]
    assert len(orphaned_fields(manifest)) == 2


def test_a_field_dead_in_several_paragraphs_names_all_of_them():
    """`had_amount` and `pro_rata_as_per_your_hours_of_work` each had slots in
    more than one deleted paragraph. The reviewer needs every one, because
    fixing only the first leaves the field just as unprintable."""
    manifest = _manifest(
        fields=[{"id": "had_amount", "slots": [_slot(32), _slot(127), _slot(32, 4)]}],
        delete_always=[{"paragraph_index": p, "span_index": 0} for p in (32, 127)],
    )
    detail = orphaned_fields(manifest)[0].detail

    assert "paragraphs 32, 127" in detail
    assert "all 3 of its slots" in detail  # three slots, two paragraphs


def test_a_field_dead_everywhere_stops_listing_paragraphs_and_starts_counting():
    """One defect, one cause. A note that spells out twenty paragraph numbers is
    a note people stop reading."""
    dead = list(range(40, 40 + MAX_PARAGRAPHS_NAMED + 3))
    manifest = _manifest(
        fields=[{"id": "scheduled_weekly_hours", "slots": [_slot(p) for p in dead]}],
        delete_always=[{"paragraph_index": p, "span_index": 0} for p in dead],
    )
    detail = orphaned_fields(manifest)[0].detail

    named = ", ".join(str(p) for p in dead[:MAX_PARAGRAPHS_NAMED])
    assert f"paragraphs {named} and {len(dead) - MAX_PARAGRAPHS_NAMED} more" in detail
    assert str(dead[MAX_PARAGRAPHS_NAMED]) not in detail


# ----------------------------------------------------------------- negatives
def test_a_healthy_manifest_is_silent():
    assert orphaned_fields(_manifest()) == []


def test_a_paragraph_a_block_rebuilds_is_not_gone():
    """The check that changes the answer on the manifest that was broken.

    Paragraph 273 of the production manifest is in `delete_always` *and* covered
    by both arms of the recruitment switch. The renderer keeps whichever arm the
    record selects, so the two fields living there do reach the reader. Without
    this, the gate reports ten orphans instead of eight and blocks a template
    that works.
    """
    manifest = _manifest(
        fields=[
            {"id": "gbs_dalian_recruiting_delivery_apac_services", "slots": [_slot(273)]},
            {"id": "country_px_group_services_email_address", "slots": [_slot(273, 4)]},
        ],
        blocks=[
            {"id": "blk_0_transaction_type_with_recruitment", "start_paragraph": 273, "end_paragraph": 273},
            {"id": "blk_0_transaction_type_without_recruitment", "start_paragraph": 273, "end_paragraph": 273},
        ],
        delete_always=[{"paragraph_index": 273, "span_index": 0}],
    )
    assert orphaned_fields(manifest) == []
    assert 273 not in unreachable_paragraphs(manifest)


def test_a_block_protects_the_whole_range_it_spans_inclusive():
    manifest = _manifest(
        fields=[{"id": "bonus_global_performance_plan", "slots": [_slot(p)]} for p in (120, 124, 127)],
        blocks=[{"id": "blk_bonus", "start_paragraph": 120, "end_paragraph": 127}],
        delete_always=[{"paragraph_index": p, "span_index": 0} for p in (120, 124, 127)],
    )
    assert unreachable_paragraphs(manifest) == set()
    assert orphaned_fields(manifest) == []


@pytest.mark.parametrize("entry,why", [
    ({"paragraph_index": 197, "span_index": 0, "partial": True},
     "instruction prose sharing a span with placeholders that still get filled"),
    ({"paragraph_index": 197, "span_index": 0, "remove": ["[[IF x]]", "[[ENDIF]]"]},
     "control tokens cut out of a span whose remaining text survives"),
    ({"paragraph_index": 197, "span_index": 0, "scope": "span"},
     "one arm of an inline switch blanked, static text around it kept"),
])
def test_a_span_scoped_deletion_does_not_delete_the_paragraph(entry, why):
    """`app/generation/docx_renderer.py` skips exactly these three shapes when it
    builds `drop_paragraph_indices`. Reading them as paragraph deletions is what
    would report the hospira signature block as an orphan."""
    manifest = _manifest(fields=[{"id": "first_name", "slots": [_slot(197)]}], delete_always=[entry])

    assert not deletes_whole_paragraph(entry), why
    assert unreachable_paragraphs(manifest) == set()
    assert orphaned_fields(manifest) == []


def test_a_condition_variable_with_no_slots_is_not_an_orphan():
    """`colleague_type` is bound and read and printed nowhere -- that is what a
    condition variable is. `validate_manifest` owns the fields that have no slot
    because the compiler failed to place them (`field_without_slot`); a second
    finding here would put two notes on one defect."""
    manifest = _manifest(fields=[{"id": "colleague_type", "slots": []}, {"id": "transaction_type"}])
    assert orphaned_fields(manifest) == []


def test_one_surviving_slot_keeps_the_field():
    """The field still prints, once. `every one of its slots` is the rule, and a
    partially-deleted field is a different defect for a different gate."""
    manifest = _manifest(
        fields=[{"id": "position_number_description", "slots": [_slot(15), _slot(88)]}],
        delete_always=[{"paragraph_index": 15, "span_index": 0}],
    )
    assert orphaned_fields(manifest) == []


def test_a_slot_with_no_paragraph_index_is_not_proven_dead():
    """Unknown is not unreachable. This check accuses nothing it cannot show."""
    manifest = _manifest(
        fields=[{"id": "had_amount", "slots": [_slot(15), {"kind": "mergefield", "code": "SALARY"}]}],
        delete_always=[{"paragraph_index": 15, "span_index": 0}],
    )
    assert orphaned_fields(manifest) == []


@pytest.mark.parametrize("block", [
    {"id": "b", "start_paragraph": 20, "end_paragraph": 15},   # inverted
    {"id": "b", "start_paragraph": 15},                        # no end
    {"id": "b", "end_paragraph": 15},                          # no start
])
def test_a_malformed_block_range_does_not_manufacture_an_orphan(block):
    """`validate_manifest` already refuses to approve these (`block_range_inverted`,
    `block_without_range`). Stacking a second, wrong finding on the real one only
    teaches a reviewer that this gate is noisy."""
    manifest = _manifest(
        fields=[{"id": "secondment_duration", "slots": [_slot(15)]}],
        blocks=[block],
        delete_always=[{"paragraph_index": 15, "span_index": 0}],
    )
    assert orphaned_fields(manifest) == []


# ------------------------------------------------------------------- shapes
@pytest.mark.parametrize("manifest", [
    {},
    {"fields": [], "blocks": [], "delete_always": []},
    {"fields": None, "blocks": None, "delete_always": None},
    {"fields": [{"id": "x", "slots": [_slot(15)]}]},                      # no delete_always key
    {"delete_always": [{"span_index": 0}]},                               # entry names no paragraph
    {"fields": [{"id": "x", "slots": [_slot(True)]}],                     # bool is not paragraph 1
     "delete_always": [{"paragraph_index": 1, "span_index": 0}]},
])
def test_an_incomplete_manifest_is_read_without_crashing_or_accusing(manifest):
    """A QA check that raises on a half-built manifest is a check nobody can run
    during compilation, which is the only moment this one is useful."""
    assert orphaned_fields(manifest) == []


def test_findings_are_in_manifest_order_and_identical_across_runs():
    """No clock, no randomness, no set iteration leaking into the output: the
    same manifest has to produce the same notes, or the compile snapshots and the
    audit record disagree with themselves."""
    manifest = _manifest(
        fields=[{"id": f, "slots": [_slot(15)]} for f in ("zulu", "alpha", "mike")],
        delete_always=[{"paragraph_index": 15, "span_index": 0}],
    )
    assert [o.field_id for o in orphans(manifest)] == ["zulu", "alpha", "mike"]
    assert orphaned_fields(manifest) == orphaned_fields(manifest)


# ------------------------------------------------------- the real manifests
def _compiled(fixtures_dir, name):
    from app.compiler.rule_compiler import compile_manifest
    from app.templates.parsers.docx_prescan import prescan

    return dataclasses.asdict(compile_manifest(prescan(str(fixtures_dir / "templates" / f"{name}.docx"))))


@pytest.mark.parametrize("template", [
    "icc_ct036_original", "icc_ct040_original", "icc_ct036_template", "icc_ct040_template",
])
def test_the_client_masters_report_no_orphans(fixtures_dir, template):
    """Every one of these compiles into letters the goldens and canaries pass.
    A finding on any of them is this gate being wrong, not the template."""
    assert orphaned_fields(_compiled(fixtures_dir, template)) == []


def test_the_hospira_signature_block_is_not_an_orphan(fixtures_dir):
    """The false positive the naive rule produces on a working template.

    Paragraph 197 reads `Please put collegue first and last name from source
    file  <First Name> <Last Name>` and carries a `partial` delete_always entry:
    the instruction goes, the placeholders stay. All three hospira goldens sign
    off `Olivia Williams` / `Liam ...` / `Emma ...` from exactly that line, so
    reporting `first_name` and `last_name` here would block three letters that
    are correct.
    """
    manifest = _compiled(fixtures_dir, "hospira_offer")
    declared = {f["id"] for f in manifest["fields"]}

    assert {"first_name", "last_name"} <= declared, "fixture changed; this test no longer proves anything"
    assert 197 in {e["paragraph_index"] for e in manifest["delete_always"]}
    assert orphaned_fields(manifest) == []


def test_the_stored_compensation_manifest_reports_no_orphans(fixtures_dir):
    """`address_line2`'s only slot is in paragraph 32, which is in
    `delete_always` and inside `blk_0_show_address_line2`. The block rebuilds the
    line, and the four compensation goldens print the address."""
    import json

    manifest = json.loads((fixtures_dir / "manifests" / "compensation.json").read_text())
    assert orphaned_fields(manifest) == []


def test_the_same_template_without_its_blocks_does_report_the_orphan(fixtures_dir):
    """The other half of the previous test, and the evidence that block coverage
    is load-bearing rather than decorative: compile `compensation_letter.docx` by
    rules alone -- which finds no blocks -- and paragraph 32 really is deleted
    with nothing to rebuild it, so `address_line2` really cannot be printed."""
    manifest = _compiled(fixtures_dir, "compensation_letter")

    assert manifest["blocks"] == [], "fixture changed; this test no longer isolates block coverage"
    assert [o.field_id for o in orphans(manifest)] == ["address_line2"]
