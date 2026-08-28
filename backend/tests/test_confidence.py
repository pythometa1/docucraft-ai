"""What the approval gate is allowed to wave through, and on what evidence.

The mapping suggestions that reach a reviewer used to carry a per-tier label:
0.95 because an exact slug matched, 0.80 because a model proposed it. That
number said nothing about whether the mapping had ever been approved, whether
the source column's type could even hold the value, or whether a second column
matched just as well -- so a reviewer had no way to spend attention where it
was needed, and the fields where being wrong is most expensive (salary, dates,
identifiers) looked exactly like the ones where it is cheapest.

These tests pin §13: the combination formula, the four vetoes, the four bands,
and the rule the architecture record states twice -- a model's own stated
confidence is never an input.
"""

import math

import pytest

from app.compiler.confidence import (
    AMBIGUITY_MARGIN,
    AUTO_ACCEPT_FLOOR,
    CONFIRM_FLOOR,
    INITIAL_WEIGHTS,
    MINIMUM_SIGNALS_WHEN_MATERIAL,
    REVIEW_FLOOR,
    SEMANTIC_SIMILARITY_FLOOR,
    VETO_AMBIGUOUS,
    VETO_NO_PRECEDENT,
    VETO_THIN_EVIDENCE,
    VETO_TYPE_MISMATCH,
    WEIGHTS_CALIBRATED,
    Band,
    Candidate,
    Signal,
    TypeMatch,
    band,
    band_for_score,
    business_rule_consistency,
    compare_types,
    decide,
    exact_name_match,
    family_inheritance,
    historical_approvals,
    is_date_field,
    is_money_or_identifier,
    requires_corroboration,
    score,
    semantic_similarity,
    sentence_context_match,
    vetoes,
)


def _well_evidenced(**overrides) -> Candidate:
    """A mapping with precedent, family and a checked type: no intrinsic veto.

    Used as the baseline so a test that is about one rule does not accidentally
    also trip the cold-start veto, which applies to everything.
    """
    kwargs = dict(
        target="new_manager_name",
        source_ref="source.new_manager_name",
        signals=(
            exact_name_match(True),
            semantic_similarity(0.93),
            historical_approvals(42),
            family_inheritance(0.98),
            sentence_context_match(0.90),
        ),
        target_field={"id": "new_manager_name", "type": "string"},
        source_type="string",
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


# ---- the combination formula ----------------------------------------------


def test_a_single_signal_scores_exactly_its_own_weight():
    """1 - (1 - s*w) is s*w. If this drifts, every band moves with it."""
    candidate = Candidate("manager", "Manager", signals=(exact_name_match(True),))
    assert score(candidate) == pytest.approx(INITIAL_WEIGHTS["exact_name_match"])


def test_two_signals_combine_as_one_minus_the_product_of_their_residuals():
    candidate = Candidate(
        "manager", "Manager",
        signals=(exact_name_match(True), sentence_context_match(1.0)),
    )
    assert score(candidate) == pytest.approx(1 - (1 - 0.55) * (1 - 0.30))


def test_no_amount_of_evidence_reaches_certainty():
    """A score of 1.0 would mean "no human need ever look", which no evidence buys.

    The formula is chosen so the product of residuals is never zero. A scoring
    function that could return 1.0 would eventually auto-accept a wrong mapping
    with no reviewer in the loop at all.
    """
    everything = Candidate(
        "annual_salary", "Base Salary",
        signals=(
            exact_name_match(True),
            semantic_similarity(1.0),
            historical_approvals(10_000),
            sentence_context_match(1.0),
            family_inheritance(1.0),
            business_rule_consistency(1.0),
        ),
        target_field={"id": "annual_salary", "type": "currency"},
        source_type="currency",
    )
    assert score(everything) < 1.0


def test_a_weak_extra_signal_raises_the_score_and_never_lowers_it():
    """Evidence must accumulate. A reviewer who adds a corroborating signal and
    watches the score fall stops trusting the number entirely."""
    base = Candidate("manager", "Manager", signals=(exact_name_match(True),))
    more = Candidate(
        "manager", "Manager",
        signals=(exact_name_match(True), business_rule_consistency(0.1)),
    )
    assert score(more) > score(base)


def test_the_score_does_not_depend_on_the_order_signals_were_gathered():
    forwards = Candidate(
        "manager", "Manager",
        signals=(exact_name_match(True), semantic_similarity(0.9), historical_approvals(3)),
    )
    backwards = Candidate(
        "manager", "Manager",
        signals=(historical_approvals(3), semantic_similarity(0.9), exact_name_match(True)),
    )
    assert score(forwards) == pytest.approx(score(backwards))


def test_a_candidate_with_no_evidence_scores_zero_rather_than_a_floor():
    assert score(Candidate("manager", "Manager")) == 0.0


# ---- per-signal transforms -------------------------------------------------


@pytest.mark.parametrize("cosine", [0.0, 0.5, 0.74])
def test_semantic_similarity_below_the_floor_contributes_nothing(cosine):
    """An embedding model puts unrelated HR field names around 0.6. Letting that
    count would make every candidate look mildly supported, and the two-signal
    rule on salary fields would be satisfied by noise."""
    signal = semantic_similarity(cosine)
    assert signal.strength == 0.0
    assert signal.supports is False


def test_semantic_similarity_above_the_floor_passes_the_cosine_through():
    assert semantic_similarity(0.88).strength == pytest.approx(0.88)
    assert semantic_similarity(SEMANTIC_SIMILARITY_FLOOR).supports is True


@pytest.mark.parametrize("n,expected", [(0, 0.0), (8, 1 - math.exp(-1)), (42, 1 - math.exp(-5.25))])
def test_historical_approvals_saturate_rather_than_accumulate_linearly(n, expected):
    """The fortieth approval is not four times the evidence of the tenth. An
    unsaturated count would let one popular mapping auto-accept alone."""
    assert historical_approvals(n).strength == pytest.approx(expected)


def test_a_single_historical_signal_cannot_carry_a_mapping_on_its_own():
    thousands = Candidate(
        "manager", "Manager",
        signals=(historical_approvals(1000),),
        target_field={"id": "manager", "type": "string"},
    )
    assert score(thousands) <= INITIAL_WEIGHTS["historical_approvals"]
    assert band(thousands) is not Band.AUTO_ACCEPT


def test_family_inheritance_is_scaled_by_the_measured_family_similarity():
    """A mapping inherited from a 98% similar sibling is near-conclusive; the
    same mapping inherited from a 60% match is a hint. Treating both as
    "inherited" is how one template's approved mapping lands wrongly on another."""
    assert family_inheritance(0.98).strength == pytest.approx(0.98)
    assert family_inheritance(0.60).strength == pytest.approx(0.60)
    assert score(Candidate("m", "M", signals=(family_inheritance(0.98),))) > score(
        Candidate("m", "M", signals=(family_inheritance(0.60),))
    )


@pytest.mark.parametrize("factory,bad", [
    (semantic_similarity, 1.4),
    (semantic_similarity, -0.1),
    (historical_approvals, -1),
    (sentence_context_match, 2.0),
    (family_inheritance, 1.01),
    (business_rule_consistency, -0.5),
])
def test_a_signal_built_from_an_out_of_range_measurement_is_refused(factory, bad):
    """An out-of-range strength silently breaks the formula's guarantee that
    evidence only accumulates -- s > 1 can make a residual negative."""
    with pytest.raises(ValueError):
        factory(bad)


# ---- the rule the record states twice --------------------------------------


@pytest.mark.parametrize("name", [
    "llm_confidence",
    "model_confidence",
    "self_reported_confidence",
    "confidence",
    "stated_confidence",
    "model_certainty",
    "gpt_logprob",
    "claude_score",
])
def test_a_models_own_stated_confidence_is_refused_as_a_signal(name):
    """§13: never consume the model's own confidence. It is uncalibrated and
    unauditable, and it is highest exactly where it is most dangerous -- on a
    plausible but wrong mapping the model has no way to doubt. The gate must be
    driven by independent, computed evidence or it is driven by nothing."""
    with pytest.raises(ValueError, match="stated confidence"):
        Signal(name, 0.99)


def test_an_unknown_signal_name_is_refused_rather_than_scored_at_zero():
    """A typo that scores as nothing is a mapping quietly losing its evidence."""
    with pytest.raises(ValueError, match="Unknown signal"):
        Signal("exact_name_matched", 1.0)


def test_every_declared_weight_names_a_constructible_signal():
    """Guards the ban list against a future signal name that collides with it."""
    for name in INITIAL_WEIGHTS:
        assert Signal(name, 1.0).weight == INITIAL_WEIGHTS[name]


@pytest.mark.parametrize("strength", [-0.01, 1.01, float("nan")])
def test_a_signal_strength_outside_zero_to_one_is_refused(strength):
    with pytest.raises(ValueError):
        Signal("exact_name_match", strength)


def test_the_same_signal_supplied_twice_is_refused():
    """The formula runs over *independent* signals. Counting one twice invents a
    second piece of evidence and inflates the score towards auto-accept."""
    with pytest.raises(ValueError, match="twice"):
        Candidate("m", "M", signals=(exact_name_match(True), exact_name_match(True)))


def test_a_caller_supplied_type_compatibility_signal_is_refused():
    """The type gate both scores and vetoes. Two sources of truth for it means a
    candidate that scores as type-checked while the veto sees nothing."""
    with pytest.raises(ValueError, match="type_compatibility"):
        Candidate("m", "M", signals=(Signal("type_compatibility", 1.0),))


# ---- type compatibility ----------------------------------------------------


@pytest.mark.parametrize("declared,observed,expected", [
    ("date", "date", TypeMatch.COMPATIBLE),
    ("currency", "number", TypeMatch.COMPATIBLE),
    ("string", "number", TypeMatch.COMPATIBLE),
    ("currency", "string", TypeMatch.MISMATCH),
    ("date", "string", TypeMatch.MISMATCH),
    ("date", "number", TypeMatch.MISMATCH),
    ("currency", None, TypeMatch.UNKNOWN),
    (None, "number", TypeMatch.UNKNOWN),
    ("gemstone", "number", TypeMatch.UNKNOWN),
])
def test_type_comparison_keeps_unknown_separate_from_disagreement(declared, observed, expected):
    """Collapsing "not probed" into "mismatch" vetoes every mapping from an
    untyped CSV; collapsing it into "compatible" hands 0.30 of weight to a
    candidate nobody type-checked."""
    assert compare_types(declared, observed) is expected


def test_an_unchecked_type_earns_no_weight_and_no_veto():
    candidate = _well_evidenced(source_type=None)
    assert candidate.type_match is TypeMatch.UNKNOWN
    assert VETO_TYPE_MISMATCH not in vetoes(candidate)
    assert score(candidate) < score(_well_evidenced())


# ---- the four vetoes -------------------------------------------------------


def test_a_declared_type_mismatch_blocks_however_strong_the_rest_of_the_evidence():
    """A date field bound to a free-text column produces a letter with a
    plausible-looking wrong date, which nobody catches by reading it."""
    candidate = _well_evidenced(
        target="effective_date",
        target_field={"id": "effective_date", "type": "date"},
        source_type="string",
    )
    assert score(candidate) > AUTO_ACCEPT_FLOOR - 0.05
    assert VETO_TYPE_MISMATCH in vetoes(candidate)
    assert band(candidate) is Band.REVIEW


@pytest.mark.parametrize("target,spec", [
    ("annual_salary", {"id": "annual_salary", "type": "currency"}),
    ("effective_date", {"id": "effective_date", "type": "date"}),
    ("employee_id", {"id": "employee_id", "type": "string"}),
])
def test_a_money_date_or_identifier_field_with_one_signal_is_vetoed(target, spec):
    """One signal is a single point of failure on exactly the fields where being
    wrong is most expensive: a wrong name is visible to the reader, a wrong
    amount or date reads as authoritative."""
    candidate = Candidate(
        target=target, source_ref="Column A",
        signals=(historical_approvals(30),),
        target_field=spec, source_type=None,
    )
    assert len(candidate.supporting_signals()) < MINIMUM_SIGNALS_WHEN_MATERIAL
    assert VETO_THIN_EVIDENCE in vetoes(candidate)
    assert band(candidate) is Band.REVIEW


def test_a_second_independent_signal_clears_the_material_field_veto():
    candidate = Candidate(
        target="annual_salary", source_ref="Base Salary",
        signals=(exact_name_match(True), historical_approvals(30), semantic_similarity(0.91)),
        target_field={"id": "annual_salary", "type": "currency"},
    )
    assert VETO_THIN_EVIDENCE not in vetoes(candidate)


def test_a_signal_that_was_computed_but_found_nothing_is_not_support():
    """Evidence considered is recorded; evidence that supports nothing must not
    satisfy the two-signal minimum, or a below-floor cosine corroborates a salary."""
    candidate = Candidate(
        target="annual_salary", source_ref="Base Salary",
        signals=(historical_approvals(30), semantic_similarity(0.4)),
        target_field={"id": "annual_salary", "type": "currency"},
    )
    assert "semantic_similarity:cos=0.40" in candidate.evidence()
    assert VETO_THIN_EVIDENCE in vetoes(candidate)


def test_a_mapping_with_neither_precedent_nor_family_is_vetoed():
    """The cold-start posture §13 asks for: on the first template of a new
    organisation nothing has been approved and nothing has a family, so every
    mapping is reviewed by a human rather than inferred from string shape."""
    candidate = Candidate(
        "colleague_first_name", "Colleague First Name",
        signals=(exact_name_match(True), semantic_similarity(0.99), sentence_context_match(1.0)),
        target_field={"id": "colleague_first_name", "type": "string"},
        source_type="string",
    )
    assert VETO_NO_PRECEDENT in vetoes(candidate)
    assert band(candidate) is Band.REVIEW


@pytest.mark.parametrize("signal", [historical_approvals(1), family_inheritance(0.8)])
def test_either_precedent_or_a_family_match_clears_the_cold_start_veto(signal):
    candidate = Candidate(
        "colleague_first_name", "Colleague First Name",
        signals=(exact_name_match(True), signal),
        target_field={"id": "colleague_first_name", "type": "string"},
    )
    assert VETO_NO_PRECEDENT not in vetoes(candidate)


def test_a_zero_strength_precedent_does_not_count_as_precedent():
    """`historical_approvals(0)` is the record of a lookup that found nothing.
    Reading it as precedent would clear the veto it exists to raise."""
    candidate = Candidate(
        "colleague_first_name", "Colleague First Name",
        signals=(exact_name_match(True), historical_approvals(0)),
    )
    assert VETO_NO_PRECEDENT in vetoes(candidate)


def test_two_candidates_within_the_margin_are_both_vetoed_as_ambiguous():
    """Ranking always produces a winner, including when the top two are
    indistinguishable. Applying the first of two equally good columns is the
    silent wrong answer the ambiguity veto exists to prevent."""
    first = Candidate(
        "manager", "Manager Name",
        signals=(exact_name_match(True), historical_approvals(10)),
        target_field={"id": "manager", "type": "string"},
    )
    second = Candidate(
        "manager", "Line Manager",
        signals=(exact_name_match(True), historical_approvals(9)),
        target_field={"id": "manager", "type": "string"},
    )
    decision = decide([first, second])
    assert decision.ranked[0].score - decision.ranked[1].score <= AMBIGUITY_MARGIN
    assert all(VETO_AMBIGUOUS in s.vetoes for s in decision.ranked)
    assert decision.band is Band.REVIEW
    assert decision.auto_applicable is False


def test_a_clear_winner_carries_no_ambiguity_veto():
    strong = _well_evidenced()
    weak = Candidate(
        "new_manager_name", "Random Column",
        signals=(semantic_similarity(0.78), family_inheritance(0.5)),
        target_field={"id": "new_manager_name", "type": "string"},
        source_type="string",
    )
    decision = decide([strong, weak])
    assert decision.ranked[0].candidate is strong
    assert VETO_AMBIGUOUS not in decision.ranked[0].vetoes
    assert decision.band is Band.AUTO_ACCEPT


def test_a_third_candidate_far_behind_does_not_dilute_the_tie_at_the_top():
    first = Candidate("manager", "Manager Name", signals=(exact_name_match(True), historical_approvals(10)))
    second = Candidate("manager", "Line Manager", signals=(exact_name_match(True), historical_approvals(9)))
    third = Candidate("manager", "Cost Centre", signals=(semantic_similarity(0.76), family_inheritance(0.3)))
    decision = decide([first, second, third])
    assert VETO_AMBIGUOUS in decision.ranked[0].vetoes
    assert VETO_AMBIGUOUS in decision.ranked[1].vetoes
    assert VETO_AMBIGUOUS not in decision.ranked[2].vetoes


# ---- the four bands --------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (1.00, Band.AUTO_ACCEPT),
    (AUTO_ACCEPT_FLOOR, Band.AUTO_ACCEPT),
    (AUTO_ACCEPT_FLOOR - 0.0001, Band.CONFIRM),
    (CONFIRM_FLOOR, Band.CONFIRM),
    (CONFIRM_FLOOR - 0.0001, Band.REVIEW),
    (REVIEW_FLOOR, Band.REVIEW),
    (REVIEW_FLOOR - 0.0001, Band.BLOCK),
    (0.0, Band.BLOCK),
])
def test_the_bands_partition_the_score_range_at_the_documented_thresholds(value, expected):
    assert band_for_score(value, vetoed=False, money_or_identifier=False) is expected


@pytest.mark.parametrize("value", [1.0, 0.99, 0.85, 0.60])
def test_any_veto_forces_review_whatever_the_score_says(value):
    """The record's signal table says a veto forces REVIEW and its band table
    says any veto blocks. We take the stricter reading: both mean a human
    decides, and an uncalibrated scorer must fail towards the conservative one."""
    assert band_for_score(value, vetoed=True, money_or_identifier=False) is Band.REVIEW


@pytest.mark.parametrize("value", [1.0, AUTO_ACCEPT_FLOOR, 0.995])
def test_a_money_or_identifier_field_never_auto_accepts_however_strong_the_evidence(value):
    """The one click between a very confident wrong mapping and a letter that
    states the wrong salary."""
    assert band_for_score(value, vetoed=False, money_or_identifier=True) is Band.CONFIRM


def test_a_salary_mapping_with_overwhelming_evidence_still_asks_for_confirmation():
    candidate = Candidate(
        "annual_salary", "Base Salary",
        signals=(
            exact_name_match(True), semantic_similarity(0.97), historical_approvals(40),
            family_inheritance(0.98), sentence_context_match(0.95),
            business_rule_consistency(0.9),
        ),
        target_field={"id": "annual_salary", "type": "currency"},
        source_type="number",
    )
    assert score(candidate) >= AUTO_ACCEPT_FLOOR
    assert vetoes(candidate) == []
    assert band(candidate) is Band.CONFIRM


def test_a_well_evidenced_non_material_field_can_auto_accept():
    """Auto-accept has to be reachable, or the band is decoration -- but only by
    five independent signals agreeing, never by one being emphatic."""
    candidate = _well_evidenced()
    assert vetoes(candidate) == []
    assert band(candidate) is Band.AUTO_ACCEPT


def test_a_list_level_veto_reaches_the_band_through_band_itself():
    candidate = _well_evidenced()
    assert band(candidate, extra_vetoes=(VETO_AMBIGUOUS,)) is Band.REVIEW


# ---- field classification --------------------------------------------------


@pytest.mark.parametrize("field", [
    "annual_salary", "base_pay", "sign_on_bonus", "total_compensation",
    "hourly_rate", "car_allowance", "employee_id", "account_number",
    "national_insurance_number", "passport", "payroll_ref", "iban",
    {"id": "x", "type": "currency"}, {"id": "x", "value_type": "identifier"},
    {"id": "obj_0117", "template_token": "<Annual Salary>"},
])
def test_money_and_identifier_fields_are_recognised(field):
    assert is_money_or_identifier(field) is True


@pytest.mark.parametrize("field", [
    "colleague_first_name", "manager_name", "week_number", "department",
    "job_title", "office_location", "president_name", "candidate_statement",
    {"id": "start_date", "type": "date"},
])
def test_ordinary_fields_are_not_misclassified_as_money_or_identifier(field):
    """Over-classifying costs a reviewer click; misclassifying every field as
    material costs the auto-accept band its reason to exist."""
    assert is_money_or_identifier(field) is False


@pytest.mark.parametrize("field,expected", [
    ("effective_date", True),
    ("dob", True),
    ({"id": "x", "value_type": "date"}, True),
    ("manager_name", False),
])
def test_date_fields_are_recognised_for_the_two_signal_rule(field, expected):
    assert is_date_field(field) is expected
    if expected:
        assert requires_corroboration(field) is True


def test_a_plain_text_field_needs_no_corroborating_second_signal():
    assert requires_corroboration("colleague_first_name") is False


def test_classifying_something_that_is_not_a_field_raises_rather_than_returning_false():
    """A silent False here auto-accepts a salary mapping. The one direction this
    helper must never fail in is "no, it is not material"."""
    with pytest.raises(TypeError):
        is_money_or_identifier(None)


def test_a_candidate_without_a_field_spec_is_classified_from_its_target_id():
    candidate = Candidate("annual_salary", "Base Salary", signals=(exact_name_match(True),))
    assert is_money_or_identifier(candidate.field_subject) is True


# ---- decisions and the calibration record ----------------------------------


def test_decide_ranks_the_candidates_and_reports_the_leaders_band():
    decision = decide([
        Candidate("new_manager_name", "Random Column",
                  signals=(semantic_similarity(0.76), family_inheritance(0.4))),
        _well_evidenced(),
    ])
    assert decision.target == "new_manager_name"
    assert decision.leader.candidate.source_ref == "source.new_manager_name"
    assert decision.band is decision.leader.band
    assert [s.score for s in decision.ranked] == sorted(
        (s.score for s in decision.ranked), reverse=True
    )


def test_decide_orders_equal_scores_deterministically():
    """An approval summary that reshuffles between two runs of the same compile
    cannot be reviewed, and a reviewer's "I already looked at that one" is wrong."""
    a = Candidate("manager", "B Column", signals=(exact_name_match(True), historical_approvals(5)))
    b = Candidate("manager", "A Column", signals=(exact_name_match(True), historical_approvals(5)))
    assert [s.candidate.source_ref for s in decide([a, b]).ranked] == ["A Column", "B Column"]
    assert [s.candidate.source_ref for s in decide([b, a]).ranked] == ["A Column", "B Column"]


def test_decide_refuses_candidates_belonging_to_different_targets():
    """The ambiguity veto compares candidates with each other; mixing targets
    would let one field's runner-up veto another field's winner."""
    with pytest.raises(ValueError, match="one target"):
        decide([
            Candidate("manager", "Manager", signals=(exact_name_match(True),)),
            Candidate("salary", "Salary", signals=(exact_name_match(True),)),
        ])


def test_decide_refuses_an_empty_candidate_list():
    """A target with no candidate is an unmatched field, which the binding plan
    already reports. Manufacturing a decision for it hides it among mappings
    that actually exist."""
    with pytest.raises(ValueError, match="at least one candidate"):
        decide([])


def test_a_decision_records_the_score_and_evidence_the_calibration_needs():
    """§14: log every suggestion with its score and evidence. That log is the
    only dataset that can ever calibrate these weights, and it cannot be
    reconstructed later -- so the shape is pinned here."""
    payload = decide([_well_evidenced()]).as_dict()
    assert payload["target"] == "new_manager_name"
    assert payload["band"] == "AUTO_ACCEPT"
    assert payload["weights_calibrated"] is False
    leader = payload["candidates"][0]
    assert leader["source_ref"] == "source.new_manager_name"
    assert 0.0 < leader["score"] < 1.0
    assert leader["evidence"] == [
        "exact_name_match",
        "semantic_similarity:cos=0.93",
        "historical_approvals:42_approvals",
        "family_inheritance:0.98",
        "sentence_context_match:0.90",
        "type_compatibility:string->string",
    ]
    assert leader["vetoes"] == []


def test_the_weights_are_the_documented_starting_values_and_are_not_calibrated():
    """These are estimates, not measurements. A weight that drifts by accident
    moves every band with it, and nothing else in the system would notice."""
    assert INITIAL_WEIGHTS == {
        "exact_name_match": 0.55,
        "semantic_similarity": 0.35,
        "historical_approvals": 0.60,
        "type_compatibility": 0.30,
        "sentence_context_match": 0.30,
        "family_inheritance": 0.65,
        "business_rule_consistency": 0.25,
    }
    assert (SEMANTIC_SIMILARITY_FLOOR, AMBIGUITY_MARGIN) == (0.75, 0.05)
    assert (AUTO_ACCEPT_FLOOR, CONFIRM_FLOOR, REVIEW_FLOOR) == (0.97, 0.80, 0.50)
    assert WEIGHTS_CALIBRATED is False


def test_a_raw_dict_smuggled_in_as_a_signal_is_refused():
    """Signal validation is where the model's own confidence is turned away. A
    caller passing an unvalidated mapping straight through -- the shape an LLM
    tool call arrives in -- would route around it entirely."""
    with pytest.raises(TypeError, match="Signal objects"):
        Candidate("m", "M", signals=({"name": "llm_confidence", "strength": 0.99},))


def test_a_veto_cannot_rescue_a_score_below_the_floor():
    """A veto means "a human must look at this". It does not mean "there is
    evidence here" -- a candidate under the review floor has none, and §13's band
    table gives that its own row regardless of how it got there."""
    assert band_for_score(0.10, vetoed=True, money_or_identifier=False) is Band.BLOCK
    assert band_for_score(0.0, vetoed=True, money_or_identifier=False) is Band.BLOCK


def test_a_fresh_installation_can_still_be_used():
    """The reading this pins. §13's third veto -- "no historical precedent AND no
    family match" -- is true of every mapping on a new installation, because
    there is no history and there are no families yet.

    Treating a veto as BLOCK would mean a customer's first template has every
    field blocked, the manifest can never be locked, and the product cannot be
    started until data it has no way to acquire already exists.
    """
    first_ever = band_for_score(0.7953, vetoed=True, money_or_identifier=False)
    assert first_ever is Band.REVIEW, "a new customer must be able to review and approve"
    assert first_ever is not Band.AUTO_ACCEPT, "but never silently"
