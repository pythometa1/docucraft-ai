"""§13's confidence function, in the path that actually proposes bindings.

The failure these pin down is the one the architecture record spends a whole
section on: a suggestion that carries a number nobody can audit. Before this,
`source_resolver` graded its own proposals with a label for the tier that made
them -- 0.95 for every exact slug match -- so a mapping approved forty-two
times and a lucky string match arrived at the reviewer looking identical. The
tests below assert the three properties that replaced it: the score is a
combination of independent signals, every target carries its ranked
alternatives, and a veto is visible with the rule that fired it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.compiler import confidence as cf
from app.generation.source_resolver import (
    SIGNALS_COMPUTED,
    SIGNALS_NOT_COMPUTED,
    infer_column_type,
    max_attainable_score,
    profile_columns,
    suggest_bindings,
)


def _manifest(*fields, conditions=()):
    return {"fields": list(fields), "conditions": list(conditions), "blocks": []}


def _field(field_id, type_="string", label=None, **extra):
    slots = [{"kind": "blue_placeholder", "text": label or f"<{field_id}>"}]
    return {"id": field_id, "type": type_, "slots": slots, **extra}


def _rows(**columns):
    """Records in the shape `extract_records` produces: every cell a string."""
    length = max(len(v) for v in columns.values())
    return [
        {"_row_index": i, **{name: values[i] if i < len(values) else "" for name, values in columns.items()}}
        for i in range(length)
    ]


class _Prior:
    """The shape `MappingMemory.lookup` hands back, reduced to what is read.

    `similarity` is 1.0 when the stored mapping is for the field being asked
    about, and a context cosine when it is for a neighbouring one.
    """

    def __init__(self, source_column: str, net_approvals: int, similarity: float = 1.0):
        self.source_column = source_column
        self.net_approvals = net_approvals
        self.similarity = similarity


class _Lookup:
    def __init__(self, *priors):
        self.prior_mappings = list(priors)


def _by_id(plan):
    return {s.field_id: s for s in plan.suggestions}


# ------------------------------------------------------------- the signal set


def test_the_live_signal_set_partitions_section_13s_seven_signals():
    """Neither table may drift from §13. A signal quietly dropped from both is a
    weight the score silently stops using; one that appears in neither is a
    number nobody declared the provenance of."""
    declared = set(SIGNALS_COMPUTED) | set(SIGNALS_NOT_COMPUTED)
    assert declared == set(cf.INITIAL_WEIGHTS)
    assert not set(SIGNALS_COMPUTED) & set(SIGNALS_NOT_COMPUTED)
    assert all(reason.strip() for reason in SIGNALS_NOT_COMPUTED.values())


def test_nothing_can_auto_accept_while_three_signals_are_missing():
    """With four of seven signals live the ceiling is below the 0.97 auto-accept
    floor. The honest response is to report that, not to lower the floor until
    something clears it -- the record's day-one goal is a near-zero escaped
    error rate, not a high automation rate."""
    assert max_attainable_score() < cf.AUTO_ACCEPT_FLOOR

    columns = ["Colleague First Name"]
    plan = suggest_bindings(
        _manifest(_field("colleague_first_name", label="<Colleague First Name>")),
        columns,
        records=_rows(**{"Colleague First Name": ["Olivia", "Marcus"]}),
        memory_lookups={"colleague_first_name": _Lookup(_Prior("Colleague First Name", 500))},
    )
    assert plan.auto_applicable_bindings() == {}
    assert plan.as_field_bindings() == {"colleague_first_name": "Colleague First Name"}


# ------------------------------------------------------------------ the score


def test_an_exact_match_is_scored_from_its_evidence_not_from_its_tier():
    """The old resolver stamped 0.95 on every exact slug match. The score is now
    the §13 combination over the signals that particular candidate carries, so
    two exact matches with different evidence no longer look identical."""
    manifest = _manifest(_field("start_date", "date", label="<Start Date>"))
    records = _rows(**{"Start Date": ["01/08/2026", "15/09/2026"]})

    cold = _by_id(suggest_bindings(manifest, ["Start Date"], records=records))["start_date"]
    warm = _by_id(suggest_bindings(
        manifest, ["Start Date"], records=records,
        memory_lookups={"start_date": _Lookup(_Prior("Start Date", 3))},
    ))["start_date"]

    assert cold.confidence != 0.95
    # exact_name_match, semantic_similarity and the type gate, combined.
    assert cold.confidence == pytest.approx(1 - 0.45 * 0.65 * 0.70, abs=1e-4)
    assert warm.confidence > cold.confidence, "precedent is evidence and must raise the score"


def test_evidence_accumulates_and_never_reaches_certainty():
    """The shape of the combination formula is the reason it was chosen: a weak
    second signal can only raise a score, and no finite pile of evidence reaches
    1.0. A scorer that can report "certain" invites a gate that stops asking."""
    manifest = _manifest(_field("location", label="<Location>"))
    records = _rows(Location=["Melbourne", "Dalian"])
    scores = [
        _by_id(suggest_bindings(
            manifest, ["Location"], records=records,
            memory_lookups={"location": _Lookup(_Prior("Location", n))},
        ))["location"].confidence
        for n in (0, 1, 8, 80, 800)
    ]
    assert scores == sorted(scores)
    assert scores[-1] < 1.0


def test_a_near_match_below_the_cosine_floor_earns_no_confidence_from_it():
    """A truncated header still gets proposed -- that is what the near-match tier
    is for -- but §13 floors the semantic signal at 0.75, so a proposal nothing
    else supports arrives with a score that says so rather than the 0.62 name
    ratio dressed up as a confidence."""
    manifest = _manifest(_field("employee_reference_number", label="<Employee Reference Number>"))
    plan = suggest_bindings(manifest, ["Employee Ref Numbr"], records=_rows(**{"Employee Ref Numbr": ["A-1", "A-2"]}))
    suggestion = _by_id(plan)["employee_reference_number"]

    assert suggestion.column == "Employee Ref Numbr"
    assert suggestion.method == "fuzzy"
    assert "exact_name_match:0.00" in suggestion.evidence
    assert "semantic_similarity" in " ".join(suggestion.evidence)
    assert suggestion.confidence < cf.CONFIRM_FLOOR
    assert suggestion.band in (cf.Band.REVIEW.value, cf.Band.BLOCK.value)


def test_a_truncated_header_still_finds_its_field():
    """The case the resolver was built around: a spreadsheet header cut off at
    28 characters for a field id of 42. It has to keep binding -- a scoring
    change that quietly stopped matching it would leave a placeholder in the
    letter, which is a worse outcome than a low score."""
    field_id = "gbs_dalian_recruiting_delivery_apac_services"
    plan = suggest_bindings(
        _manifest(_field(field_id, label="<GBS Dalian Recruiting Delivery APAC Services>")),
        ["GBS Dalian Recruiting Delive"],
        records=_rows(**{"GBS Dalian Recruiting Delive": ["APAC Recruitment Team"]}),
    )
    assert plan.as_field_bindings() == {field_id: "GBS Dalian Recruiting Delive"}


# --------------------------------------------------------------------- vetoes


def test_a_mapping_with_no_precedent_and_no_family_is_vetoed_to_review():
    """§13's third veto, and the posture it implies: on the first template of a
    new organisation nothing has been approved before, so every mapping reaches
    a human. That is the design, not a cold-start bug to tune away."""
    plan = suggest_bindings(
        _manifest(_field("position_title", label="<Position Title>")),
        ["Position Title"],
        records=_rows(**{"Position Title": ["Production Technician"]}),
        memory_lookups={"position_title": _Lookup()},
    )
    suggestion = _by_id(plan)["position_title"]
    assert cf.VETO_NO_PRECEDENT in suggestion.vetoes
    # A veto sends a candidate to REVIEW, not BLOCK: §13's signal table says
    # "Vetoes -- force REVIEW regardless of computed score", and BLOCK is the
    # band for a score below 0.50. The distinction is load-bearing on a new
    # installation, where the "no precedent and no family" veto is true of
    # every mapping and BLOCK would make the product unusable on day one.
    assert suggestion.band == cf.Band.REVIEW.value


def test_prior_approvals_lift_a_mapping_out_of_the_block_band():
    """The signal §13 weights highest, and the reason mapping memory exists: the
    ninth offer letter from a customer must not be reviewed as though it were
    the first."""
    plan = suggest_bindings(
        _manifest(_field("position_title", label="<Position Title>")),
        ["Position Title"],
        records=_rows(**{"Position Title": ["Production Technician"]}),
        memory_lookups={"position_title": _Lookup(_Prior("Position Title", 2))},
    )
    suggestion = _by_id(plan)["position_title"]
    assert suggestion.vetoes == []
    assert suggestion.band == cf.Band.CONFIRM.value
    assert any(e.startswith("historical_approvals:2") for e in suggestion.evidence)


def test_a_money_field_bound_to_a_column_of_prose_is_vetoed_on_type():
    """The type gate reads the column's *values*. A salary field bound to a
    column of free text scores well on every name-based signal -- which is
    exactly the mapping that puts a sentence where an amount belongs."""
    plan = suggest_bindings(
        _manifest(_field("annual_salary", "currency", label="<Annual Salary>")),
        ["Annual Salary"],
        records=_rows(**{"Annual Salary": ["to be confirmed", "as per band"]}),
        memory_lookups={"annual_salary": _Lookup(_Prior("Annual Salary", 40))},
    )
    suggestion = _by_id(plan)["annual_salary"]
    assert suggestion.observed_type == "text"
    assert cf.VETO_TYPE_MISMATCH in suggestion.vetoes
    # A veto sends a candidate to REVIEW, not BLOCK: §13's signal table says
    # "Vetoes -- force REVIEW regardless of computed score", and BLOCK is the
    # band for a score below 0.50. The distinction is load-bearing on a new
    # installation, where the "no precedent and no family" veto is true of
    # every mapping and BLOCK would make the product unusable on day one.
    assert suggestion.band == cf.Band.REVIEW.value
    # The proposal survives the veto: a blocked mapping is still the mapping a
    # reviewer has to look at, and dropping it would hide the finding.
    assert plan.as_field_bindings() == {"annual_salary": "Annual Salary"}


def test_a_money_field_with_one_signal_needs_corroboration():
    """§13's second veto. One emphatic signal on a money field is a single point
    of failure, and being wrong there is the most expensive way to be wrong."""
    plan = suggest_bindings(
        _manifest(_field("bonus_amount", "currency", label="<Bonus Amount>")),
        ["Bonus Amount"],
        # No records, so the type is unmeasured and the gate withholds both its
        # weight and its veto -- leaving the name signals alone in support.
        memory_lookups={"bonus_amount": _Lookup()},
    )
    suggestion = _by_id(plan)["bonus_amount"]
    assert cf.VETO_THIN_EVIDENCE not in suggestion.vetoes, "name and semantics are two signals"

    # Precedent alone, on a column no name or embedding signal would ever
    # propose, and with no sample rows to type it: one signal, and §13 refuses
    # to let one signal carry a money field however strong it is.
    thin = suggest_bindings(
        _manifest(_field("bonus_amount", "currency", label="<Bonus Amount>")),
        ["Discretionary Pool"],
        memory_lookups={"bonus_amount": _Lookup(_Prior("Discretionary Pool", 1))},
    )
    proposal = _by_id(thin)["bonus_amount"]
    assert proposal.column == "Discretionary Pool"
    # One signal supports it; the other two were computed and found nothing,
    # and the type gate never ran because nothing typed the column.
    assert "historical_approvals:1_approvals" in proposal.evidence
    assert "exact_name_match:0.00" in proposal.evidence
    assert not any(e.startswith("type_compatibility") for e in proposal.evidence)
    assert cf.VETO_THIN_EVIDENCE in proposal.vetoes


def test_two_columns_that_normalise_to_the_same_field_veto_each_other():
    """§13's fourth veto, and the one a single winner cannot express. Ranking
    two indistinguishable columns still picks one, and picking one of two
    identical candidates silently is precisely the wrong answer."""
    plan = suggest_bindings(
        _manifest(_field("line_manager", label="<Line Manager>")),
        ["Line Manager", "Line-Manager"],
        records=_rows(**{"Line Manager": ["Ada"], "Line-Manager": ["Grace"]}),
    )
    suggestion = _by_id(plan)["line_manager"]
    assert cf.VETO_AMBIGUOUS in suggestion.vetoes
    # A veto sends a candidate to REVIEW, not BLOCK: §13's signal table says
    # "Vetoes -- force REVIEW regardless of computed score", and BLOCK is the
    # band for a score below 0.50. The distinction is load-bearing on a new
    # installation, where the "no precedent and no family" veto is true of
    # every mapping and BLOCK would make the product unusable on day one.
    assert suggestion.band == cf.Band.REVIEW.value
    assert [a["source_ref"] for a in suggestion.alternatives] == ["Line-Manager"]
    assert cf.VETO_AMBIGUOUS in suggestion.alternatives[0]["vetoes"]


def test_the_same_inputs_always_produce_the_same_ranking():
    """An approval summary that reshuffles between two runs of the same data is
    unreviewable: a reviewer cannot tell a real change from a reordering."""
    manifest = _manifest(_field("line_manager", label="<Line Manager>"))
    records = _rows(**{"Line Manager": ["Ada"], "Line-Manager": ["Grace"]})
    first = suggest_bindings(manifest, ["Line Manager", "Line-Manager"], records=records)
    second = suggest_bindings(manifest, ["Line-Manager", "Line Manager"], records=records)
    assert first.as_field_bindings() == second.as_field_bindings()
    assert _by_id(first)["line_manager"].alternatives == _by_id(second)["line_manager"].alternatives


# --------------------------------------------------------- ranked alternatives


def test_every_target_comes_back_with_its_ranked_alternatives():
    """The REVIEW band is *defined* as "presented with ranked alternatives and
    the reasoning behind each". A response carrying one winner cannot satisfy
    it, whatever the winner scores."""
    plan = suggest_bindings(
        _manifest(_field("manager_name", label="<Manager Name>")),
        ["Manager Name", "Manager Namee", "Unrelated Column"],
        records=_rows(**{
            "Manager Name": ["Ada"], "Manager Namee": ["Grace"], "Unrelated Column": ["x"],
        }),
    )
    suggestion = _by_id(plan)["manager_name"]
    assert suggestion.column == "Manager Name"
    assert [a["source_ref"] for a in suggestion.alternatives] == ["Manager Namee"]
    for alternative in suggestion.alternatives:
        assert set(alternative) >= {"source_ref", "score", "band", "evidence", "vetoes"}
    scores = [suggestion.confidence] + [a["score"] for a in suggestion.alternatives]
    assert scores == sorted(scores, reverse=True)


def test_a_contested_column_goes_to_the_field_with_the_better_case():
    """One column, one field: two fields drawing from the same column means one
    of them is wrong, and the letter repeats a value in two places. The loser
    falls to its next-ranked candidate rather than being dropped."""
    plan = suggest_bindings(
        _manifest(
            _field("start_date", "date", label="<Start Date>"),
            _field("original_start_date", "date", label="<Original Start Date>"),
        ),
        ["Start Date", "Original Start Date"],
        records=_rows(**{
            "Start Date": ["01/08/2026"], "Original Start Date": ["15/03/2021"],
        }),
    )
    assert plan.as_field_bindings() == {
        "start_date": "Start Date",
        "original_start_date": "Original Start Date",
    }


def test_a_field_with_no_candidate_at_all_is_reported_unmatched():
    """An unbound field is a placeholder left in the letter. It has to be named
    as unmatched rather than handed a low-confidence guess."""
    plan = suggest_bindings(
        _manifest(_field("signatory_title", label="<Signatory Title>")),
        ["Widget Count"],
        records=_rows(**{"Widget Count": ["7"]}),
    )
    suggestion = _by_id(plan)["signatory_title"]
    assert suggestion.column is None
    assert suggestion.method == "unmatched"
    # BLOCK, and rightly: nothing matched at all. That is the score-below-the-
    # floor case the band table describes, not a veto over real evidence.
    assert suggestion.band == cf.Band.BLOCK.value
    assert plan.unmatched_fields == ["signatory_title"]
    assert plan.unused_columns == ["Widget Count"]


# ---------------------------------------------------------------- memory rules


def test_memory_that_was_never_consulted_omits_the_signal_rather_than_scoring_zero():
    """"Nobody asked" and "asked, and this organisation has never approved it"
    are different findings. Recording the second as the first would fabricate a
    computed signal; recording the first as the second would let a caller that
    forgot to pass memory look like a cold-start organisation."""
    manifest = _manifest(_field("grade", label="<Grade>"))
    records = _rows(Grade=["Level 4"])

    unasked = _by_id(suggest_bindings(manifest, ["Grade"], records=records))["grade"]
    asked = _by_id(suggest_bindings(
        manifest, ["Grade"], records=records, memory_lookups={"grade": _Lookup()},
    ))["grade"]

    assert not any(e.startswith("historical_approvals") for e in unasked.evidence)
    assert "historical_approvals:0_approvals" in asked.evidence
    # A signal computed and found empty does not change the score...
    assert unasked.confidence == pytest.approx(asked.confidence)
    # ...but it is the difference between having asked and not, which is what
    # the no-precedent veto turns on either way.
    assert cf.VETO_NO_PRECEDENT in unasked.vetoes and cf.VETO_NO_PRECEDENT in asked.vetoes


def test_a_previously_approved_column_is_proposed_even_when_the_names_disagree():
    """The mapping memory tier: "Emp Fname" was approved against
    `colleague_first_name` last month, and no name or embedding signal would
    ever propose it again."""
    plan = suggest_bindings(
        _manifest(_field("colleague_first_name", label="<Colleague First Name>")),
        ["Emp Fname"],
        records=_rows(**{"Emp Fname": ["Olivia"]}),
        memory_lookups={"colleague_first_name": _Lookup(_Prior("Emp Fname", 6))},
    )
    suggestion = _by_id(plan)["colleague_first_name"]
    assert suggestion.column == "Emp Fname"
    assert suggestion.method == "memory"
    assert suggestion.vetoes == []


def test_a_neighbouring_fields_precedent_is_not_this_fields_precedent():
    """Memory ranks prior mappings for the field asked about and for fields
    whose context resembles it. §13's signal is "the same mapping approved 42
    times", so crediting `start_date` with what a reviewer approved for
    `effective_date` would manufacture evidence for a column nobody confirmed --
    on exactly the pair of fields most likely to be confused."""
    plan = suggest_bindings(
        _manifest(_field("start_date", "date", label="<Start Date>")),
        ["Start Date"],
        records=_rows(**{"Start Date": ["01/08/2026"]}),
        memory_lookups={"start_date": _Lookup(_Prior("Start Date", 9, similarity=0.61))},
    )
    suggestion = _by_id(plan)["start_date"]
    assert "historical_approvals:0_approvals" in suggestion.evidence
    assert cf.VETO_NO_PRECEDENT in suggestion.vetoes


def test_an_alias_a_reviewer_confirmed_counts_as_an_exact_name_match():
    """A synonym fold is a human-approved equivalence. §13 folds it into
    exact_name_match deliberately -- it is not a weaker kind of guess than a
    literal match."""
    plan = suggest_bindings(
        _manifest(_field("colleague_first_name", label="<Colleague First Name>")),
        ["Emp Fname"],
        dictionary={"emp_fname": "colleague_first_name"},
        records=_rows(**{"Emp Fname": ["Olivia"]}),
    )
    suggestion = _by_id(plan)["colleague_first_name"]
    assert suggestion.method == "dictionary"
    # Full strength, exactly as a literal match: the evidence line carries no
    # qualifier because the fold is not a weaker kind of agreement.
    assert "exact_name_match" in suggestion.evidence


# ------------------------------------------------------------------ the model


def test_a_models_own_confidence_never_reaches_the_score():
    """§13 excludes a self-reported confidence twice over. An LLM proposal is
    scored on the independent signals it happens to carry, so a proposal nothing
    supports lands in front of a human instead of at the old flat 0.80."""
    def matcher(field_ids, columns):
        return [(field_ids[0], columns[0], "the model is 99% sure this is the one")]

    plan = suggest_bindings(
        _manifest(_field("recruiter_contact", label="<Recruiter Contact>")),
        ["Widget Count"],
        records=_rows(**{"Widget Count": ["7"]}),
        llm_matcher=matcher,
    )
    suggestion = _by_id(plan)["recruiter_contact"]
    assert suggestion.column == "Widget Count"
    assert suggestion.method == "llm"
    assert suggestion.confidence != 0.8
    assert suggestion.confidence < cf.REVIEW_FLOOR
    # BLOCK, and rightly: nothing matched at all. That is the score-below-the-
    # floor case the band table describes, not a veto over real evidence.
    assert suggestion.band == cf.Band.BLOCK.value


def test_the_model_is_only_asked_about_what_the_deterministic_tiers_missed():
    """Cost is meant to be proportional to the residue, not the template. A
    model call per field would make onboarding an estate unaffordable."""
    asked = {}

    def matcher(field_ids, columns):
        asked["fields"], asked["columns"] = list(field_ids), list(columns)
        return []

    suggest_bindings(
        _manifest(_field("grade", label="<Grade>"), _field("mystery_field", label="<Mystery Field>")),
        ["Grade", "Widget Count"],
        records=_rows(Grade=["Level 4"], **{"Widget Count": ["7"]}),
        llm_matcher=matcher,
    )
    assert asked["fields"] == ["mystery_field"]
    assert "Grade" not in asked["columns"]


# ------------------------------------------------------------- column typing


@pytest.mark.parametrize(
    "values,expected",
    [
        (["01/08/2026", "15/03/2021"], "date"),
        (["82000", "90,200.00", "$8,200"], "number"),
        (["(1,200)", "-450"], "number"),
        (["1 200", "1 500"], "number"),
        (["38"], "number"),
        (["Yes", "no"], "boolean"),
        (["Melbourne, Victoria"], "text"),
        (["82000", "to be confirmed"], "text"),
        ([], None),
        (["", "   "], None),
        (["nan"], "text"),
    ],
)
def test_a_columns_type_is_read_from_its_values(values, expected):
    """Every cell in a CSV is text on disk. Typing a column from the file format
    would either veto every mapping in the estate or check nothing at all; and a
    majority vote would hide the three "to be confirmed" rows that are exactly
    the ones that render a sentence where an amount belongs."""
    assert infer_column_type(values) == expected


def test_a_column_whose_name_carries_no_words_still_produces_a_candidate():
    """Exports really do contain a column called "#". The embedder refuses text
    with nothing to hash rather than returning a directionless zero vector, and
    that refusal has to mean "no semantic signal", not "the request failed"."""
    plan = suggest_bindings(
        _manifest(_field("row_number", label="<Row Number>")),
        ["#"],
        records=_rows(**{"#": ["1", "2"]}),
    )
    suggestion = _by_id(plan)["row_number"]
    assert suggestion.column is None, "nothing proposes a column with no name to match on"
    assert plan.unused_columns == ["#"]


def test_a_proposal_nothing_supports_says_so_in_words():
    """The rationale is what a reviewer reads first. A model proposal with no
    independent signal behind it must not read like a finding."""
    def matcher(field_ids, columns):
        return [(field_ids[0], columns[0], "")]

    plan = suggest_bindings(
        _manifest(_field("recruiter_contact", label="<Recruiter Contact>")),
        ["Widget Count"],
        llm_matcher=matcher,
    )
    suggestion = _by_id(plan)["recruiter_contact"]
    assert suggestion.rationale.startswith("Proposed by the mapping model")
    assert "nothing independent supports it" in suggestion.rationale


def test_a_model_naming_a_column_that_is_not_in_the_sheet_is_ignored():
    """A model that hallucinates a column name must not create a binding to a
    column that does not exist -- the fill would silently leave the field
    blank, which is the failure mode this whole path exists to avoid."""
    def matcher(field_ids, columns):
        return [(field_ids[0], "Column That Does Not Exist", "invented"),
                ("field_that_does_not_exist", columns[0], "invented")]

    plan = suggest_bindings(
        _manifest(_field("recruiter_contact", label="<Recruiter Contact>")),
        ["Widget Count"],
        records=_rows(**{"Widget Count": ["7"]}),
        llm_matcher=matcher,
    )
    assert plan.as_field_bindings() == {}
    assert plan.unmatched_fields == ["recruiter_contact"]


def test_an_empty_column_is_unknown_rather_than_a_type_mismatch():
    """A column with no values has not disagreed with anything. UNKNOWN
    withholds the weight and the veto, which is the honest answer -- vetoing
    would block every optional field in the estate."""
    plan = suggest_bindings(
        _manifest(_field("end_date", "date", label="<End Date>")),
        ["End Date"],
        records=_rows(**{"End Date": ["", ""]}),
    )
    suggestion = _by_id(plan)["end_date"]
    assert suggestion.observed_type is None
    assert cf.VETO_TYPE_MISMATCH not in suggestion.vetoes


def test_column_profiles_report_what_the_reviewer_needs_to_see():
    profiles = {p.name: p for p in profile_columns(
        ["Start Date", "Notes"],
        _rows(**{"Start Date": ["01/08/2026", ""], "Notes": ["", ""]}),
    )}
    assert profiles["Start Date"].observed_type == "date"
    assert profiles["Start Date"].sample_value == "01/08/2026"
    assert profiles["Start Date"].non_empty == 1
    assert profiles["Notes"].observed_type is None


# ------------------------------------------------------------- condition vars


def test_a_condition_variable_declares_no_type_and_is_not_given_one():
    """`colleague_type` has no placeholder and no field entry, so nothing
    declares its type. Recording "string" -- which accepts every value on earth
    -- would be inventing a declaration nobody made, and the type gate would
    report a check that never happened."""
    plan = suggest_bindings(
        _manifest(conditions=[{"id": "c", "expression": "colleague_type == 'Full time'"}]),
        ["Colleague Type"],
        records=_rows(**{"Colleague Type": ["Full time"]}),
    )
    suggestion = _by_id(plan)["colleague_type"]
    assert suggestion.column == "Colleague Type"
    assert suggestion.declared_type is None
    assert not any(e.startswith("type_compatibility") for e in suggestion.evidence)


def test_band_counts_summarise_what_a_reviewer_is_facing():
    plan = suggest_bindings(
        _manifest(_field("grade", label="<Grade>"), _field("mystery_field", label="<Mystery Field>")),
        ["Grade"],
        records=_rows(Grade=["Level 4"]),
        memory_lookups={"grade": _Lookup(_Prior("Grade", 5)), "mystery_field": _Lookup()},
    )
    counts = plan.band_counts()
    assert counts[cf.Band.CONFIRM.value] == 1
    assert counts[cf.Band.AUTO_ACCEPT.value] == 0
    # The unmatched field has no column, so it is not a banded candidate; it is
    # reported as unmatched instead of being counted as a blocked mapping.
    assert sum(counts.values()) == 1
    assert plan.unmatched_fields == ["mystery_field"]


# ------------------------------------------------------- the onboarding log

HOSPIRA = Path(__file__).resolve().parent / "fixtures" / "templates" / "hospira_offer.docx"
WORKBOOK = Path(__file__).resolve().parent / "fixtures" / "records" / "colleagues.xlsx"


def test_the_onboarding_log_names_a_mapping_that_will_fill_but_be_wrong():
    """A test fill can pass every QA gate on a mapping that is wrong: the
    placeholder is filled, nothing is left over, and the salary is a sentence.
    The agentic loop's log has to carry the vetoes as well as the QA failures,
    because the gates cannot see this one."""
    from app.compiler.mapping_agent import _binding_vetoes

    plan = suggest_bindings(
        _manifest(_field("annual_salary", "currency", label="<Annual Salary>")),
        ["Annual Salary"],
        records=_rows(**{"Annual Salary": ["to be confirmed"]}),
    )
    assert _binding_vetoes(plan) == [
        f"annual_salary -> Annual Salary: {cf.VETO_TYPE_MISMATCH}"
    ]


def test_the_onboarding_log_stays_quiet_about_the_cold_start_veto():
    """Nothing has precedent on a template nobody has onboarded yet, so that
    veto fires on every candidate of every template. Reporting it here would
    bury the vetoes that describe a real defect under one per field."""
    from app.compiler.mapping_agent import _binding_vetoes

    plan = suggest_bindings(
        _manifest(_field("grade", label="<Grade>")),
        ["Grade"],
        records=_rows(Grade=["Level 4"]),
        memory_lookups={"grade": _Lookup()},
    )
    assert cf.VETO_NO_PRECEDENT in _by_id(plan)["grade"].vetoes
    assert _binding_vetoes(plan) == []


@pytest.mark.skipif(
    not (HOSPIRA.exists() and WORKBOOK.exists()),
    reason="Hospira template/workbook fixtures are missing from backend/tests/fixtures/",
)
def test_the_agentic_compile_verifies_its_bindings_against_the_real_workbook():
    """End to end on the real template and its real spreadsheet: the loop's log
    is serialisable, and a workbook whose columns type cleanly produces no
    binding vetoes -- so a veto in that log always means something."""
    from app.compiler.mapping_agent import compile_agentic
    from app.generation.source_ingestion import extract_records

    columns, records = extract_records(str(WORKBOOK), "xlsx")
    result = compile_agentic(str(HOSPIRA), sample_records=records[:1], columns=columns)

    first = result.iterations[0]
    assert first.binding_vetoes == [], first.binding_vetoes
    assert "binding_vetoes" in result.log()[0]


def test_a_revision_may_not_delete_a_paragraph_that_carries_a_value():
    """The agent's revision schema offers two moves -- widen or narrow a block,
    or delete a paragraph -- and neither of them is "add the missing field". So
    when the QA failure is an unclaimed placeholder, deleting the line it sits on
    is the only move available, and the model takes it.

    Measured on a real payroll notification: handed "leftover placeholder
    brackets", it proposed deleting paragraphs 31, 32, 44 and 45, which were the
    Legal Entity and Fixed Term table rows. Label and value both went. The next
    iteration's `resolved_value_absent` gate caught it -- but by then the manifest
    said to delete content the letter needs, and the failure had moved from a
    visible `<Yes/No>` a reviewer would spot to a paragraph that is simply gone.
    """
    from app.compiler.mapping_agent import _apply_revision
    from app.compiler.rule_compiler import CompiledManifest

    manifest = CompiledManifest(
        fields=[{"id": "legal_entity", "slots": [
            {"paragraph_index": 31, "span_index": 0, "text": "<1G0 Hospira Australia Pty Ltd>"},
        ]}],
        conditions=[], blocks=[], delete_always=[],
        mergefield_paragraphs=[], hyperlink_paragraphs=[],
        confidence=1.0, compiled_by="test", prescan_summary={},
    )

    applied = _apply_revision(manifest, {"block_corrections": [], "delete_paragraphs": [31, 32, 44, 45]})

    deleted = sorted(entry["paragraph_index"] for entry in manifest.delete_always)
    assert 31 not in deleted, "the paragraph carrying the legal entity must survive"
    assert deleted == [32, 44, 45], "the deletions that harm nothing still apply"
    assert applied == 3
