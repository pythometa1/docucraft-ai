"""The registry, the section trees, and the role ranking.

Small functions, and the reason to pin them now rather than when M2 first calls
them is that each one answers a question whose wrong answer is invisible:
which birth date a report counts from, which sources a section may cite, and
whether somebody may confirm a regulatory determination.
"""

import pytest

from app.safety import registry, roles, trees


# ------------------------------------------------------------- the registry

def test_every_report_type_has_a_tree_and_every_tree_has_a_type():
    """The two halves are edited in different files. A key in one and not the
    other is a report type offered in the picker that cannot be created, or a
    tree nothing can reach."""
    assert set(registry.DELIVERABLES) == set(trees.TREES)
    assert set(trees.TABLE_KEYS) == set(trees.TREES)
    assert set(trees.SOURCE_TYPES) == set(trees.TREES)
    assert set(trees.GUIDANCE) == set(trees.TREES)


def test_an_unknown_report_type_raises_rather_than_returning_a_default():
    with pytest.raises(KeyError):
        registry.deliverable("psur_2")
    with pytest.raises(KeyError):
        registry.cumulative_anchor("psur_2")
    with pytest.raises(KeyError):
        trees.seed_sections("psur_2")


def test_the_development_report_counts_from_the_development_birth_date():
    assert registry.cumulative_anchor("dsur") == registry.DIBD
    assert registry.cumulative_anchor("pbrer") == registry.IBD


def test_only_the_recurring_reports_are_periodic():
    assert registry.is_periodic("pbrer") is True
    assert registry.is_periodic("dsur") is True
    # An RMP has a version, not an interval; a signal evaluation is written
    # when a signal appears.
    assert registry.is_periodic("rmp") is False
    assert registry.is_periodic("signal_eval") is False


def test_the_checklist_follows_the_report_types_chosen():
    """A product doing only literature monitoring is not told it is missing a
    trial registry."""
    only_literature = registry.requirements(["lit_review"])
    assert only_literature["required"] == ["literature"]
    assert "study_registry" not in only_literature["recommended"]

    with_dsur = registry.requirements(["lit_review", "dsur"])
    assert "study_registry" in with_dsur["required"]
    assert "literature" in with_dsur["required"]
    # Nothing appears in both lists: a required source is not also a suggestion.
    assert not set(with_dsur["required"]) & set(with_dsur["recommended"])
    assert with_dsur["labels"]["previous_report"].startswith("Previous")


def test_an_unknown_key_in_the_checklist_is_skipped_not_fatal():
    """The checklist is computed from what a product selected, and a stale
    selection must not break the upload screen."""
    assert registry.requirements(["nonsense"])["required"] == []
    assert registry.requirements(["nonsense", "dsur"])["required"]


def test_a_section_may_always_reach_the_previous_report():
    """A periodic report is written against its predecessor everywhere, not
    only in the sections that quote it."""
    for key in registry.DELIVERABLES:
        for code, _title in trees.TREES[key]:
            sources = registry.source_types_for(key, code)
            assert "previous_report" in sources, (key, code)
            assert "rsi_doc" in sources, (key, code)


def test_a_section_reaches_the_sources_its_subject_needs():
    assert "nonclinical" in registry.source_types_for("dsur", "12")
    assert "literature" in registry.source_types_for("dsur", "13")
    assert "study_registry" in registry.source_types_for("dsur", "5")
    assert "epi_data" in registry.source_types_for("rmp", "II.SI")
    # And does not reach ones it does not: a non-clinical section that could
    # cite a sales report is a section that will.
    assert "exposure_data" not in registry.source_types_for("dsur", "12")


def test_the_catalogue_is_what_the_picker_shows():
    entries = {e["key"]: e for e in registry.catalogue()}
    assert set(entries) == set(registry.DELIVERABLES)
    assert entries["pbrer"]["section_count"] == len(trees.PBRER)
    assert entries["dsur"]["cumulative_anchor"] == "dibd"
    assert entries["rmp"]["periodic"] is False


# ---------------------------------------------------------------- the trees

def test_depth_comes_from_the_code():
    sections = {s["section_code"]: s for s in trees.seed_sections("pbrer")}
    assert sections["16"]["level"] == 1
    assert sections["16.2"]["level"] == 2
    rmp = {s["section_code"]: s for s in trees.seed_sections("rmp")}
    assert rmp["II"]["level"] == 1
    assert rmp["II.SVIII"]["level"] == 2


def test_a_section_with_children_is_a_container_and_carries_no_table():
    sections = {s["section_code"]: s for s in trees.seed_sections("pbrer")}
    assert sections["5"]["is_container"] is True
    assert sections["5"]["table_key"] is None
    assert sections["5.1"]["is_container"] is False
    assert sections["5.1"]["table_key"] == "exposure_table"
    # A leaf with no children is not a container even at the top level.
    assert sections["11"]["is_container"] is False


def test_a_container_that_declares_a_table_still_does_not_get_one():
    """The marker would render above the subsections it summarises. `16.1`
    holds the safety concern table; `16`, which contains it, must not."""
    sections = {s["section_code"]: s for s in trees.seed_sections("pbrer")}
    assert sections["16"]["table_key"] is None
    assert sections["16.1"]["table_key"] == "safety_concern_table"


def test_table_key_for_reads_the_same_map_the_seed_does():
    assert trees.table_key_for("dsur", "7.3") == "summary_tab_soc_pt"
    assert trees.table_key_for("dsur", "1") is None
    assert trees.table_key_for("nonsense", "1") is None


def test_guidance_defaults_to_the_title_and_is_never_empty():
    for key in trees.TREES:
        for section in trees.seed_sections(key):
            assert (section["guidance_text"] or "").strip(), (key, section)


def test_the_sections_that_must_not_conclude_say_so():
    """§8's fifth rule -- do not conclude a benefit-risk change without a
    sourced conclusion -- is stated at the point of use, not only once in the
    system prompt where it competes with ten other sentences."""
    sections = {s["section_code"]: s for s in trees.seed_sections("pbrer")}
    assert "ASSESSMENT REQUIRED" in sections["18.2"]["guidance_text"]
    assert "ASSESSMENT REQUIRED" in sections["19"]["guidance_text"]
    dsur = {s["section_code"]: s for s in trees.seed_sections("dsur")}
    assert "ASSESSMENT REQUIRED" in dsur["18.2"]["guidance_text"]


def test_the_interval_and_cumulative_rule_is_stated_where_both_appear():
    dsur = {s["section_code"]: s for s in trees.seed_sections("dsur")}
    assert "cumulative" in dsur["7.3"]["guidance_text"].lower()
    assert "never be merged" in dsur["7.3"]["guidance_text"]


def test_a_disproportionality_section_carries_the_screening_disclaimer():
    """§9: a screening statistic indicates reporting frequency, not causality.
    The section that presents it says so."""
    sections = {s["section_code"]: s for s in trees.seed_sections("signal_eval")}
    guidance = sections["5"]["guidance_text"]
    assert "not causality" in guidance and "screening statistic" in guidance


def test_section_codes_are_unique_within_a_report():
    for key in trees.TREES:
        codes = [c for c, _t in trees.TREES[key]]
        assert len(codes) == len(set(codes)), key


# ---------------------------------------------------------------- the roles

def test_the_roles_nest_weakest_first():
    assert registry.PV_ROLES == ("writer", "reviewer", "qualified_person")
    assert roles._rank("qualified_person") > roles._rank("reviewer")
    assert roles._rank("reviewer") > roles._rank("writer")


def test_no_role_ranks_below_every_role():
    """Somebody with no membership on a product holds nothing, and `None` must
    not accidentally sort above `writer`."""
    assert roles._rank(None) == -1
    assert roles._rank("") == -1
    assert roles._rank("qppv") == -1, "an unrecognised role is no role"
    assert roles._rank(None) < roles._rank("writer")


def test_every_role_has_a_label_saying_what_it_is_for():
    for key in registry.PV_ROLES:
        assert key in roles.ROLE_LABELS
        assert roles.ROLE_LABELS[key].strip()
    assert "confirms expectedness" in roles.ROLE_LABELS[roles.QUALIFIED_PERSON]


def test_granting_an_unknown_role_is_refused(app_client, two_orgs):
    from fastapi import HTTPException

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as raised:
            roles.grant(db, pv_product_id="p", org_id="o", user_id="u",
                        pv_role="qppv", granted_by="u")
        assert raised.value.detail["error"]["code"] == "PV_BAD_ROLE"
    finally:
        db.close()
