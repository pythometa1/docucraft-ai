"""The section structures for the non-CTD deliverables.

Seed data has no runtime that would catch a mistake in it: a tree with a
duplicated code, a heading that quietly holds guidance, or a table pointed at
a builder nobody wrote seeds an entire dossier wrong and is noticed by a
reviewer, months later, in an exported document. What follows pins the
invariants the drafting and rendering code assumes without checking.
"""

import pytest

from app.cmc import registry
from app.cmc.deliverable_trees import SOURCE_MAPS, TABLE_SECTIONS, TREES

#: The builders the table renderer actually ships. A section naming anything
#: else renders nothing where a specification belongs.
BUILDERS = frozenset({
    "spec_table", "batch_analyses", "stability_summary", "stability_matrix",
    "batch_formula", "site_list", "impurity_table", "composition_table",
})


def _codes(tree) -> list:
    return [section[0] for section in tree]


def _order_key(code: str) -> tuple:
    """A dotted code as the tuple that sorts it as a human reads it.

    Compared as strings, "10" precedes "2" and an APQR would file the recall
    review before the batch review.
    """
    return tuple(int(part) for part in code.split("."))


def test_the_four_unbuilt_deliverables_are_the_ones_seeded():
    """The registry is the single list of what ships; a tree for a key it does
    not carry could never be selected, and a key left out stays unbuilt."""
    assert set(TREES) == {"apqr", "method_val", "process_val", "stability_report"}
    assert set(TABLE_SECTIONS) == set(TREES)
    assert set(SOURCE_MAPS) == set(TREES)
    for key in TREES:
        assert key in registry.DELIVERABLES, key


def test_every_tree_numbers_each_section_exactly_once():
    """A repeated code gives two sections one identity, and the second draft
    written into it overwrites the first."""
    for key, tree in TREES.items():
        codes = _codes(tree)
        assert len(codes) == len(set(codes)), (key, codes)


def test_a_container_is_exactly_a_section_with_no_guidance():
    """A container is a heading whose prose lives in its children, so guidance
    on one is guidance no drafting prompt will ever receive; a leaf without
    guidance leaves the prompt nothing to say about the section."""
    for key, tree in TREES.items():
        for code, title, guidance, container in tree:
            assert title, (key, code)
            assert bool(guidance) is not container, (key, code)


def test_every_subsection_sits_under_a_container_that_precedes_it():
    """4.1 without a 4 is an orphan heading, and a 4 that is a leaf would be
    drafted as prose and then have its own children printed underneath it."""
    for key, tree in TREES.items():
        containers = {code for code, _t, _g, container in tree if container}
        seen: set = set()
        for code, _title, _guidance, _container in tree:
            parent = code.rsplit(".", 1)[0]
            if parent != code:
                assert parent in containers, (key, code)
                assert parent in seen, (key, code)
            seen.add(code)


def test_every_container_has_children():
    """A container with no children is a heading with no content at all: it
    holds no guidance by the rule above and nothing follows it."""
    for key, tree in TREES.items():
        codes = _codes(tree)
        for code, _title, _guidance, container in tree:
            if container:
                assert any(c.startswith(code + ".") for c in codes), (key, code)


def test_each_tree_is_in_document_order_and_long_enough_to_be_a_report():
    """Sections seed with their position as sort_order, so the tuple order is
    the order of the exported document."""
    for key, tree in TREES.items():
        codes = _codes(tree)
        assert len(codes) >= 8, (key, len(codes))
        keys = [_order_key(code) for code in codes]
        assert keys == sorted(keys), key
        assert len(set(keys)) == len(keys), key


def test_the_apqr_is_numbered_as_plain_integers():
    """EU GMP Chapter 1 gives the annual review no numbering, so a dotted code
    invented here would assert a CTD location the document does not have."""
    assert _codes(TREES["apqr"]) == [str(n) for n in range(1, 15)]
    titles = [section[1] for section in TREES["apqr"]]
    assert titles[0].startswith("Scope")
    assert titles[-1].startswith("Conclusions")


def test_the_validation_parameters_are_a_container_with_a_child_per_parameter():
    """ICH Q2 names the characteristics individually, and each is separately
    applicable: an assay validates linearity, an identity test does not."""
    tree = TREES["method_val"]
    by_code = {code: (title, guidance, container) for code, title, guidance, container in tree}
    assert by_code["4"][2] is True
    children = {code for code in by_code if code.startswith("4.")}
    assert {"4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8"} <= children
    # Repeatability and intermediate precision are separate answers, so
    # precision is itself a container rather than one paragraph covering both.
    assert by_code["4.4"][2] is True
    assert {"4.4.1", "4.4.2"} <= children


def test_every_table_section_names_a_real_section_and_a_real_builder():
    """An unknown builder key resolves to nothing at render time, which loses
    the table without losing the section that was supposed to carry it."""
    for key, tables in TABLE_SECTIONS.items():
        tree = TREES[key]
        leaves = {code for code, _t, _g, container in tree if not container}
        for code, builder in tables.items():
            assert code in leaves, (key, code)
            assert builder in BUILDERS, (key, code, builder)


def test_the_builder_names_are_the_ones_the_renderer_actually_ships():
    """`BUILDERS` above is a copy of another module's truth, and a copy drifts.
    A name only this file believes in passes every test here and renders
    nothing where a specification belongs."""
    tables = pytest.importorskip("app.cmc.tables")
    assert BUILDERS == frozenset(tables.BUILDERS)


def test_the_number_bearing_sections_carry_the_builder_that_matches_them():
    """The point of the mapping: a section whose content is measured data must
    render from the store rather than from anything a model writes."""
    assert TABLE_SECTIONS["stability_report"]["4"] == "stability_summary"
    assert TABLE_SECTIONS["stability_report"]["3"] == "spec_table"
    assert TABLE_SECTIONS["process_val"]["2.2"] == "batch_formula"
    assert TABLE_SECTIONS["process_val"]["6"] == "batch_analyses"
    assert TABLE_SECTIONS["apqr"]["9"] == "stability_summary"


def test_no_section_reads_a_style_reference_or_an_unknown_doc_type():
    """A previously approved dossier is retrievable for style and citable as
    fact for nothing; a doc type outside the registry filters retrieval down to
    an empty set and the section silently drafts on no evidence."""
    citable = set(registry.DOC_TYPES) - {registry.STYLE_REFERENCE_TYPE}
    for key, source_map in SOURCE_MAPS.items():
        for section, types in source_map.items():
            assert types, (key, section)
            assert set(types) <= citable, (key, section, types)
            assert len(set(types)) == len(types), (key, section, types)


def test_every_source_map_key_names_a_section_or_a_parent_of_one():
    """Longest-prefix-wins never errors on a key that matches nothing, so a
    typo here reads as a section that simply chose no filter."""
    for key, source_map in SOURCE_MAPS.items():
        codes = set(_codes(TREES[key]))
        for section in source_map:
            assert section in codes or any(
                code.startswith(section + ".") for code in codes), (key, section)


def test_guidance_reaches_a_prompt_and_a_document_as_plain_ascii():
    """The guidance is handed to the drafting prompt verbatim and copied into
    headings, and a smart quote or an en dash arriving from a spec document is
    how an export acquires a character its style has no glyph for."""
    for key, tree in TREES.items():
        for code, title, guidance, _container in tree:
            for text in (title, guidance):
                assert text.isascii(), (key, code, text)
                assert "--" not in text.replace(" -- ", " "), (key, code, text)
