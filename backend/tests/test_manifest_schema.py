"""What the typed manifest envelope has to guarantee before it is worth having.

Two failures run underneath most of these tests.

The first is a manifest that cannot prove what it ran. Production pins an exact
(template_hash, manifest_hash) pair, so a hash that shifts when nothing
meaningful changed -- a re-serialisation that reorders keys, an integer that
came back from JSON as a float, a second approver signing the same rules --
makes the pin a nuisance everybody learns to override. A hash that stays still
when an expression changes makes it a lie.

The second is a source field nobody knew was required. `required_source_fields`
is what a batch is validated against before it runs, and every name the closure
misses becomes a blocked document discovered one row at a time.
"""

import types

import pytest

from app.manifests.models import (
    DEFAULT_EXPRESSION_LANG, ManifestEnvelope, ManifestObject, ManifestStatus,
    ObjectType, envelope_from_row, from_legacy_status, is_production_anchor,
    to_legacy_objects, to_legacy_status, to_row_values,
)
from app.manifests.validator import validate_manifest
from app.manifests.versioning import (
    canonical_form, is_editable, is_locked, lock, manifest_hash,
    manifest_hash_matches, pin_failures, required_source_fields,
    source_schema_hash, supersede, template_hash, unstorable_object_types,
)
from app.generation.resolution_engine import CyclicDependencyError


# ---- fixtures in the small sense ----

def _field(object_id="full_name", **attributes):
    base = {
        "type": "string",
        "slots": [{"paragraph_index": 0, "span_index": 0, "text": "<full_name>", "field_id": object_id}],
        "on_missing": "BLOCK",
        "anchor": {"kind": "run_path", "path": "body/p[3]/r[1]", "ordinal": 1},
        "source_ref": None,
        "format": None,
        "status": "APPROVED",
    }
    base.update(attributes)
    return ManifestObject(object_id, ObjectType.FIELD, base)


def _condition(object_id="c1", expression="colleague_type == 'Full time'", **attributes):
    base = {"expression": expression, "keeps_blocks": ["b1"]}
    base.update(attributes)
    return ManifestObject(object_id, ObjectType.CONDITION, base)


def _section(object_id="b1", **attributes):
    base = {"start_paragraph": 4, "end_paragraph": 7}
    base.update(attributes)
    return ManifestObject(object_id, ObjectType.SECTION, base)


def _envelope(**overrides) -> ManifestEnvelope:
    base = dict(
        manifest_id="mf_8f21c4",
        manifest_version=7,
        status=ManifestStatus.DRAFT,
        organization_id="org_pfz_hr_au",
        template_version_id="tv_2291",
        template_family_id="fam_offer_letter_apac",
        template_hash="sha256:" + "9d" * 32,
        source_schema_ref="workday/hr_letter_extract",
        source_schema_hash="sha256:" + "41" * 32,
        renderer_contract={"format": "DOCX", "engine": "ooxml_patch", "version": "3.2.0"},
        qa_policy={"blocking": ["unresolved_placeholder"], "warning": ["long_line"]},
        objects=(_field(), _condition(), _section()),
    )
    base.update(overrides)
    return ManifestEnvelope(**base)


def _row(**overrides):
    """A stand-in for a `TemplateManifest` row, attribute for attribute."""
    base = dict(
        id="mf_row", org_id="org_a", template_version_id="tv_1", version_no=3,
        status="approved",
        fields=[{"id": "full_name", "type": "string", "object_type": "FIELD",
                 "slots": [{"paragraph_index": 0}]}],
        conditions=[{"id": "c1", "expression": "colleague_type == 'Full time'",
                     "object_type": "CONDITION", "keeps_blocks": ["b1"]}],
        blocks=[{"id": "b1", "start_paragraph": 4, "end_paragraph": 7, "object_type": "SECTION"}],
        delete_always=[{"paragraph_index": 9, "span_index": 2}],
        template_hash="sha256:" + "ab" * 32, manifest_hash="sha256:" + "cd" * 32,
        source_schema_ref="workday/extract", source_schema_hash="sha256:" + "ef" * 32,
        expression_lang=DEFAULT_EXPRESSION_LANG,
        renderer_contract={"format": "DOCX", "engine": "ooxml_patch", "version": "3.2.0"},
        required_source_fields=["colleague_type", "full_name"],
        qa_policy={"blocking": [], "warning": []},
        supersedes="mf_older", approved_by="u_1042", approved_at=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ---- the status bridge ----

def test_the_locked_state_is_written_to_the_table_as_approved():
    """§6 names the immutable state LOCKED; this schema has always stored
    "approved". A call site that writes "LOCKED" into the column produces a
    manifest no query for approved manifests can find, so the template silently
    has no live manifest at all."""
    assert to_legacy_status(ManifestStatus.LOCKED) == "approved"
    assert from_legacy_status("approved") is ManifestStatus.LOCKED


def test_every_stored_status_string_reads_back_as_one_of_the_four_states():
    assert from_legacy_status("draft") is ManifestStatus.DRAFT
    assert from_legacy_status("in_review") is ManifestStatus.IN_REVIEW
    assert from_legacy_status("superseded") is ManifestStatus.SUPERSEDED


def test_a_deprecated_manifest_reads_as_superseded_rather_than_as_a_draft():
    """"deprecated" predates §6 and has no state of its own. Reading it as DRAFT
    would make a retired manifest editable and, worse, runnable-once-approved
    again; SUPERSEDED keeps it exactly as retired as it was."""
    assert from_legacy_status("deprecated") is ManifestStatus.SUPERSEDED
    assert not is_editable(from_legacy_status("deprecated"))


def test_an_unreadable_stored_status_raises_instead_of_defaulting():
    """Defaulting an unknown status to DRAFT makes a typo mean "editable";
    defaulting it to LOCKED makes a typo mean "run this in production"."""
    with pytest.raises(ValueError, match="Unknown stored manifest status"):
        from_legacy_status("approvd")


def test_is_locked_gives_the_same_answer_on_both_sides_of_the_bridge():
    assert is_locked("approved") and is_locked(ManifestStatus.LOCKED) and is_locked("LOCKED")
    assert not is_locked("in_review")


# ---- the envelope ----

def test_an_envelope_round_trips_through_plain_json_unchanged():
    """The envelope has to survive the existing JSON columns without a
    migration. Anything that does not round-trip is a field that silently
    resets to its default the first time a manifest is read and written back."""
    envelope = _envelope()
    assert ManifestEnvelope.from_dict(envelope.to_dict()) == envelope


def test_from_dict_refuses_a_key_it_does_not_understand():
    """A misspelled key that is quietly ignored is a policy that was written,
    saved, reviewed and never applied."""
    payload = _envelope().to_dict()
    payload["qa_polciy"] = {"blocking": ["everything"]}
    with pytest.raises(ValueError, match="Unknown manifest envelope keys: qa_polciy"):
        ManifestEnvelope.from_dict(payload)


def test_from_dict_refuses_an_envelope_with_no_identity():
    payload = _envelope().to_dict()
    del payload["organization_id"]
    with pytest.raises(ValueError, match="missing required keys: organization_id"):
        ManifestEnvelope.from_dict(payload)


def test_two_objects_cannot_claim_the_same_id():
    """Both are addressed by id -- by conditions, by the binding screen, by the
    audit line -- so which one applies depends on iteration order."""
    with pytest.raises(ValueError, match="two objects with the same object_id"):
        _envelope(objects=(_field(), _field()))


def test_a_manifest_version_below_one_is_refused():
    with pytest.raises(ValueError, match="manifest_version starts at 1"):
        _envelope(manifest_version=0)


def test_a_locked_envelope_cannot_be_edited_in_place():
    """§6's lock semantics are structural, not advisory: editing produces n+1 in
    DRAFT. An envelope that can be mutated is an envelope production's pin can
    be moved out from under."""
    envelope = _envelope()
    with pytest.raises(Exception):
        envelope.status = ManifestStatus.LOCKED


def test_an_object_declares_a_mandatory_attribute_by_carrying_it_even_as_null():
    """§6's own worked FIELD example carries "format": null. Presence is the
    test: a declared-and-empty attribute is a decision somebody recorded, where
    an absent one is a question nobody answered."""
    assert _field().missing_attributes() == ()
    stripped = ManifestObject("x", ObjectType.FIELD, {"anchor": {}, "source_ref": "source.x"})
    assert set(stripped.missing_attributes()) == {"format", "on_missing", "status"}


def test_the_mandatory_attributes_of_a_condition_follow_the_doc_table():
    condition = ManifestObject("c9", ObjectType.CONDITION, {"expression": "a == 'b'"})
    assert set(condition.missing_attributes()) == {
        "anchor_range", "on_true", "on_false", "test_cases",
    }


def test_colour_is_evidence_and_never_a_production_anchor():
    """A legacy estate that encodes meaning in run colour gives the compiler a
    strong signal, but a single reformat in Word repoints every mapping that
    used colour to address the document."""
    assert is_production_anchor("content_control")
    assert is_production_anchor("mergefield")
    assert is_production_anchor("run_path")
    assert not is_production_anchor("style_or_colour")
    assert not is_production_anchor(None)
    assert not is_production_anchor("whatever_the_compiler_felt_like")


def test_an_unknown_object_type_is_refused_rather_than_stored():
    with pytest.raises(ValueError, match="Unknown manifest object type"):
        ManifestObject("x", "PARAGRAPH", {})


# ---- the bridge onto the table ----

def test_a_row_reads_back_as_the_envelope_it_would_be_written_from():
    """The envelope is only useful if it can be produced from what is already
    stored. A round trip that loses an object turns "introduce the envelope"
    into a data migration."""
    row = _row()
    envelope = envelope_from_row(row, template_family_id="fam_1")
    assert envelope.status is ManifestStatus.LOCKED
    assert envelope.manifest_version == 3
    columns = to_row_values(envelope).columns
    assert columns["status"] == "approved"
    assert columns["fields"] == row.fields
    assert columns["conditions"] == row.conditions
    assert columns["blocks"] == row.blocks


def test_a_section_and_a_table_row_survive_sharing_the_blocks_column():
    """Both are regions and both live in `blocks`; without the recorded type a
    repeated table row comes back as a plain removable section, and its
    iteration is lost."""
    envelope = _envelope(objects=(
        _section("b1"),
        ManifestObject("b2", ObjectType.TABLE_ROW,
                       {"anchor_row": 2, "iterate_over": "benefits", "column_refs": {}}),
    ))
    row = types.SimpleNamespace(**{**vars(_row()), **to_row_values(envelope).columns})
    back = envelope_from_row(row)
    assert [o.object_type for o in back.objects] == [ObjectType.SECTION, ObjectType.TABLE_ROW]


def _signature(object_id="sig_1"):
    return ManifestObject(object_id, ObjectType.SIGNATURE, {
        "anchor": {}, "signer_source_ref": "source.signatory_name",
        "image_policy": "none", "esign_ref": None,
    })


def test_an_object_type_the_table_has_no_column_for_is_named_not_dropped():
    """A STATIC or SIGNATURE object written into a row that cannot hold it is
    approved manifest content thrown away silently. The mapping reports it so a
    caller cannot miss it.

    This is the three-legacy-list projection, which is still what a caller gets
    unless it asks for the `objects` column -- so a writer that has not been
    taught to emit that column keeps being told what its write would discard.
    """
    envelope = _envelope(objects=(_field(), _signature()))
    mapping = to_row_values(envelope)
    assert mapping.unmapped == ("sig_1",)
    assert not mapping.is_complete()
    assert ObjectType.SIGNATURE in unstorable_object_types()


def test_the_objects_column_holds_every_type_the_three_lists_cannot():
    """Opting in leaves nothing unmapped, because nothing is lost.

    Five of §6's ten types -- STATIC, NARRATIVE, HEADER, FOOTER, SIGNATURE --
    have no legacy list. A real offer letter has a signature block, so this was
    the common case rather than the edge one, and it is what made inheriting an
    approved manifest refuse outright rather than carry it across.
    """
    envelope = _envelope(objects=(_field(), _signature()))
    mapping = to_row_values(envelope, objects_column=True)

    assert mapping.unmapped == ()
    assert mapping.is_complete()
    assert [o["object_id"] for o in mapping.columns["objects"]] == ["full_name", "sig_1"]
    assert unstorable_object_types(objects_column=True) == ()


def test_both_projections_are_written_together_never_one_without_the_other():
    """`fill_template`, `validate_manifest`, the source resolver and the data
    template builder all read `fields`/`conditions`/`blocks`. A write that
    filled only `objects` would leave every one of them reading an empty
    manifest -- a template that compiles, approves, and fills in nothing."""
    envelope = _envelope(objects=(_field(), _condition(), _section(), _signature()))
    columns = to_row_values(envelope, objects_column=True).columns
    legacy = to_legacy_objects(envelope)

    assert columns["fields"] == legacy["fields"]
    assert columns["conditions"] == legacy["conditions"]
    assert columns["blocks"] == legacy["blocks"]
    assert [o["object_id"] for o in columns["objects"]] == [
        "full_name", "c1", "b1", "sig_1"]


def test_a_row_written_with_the_objects_column_round_trips_every_type():
    """The point of the column: what goes in comes back out, signature included.
    Through the legacy lists alone `sig_1` does not survive the trip at all."""
    envelope = _envelope(objects=(_field(), _condition(), _section(), _signature()))
    columns = to_row_values(envelope, objects_column=True).columns
    row = types.SimpleNamespace(**{**vars(_row()), **columns})

    back = envelope_from_row(row, template_family_id=envelope.template_family_id)
    assert back.objects == envelope.objects
    assert ObjectType.SIGNATURE in {o.object_type for o in back.objects}


def test_a_row_that_predates_the_objects_column_reads_exactly_as_it_did_before():
    """Introducing the column changed the envelope of no existing row. An empty
    `objects` means "written before the column existed, or by a caller that did
    not opt in", and the three lists are read instead."""
    legacy_only = _row()                    # no `objects` attribute at all
    empty_column = _row(objects=[])         # the column, defaulted

    assert envelope_from_row(empty_column).objects == envelope_from_row(legacy_only).objects
    assert [o.object_id for o in envelope_from_row(empty_column).objects] == [
        "full_name", "c1", "b1"]


def test_the_objects_column_wins_over_the_legacy_lists_rather_than_merging():
    """A row carries both projections of the same envelope, so merging them
    would list every FIELD twice -- and `ManifestEnvelope` refuses duplicate
    object ids, which turns the merge into an unreadable manifest rather than a
    subtle one."""
    envelope = _envelope(objects=(_field(), _condition(), _section()))
    row = types.SimpleNamespace(
        **{**vars(_row()), **to_row_values(envelope, objects_column=True).columns})

    back = envelope_from_row(row)
    assert [o.object_id for o in back.objects] == ["full_name", "c1", "b1"]


def test_writing_an_envelope_back_never_touches_delete_always():
    """`delete_always` is instruction text the renderer strips, not a §6 object.
    A mapping that emitted it would blank the compiler's work every time a
    manifest was read and saved, and every deleted instruction phrase would
    reappear in the letter."""
    assert "delete_always" not in to_row_values(_envelope()).columns


def test_the_envelope_produces_the_shape_the_existing_validator_reads():
    """The point of the projection: `validate_manifest`, the source resolver and
    the fill engine all read {fields, conditions, blocks}, so the envelope can
    be introduced without rewriting any of them."""
    assert validate_manifest(to_legacy_objects(_envelope())) == []


# ---- canonical hashing ----

def test_key_order_does_not_change_the_manifest_hash():
    """Two logically identical manifests must hash identically, or every
    round trip through JSON looks like a change to the contract."""
    a = _envelope(qa_policy={"blocking": ["x"], "warning": ["y"]})
    b = _envelope(qa_policy={"warning": ["y"], "blocking": ["x"]})
    assert manifest_hash(a) == manifest_hash(b)


def test_object_order_does_not_change_the_manifest_hash():
    ordered = _envelope(objects=(_field(), _condition(), _section()))
    shuffled = _envelope(objects=(_section(), _field(), _condition()))
    assert manifest_hash(ordered) == manifest_hash(shuffled)


def test_an_integral_float_hashes_as_the_integer_it_equals():
    """A rounding of 2 read back from JSON as 2.0 is the same instruction. If
    the digest disagreed, a manifest would fail its own pin after a round trip
    through the database."""
    assert canonical_form({"rounding": 2}) == canonical_form({"rounding": 2.0})
    assert canonical_form(0.1) != canonical_form(0.2)


def test_a_number_and_the_string_that_looks_like_it_hash_differently():
    """`"1"` and `1` type-check differently against a source schema, so a hash
    that conflated them would call two different contracts the same one."""
    assert canonical_form(1) != canonical_form("1")
    assert canonical_form(True) != canonical_form(1)


def test_a_list_of_strings_cannot_be_confused_with_one_joined_string():
    assert canonical_form(["a", "b"]) != canonical_form(["a,b"])


def test_a_non_finite_number_refuses_to_hash():
    """NaN is not equal to itself, so a manifest containing one could never
    verify against its own pin."""
    with pytest.raises(ValueError, match="non-finite"):
        canonical_form({"confidence": float("nan")})


def test_a_value_the_encoder_does_not_recognise_raises():
    """Stringifying an unknown object into the digest produces a hash only the
    process that built it can reproduce."""
    with pytest.raises(TypeError, match="Cannot canonicalise"):
        canonical_form({"when": object()})


def test_changing_an_expression_changes_the_manifest_hash():
    before = _envelope()
    after = _envelope(objects=(_field(), _condition(expression="colleague_type == 'Part time'"), _section()))
    assert manifest_hash(before) != manifest_hash(after)


def test_changing_one_object_attribute_changes_the_manifest_hash():
    before = _envelope()
    after = _envelope(objects=(_field(on_missing="BLANK"), _condition(), _section()))
    assert manifest_hash(before) != manifest_hash(after)


def test_changing_the_qa_policy_changes_the_manifest_hash():
    assert manifest_hash(_envelope()) != manifest_hash(_envelope(qa_policy={"blocking": []}))


def test_changing_the_renderer_contract_changes_the_manifest_hash():
    """§19 wants a renderer version change attributable rather than argued
    about; the manifest declares which renderer it expects, so an upgrade is a
    visible decision."""
    contract = {"format": "DOCX", "engine": "ooxml_patch", "version": "3.3.0"}
    assert manifest_hash(_envelope()) != manifest_hash(_envelope(renderer_contract=contract))


def test_who_approved_a_manifest_is_outside_its_hash():
    """Sealing writes `approved_by`, `approved_at` and `manifest_hash` onto the
    envelope. If the digest covered them, computing it would change the value
    being computed and no two systems would ever agree on the pin."""
    unsigned = _envelope()
    signed = _envelope(approved_by="u_1042", approved_at="2026-08-25T11:04:19Z",
                       manifest_hash="sha256:" + "00" * 32)
    assert manifest_hash(unsigned) == manifest_hash(signed)


def test_which_org_owns_a_manifest_is_outside_its_hash():
    """Two tenants running byte-identical rules hold the same contract, which is
    what makes "every manifest in this family is the same contract" checkable."""
    assert manifest_hash(_envelope()) == manifest_hash(_envelope(
        organization_id="org_other", manifest_id="mf_other", manifest_version=1,
    ))


def test_a_stale_required_source_field_list_does_not_move_the_hash():
    """It is a projection of the objects. Hashing it would let a hand-edited
    copy change the digest of a manifest whose rules never moved."""
    assert manifest_hash(_envelope()) == manifest_hash(
        _envelope(required_source_fields=("something", "invented")))


# ---- template and schema hashes ----

def test_a_single_changed_byte_changes_the_template_hash(tmp_path):
    """The manifest's anchors are run paths into that exact package, so "looks
    the same" is not the property production pins."""
    path = tmp_path / "offer.docx"
    path.write_bytes(b"PK\x03\x04template-bytes")
    first = template_hash(path)
    assert first == template_hash(path)
    path.write_bytes(b"PK\x03\x04template-byteS")
    assert template_hash(path) != first
    assert first.startswith("sha256:")


def test_reordering_source_columns_is_not_schema_drift():
    """An export that emits its columns in a different order has not changed
    schema, and blocking a batch over it would teach operators to bypass the
    check."""
    assert source_schema_hash(["b", "a"]) == source_schema_hash(["a", "b"])
    assert source_schema_hash({"a": "string", "b": "date"}) == source_schema_hash(
        [{"name": "b", "type": "date"}, {"name": "a", "type": "string"}])


def test_a_renamed_or_retyped_column_is_schema_drift():
    """The second most likely failure in the register: a column renamed upstream
    that produces silent nulls instead of a blocked batch."""
    pinned = source_schema_hash({"start_date": "date", "full_name": "string"})
    assert source_schema_hash({"commencement_date": "date", "full_name": "string"}) != pinned
    assert source_schema_hash({"start_date": "string", "full_name": "string"}) != pinned


def test_an_empty_source_schema_refuses_to_pin():
    """A hash of nothing matches every other hash of nothing, which reads as two
    systems agreeing when neither has declared anything."""
    with pytest.raises(ValueError, match="empty source schema"):
        source_schema_hash([])


def test_a_column_declared_twice_with_two_types_raises():
    with pytest.raises(ValueError, match="twice with different types"):
        source_schema_hash([{"name": "amount", "type": "string"},
                            {"name": "amount", "type": "number"}])


# ---- the required-source-field closure ----

def test_a_field_only_a_condition_names_is_still_required():
    """The Hospira conditions all hinge on `colleague_type`, which has no
    placeholder anywhere in the letter and so no field object. A closure built
    from the field list alone leaves it unbound, every condition evaluates
    undecided, and whole sections go missing from the document."""
    manifest = {
        "fields": [{"id": "full_name"}],
        "conditions": [{"id": "c1", "expression": "colleague_type == 'Full time'"}],
    }
    assert required_source_fields(manifest) == ("colleague_type", "full_name")


def test_a_calculation_requires_its_inputs_and_not_its_own_name():
    """`total_package` is produced by the manifest, not read from the record.
    Requiring it would block every batch whose source is perfectly complete."""
    manifest = {"fields": [
        {"id": "base_salary"},
        {"id": "total_package", "kind": "computed", "formula": "base_salary + car_allowance"},
    ]}
    assert required_source_fields(manifest) == ("base_salary", "car_allowance")


def test_a_calculation_that_feeds_another_resolves_all_the_way_to_the_source():
    """This is the "closure" word doing work: one level of resolution would
    leave an intermediate name in the list that no source file can supply."""
    manifest = {"fields": [
        {"id": "gross", "kind": "computed", "formula": "base + bonus"},
        {"id": "net", "kind": "computed", "formula": "gross - tax"},
    ]}
    assert required_source_fields(manifest) == ("base", "bonus", "tax")


def test_a_declared_input_list_is_preferred_over_re_reading_the_formula():
    manifest = {"fields": [
        {"id": "rate", "kind": "computed", "formula": "cases / population * 1000",
         "inputs": ["cases", "population"]},
    ]}
    assert required_source_fields(manifest) == ("cases", "population")


def test_a_field_that_names_a_source_ref_requires_the_column_not_its_own_id():
    """Renaming the column a field reads has to change this list even though no
    object id moved -- otherwise the drift check validates the old name."""
    manifest = {"fields": [{"id": "manager", "source_ref": "source.new_manager_name"}]}
    assert required_source_fields(manifest) == ("new_manager_name",)


def test_a_repeated_region_requires_the_collection_it_repeats_over():
    """A section that repeats over a collection nobody supplied renders once, or
    not at all, and neither outcome announces itself."""
    manifest = {"blocks": [{"id": "b1", "repeat_over": "dependants"},
                           {"id": "b2", "iterate_over": "benefits"}]}
    assert required_source_fields(manifest) == ("benefits", "dependants")


def test_a_signature_requires_the_field_that_names_its_signer():
    """SIGNATURE is one of the types the legacy columns cannot hold, so a
    closure computed from the projection alone would miss exactly the field a
    letter cannot be signed without."""
    envelope = _envelope(objects=(ManifestObject(
        "sig_1", ObjectType.SIGNATURE,
        {"anchor": {}, "signer_source_ref": "source.signatory_name",
         "image_policy": "none", "esign_ref": None},
    ),))
    assert required_source_fields(envelope) == ("signatory_name",)


def test_the_closure_is_the_same_read_from_an_envelope_or_from_the_row():
    envelope = _envelope()
    assert required_source_fields(envelope) == required_source_fields(to_legacy_objects(envelope))
    assert required_source_fields(envelope) == ("colleague_type", "full_name")


def test_calculations_that_depend_on_each_other_raise_rather_than_loop():
    """A cycle is a hard error everywhere else in the pipeline; a closure that
    quietly returned a partial answer would be the one place it is not."""
    manifest = {"fields": [
        {"id": "a", "kind": "computed", "formula": "b + 1"},
        {"id": "b", "kind": "computed", "formula": "a + 1"},
    ]}
    with pytest.raises(CyclicDependencyError):
        required_source_fields(manifest)


# ---- lock and supersede ----

def test_locking_recomputes_the_closure_rather_than_trusting_it():
    """§6 is explicit that `required_source_fields` is computed, never
    hand-maintained. Locking is the last moment a wrong list can be corrected
    before a batch is validated against it."""
    envelope = _envelope(required_source_fields=("whatever_someone_typed",))
    locked = lock(envelope, approved_by="u_1042")
    assert locked.required_source_fields == ("colleague_type", "full_name")


def test_locking_seals_a_hash_that_verifies_against_the_content():
    envelope = lock(_envelope(), approved_by="u_1042")
    assert envelope.status is ManifestStatus.LOCKED
    assert manifest_hash_matches(envelope)
    assert pin_failures(envelope) == []


def test_a_manifest_with_no_pinned_template_refuses_to_lock():
    """A locked manifest whose template binary is not pinned cannot reproduce
    its own output, which is the one guarantee locking exists to make."""
    with pytest.raises(ValueError, match="pins no template_hash"):
        lock(_envelope(template_hash=None), approved_by="u_1042")


def test_a_locked_manifest_cannot_be_locked_again():
    locked = lock(_envelope(), approved_by="u_1042")
    with pytest.raises(ValueError, match="cannot be locked"):
        lock(locked, approved_by="u_2000")


def test_locking_without_naming_the_approver_is_refused():
    with pytest.raises(ValueError, match="requires the id of the person approving"):
        lock(_envelope(), approved_by="")


def test_an_edited_object_no_longer_verifies_against_the_sealed_hash(tmp_path):
    """The pin is what makes the audit trail defensible: if the objects can move
    under a recorded hash without the mismatch being visible, the trail records
    a manifest id and nothing about the bytes that ran."""
    locked = lock(_envelope(), approved_by="u_1042")
    tampered = _envelope(
        status=ManifestStatus.LOCKED, manifest_hash=locked.manifest_hash,
        objects=(_field(on_missing="BLANK"), _condition(), _section()),
    )
    assert not manifest_hash_matches(tampered)
    assert any("no longer hashes" in reason for reason in pin_failures(tampered))


def test_a_moved_template_binary_fails_the_pin(tmp_path):
    path = tmp_path / "offer.docx"
    path.write_bytes(b"the approved bytes")
    locked = lock(_envelope(template_hash=template_hash(path)), approved_by="u_1042")
    assert pin_failures(locked, path) == []
    path.write_bytes(b"the re-saved bytes")
    assert any("Template binary no longer hashes" in r for r in pin_failures(locked, path))


def test_an_unpinned_manifest_says_so_rather_than_passing_quietly():
    reasons = pin_failures(_envelope(template_hash=None))
    assert any("no manifest_hash" in r for r in reasons)
    assert any("no template_hash" in r for r in reasons)


def test_editing_a_locked_manifest_opens_a_draft_at_the_next_version():
    """Editing a locked manifest in place would move the contract production is
    pinned to, under documents already generated from it."""
    locked = lock(_envelope(), approved_by="u_1042")
    result = supersede(locked, manifest_id="mf_next")
    assert result.successor.manifest_version == locked.manifest_version + 1
    assert result.successor.status is ManifestStatus.DRAFT
    assert result.successor.supersedes == locked.manifest_id


def test_the_version_that_was_replaced_is_retired_and_kept():
    """"never deleted" is the part that matters: a letter generated last year
    still names a manifest that has to exist to explain it."""
    locked = lock(_envelope(), approved_by="u_1042")
    result = supersede(locked, manifest_id="mf_next")
    assert result.superseded.status is ManifestStatus.SUPERSEDED
    assert result.superseded.objects == locked.objects
    assert result.superseded.manifest_hash == locked.manifest_hash


def test_a_successor_inherits_the_rules_but_none_of_the_approval():
    """Otherwise an edited manifest arrives already signed by somebody who never
    saw the edit."""
    locked = lock(_envelope(), approved_by="u_1042")
    successor = supersede(locked, manifest_id="mf_next").successor
    assert successor.objects == locked.objects
    assert successor.approved_by is None
    assert successor.approved_at is None
    assert successor.manifest_hash is None


def test_superseding_the_same_version_twice_is_refused():
    """Two drafts claiming the same predecessor makes "which one replaced it"
    unanswerable."""
    retired = supersede(_envelope(), manifest_id="mf_next").superseded
    with pytest.raises(ValueError, match="already superseded"):
        supersede(retired, manifest_id="mf_third")


def test_a_superseded_manifest_is_neither_editable_nor_locked():
    retired = supersede(_envelope(), manifest_id="mf_next").superseded
    assert not is_editable(retired.status)
    assert not is_locked(retired.status)
