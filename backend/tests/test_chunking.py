"""Splitting a template across model calls, without losing what spans the cut.

The path this replaces truncated: `"\n".join(paragraphs)[:60000]`, with nothing
in the result saying so. These assert the two properties that make splitting
safe -- global indices survive, and a construct that straddles a boundary is
still seen whole by someone -- plus the termination guard, because a chunker that
loops is worse than one that truncates.
"""

from __future__ import annotations

import pytest

from app.compiler.chunking import Chunk, build_outline, chunk_paragraphs


def _paras(n: int, size: int = 100) -> list[str]:
    return [f"paragraph {i} " + "x" * size for i in range(n)]


def test_every_paragraph_is_covered_and_keeps_its_document_index():
    """A renumbered chunk produces a manifest that addresses the wrong lines,
    and looks entirely plausible doing it."""
    paras = _paras(200)
    chunks = chunk_paragraphs(paras, budget_chars=2000, overlap_paragraphs=5)

    covered = set()
    for chunk in chunks:
        for index, text in chunk.paragraphs:
            assert text == paras[index], f"chunk {chunk.number} mislabelled paragraph {index}"
            covered.add(index)
    assert covered == set(range(200))


def test_consecutive_chunks_overlap():
    """A conditional block is an instruction plus the clause it governs. Split
    between them and the clause compiles as static text."""
    chunks = chunk_paragraphs(_paras(200), budget_chars=2000, overlap_paragraphs=5)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert len(chunk.overlap_indices) >= 5


def test_an_instruction_and_its_clause_are_seen_together_by_someone():
    """The property overlap exists for, asserted directly rather than by
    counting the overlap."""
    paras = _paras(60)
    paras[29] = "USE IF ON TEMPORARY ASSIGNMENT"
    paras[30] = "You are on a temporary assignment until further notice."
    chunks = chunk_paragraphs(paras, budget_chars=1400, overlap_paragraphs=6)

    assert any(
        {29, 30} <= {i for i, _t in chunk.paragraphs}
        for chunk in chunks
    ), "no single chunk saw both the instruction and the clause it governs"


@pytest.mark.parametrize("budget", [1, 10, 50])
def test_a_hostile_budget_still_terminates_and_covers_everything(budget):
    """Overlap is capped at half a window; without that cap a budget smaller
    than the overlap re-seeds the next window with the whole of the last one and
    the loop never advances."""
    paras = _paras(20)
    chunks = chunk_paragraphs(paras, budget_chars=budget, overlap_paragraphs=10)
    assert {i for c in chunks for i, _t in c.paragraphs} == set(range(20))
    signatures = [tuple(i for i, _t in c.paragraphs) for c in chunks]
    assert len(set(signatures)) == len(signatures), "a window repeated; this would not terminate"


def test_a_paragraph_larger_than_the_budget_is_not_split():
    """`_locate` matches the model's `match_text` against a whole span. Half a
    sentence matches nothing, and the field is dropped with no fault raised."""
    paras = ["x" * 5000, "short"]
    chunks = chunk_paragraphs(paras, budget_chars=100, overlap_paragraphs=2)
    assert any(text == paras[0] for c in chunks for _i, text in c.paragraphs)


def test_a_small_template_renders_exactly_as_the_single_call_path_did():
    """Chunking must be invisible to templates that never needed it."""
    paras = ["Dear <Name>,", "USE IF PART TIME", "You work part time."]
    chunks = chunk_paragraphs(paras, budget_chars=40000, overlap_paragraphs=8)

    assert len(chunks) == 1 and chunks[0].total == 1
    legacy = "\n".join(f"[{i}] {t}" for i, t in enumerate(paras) if t.strip())
    assert chunks[0].render("") == legacy


def test_a_long_template_is_not_truncated():
    """The bug this module exists for: 60,000 characters in and the rest gone."""
    paras = ["word " * 200 for _ in range(400)]
    assert sum(len(p) for p in paras) > 60000

    chunks = chunk_paragraphs(paras, budget_chars=40000, overlap_paragraphs=8)
    covered = {i for c in chunks for i, _t in c.paragraphs}
    assert 399 in covered and covered == set(range(400))


def test_a_multi_part_chunk_says_the_indices_are_the_documents():
    rendered = chunk_paragraphs(_paras(200), budget_chars=2000, overlap_paragraphs=5)[1].render("")
    assert "whole document's" in rendered
    assert "part 2 of" in rendered


def test_the_outline_carries_instructions_from_the_whole_template():
    """Each chunk needs every instruction, not only its own: alternatives share
    one condition field, and the sibling may be forty paragraphs away."""
    paras = _paras(60)
    paras[5] = "USE FOR NEW HIRE"
    paras[45] = "USE FOR CURRENT COLLEAGUES"
    outline = build_outline(paras)

    assert "USE FOR NEW HIRE" in outline
    assert "USE FOR CURRENT COLLEAGUES" in outline
    assert "paragraph 12" not in outline, "ordinary prose does not belong in the outline"


def test_the_outline_is_empty_when_there_is_nothing_instructional():
    assert build_outline(["Dear Ada,", "Yours sincerely,"]) == ""


def test_no_paragraphs_still_yields_one_chunk():
    assert chunk_paragraphs([], budget_chars=100, overlap_paragraphs=2) == [Chunk(number=1, total=1)]
