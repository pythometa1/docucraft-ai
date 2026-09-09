"""Reading a line listing, and refusing the parts that cannot be read safely.

Two properties carry the file. A line listing is EVENT-level, so three
reactions on one case are three rows and reading each as a case trebles every
count. And `03/04/2026` is genuinely two different dates, either of which is a
receipt date that decides which reporting interval a case falls into -- so it
is refused rather than guessed, the same refusal `app.cmc.values` makes for a
bare comma decimal.
"""

from datetime import date, datetime

import pytest

from app.safety.line_listing import (
    DAY_FIRST, DATE_ORDER_KEY, MONTH_FIRST, AmbiguousDate, parse_date, parse_flag,
    read_rows, suggest, validate_mapping,
)

HEADERS = ["Case Number", "Initial Receipt Date", "Country", "Serious",
           "Preferred Term", "Verbatim Term", "Suspect Drug", "Patient Sex"]

ROWS = [
    ["GB-001", "2026-03-04", "GB", "Yes", "Headache", "bad headache",
     "Vigilazine", "F"],
    ["GB-001", "2026-03-04", "GB", "Yes", "Nausea", "felt sick",
     "Vigilazine", "F"],
    ["GB-002", "2026-04-11", "DE", "No", "Rash", "itchy rash",
     "Vigilazine", "M"],
]

MAPPING = {
    "Case Number": "worldwide_case_id",
    "Initial Receipt Date": "initial_receipt_date",
    "Country": "country_of_occurrence",
    "Serious": "is_serious",
    "Preferred Term": "meddra_pt",
    "Verbatim Term": "verbatim_term",
    "Suspect Drug": "drug_name",
    "Patient Sex": "patient_sex",
}


# --------------------------------------------------------- events, not cases

def test_three_rows_for_two_cases_produce_two_cases():
    """The defect this prevents: reading each row as a case would report two
    headaches and a nausea as three cases, and every count in the report --
    interval, cumulative, serious, by SOC -- would be inflated by exactly the
    number of reactions people happened to have."""
    result = read_rows(HEADERS, ROWS, MAPPING)
    assert len(result.cases) == 2
    first = next(c for c in result.cases if c.worldwide_case_id == "GB-001")
    assert len(first.events) == 2
    assert {e.meddra_pt for e in first.events} == {"Headache", "Nausea"}
    assert first.country_of_occurrence == "GB"
    assert first.is_serious is True


def test_a_drug_repeated_on_every_row_is_stored_once():
    result = read_rows(HEADERS, ROWS, MAPPING)
    first = next(c for c in result.cases if c.worldwide_case_id == "GB-001")
    assert [d.drug_name for d in first.drugs] == ["Vigilazine"]


def test_the_company_product_is_decided_by_the_caller():
    result = read_rows(HEADERS, ROWS, MAPPING, product_names=["Vigilazine"])
    first = result.cases[0]
    assert first.drugs[0].is_company_product is True
    plain = read_rows(HEADERS, ROWS, MAPPING, product_names=["Otherazine"])
    assert plain.cases[0].drugs[0].is_company_product is False


def test_a_row_with_no_case_identifier_is_reported_not_dropped():
    """A row that cannot be joined to its siblings would be counted as its own
    case. It is refused, and the refusal is reported against its row number so
    somebody can go and look at it."""
    rows = ROWS + [["", "2026-05-01", "FR", "Yes", "Fever", "hot", "Vigilazine", "M"]]
    result = read_rows(HEADERS, rows, MAPPING)
    assert len(result.cases) == 2
    assert 5 in result.skipped
    assert "case identifier" in result.skipped[5]
    assert result.rows_read == 4


def test_columns_nobody_mapped_are_named():
    """A column left unmapped may be the one holding the seriousness criteria."""
    partial = {k: v for k, v in MAPPING.items() if k != "Country"}
    result = read_rows(HEADERS, ROWS, partial)
    assert "Country" in result.unmapped_columns


def test_a_case_whose_rows_carry_no_term_says_so():
    rows = [["GB-003", "2026-03-04", "GB", "Yes", "", "", "Vigilazine", "F"]]
    result = read_rows(HEADERS, rows, MAPPING)
    assert result.cases[0].unmapped["events"]


def test_a_case_with_no_receipt_date_says_it_falls_in_no_interval():
    rows = [["GB-004", "", "GB", "Yes", "Headache", "ow", "Vigilazine", "F"]]
    result = read_rows(HEADERS, rows, MAPPING)
    assert "latest_receipt_date" in result.cases[0].unmapped
    assert "no reporting interval" in result.cases[0].unmapped["latest_receipt_date"]


# ------------------------------------------------------------ ambiguous dates

def test_an_unambiguous_numeric_date_reads_either_way_round():
    assert parse_date("25/03/2026") == date(2026, 3, 25)   # 25 cannot be a month
    assert parse_date("03/25/2026") == date(2026, 3, 25)   # nor here


def test_an_ambiguous_numeric_date_is_refused():
    with pytest.raises(AmbiguousDate) as raised:
        parse_date("03/04/2026")
    assert "does not say which order" in str(raised.value)


def test_a_declared_order_settles_it():
    assert parse_date("03/04/2026", order=DAY_FIRST) == date(2026, 4, 3)
    assert parse_date("03/04/2026", order=MONTH_FIRST) == date(2026, 3, 4)


def test_an_ambiguous_date_skips_its_row_rather_than_the_whole_file():
    """One unreadable row is one row reported. Refusing the file would throw
    away the ninety-nine rows that were fine."""
    headers = ["Case Number", "Initial Receipt Date", "Preferred Term"]
    rows = [["A-1", "2026-03-04", "Headache"],
            ["A-2", "03/04/2026", "Nausea"],
            ["A-3", "25/03/2026", "Rash"]]
    mapping = {"Case Number": "worldwide_case_id",
               "Initial Receipt Date": "initial_receipt_date",
               "Preferred Term": "meddra_pt"}
    result = read_rows(headers, rows, mapping)
    assert {c.worldwide_case_id for c in result.cases} == {"A-1", "A-3"}
    assert 3 in result.skipped and "order" in result.skipped[3]


def test_the_profile_carries_the_date_order():
    headers = ["Case Number", "Initial Receipt Date", "Preferred Term"]
    rows = [["A-2", "03/04/2026", "Nausea"]]
    mapping = {"Case Number": "worldwide_case_id",
               "Initial Receipt Date": "initial_receipt_date",
               "Preferred Term": "meddra_pt",
               DATE_ORDER_KEY: DAY_FIRST}
    result = read_rows(headers, rows, mapping)
    assert result.skipped == {}
    assert result.cases[0].initial_receipt_date == date(2026, 4, 3)


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-04", date(2026, 3, 4)),
    ("20260304", date(2026, 3, 4)),
    ("04-Mar-2026", date(2026, 3, 4)),
    ("4 March 2026", date(2026, 3, 4)),
    ("04-Mar-26", date(2026, 3, 4)),
    (datetime(2026, 3, 4, 12, 0), date(2026, 3, 4)),
    (date(2026, 3, 4), date(2026, 3, 4)),
    ("", None),
    (None, None),
    ("not a date", None),
    ("2026-13-40", None),
])
def test_the_unambiguous_formats(raw, expected):
    assert parse_date(raw) == expected


# ------------------------------------------------------------------- the flag

@pytest.mark.parametrize("raw,expected", [
    ("Yes", True), ("Y", True), ("1", True), ("Serious", True),
    ("No", False), ("N", False), ("0", False), ("Non-serious", False),
    ("", None), (None, None), ("maybe", None),
])
def test_a_flag_has_three_answers(raw, expected):
    """"The cell did not say" is not "no". A seriousness column that defaults
    to False turns every unfilled cell into a determination that the case was
    not serious."""
    assert parse_flag(raw) is expected


def test_a_criterion_without_a_flag_makes_the_case_serious():
    headers = ["Case Number", "Seriousness Criteria", "Preferred Term"]
    rows = [["A-9", "Hospitalisation; Life threatening", "Headache"]]
    mapping = {"Case Number": "worldwide_case_id",
               "Seriousness Criteria": "seriousness_criteria",
               "Preferred Term": "meddra_pt"}
    case = read_rows(headers, rows, mapping).cases[0]
    assert case.is_serious is True
    assert case.seriousness_criteria == ["hospitalisation", "life_threatening"]


# --------------------------------------------------------------- the guessing

def test_the_suggestion_finds_the_obvious_columns():
    suggestions = {s.column: s.field for s in suggest(HEADERS)}
    assert suggestions["Case Number"] == "worldwide_case_id"
    assert suggestions["Initial Receipt Date"] == "initial_receipt_date"
    assert suggestions["Preferred Term"] == "meddra_pt"
    assert suggestions["Verbatim Term"] == "verbatim_term"
    assert suggestions["Patient Sex"] == "patient_sex"


def test_a_local_case_id_does_not_become_the_worldwide_one():
    """Both headers contain "case", and filing local identifiers as worldwide
    ones would merge two products' cases under one numbering."""
    suggestions = {s.column: s.field
                   for s in suggest(["Local Case ID", "Worldwide Case Number"])}
    assert suggestions["Local Case ID"] == "local_case_id"
    assert suggestions["Worldwide Case Number"] == "worldwide_case_id"


def test_a_field_is_only_suggested_once():
    suggestions = [s.field for s in suggest(["Case ID", "Case Number", "Case No"])
                   if s.field]
    assert len(suggestions) == len(set(suggestions))


def test_an_unrecognised_column_is_left_for_a_person():
    suggestions = {s.column: s.field for s in suggest(["Widget throughput"])}
    assert suggestions["Widget throughput"] is None


def test_a_guess_is_labelled_as_one():
    exact = next(s for s in suggest(["Country"]) if s.column == "Country")
    partial = next(s for s in suggest(["Country of occurrence (coded)"]))
    assert exact.confidence == 1.0
    assert 0 < partial.confidence < 1.0


# ------------------------------------------------------------ the validation

def test_a_mapping_without_a_case_identifier_is_refused():
    problems = validate_mapping({"Preferred Term": "meddra_pt"})
    assert any("cannot be grouped" in p for p in problems)


def test_two_columns_on_one_field_are_refused():
    problems = validate_mapping({"A": "worldwide_case_id", "B": "worldwide_case_id"})
    assert any("same field" in p for p in problems)


def test_an_unknown_field_is_refused():
    problems = validate_mapping({"A": "worldwide_case_id", "B": "patient_shoe_size"})
    assert any("not a field" in p for p in problems)


def test_a_bad_date_order_is_refused():
    problems = validate_mapping({"A": "worldwide_case_id", DATE_ORDER_KEY: "sideways"})
    assert any("day_first" in p for p in problems)


def test_a_good_mapping_has_nothing_wrong_with_it():
    assert validate_mapping(MAPPING) == []
    assert validate_mapping({**MAPPING, DATE_ORDER_KEY: DAY_FIRST}) == []
