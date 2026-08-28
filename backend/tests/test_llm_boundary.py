"""What is allowed to become a prompt.

The live problem: source ingestion flattens each spreadsheet row into
`"employee_id: 44182; full_name: Dana Ruiz; annual_salary: 118400"` and stores it
as a chunk, and the chat and draft paths hand those chunks to the provider
verbatim. Every salary and identifier in a payroll extract has been reaching a
third party in the clear.

§16 allows template context and schema metadata through, and nothing else.
"""

import pytest

from app.llm.boundary import (
    PreparedContext, UnredactedValueError, describe_schema, prepare_context,
)
from app.llm.redaction import (
    BOOLEAN, DATE, EMAIL, FREE_TEXT, IDENTIFIER, MONEY, NUMBER, PERSON_NAME, PHONE,
    SENSITIVE_CLASSES, classify_column, redact_chunk_text, redact_record, redact_value,
)


ROW = {
    "employee_id": "EMP-000417",
    "full_name": "Dana Ruiz",
    "annual_salary": "118400",
    "joining_dt": "14/03/2024",
    "work_email": "dana.ruiz@acme.com",
    "colleague_type": "Full time",
}


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("column,expected", [
    ("annual_salary", MONEY), ("total_compensation", MONEY), ("joining_bonus", MONEY),
    ("joining_dt", DATE), ("effective_date", DATE), ("dob", DATE),
    ("employee_id", IDENTIFIER), ("payroll_no", IDENTIFIER), ("passport_number", IDENTIFIER),
    ("full_name", PERSON_NAME), ("new_manager_name", PERSON_NAME),
    ("work_email", EMAIL), ("mobile", PHONE),
])
def test_columns_are_classified_by_name(column, expected):
    assert classify_column(column).value_class == expected


def test_a_salary_column_that_is_empty_in_the_sample_is_still_a_salary_column():
    """Name before shape, deliberately. Guessing from values alone classifies an
    empty salary column as free text and passes it straight through."""
    assert classify_column("annual_salary", [None, ""]).value_class == MONEY


def test_shape_classifies_a_column_whose_name_says_nothing():
    assert classify_column("col_7", ["dana@acme.com"]).value_class == EMAIL
    assert classify_column("col_8", ["2024-03-14"]).value_class == DATE


def test_the_sensitive_set_is_what_the_doc_worries_about():
    assert {MONEY, IDENTIFIER, PERSON_NAME, EMAIL, PHONE, DATE} == set(SENSITIVE_CLASSES)
    assert FREE_TEXT not in SENSITIVE_CLASSES


# ---------------------------------------------------------------- redaction

def test_redaction_is_deterministic():
    """Or re-compiling a template produces a different prompt, a different
    response and a different manifest, and reproducibility stops being
    checkable."""
    assert redact_record(ROW) == redact_record(ROW)
    assert redact_value("118400", MONEY, column="annual_salary") == \
           redact_value("118400", MONEY, column="annual_salary")


def test_redaction_removes_the_real_value():
    redacted = redact_record(ROW)
    for column, original in ROW.items():
        if column == "colleague_type":
            continue  # free text of business meaning, see below
        assert redacted[column] != original, f"{column} survived redaction"


def test_money_keeps_its_magnitude_but_not_its_value():
    """"Six figures" is the part the compiler needs; the six figures are not."""
    redacted = redact_value("118400", MONEY, column="annual_salary")
    assert redacted != "118400"
    assert len([c for c in str(redacted) if c.isdigit()]) == 6


def test_an_identifier_keeps_its_shape():
    """`EMP-000417` tells the compiler this is a padded employee reference, and
    that stays true of the synthetic one."""
    redacted = redact_value("EMP-000417", IDENTIFIER, column="employee_id")
    assert redacted != "EMP-000417"
    assert len(redacted) == len("EMP-000417")
    assert redacted[3] == "-"
    assert redacted[:3].isalpha() and redacted[4:].isdigit()


def test_a_date_stays_a_date_written_the_same_way_round():
    day_first = redact_value("14/03/2024", DATE, column="joining_dt")
    assert day_first != "14/03/2024"
    day, month, year = day_first.split("/")
    assert len(day) == 2 and len(month) == 2 and len(year) == 4
    assert 1 <= int(day) <= 28 and 1 <= int(month) <= 12

    iso = redact_value("2024-03-14", DATE, column="joining_dt")
    assert iso.count("-") == 2 and len(iso.split("-")[0]) == 4


def test_an_email_stays_an_email():
    redacted = redact_value("dana.ruiz@acme.com", EMAIL, column="work_email")
    assert "@" in redacted and redacted.endswith((".com", ".org", ".net"))
    assert "acme" not in redacted and "ruiz" not in redacted.split("@")[0].split(".")[0]


def test_free_text_loses_its_words():
    """A free-text note is where somebody records a grievance or a medical
    reason. Length is the only shape worth keeping."""
    redacted = redact_value("Left due to a long-term health condition", FREE_TEXT, column="notes")
    assert "health" not in redacted and "condition" not in redacted
    assert "redacted" in redacted


def test_empty_and_missing_pass_through_unchanged():
    """"" is a legitimately blank field and None is a missing one; inventing a
    synthetic value for either would tell the model something untrue."""
    assert redact_value(None, MONEY) is None
    assert redact_value("", PERSON_NAME) == ""


def test_column_names_survive_because_they_are_what_the_model_maps_against():
    assert set(redact_record(ROW)) == set(ROW)


def test_redaction_is_one_way():
    """Non-reversibility is the property, and it comes from the hash rather than
    from collisions: there is no key and no inverse, so the only way back is to
    guess an input and check it -- as hard as guessing the salary outright."""
    real = "118400"
    synthetic = redact_value(real, MONEY, column="annual_salary")

    # The synthetic value is not the input, and is not derivable by any of the
    # obvious reversible transforms (offset, digit permutation, reversal).
    assert synthetic != real
    assert str(synthetic)[::-1] != real
    assert sorted(str(synthetic)) != sorted(real)


def test_the_synthetic_value_reveals_shape_and_nothing_finer():
    """Magnitude is deliberate -- the compiler needs "six figures" to map the
    column. Two salaries of the same magnitude are indistinguishable afterwards
    beyond that, which is the guarantee being offered."""
    a = str(redact_value("118400", MONEY, column="annual_salary"))
    b = str(redact_value("962150", MONEY, column="annual_salary"))
    assert len(a) == len(b) == 6
    assert a != "118400" and b != "962150"


# ------------------------------------------------------- flattened chunk text

def test_a_flattened_source_row_is_redacted_in_place():
    chunk = "employee_id: EMP-000417; full_name: Dana Ruiz; annual_salary: 118400"
    redacted = redact_chunk_text(chunk)

    assert "EMP-000417" not in redacted
    assert "Dana Ruiz" not in redacted
    assert "118400" not in redacted
    # The column names stay: they are the schema, and the schema is the point.
    for column in ("employee_id", "full_name", "annual_salary"):
        assert column in redacted


# ------------------------------------------------------------- the boundary

def test_schema_metadata_describes_shape_without_showing_data():
    schema = describe_schema(["annual_salary", "full_name"], [ROW])
    line = "\n".join(item.as_prompt_line() for item in schema)

    assert "annual_salary" in line and "full_name" in line
    assert "118400" not in line and "Dana Ruiz" not in line


def test_the_boundary_redacts_source_chunks_on_the_way_through():
    prepared = prepare_context(
        org_id="org-a", model="claude-sonnet-5",
        context_chunks=[{"id": "c1", "text": "full_name: Dana Ruiz; annual_salary: 118400"}],
    )
    sent = prepared.context_chunks[0]["text"]
    assert "Dana Ruiz" not in sent and "118400" not in sent
    assert set(prepared.record.redacted_columns) == {"full_name", "annual_salary"}


def test_the_boundary_refuses_rather_than_sending_when_redaction_is_off():
    """Fails closed. The alternative failure is silent and irreversible."""
    with pytest.raises(UnredactedValueError, match="full source rows"):
        prepare_context(
            org_id="org-a", model="claude-sonnet-5", allow_redaction=False,
            context_chunks=[{"id": "c1", "text": "annual_salary: 118400"}],
        )


def test_the_boundary_names_the_offending_columns():
    """A guard that says "something was unsafe" gets disabled; one that names
    annual_salary gets fixed."""
    with pytest.raises(UnredactedValueError) as caught:
        prepare_context(
            org_id="org-a", model="claude-sonnet-5", allow_redaction=False,
            context_chunks=[{"text": "annual_salary: 118400; work_email: d@acme.com"}],
        )
    assert "annual_salary" in str(caught.value)
    assert "work_email" in str(caught.value)


def test_non_sensitive_context_passes_through_untouched():
    """Redaction that eats the business meaning would make the compiler worse at
    its job for no privacy gain."""
    prepared = prepare_context(
        org_id="org-a", model="claude-sonnet-5",
        context_chunks=[{"id": "c1", "text": "colleague_type: Full time"}],
    )
    assert prepared.context_chunks[0]["text"] == "colleague_type: Full time"
    assert prepared.record.redacted_columns == ()


def test_the_boundary_demands_a_tenant():
    """A prompt that cannot be attributed to a tenant cannot be logged, retained
    or deleted under that tenant's policy."""
    with pytest.raises(UnredactedValueError, match="org_id"):
        prepare_context(org_id="", model="claude-sonnet-5")


def test_the_boundary_demands_a_pinned_model():
    """§16 model pinning: a provider changing its model must not silently alter
    an approved compile."""
    with pytest.raises(UnredactedValueError, match="model"):
        prepare_context(org_id="org-a", model="")


def test_the_prompt_record_carries_what_an_audit_needs():
    prepared = prepare_context(
        org_id="org-a", model="claude-opus-5",
        columns=["annual_salary"], sample_rows=[ROW],
        context_chunks=[{"id": "c1", "text": "full_name: Dana Ruiz"}],
    )
    recorded = prepared.record.as_dict()
    assert recorded["org_id"] == "org-a"
    assert recorded["model"] == "claude-opus-5"
    assert recorded["schema_columns"] == ["annual_salary"]
    assert recorded["redacted_columns"] == ["full_name"]
    assert recorded["context_items"] == 1


def test_template_context_is_carried_through_because_it_is_allowed():
    """§16 permits template context. The boundary exists to stop source values,
    not to stop the compiler seeing the template it is compiling."""
    prepared = prepare_context(
        org_id="org-a", model="claude-sonnet-5",
        template_context="You will report to <New Reporting To>.",
    )
    assert "<New Reporting To>" in prepared.template_context
    assert isinstance(prepared, PreparedContext)
