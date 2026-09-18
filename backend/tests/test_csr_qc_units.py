"""The QC number-matching tests the spec makes non-negotiable.

Acceptance criterion 5 is "altered number -> mismatch flag", and the whole
grounding claim of the CSR module rests on it: if a number can drift away from
its source without QC noticing, the citations are decoration. These assert the
two halves of that guarantee -- a number with no citation is found, and a
number with a citation is checked against what the citation points at -- plus
the normalisation that keeps the check from crying wolf over a comma.

The false-positive cases matter as much as the true ones. A panel that flags
"9.4.6 Blinding" as an unsourced 9.4.6 gets switched off in a week, and then
the real findings go unread too.
"""

from __future__ import annotations

import pytest

from app.csr.qc import (
    collect_abbreviations,
    cross_section_consistency,
    data_needed_findings,
    extract_headline_counts,
    normalize_number,
    section_findings,
    uncited_numbers,
    verify_citation_values,
)


# ------------------------------------------------------------ citation coverage

def test_numbers_in_a_sentence_with_no_citation_are_flagged():
    """The core grounding check: prose that states a fact and names no source."""
    content = "A total of 240 patients received at least one dose of 50 mg."

    assert uncited_numbers(content) == ["240", "50"]


def test_the_same_sentence_with_a_citation_is_clean():
    """Identical numbers, one marker: the check is about sourcing, not digits."""
    content = "A total of 240 patients received at least one dose of 50 mg [S2, p.14]."

    assert uncited_numbers(content) == []


def test_a_citation_written_with_a_table_reference_also_counts():
    """Prompt rule 2 permits [S#, Table Y]; only accepting p.X would flag every
    disposition and efficacy sentence in the report."""
    content = "In total 120 patients were randomized [S1, Table 14.1.1]."

    assert uncited_numbers(content) == []


def test_a_heading_number_is_never_an_uncited_number():
    """Prompt rule 6 makes the model emit its own heading. Counting that as an
    unsourced fact would put a finding on all 60-odd sections of every CSR."""
    content = (
        "9.4.6 Blinding\n"
        "The study was double-blind and the randomization code was held by the "
        "sponsor's statistician [S1, p.31]."
    )

    assert uncited_numbers(content) == []


def test_prose_that_opens_with_a_count_keeps_its_number():
    """The other side of the heading rule -- "120 patients ..." starts a line
    with a number too, and losing it would be a silent hole in the check."""
    content = "120 patients were enrolled at 14 sites."

    assert uncited_numbers(content) == ["120", "14"]


def test_a_decimal_and_an_abbreviation_do_not_split_a_sentence():
    """Splitting at "e.g." or at "p. 4" would strand the numbers in a fragment
    that has no marker, and report correctly cited prose as uncited."""
    content = "Rescue medication (e.g. paracetamol 1.5 g) was allowed [S1, p. 44]."

    assert uncited_numbers(content) == []


def test_a_declared_gap_is_not_an_uncited_number():
    """[DATA NEEDED: Table 14.1.1] is the behaviour prompt rule 3 asks for;
    flagging the table id inside it would punish the model for complying."""
    content = "The sample size assumptions are [DATA NEEDED: Table 14.1.1 power calculation]."

    assert uncited_numbers(content) == []


def test_each_line_of_a_disposition_list_is_judged_on_its_own():
    """One fact per line: a marker on the second bullet must not excuse the
    first, which is how an uncited count slips through a list."""
    content = "- 120 patients enrolled\n- 45 patients completed [S1, Table 14.1.1]"

    assert uncited_numbers(content) == ["120"]


# ---------------------------------------------------------------- normalisation

@pytest.mark.parametrize("text,expected", [
    ("12.3%", "12.3"),      # a TLF prints the percent sign, a draft may not
    ("45.0%", "45"),
    ("1,234", "1234"),      # thousands separators are formatting, not value
    ("1,234.50", "1234.5"),
    ("12.30", "12.3"),      # trailing zeros are precision, not a different number
    ("120", "120"),         # never 1.2E+2
    ("0.001", "0.001"),
    ("-2.5", "-2.5"),
    ("+2.5", "2.5"),
    ("-0.0", "0"),
    ("  57  ", "57"),
])
def test_normalize_number_reduces_formats_to_one_value(text, expected):
    assert normalize_number(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "n/a", "twelve", "12.3.4", "1,23", "NaN", "Infinity", "1e5", None,
])
def test_normalize_number_returns_none_for_anything_that_is_not_a_number(text):
    """"NaN" and "1e5" construct as Decimals without complaint. A NaN that
    equals nothing would turn every value verification into a silent pass."""
    assert normalize_number(text) is None


# --------------------------------------------------- number-to-source verification

def test_a_value_present_in_a_different_format_verifies():
    """The draft writes 12.3, the TLF prints 12.3%. Reporting a mismatch there
    would train writers to ignore the panel."""
    citations = [{"marker": "[S3, Table 14.3.1]", "cited_value": "12.3", "chunk_id": "c1"}]
    chunks = {"c1": "Responder rate: 12.3% (95% CI 8.1, 17.4)"}

    assert verify_citation_values(citations, chunks) == []


def test_a_thousands_separated_source_verifies_an_unseparated_value():
    citations = [{"marker": "[S1, p.4]", "cited_value": "1234", "chunk_id": "c1"}]
    chunks = {"c1": "A total of 1,234 subjects were screened."}

    assert verify_citation_values(citations, chunks) == []


def test_an_altered_number_is_flagged():
    """Acceptance criterion 5, stated directly."""
    citations = [{"marker": "[S1, Table 14.1.1]", "cited_value": "121", "chunk_id": "c1"}]
    chunks = {"c1": "Randomized: 120 subjects"}

    findings = verify_citation_values(citations, chunks)

    assert [f.code for f in findings] == ["NUMBER_NOT_IN_SOURCE"]
    assert findings[0].detail == {
        "marker": "[S1, Table 14.1.1]", "cited_value": "121", "chunk_id": "c1"}


def test_a_value_that_is_only_a_substring_of_a_source_number_is_flagged():
    """"120" occurs inside "1120". Substring matching would pass an off-by-a-
    thousand count, which is the failure this check exists to catch."""
    citations = [{"marker": "[S1, p.7]", "cited_value": "120", "chunk_id": "c1"}]
    chunks = {"c1": "Screened: 1120 subjects"}

    assert [f.code for f in verify_citation_values(citations, chunks)] == ["NUMBER_NOT_IN_SOURCE"]


def test_an_unresolved_citation_is_flagged():
    """A marker pointing at nothing looks exactly like a verified one on the
    page, which makes it the more dangerous of the two failures."""
    citations = [
        {"marker": "[S9, p.1]", "cited_value": "45", "chunk_id": None},
        {"marker": "[S8, p.2]", "cited_value": "45", "chunk_id": "gone"},
    ]

    findings = verify_citation_values(citations, {"c1": "45"})

    assert [f.code for f in findings] == ["CITATION_UNRESOLVED", "CITATION_UNRESOLVED"]


def test_a_citation_carrying_no_value_is_not_a_finding():
    """Markers on prose ("as specified in the protocol [S1, p.3]") carry no
    number; demanding one would flag every method sentence in the report."""
    citations = [{"marker": "[S1, p.3]", "cited_value": None, "chunk_id": "c1"}]

    assert verify_citation_values(citations, {"c1": "double-blind"}) == []


# --------------------------------------------------- cross-section N consistency

def test_disagreeing_enrolled_counts_across_sections_are_flagged():
    counts = {
        "10.1": extract_headline_counts("A total of 120 patients were enrolled [S1, p.4]."),
        "11.1": extract_headline_counts("The full analysis set comprised the 118 patients enrolled [S2, p.9]."),
    }

    findings = cross_section_consistency(counts)

    assert [f.code for f in findings] == ["COUNT_DISAGREEMENT"]
    assert findings[0].detail["concept"] == "enrolled"
    assert findings[0].detail["values_by_section"] == {"10.1": ["120"], "11.1": ["118"]}
    assert "10.1" in findings[0].message and "11.1" in findings[0].message


def test_agreeing_counts_are_silent():
    counts = {
        "10.1": extract_headline_counts("A total of 120 patients were enrolled [S1, p.4]."),
        "11.1": extract_headline_counts("The full analysis set comprised the 120 patients enrolled [S2, p.9]."),
    }

    assert cross_section_consistency(counts) == []


def test_a_spelling_variant_does_not_hide_a_disagreement():
    """The disagreement is about patients. It would be absurd for a British
    "randomised" in one section to excuse a different N in the next."""
    counts = {
        "10.1": extract_headline_counts("240 patients were randomised [S1, p.4]."),
        "12.1": extract_headline_counts("238 patients were randomized [S3, p.2]."),
    }

    assert [f.detail["concept"] for f in cross_section_consistency(counts)] == ["randomized"]


def test_a_thousands_separator_is_not_a_disagreement():
    counts = {
        "10.1": extract_headline_counts("1,234 patients were screened [S1, p.2]."),
        "11.1": extract_headline_counts("1234 patients were screened [S2, p.2]."),
    }

    assert cross_section_consistency(counts) == []


def test_a_count_stated_in_only_one_section_is_silent():
    assert cross_section_consistency({"10.1": {"treated": ["120"]}}) == []


def test_the_count_in_front_of_the_word_wins_over_a_nearer_one_behind_it():
    """"120 were screened and 100 were enrolled": the nearest number to
    "screened" is 100. Pairing those would invent a disagreement out of a
    perfectly correct sentence."""
    counts = extract_headline_counts("120 patients were screened and 100 were enrolled [S1, p.4].")

    assert counts == {"screened": ["120"], "enrolled": ["100"]}


def test_a_percentage_or_a_parenthetical_is_not_a_headline_count():
    """"60 (75.0%) patients completed" states one N and two derived figures."""
    counts = extract_headline_counts("A total of 60 (75.0%) patients completed the study [S1, p.5].")

    assert counts == {"completed": ["60"]}


def test_a_count_written_after_its_label_is_found():
    assert extract_headline_counts("Patients enrolled: 120 [S1, p.4]") == {"enrolled": ["120"]}


def test_a_table_reference_is_never_a_headline_count():
    """A table id sits exactly where the count-finder looks, on both sides of
    the word. Recording 14.1 as the number enrolled would disagree with the
    real 120 in the next section -- a disagreement invented out of two
    perfectly correct sections, which is the one outcome this must not do."""
    assert extract_headline_counts("Patients were enrolled (Table 14.1.1) [S1, p.4].") == {}
    assert extract_headline_counts("Table 14.1.1 gives the number enrolled [S1].") == {}
    # The parenthesised N is the case the lookahead exists for; it must survive.
    assert extract_headline_counts("Patients randomized (N=120) received drug [S1].") == {
        "randomized": ["120"]}


# ------------------------------------------------------------------ declared gaps

def test_each_declared_gap_becomes_one_blocking_finding():
    findings = data_needed_findings("9.7", ["the sample size calculation", "the sample size calculation", ""])

    assert [f.code for f in findings] == ["DATA_NEEDED", "DATA_NEEDED"]
    assert findings[0].section_number == "9.7"
    assert findings[0].detail["payload"] == "the sample size calculation"
    assert findings[1].detail["payload"] == "unspecified"


# ------------------------------------------------------------------ abbreviations

def test_an_abbreviation_is_paired_with_its_expansion_and_its_repeats_counted():
    texts = [
        "Every Adverse Event (AE) was coded using MedDRA.",
        "The AE rate was similar across arms. Serious AEs are in Section 12.3.",
    ]

    assert collect_abbreviations(texts) == [
        {"abbreviation": "AE", "expansion": "Adverse Event", "count": 3},
    ]


def test_an_abbreviation_that_was_never_expanded_reports_no_expansion():
    """The omission is exactly what Section 4 exists to surface. Guessing an
    expansion would put a definition in the report that nobody wrote."""
    collected = collect_abbreviations(["The ITT population was analysed."])

    assert collected == [{"abbreviation": "ITT", "expansion": None, "count": 1}]


def test_a_multi_word_expansion_with_joining_words_is_captured():
    collected = collect_abbreviations([
        "The Case Report Form (CRF) was completed.",
        "A treatment-emergent adverse event (TEAE) was any AE after first dose.",
    ])

    assert [(c["abbreviation"], c["expansion"]) for c in collected] == [
        ("AE", None),
        ("CRF", "Case Report Form"),
        ("TEAE", "treatment-emergent adverse event"),
    ]


def test_all_caps_prose_and_citation_markers_do_not_become_abbreviations():
    """A Section 4 padded with "AND" and "S1" is one a reviewer stops reading,
    and then the genuine omissions go unnoticed too."""
    collected = collect_abbreviations([
        "STUDY OBJECTIVES AND ENDPOINTS\nThe CSR was prepared per ICH E3 [S1, p.1].",
    ])

    assert [c["abbreviation"] for c in collected] == ["CSR", "E3", "ICH"]


def test_an_all_caps_heading_word_is_not_an_abbreviation():
    """ICH E3 writes SAFETY, DESIGN and ETHICS as headings, and every one of
    them is short enough to pass the acronym length rule. A Section 4 opening
    with those three is one a reviewer stops reading before reaching the
    omission the list exists to show."""
    collected = collect_abbreviations([
        "SAFETY EVALUATION\nSTUDY DESIGN\nETHICS REVIEW\n"
        "The ITT population had AEs and one SAE.",
    ])

    assert [c["abbreviation"] for c in collected] == ["AE", "ITT", "SAE"]


def test_abbreviations_come_back_alphabetically():
    collected = collect_abbreviations(["The SAP, the CRF and the ICF were reviewed."])

    assert [c["abbreviation"] for c in collected] == ["CRF", "ICF", "SAP"]


# ------------------------------------------------------------ the per-section run

def test_section_findings_reports_every_per_section_defect_and_stamps_the_section():
    content = (
        "10.1 Disposition of Patients\n"
        "In total 120 patients were randomized [S1, Table 14.1.1].\n"
        "Of these, 15 discontinued.\n"
        "[DATA NEEDED: reasons for discontinuation by arm]"
    )
    citations = [{"marker": "[S1, Table 14.1.1]", "cited_value": "121", "chunk_id": "c1"}]

    findings = section_findings(
        section_number="10.1",
        content=content,
        citations=citations,
        chunk_text_by_id={"c1": "Randomized: 120 subjects"},
    )

    assert [f.code for f in findings] == [
        "UNCITED_NUMBER", "NUMBER_NOT_IN_SOURCE", "DATA_NEEDED"]
    assert {f.section_number for f in findings} == {"10.1"}
    assert findings[0].detail["numbers"] == ["15"]


def test_a_clean_section_produces_no_findings():
    """The check has to be able to say yes, or approval means nothing."""
    content = (
        "10.1 Disposition of Patients\n"
        "In total 120 patients were randomized and 105 completed the study "
        "[S1, Table 14.1.1]."
    )
    citations = [{"marker": "[S1, Table 14.1.1]", "cited_value": "120", "chunk_id": "c1"}]

    findings = section_findings(
        section_number="10.1",
        content=content,
        citations=citations,
        chunk_text_by_id={"c1": "Randomized: 120 subjects; Completed: 105"},
    )

    assert findings == []


def test_a_finding_serialises_to_json_safe_primitives():
    """QcFinding crosses the wire on GET /csr/.../qc; a Decimal in `detail`
    is a 500 on the Issues tab."""
    finding = section_findings(
        section_number="9.7",
        content="The power was 90%.",
        citations=[],
        chunk_text_by_id={},
    )[0].as_dict()

    assert finding["code"] == "UNCITED_NUMBER"
    assert finding["section_number"] == "9.7"
    assert finding["detail"]["numbers"] == ["90%"]
