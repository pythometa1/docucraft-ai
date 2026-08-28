"""The §6 object model, and the anchors that tie it to a pinned template.

A placeholder string is a poor anchor: Word fragments text across runs, re-saving
normalises whitespace, and the same token can appear twice in one letter. So an
anchor is a composite reference that either resolves exactly once against the
pinned template version or fails loudly. "Fails loudly" is the property under
test here — an anchor that quietly repoints is worse than one that breaks,
because it writes the right value into the wrong sentence.
"""

import pytest

from app.templates.semantic_model import (
    ANCHOR_BBOX, ANCHOR_CONTENT_CONTROL, ANCHOR_KINDS, ANCHOR_MERGEFIELD,
    ANCHOR_RUN_PATH, ANCHOR_STYLE_ID, Anchor, AnchorAmbiguousError,
    AnchorNotFoundError, AnchorRange, CONTEXT_WINDOW, EvidenceOnlyAnchorError,
    ConditionObject, FieldObject, TemplateInventory, context_hash, document_text,
    lift_slot_to_anchor, paragraph_offsets, resolve, run_test_cases, static_text_hash,
)


PARAGRAPHS = [
    "Dear <Colleague First Name>,",
    "You will report to <New Reporting To>.",
    "Your start date is <Start Date>.",
]


def _range(start_paragraph, end_paragraph, texts=PARAGRAPHS):
    """The region a condition governs, addressed by its two endpoints."""
    return AnchorRange(
        start=_paragraph_start_anchor(start_paragraph, texts),
        end=_paragraph_start_anchor(end_paragraph, texts),
    )


def _paragraph_start_anchor(paragraph_index, texts=PARAGRAPHS):
    full = document_text(texts)
    at = paragraph_offsets(texts)[paragraph_index]
    return Anchor(
        kind=ANCHOR_RUN_PATH,
        path=f"body/p[{paragraph_index}]",
        ordinal=1,
        token=texts[paragraph_index],
        context_hash=context_hash(full, at),
    )


def _run_path_anchor(token, paragraph_index=1, ordinal=1, texts=PARAGRAPHS):
    """An anchor built the way the compiler would build it."""
    full = document_text(texts)
    at = full.index(token)
    return Anchor(
        kind=ANCHOR_RUN_PATH,
        path=f"body/p[{paragraph_index}]/r[0]",
        ordinal=ordinal,
        token=token,
        context_hash=context_hash(full, at),
    )


# ------------------------------------------------------------- context hash

def test_the_context_hash_covers_the_window_the_doc_specifies():
    """±60 characters, which is what makes it sensitive to the sentence moving
    while staying insensitive to the rest of the letter changing."""
    assert CONTEXT_WINDOW == 60


def test_the_same_surroundings_hash_the_same():
    full = document_text(PARAGRAPHS)
    at = full.index("<New Reporting To>")
    assert context_hash(full, at) == context_hash(full, at)


def test_changed_surroundings_change_the_hash():
    """The drift the anchor exists to detect: the token is still there, but it is
    no longer in the sentence the mapping was approved against."""
    original = document_text(PARAGRAPHS)
    edited = document_text([
        PARAGRAPHS[0],
        "You will report, on a dotted line, to <New Reporting To>.",
        PARAGRAPHS[2],
    ])
    before = context_hash(original, original.index("<New Reporting To>"))
    after = context_hash(edited, edited.index("<New Reporting To>"))
    assert before != after


def test_whitespace_normalisation_survives_a_word_resave():
    """Re-saving in Word collapses runs of whitespace. That is not an edit to the
    meaning, and treating it as drift would break every anchor on every save."""
    tight = "You will report to <X>."
    loose = "You  will   report to <X>."
    assert context_hash(tight, tight.index("<X>")) == context_hash(loose, loose.index("<X>"))


def test_an_offset_outside_the_text_is_refused():
    """An anchor built against the wrong document is a compile-time bug, not a
    hash to compare later and find surprising."""
    with pytest.raises(ValueError, match="outside"):
        context_hash("short", 999)


def test_a_negative_window_is_refused():
    with pytest.raises(ValueError, match="negative"):
        context_hash("text", 0, window=-1)


def test_static_text_is_hashed_byte_for_byte():
    """Unlike the context hash. §6's rule is that a STATIC region matches the
    template byte-for-byte: approved immutable text that gained a double space
    gained it from somebody editing the template."""
    assert static_text_hash("Terms apply.") != static_text_hash("Terms  apply.")
    assert static_text_hash("Terms apply.") == static_text_hash("Terms apply.")


# ------------------------------------------------------------- resolution

def test_an_anchor_resolves_exactly_once():
    match = resolve(_run_path_anchor("<New Reporting To>"), PARAGRAPHS)
    assert match.paragraph_index == 1
    assert "<New Reporting To>" in PARAGRAPHS[match.paragraph_index]


def test_a_token_that_is_gone_fails_loudly():
    """Zero matches is a blocked generation, never a silent skip."""
    anchor = _run_path_anchor("<New Reporting To>")
    without = ["Dear <Colleague First Name>,", "You will report to your manager.", PARAGRAPHS[2]]
    with pytest.raises(AnchorNotFoundError):
        resolve(anchor, without)


def test_a_token_that_now_appears_twice_fails_loudly():
    """Two matches is the dangerous case: the fill would succeed and write the
    value into whichever one it happened to find."""
    duplicated = [
        PARAGRAPHS[0],
        "You will report to <New Reporting To>.",
        "Copy your acceptance to <New Reporting To>.",
    ]
    full = document_text(duplicated)
    ambiguous = Anchor(
        kind=ANCHOR_RUN_PATH, path="body/p[1]/r[0]", ordinal=3,
        token="<New Reporting To>", context_hash=context_hash(full, full.index("<New Reporting To>")),
    )
    with pytest.raises((AnchorAmbiguousError, AnchorNotFoundError)):
        resolve(ambiguous, duplicated)


def test_the_ordinal_picks_between_two_occurrences():
    """"report to <Manager>, copying <Manager>" is why the ordinal exists: the
    two occurrences are different places to write."""
    texts = [PARAGRAPHS[0], "Report to <Manager>, copying <Manager>.", PARAGRAPHS[2]]
    full = document_text(texts)
    second_at = full.index("<Manager>", full.index("<Manager>") + 1)

    second = Anchor(
        kind=ANCHOR_RUN_PATH, path="body/p[1]/r[2]", ordinal=2, token="<Manager>",
        context_hash=context_hash(full, second_at),
    )
    assert resolve(second, texts).paragraph_index == 1


# ------------------------------------------------- anchor kinds and stability

def test_every_declared_anchor_kind_is_one_the_doc_names():
    assert set(ANCHOR_KINDS) == {
        ANCHOR_CONTENT_CONTROL, ANCHOR_MERGEFIELD, ANCHOR_RUN_PATH, ANCHOR_BBOX, ANCHOR_STYLE_ID,
    }


def test_an_unknown_anchor_kind_is_refused():
    with pytest.raises(ValueError, match="unknown anchor kind"):
        Anchor(kind="colour_class")


def test_a_style_anchor_cannot_be_resolved_in_production():
    """§6 is emphatic: colour coding is evidence during onboarding, never the
    addressing mechanism. A single reformat in Word would silently repoint every
    mapping that trusted it."""
    anchor = Anchor(kind=ANCHOR_STYLE_ID, style_id="BlueInstruction")
    with pytest.raises(EvidenceOnlyAnchorError):
        resolve(anchor, PARAGRAPHS)


def test_a_run_path_anchor_without_a_context_hash_is_refused():
    """Not a weaker anchor — an anchor that cannot detect the drift it exists to
    detect."""
    with pytest.raises(ValueError, match="context_hash"):
        Anchor(kind=ANCHOR_RUN_PATH, path="body/p[1]/r[0]", ordinal=1, token="<X>")


def test_a_run_path_anchor_needs_a_one_based_ordinal():
    full = document_text(PARAGRAPHS)
    digest = context_hash(full, full.index("<Start Date>"))
    for bad in (0, -1, None, True):
        with pytest.raises(ValueError):
            Anchor(kind=ANCHOR_RUN_PATH, path="body/p[2]/r[0]", ordinal=bad,
                   token="<Start Date>", context_hash=digest)


def test_a_malformed_context_hash_is_refused():
    with pytest.raises(ValueError, match="sha256"):
        Anchor(kind=ANCHOR_RUN_PATH, path="body/p[1]/r[0]", ordinal=1,
               token="<X>", context_hash="not-a-hash")


# ------------------------------------------------------------- lifting slots

def test_a_positional_slot_lifts_into_an_anchor():
    """An estate already onboarded gains drift detection without every template
    going back through the compiler."""
    anchor = lift_slot_to_anchor(
        {"paragraph_index": 1, "span_index": 0, "text": "<New Reporting To>"}, PARAGRAPHS,
    )
    assert anchor.kind == ANCHOR_RUN_PATH
    assert anchor.token == "<New Reporting To>"
    assert anchor.context_hash.startswith("sha256:")
    assert resolve(anchor, PARAGRAPHS).paragraph_index == 1


def test_a_mergefield_slot_lifts_to_a_mergefield_anchor():
    """§6 ranks MERGEFIELD above run paths for stability. Rewriting a stable
    anchor as a less stable one to keep the code uniform is a downgrade."""
    anchor = lift_slot_to_anchor(
        {"paragraph_index": 1, "span_index": 0, "kind": "mergefield", "code": "new_manager_name"},
        PARAGRAPHS,
    )
    assert anchor.kind == ANCHOR_MERGEFIELD
    assert anchor.field_code == "new_manager_name"


def test_lifting_an_ambiguous_slot_asks_rather_than_guesses():
    """Assuming the first occurrence would put the value in the wrong half of
    "report to <Manager>, copying <Manager>"."""
    texts = [PARAGRAPHS[0], "Report to <Manager>, copying <Manager>.", PARAGRAPHS[2]]
    with pytest.raises(ValueError):
        lift_slot_to_anchor({"paragraph_index": 1, "span_index": 0, "text": "<Manager>"}, texts)

    disambiguated = lift_slot_to_anchor(
        {"paragraph_index": 1, "span_index": 0, "text": "<Manager>"}, texts, occurrence=2,
    )
    assert disambiguated.ordinal == 2


def test_lifting_refuses_a_slot_that_addresses_nothing():
    with pytest.raises(ValueError):
        lift_slot_to_anchor({"span_index": 0, "text": "<X>"}, PARAGRAPHS)
    with pytest.raises(ValueError):
        lift_slot_to_anchor({"paragraph_index": 99, "span_index": 0, "text": "<X>"}, PARAGRAPHS)


def test_lifting_refuses_something_that_is_not_a_slot():
    with pytest.raises(TypeError):
        lift_slot_to_anchor("body/p[1]", PARAGRAPHS)


# --------------------------------------------------------- document geometry

def test_paragraph_offsets_locate_each_paragraph_in_the_joined_text():
    full = document_text(PARAGRAPHS)
    for index, offset in enumerate(paragraph_offsets(PARAGRAPHS)):
        assert full[offset:offset + len(PARAGRAPHS[index])] == PARAGRAPHS[index]


def test_an_inventory_reads_the_same_paragraphs_back():
    inventory = TemplateInventory.of(PARAGRAPHS)
    assert list(inventory.paragraph_texts) == PARAGRAPHS


# ------------------------------------------------------------- object model

def test_a_field_object_carries_what_the_doc_says_it_must():
    field = FieldObject(
        object_id="obj_0117",
        anchor=_run_path_anchor("<New Reporting To>"),
        source_ref="source.new_manager_name",
        format=None,
        value_type="string",
        on_missing="BLOCK",
        status="APPROVED",
    )
    assert field.object_type == "FIELD"
    assert field.on_missing == "BLOCK"
    assert field.source_refs() == ("source.new_manager_name",)


def test_a_field_object_will_not_be_anchored_by_colour():
    """The production anchor can never be the evidence signal."""
    with pytest.raises(EvidenceOnlyAnchorError):
        FieldObject(
            object_id="obj_colour",
            anchor=Anchor(kind=ANCHOR_STYLE_ID, style_id="BlueInstruction"),
            source_ref="source.x", format=None, on_missing="BLANK", status="APPROVED",
        )


def test_a_field_object_refuses_an_unknown_missing_policy():
    """A typo in a manifest must stop a batch, not silently downgrade a required
    field to "leave it empty"."""
    with pytest.raises(ValueError):
        FieldObject(
            object_id="obj_1",
            anchor=_run_path_anchor("<Start Date>", paragraph_index=2),
            source_ref="source.start_date",
            format=None,
            value_type="date",
            on_missing="BLNAK",
            status="APPROVED",
        )


def test_a_condition_object_runs_its_test_cases():
    """§7's worked example, including the third case that matters: a temporary
    assignment with no end date is incomplete source data, not a false
    condition."""
    condition = ConditionObject(
        object_id="obj_cond_1",
        anchor_range=_range(1, 2),
        expression="assignment_type == 'TEMPORARY' and transfer_end_date != ''",
        on_true="KEEP",
        on_false="REMOVE_BLOCK",
        status="APPROVED",
        test_cases=(
            {"in": {"assignment_type": "TEMPORARY", "transfer_end_date": "2026-11-30"}, "expect": "KEEP"},
            {"in": {"assignment_type": "PERMANENT", "transfer_end_date": ""}, "expect": "REMOVE_BLOCK"},
            {"in": {"assignment_type": "TEMPORARY"}, "expect": "BLOCK_MISSING_INPUT"},
        ),
    )
    results = run_test_cases(condition)
    assert all(r.passed for r in results), [r for r in results if not r.passed]


def test_a_condition_test_case_that_expects_the_wrong_outcome_fails():
    """The suite has to be able to go red, or it is decoration."""
    condition = ConditionObject(
        object_id="obj_cond_2",
        anchor_range=_range(1, 2),
        expression="assignment_type == 'TEMPORARY'",
        on_true="KEEP",
        on_false="REMOVE_BLOCK",
        status="APPROVED",
        test_cases=({"in": {"assignment_type": "PERMANENT"}, "expect": "KEEP"},),
    )
    assert not all(r.passed for r in run_test_cases(condition))
