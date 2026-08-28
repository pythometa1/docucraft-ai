"""What the compiler is shown about the tenant's own data, and what it is not.

§10 ends with a constraint rather than an optimisation: "pass only top evidence
to the compiler agent; never pass the entire corpus blindly". Until this existed
the compiler was passed no evidence at all, which satisfies the letter of that
sentence and none of its purpose. A template reading "Date:" compiled to a field
called `letter_date` while every spreadsheet in the organisation called that
column `Date`, and the binder was left to rediscover a pairing the compiler had
been in a position to get right.

These tests cover the two ways that goes wrong once evidence is being passed:
sending nothing when there is something to send, and sending the same column
sixteen times because the index holds a row per upload rather than per column.
"""

import numpy as np
import pytest

from app.compiler.llm_compiler import _evidence_section
from app.expressions.plain_english import annotate_conditions
from app.retrieval.vector import (
    SOURCE_COLUMN_DESCRIPTION,
    HashingEmbedder,
    VectorRecord,
    embed_one,
)


# ------------------------------------------------------------ evidence section

class _Item:
    def __init__(self, text):
        self.text = text


def test_no_evidence_renders_nothing():
    """Not an empty heading. "Columns that already exist:" with nothing under it
    asserts the organisation has no source data, which retrieval never said."""
    assert _evidence_section(None) == ""
    assert _evidence_section([]) == ""


def test_evidence_section_lists_columns_and_stays_advisory():
    section = _evidence_section([_Item("Date (date), for example 09/06/2023")])
    assert "Date (date), for example 09/06/2023" in section
    # The instruction has to permit inventing a name. A template can legitimately
    # need a field no source file has yet, and a model told these are the only
    # allowed ids would force that placeholder onto the nearest wrong column.
    assert "do not force it onto a column that does not mean the same thing" in section
    # The system prompt asks for snake_case ids and the expression language only
    # parses identifiers. Told merely to "use the column's name", gpt-5 emitted
    # `Transfer Type == 'Permanent'` -- a legal-looking rule with a space in the
    # name, which fails to parse and takes all five conditions down with it.
    assert "snake_case" in section


# --------------------------------------------------------------- corpus dedupe

def _row(record_id: str, column: str, text: str) -> VectorRecord:
    embedder = HashingEmbedder()
    return VectorRecord(
        record_id=record_id, org_id="org-1", kind=SOURCE_COLUMN_DESCRIPTION, text=text,
        vector=embed_one(embedder, text), provider=embedder.name,
        metadata={"column": column},
    )


class _FakeStore:
    """Only the two methods `_compile_evidence` reaches for."""

    def __init__(self, rows):
        self._rows = rows

    def scoped_rows(self, *, org_id, doc_type=None, kind=None):
        return [r for r in self._rows if r.org_id == org_id and (kind is None or r.kind == kind)]


def test_evidence_is_one_entry_per_column(monkeypatch):
    """`index_source_columns` keys on `{source_version_id}:{column}`, so a column
    gains a row on every re-upload. Ranked undeduplicated they tie with
    themselves and fill the list -- on the tenant this was measured against, the
    top 16 were three distinct columns repeated. The columns this exists to
    reveal never appeared."""
    from app.routers import manifests

    rows = [
        _row(f"v{v}:Date", "Date", "Date (date), for example 09/06/2023")
        for v in range(7)
    ] + [
        _row(f"v{v}:Transfer Type", "Transfer Type", "Transfer Type (free_text), for example Permanent")
        for v in range(7)
    ]
    monkeypatch.setattr(manifests, "SqlVectorStore", lambda db: _FakeStore(rows))

    evidence = manifests._compile_evidence(None, "org-1", ["Date:", "Transfer Type:"])
    texts = [item.text for item in evidence]
    assert len(texts) == len(set(texts)), f"duplicate columns reached the prompt: {texts}"
    assert any(t.startswith("Date (date)") for t in texts)
    assert any(t.startswith("Transfer Type") for t in texts)


def test_evidence_is_empty_when_nothing_is_indexed(monkeypatch):
    from app.routers import manifests
    monkeypatch.setattr(manifests, "SqlVectorStore", lambda db: _FakeStore([]))
    assert manifests._compile_evidence(None, "org-1", ["Date:"]) == []


def test_evidence_failure_does_not_block_a_compile(monkeypatch):
    """Advisory context, not a precondition. A store that is briefly unreachable
    must leave the tenant able to compile the way they did before this existed."""
    from app.routers import manifests

    def _explode(db):
        raise RuntimeError("vector store unreachable")

    monkeypatch.setattr(manifests, "SqlVectorStore", _explode)
    assert manifests._compile_evidence(None, "org-1", ["Date:"]) == []


# -------------------------------------------------------- §7 approval sentences

def test_conditions_are_annotated_with_their_meaning():
    [item] = annotate_conditions([{"id": "c1", "expression": "transfer_type == 'Permanent'"}])
    assert item["plain_english"] == "the transfer type is Permanent"
    # §7's undecidable case has to be named, not implied.
    assert "blocked rather than" in item["approval_sentence"]
    assert item["plain_english_error"] is None
    assert item["expression"] == "transfer_type == 'Permanent'", "the executable form must survive"


def test_an_unrenderable_condition_is_shown_as_broken_not_dropped():
    """An approval screen that omits the one condition it could not explain
    invites a sign-off on a manifest the reviewer never saw in full."""
    annotated = annotate_conditions([
        {"id": "ok", "expression": "a == 'x'"},
        {"id": "bad", "expression": "if the colleague is happy then keep this"},
    ])
    assert len(annotated) == 2
    broken = annotated[1]
    assert broken["plain_english"] is None
    assert broken["plain_english_error"]
    assert annotated[0]["plain_english"] == "the a is x"


def test_annotate_conditions_tolerates_an_empty_manifest():
    assert annotate_conditions(None) == []
    assert annotate_conditions([]) == []


# ------------------------------------- prose conditions must not crash the compile

def _docx(paragraphs) -> str:
    import io
    import os
    import tempfile

    import docx

    d = docx.Document()
    for text in paragraphs:
        d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    path = os.path.join(tempfile.mkdtemp(), "t.docx")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def test_a_prose_condition_is_recorded_rather_than_crashing_the_compile():
    """A model asked for an executable rule sometimes answers in English.

    `_expression_is_executable` already refuses those, and the refusal is right:
    a condition nothing can evaluate must not reach a manifest. But the note
    explaining the drop appended to `notes` before `notes` existed, so the whole
    compile died on `UnboundLocalError` instead -- turning a handled case into a
    422 for the tenant, and only on the templates whose instructions were
    written loosely enough to provoke it.
    """
    from app.compiler import llm_compiler
    from app.compiler.llm_compiler import compile_manifest_llm

    class _Result:
        model = "test"
        error = None
        input_tokens = output_tokens = 0
        data = {
            "fields": [],
            "conditions": [{
                "id": "keep_if_happy",
                "expression": "if the colleague is happy then keep this section",
                "effect": "keep",
                "start_paragraph": 1, "end_paragraph": 1,
                "compiled_from": "For happy colleagues:",
            }],
            "scaffolding_paragraphs": [], "language": "en", "notes": [],
        }

    class _Provider:
        def structured(self, **kw):
            return _Result()

        def generate(self, **kw):
            raise AssertionError("not used")

    paragraphs = ["Heading", "Some conditional text"]
    scan_path = _docx(paragraphs)
    from app.templates.parsers.docx_prescan import prescan

    original = llm_compiler.get_llm_provider
    llm_compiler.get_llm_provider = lambda *a, **k: _Provider()
    try:
        manifest = compile_manifest_llm(prescan(scan_path), paragraphs)
    finally:
        llm_compiler.get_llm_provider = original

    assert manifest.conditions == [], "prose is not an executable rule and must not reach the manifest"
    assert any("prose rather than an" in note for note in manifest.notes), (
        f"the drop has to be reported to the reviewer; notes were {manifest.notes}"
    )


# ------------------------------------- the round trip must not store the sentences

def test_a_reviewer_round_trip_does_not_store_the_rendered_sentences():
    """GET annotates, PATCH stores. Without stripping, the screen's own save
    writes the display text into the contract that gets hashed -- and a stored
    description is a false record of the approval the moment the expression
    beside it is edited."""
    from app.expressions.plain_english import strip_derived

    annotated = annotate_conditions([{"id": "c1", "expression": "a == 'x'", "keeps_blocks": ["b1"]}])
    assert "plain_english" in annotated[0], "precondition: the read path added the keys"

    [stored] = strip_derived(annotated)
    assert stored == {"id": "c1", "expression": "a == 'x'", "keeps_blocks": ["b1"]}


def test_stripping_leaves_a_condition_that_was_never_annotated_alone():
    from app.expressions.plain_english import strip_derived
    original = [{"id": "c1", "expression": "a == 'x'"}]
    assert strip_derived(original) == original
