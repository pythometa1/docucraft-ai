"""What `plan_export` refuses, and why.

`plan_export` is the whole export gate, and it runs before a single byte is
written -- so what it fails to notice is what ships. Two things it used to miss:

* a data section whose `[TABLE: key]` line was no longer in its text. The key
  was recorded on `SectionRender.table_keys` from the first version of the
  module and then read by nothing at all, so the section exported without its
  table: a specification section containing no specification, and no blocker,
  warning or gap in the prose to notice it by.
* a section marked not applicable that nonetheless carried a model-written
  draft. The approval check was skipped on applicability alone, so unreviewed
  AI prose exported under a heading nobody expected to contain any.
"""

from app.cmc.export import plan_export


def _row(code, title, status, applicability, content, table_keys=(),
         justification_only=False):
    return (code, title, status, applicability, content, list(table_keys),
            justification_only)


def _codes(plan):
    return {b["code"] for b in plan.blockers}


# ------------------------------------------------------- the declared table

def test_a_data_section_missing_its_table_marker_is_a_blocker():
    plan = plan_export([
        _row("P.5.1", "Specification(s)", "approved", "applicable",
             "The specification is given below.", ["spec_table"]),
    ])
    assert "TABLE_MISSING" in _codes(plan)
    message = next(b for b in plan.blockers if b["code"] == "TABLE_MISSING")["message"]
    assert "spec_table" in message


def test_a_data_section_carrying_its_marker_passes():
    plan = plan_export([
        _row("P.5.1", "Specification(s)", "approved", "applicable",
             "The specification is given below.\n\n[TABLE: spec_table]\n",
             ["spec_table"]),
    ])
    assert plan.blockers == []


def test_the_marker_has_to_be_the_declared_one():
    """A model that invented a key satisfies no declaration. Both problems are
    reported: the declared table is absent, and the invented one has to be
    resolved against a builder that does not exist."""
    plan = plan_export([
        _row("P.5.1", "Specification(s)", "approved", "applicable",
             "Below.\n\n[TABLE: spec_dp]\n", ["spec_table"]),
    ])
    assert "TABLE_MISSING" in _codes(plan)


def test_a_marker_indented_or_padded_still_counts():
    plan = plan_export([
        _row("P.5.1", "Specification(s)", "approved", "applicable",
             "Below.\n\n   [TABLE: spec_table]   \n", ["spec_table"]),
    ])
    assert plan.blockers == []


def test_a_section_with_no_declared_table_is_not_asked_for_one():
    plan = plan_export([
        _row("P.2", "Pharmaceutical Development", "approved", "applicable",
             "Development narrative.", []),
    ])
    assert plan.blockers == []


# ------------------------------------------ not applicable, and what is in it

def test_a_justification_only_section_needs_no_approval():
    """The ordinary case: nobody drafted it, so there is nothing to approve."""
    plan = plan_export([
        _row("S.1.3", "General Properties", "not_started", "not_applicable",
             "Not applicable: the drug substance is covered by a DMF.",
             [], justification_only=True),
    ])
    assert plan.blockers == []
    assert [s.section_code for s in plan.sections] == ["S.1.3"]


def test_a_not_applicable_section_carrying_a_draft_still_needs_approval():
    """The defect: `applicability` alone decided the section was exempt, and
    the router hands such a section its DRAFT whenever one is non-empty."""
    plan = plan_export([
        _row("S.1.3", "General Properties", "draft", "not_applicable",
             "The drug substance is a white crystalline powder with...",
             [], justification_only=False),
    ])
    assert "SECTION_NOT_APPROVED" in _codes(plan)


def test_a_referenced_dmf_section_carrying_a_draft_needs_approval_too():
    plan = plan_export([
        _row("S.2.2", "Description of Manufacturing Process", "in_review",
             "referenced_dmf", "The process comprises four stages...",
             [], justification_only=False),
    ])
    assert "SECTION_NOT_APPROVED" in _codes(plan)


def test_an_approved_not_applicable_draft_is_allowed_through():
    plan = plan_export([
        _row("S.1.3", "General Properties", "approved", "not_applicable",
             "Reviewed text explaining the position.", [], justification_only=False),
    ])
    assert plan.blockers == []


# ------------------------------------------------------------ back-compat

def test_the_justification_flag_defaults_to_false_for_a_six_field_row():
    """Older callers pass six fields. They get the safe reading -- a
    not-applicable section with content is treated as drafted, and therefore
    as needing approval -- rather than the exempting one."""
    plan = plan_export([
        ("S.1.3", "General Properties", "draft", "not_applicable",
         "Some content.", []),
    ])
    assert "SECTION_NOT_APPROVED" in _codes(plan)


def test_require_approved_false_still_reports_the_missing_table():
    """An override is about approval, not about a table that is not there."""
    plan = plan_export([
        _row("P.5.1", "Specification(s)", "draft", "applicable",
             "No marker here.", ["spec_table"]),
    ], require_approved=False)
    assert _codes(plan) == {"TABLE_MISSING"}
