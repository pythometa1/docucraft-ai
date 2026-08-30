"""The client's employment-contract family — both markup conventions, both switches.

Four fixtures, two documents in two dialects each:

* `icc_ct036_template.docx` / `icc_ct040_template.docx` — rewritten for an
  assembly tool, marked up with **font colour** (red instruction, blue value).
* `icc_ct036_original.docx` / `icc_ct040_original.docx` — the client's own
  masters, marked up with **Word's highlighter** (green instruction, yellow
  value) and a terser instruction dialect (`For Full time colleagues:`).

The originals used to compile to nothing at all: the pre-scanner read `w:color`
only, so every run classified as static text — 0 fields, 0 conditions from a
document carrying 25 of one and 5 of the other. Every template therefore needed
a hand rewrite before onboarding, which at estate scale is the whole cost.
`test_original_and_icc_compile_alike` is the check that keeps that unnecessary.

The switch these files exist to prove is *inline*: two branches inside one
paragraph, with static text on both sides. Paragraph-ranged blocks cannot say
"keep this run, drop that one", and the fallback — deleting the paragraph —
removed `Please return one copy to your Manager` and `To signify your
acceptance` from a real client letter while `qa_passed` was True. That is what
the canaries in `fixtures/canaries/` assert: the sentence that must be *there*.
"""

from __future__ import annotations

import dataclasses
import json
import re

import docx
import pytest

from app.generation.source_resolver import apply_binding, suggest_bindings
from app.templates.parsers.docx_prescan import W_NS, prescan
from app.generation.docx_renderer import fill_template
from app.generation.source_ingestion import extract_records
from app.compiler.rule_compiler import compile_manifest

from golden import docx_xml_parts

W = f"{{{W_NS}}}"

# Layout is claimed to be preserved "by construction" because the engine mutates
# a copy of the template and only ever edits text. These are the parts that
# would have to change for alignment, colour, font size or list numbering to
# move, so comparing them to the template is the claim made checkable.
LAYOUT_PARTS = ("word/styles.xml", "word/numbering.xml", "word/theme/theme1.xml")

FAMILIES = {
    "ct036": {"canaries": "icc_ct036", "record": "icc_ct036.xlsx",
              "fields": 25, "conditions": 5, "blocks": 11, "inline": 4},
    "ct040": {"canaries": None, "record": "icc_ct040.xlsx",
              "fields": 24, "conditions": 4, "blocks": 10, "inline": 4},
}
DIALECTS = ("template", "original")


def _template(fixtures_dir, family: str, dialect: str):
    return fixtures_dir / "templates" / f"icc_{family}_{dialect}.docx"


def _compile(path):
    return dataclasses.asdict(compile_manifest(prescan(str(path))))


def _record(fixtures_dir, family: str, manifest: dict, **overrides):
    columns, records = extract_records(str(fixtures_dir / "records" / FAMILIES[family]["record"]), "xlsx")
    bindings = suggest_bindings(manifest, columns).as_field_bindings()
    return {**apply_binding(records[0], bindings), **overrides}


def _text(path) -> str:
    """Whitespace-normalised, because a canary is a sentence and a sentence
    survives run boundaries that put arbitrary spacing between its words."""
    d = docx.Document(str(path))
    joined = "\n".join(
        "".join(t.text or "" for t in p.iter(f"{W}t"))
        for p in d.element.body.iter(f"{W}p")
    )
    return re.sub(r"\s+", " ", joined)


pytestmark = pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).resolve().parent
         / "fixtures" / "templates" / "icc_ct036_template.docx").exists(),
    reason="ICC template fixtures are missing from backend/tests/fixtures/",
)


# ------------------------------------------------------------------ compiling
@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_dialect_compiles(fixtures_dir, family, dialect):
    expected = FAMILIES[family]
    manifest = _compile(_template(fixtures_dir, family, dialect))
    assert len(manifest["fields"]) == expected["fields"]
    assert len(manifest["conditions"]) == expected["conditions"]
    assert len(manifest["blocks"]) == expected["blocks"]
    inline = [b for b in manifest["blocks"] if b.get("start_span") is not None]
    assert len(inline) == expected["inline"]


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_the_original_is_recognised_as_highlight_markup(fixtures_dir, family):
    assert prescan(str(_template(fixtures_dir, family, "original"))).uses_highlight_markup
    assert not prescan(str(_template(fixtures_dir, family, "template"))).uses_highlight_markup


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_original_and_icc_compile_alike(fixtures_dir, family):
    """The hand rewrite from green/yellow to red/blue changes no meaning.

    If this holds, a client can send the master they already maintain and the
    rewrite step disappears — which is the difference between onboarding an
    estate and hand-editing it.
    """
    original = _compile(_template(fixtures_dir, family, "original"))
    icc = _compile(_template(fixtures_dir, family, "template"))

    assert {f["id"] for f in original["fields"]} == {f["id"] for f in icc["fields"]}
    # Case only: the two authors capitalise "Fixed Term" / "Fixed term"
    # differently, and condition matching casefolds.
    assert ({c["expression"].lower() for c in original["conditions"]}
            == {c["expression"].lower() for c in icc["conditions"]})
    assert (sum(1 for b in original["blocks"] if b.get("start_span") is not None)
            == sum(1 for b in icc["blocks"] if b.get("start_span") is not None))


# ----------------------------------------------------------------- generating
@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_letters_pass_qa(fixtures_dir, tmp_path, family, dialect):
    template = _template(fixtures_dir, family, dialect)
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    result = fill_template(str(template), str(out), manifest, _record(fixtures_dir, family, manifest))
    assert result.qa_passed, result.qa_notes


@pytest.mark.parametrize("dialect", DIALECTS)
def test_canaries_hold_for_the_with_recruitment_branch(fixtures_dir, tmp_path, dialect):
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    fill_template(str(template), str(out), manifest, _record(fixtures_dir, "ct036", manifest))
    text = _text(out)

    canaries = json.loads((fixtures_dir / "canaries" / "icc_ct036.json").read_text())["with_recruitment"]
    missing = [s for s in canaries["must_contain"] if s not in text]
    leaked = [s for s in canaries["must_not_contain"] if s in text]
    assert not missing, f"content lost from the letter: {missing}"
    assert not leaked, f"scaffolding leaked into the letter: {leaked}"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_switch_flips_with_the_source_value(fixtures_dir, tmp_path, dialect):
    """The two client PDFs differ in exactly one word, and this is it."""
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    canaries = json.loads((fixtures_dir / "canaries" / "icc_ct036.json").read_text())

    for branch, transaction_type in (("with_recruitment", "With Recruitment"),
                                     ("without_recruitment", "Without Recruitment")):
        out = tmp_path / f"{branch}.docx"
        record = _record(fixtures_dir, "ct036", manifest, transaction_type=transaction_type)
        result = fill_template(str(template), str(out), manifest, record)
        assert result.qa_passed, result.qa_notes
        text = _text(out)
        assert not [s for s in canaries[branch]["must_contain"] if s not in text]
        assert not [s for s in canaries[branch]["must_not_contain"] if s in text]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_static_text_around_the_switch_survives(fixtures_dir, tmp_path, dialect):
    """The sentence is whole, not a dangling tail.

    Before span-scoped blocks the letter kept `within seven (7) days` and lost
    the clause it belonged to, which reads as a typo rather than as a bug and
    passed every gate.
    """
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    fill_template(str(template), str(out), manifest, _record(fixtures_dir, "ct036", manifest))
    text = _text(out)

    assert "Please return one copy to your Manager and APAC Recruitment Operations" in text
    assert "within seven (7) days" in text
    assert text.index("Please return one copy") < text.index("within seven (7) days")


# ------------------------------------------------------------------- fidelity
@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("dialect", DIALECTS)
def test_layout_parts_are_byte_identical_to_the_template(fixtures_dir, tmp_path, family, dialect):
    template = _template(fixtures_dir, family, dialect)
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    fill_template(str(template), str(out), manifest, _record(fixtures_dir, family, manifest))

    before, after = docx_xml_parts(template), docx_xml_parts(out)
    for part in LAYOUT_PARTS:
        if part in before:
            assert before[part] == after[part], f"{part} changed -- layout is not preserved"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_fuse_hyperlink_survives_with_its_target(fixtures_dir, tmp_path, dialect):
    """Text-only editing keeps a hyperlink; regenerating the document loses it.

    This one is a live Pfizer service-desk URL sitting inside otherwise static
    prose, so it is the sharpest available check that the engine edited the
    original rather than rebuilding it.
    """
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    fill_template(str(template), str(out), manifest, _record(fixtures_dir, "ct036", manifest))

    links = prescan(str(out)).hyperlinks
    fuse = [h for h in links if h.text.strip() == "FUSE"]
    assert fuse, "the FUSE hyperlink did not survive the fill"
    assert fuse[0].target.startswith("https://"), fuse[0].target


def test_highlights_do_not_reach_the_reader(fixtures_dir, tmp_path):
    """Green and yellow are instructions to the template author, not formatting."""
    template = _template(fixtures_dir, "ct036", "original")
    manifest = _compile(template)
    out = tmp_path / "letter.docx"
    fill_template(str(template), str(out), manifest, _record(fixtures_dir, "ct036", manifest))

    body = docx.Document(str(out)).element.body
    left = {(h.get(f"{W}val") or "").lower() for h in body.iter(f"{W}highlight")}
    assert not (left & {"green", "brightgreen", "yellow"}), f"markup highlighting reached the letter: {left}"


# ----------------------------------------------------------------- the gates
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_missing_condition_is_caught_rather_than_shipped(fixtures_dir, tmp_path, dialect):
    """Delete one branch's condition and nothing governs its block any more, so
    both branches survive and the letter names two different recruitment teams
    in one sentence. Every text-based gate still passes: no scaffolding leaked,
    no placeholder is unresolved. Only counting branches catches it."""
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    manifest["conditions"] = [c for c in manifest["conditions"] if "without" not in c["expression"]]

    result = fill_template(str(template), str(tmp_path / "letter.docx"), manifest,
                           _record(fixtures_dir, "ct036", manifest))
    assert not result.qa_passed
    assert any("Inline switch" in note for note in result.qa_notes), result.qa_notes


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_value_no_branch_offers_is_caught(fixtures_dir, tmp_path, dialect):
    template = _template(fixtures_dir, "ct036", dialect)
    manifest = _compile(template)
    record = _record(fixtures_dir, "ct036", manifest, colleague_type="Casual")

    result = fill_template(str(template), str(tmp_path / "letter.docx"), manifest, record)
    assert not result.qa_passed
    assert any("colleague_type" in note for note in result.qa_notes), result.qa_notes


def test_bind_surfaces_a_source_value_no_branch_matches(fixtures_dir):
    """Caught at binding time, while a `value_map` entry is still one line —
    rather than after a batch has generated letters with sections missing."""
    manifest = _compile(_template(fixtures_dir, "ct036", "template"))
    columns, records = extract_records(str(fixtures_dir / "records" / "icc_ct036.xlsx"), "xlsx")

    clean = suggest_bindings(manifest, columns, records=records)
    assert clean.unmatched_condition_values == [], "the real source should bind cleanly"

    broken = suggest_bindings(manifest, columns, records=[{**records[0], "Colleague Type": "Casual"}])
    flagged = {u.observed_value for u in broken.unmatched_condition_values}
    assert "Casual" in flagged


def test_one_field_may_govern_several_independent_switches():
    """A field is not a switch; it selects one.

    This contract keys three separate switches on `colleague_type` -- which
    opening paragraph, which hours clause, which remuneration table -- so a
    full-time colleague keeps exactly one branch of each and three of seven
    survive. The gate expected exactly one survivor per field and blocked every
    document from a correctly-compiled template. It got steadily worse as the
    compiler got better, because finding more conditions meant more survivors.
    """
    from app.qa.value_lineage_check import branch_count_failures

    manifest = {
        "conditions": [
            {"id": "opening_ft", "expression": "colleague_type == 'Full time'"},
            {"id": "opening_pt", "expression": "colleague_type == 'Part time'"},
            {"id": "fixed_term", "expression": "colleague_type == 'Fixed term'"},
            {"id": "hours_pt", "expression": "colleague_type == 'Part time'"},
            {"id": "hours_ft", "expression": "colleague_type == 'Full time'"},
            {"id": "remuneration_ft", "expression": "colleague_type == 'Full time'"},
            {"id": "remuneration_pt", "expression": "colleague_type == 'Part time'"},
        ],
        "blocks": [],
    }
    lineage = [
        {"condition_id": c["id"], "result": c["expression"].endswith("'Full time'")}
        for c in manifest["conditions"]
    ]

    assert branch_count_failures(manifest, {"colleague_type": "Full time"}, lineage, set()) == []


def test_an_inverted_branch_is_still_caught():
    """What the relaxation must not cost: an expression that fires on the wrong
    value makes the survivor count stop matching, and that is the whole check."""
    from app.qa.value_lineage_check import branch_count_failures

    manifest = {
        "conditions": [
            {"id": "opening_ft", "expression": "colleague_type == 'Full time'"},
            {"id": "opening_pt", "expression": "colleague_type == 'Part time'"},
            {"id": "hours_ft", "expression": "colleague_type == 'Full time'"},
        ],
        "blocks": [],
    }
    # `hours_ft` did not fire for a full-time colleague: one survivor, two expected.
    lineage = [
        {"condition_id": "opening_ft", "result": True},
        {"condition_id": "opening_pt", "result": False},
        {"condition_id": "hours_ft", "result": False},
    ]

    failures = branch_count_failures(manifest, {"colleague_type": "Full time"}, lineage, set())
    assert failures and "1 of 3" in failures[0]


def test_a_value_no_branch_offers_is_still_caught_directly():
    """Nought expected and nought survived is not agreement -- it is a letter
    missing the section the switch exists to choose."""
    from app.qa.value_lineage_check import branch_count_failures

    manifest = {
        "conditions": [
            {"id": "ft", "expression": "colleague_type == 'Full time'"},
            {"id": "pt", "expression": "colleague_type == 'Part time'"},
        ],
        "blocks": [],
    }
    lineage = [{"condition_id": "ft", "result": False}, {"condition_id": "pt", "result": False}]

    failures = branch_count_failures(manifest, {"colleague_type": "Casual"}, lineage, set())
    assert failures and "no branch offers" in failures[0]


# ---- the model path must express an inline switch, not just the rules --------

def _inline_reading(scan):
    """What a writer reports for the recruitment switch: two branches per
    paragraph that carries one, and nothing else.

    Deliberately no `instruction_spans`. Asking a model to enumerate the
    instruction fragments is what fails -- a real master leaves the word
    "initiated" unmarked between two instruction phrases -- so the zone is
    computed from run colour instead.
    """
    fields = {"gbs": [], "px": []}
    branches = []
    for paragraph_index in sorted({s.paragraph_index for s in scan.spans}):
        spans = [s for s in scan.spans if s.paragraph_index == paragraph_index]
        blue = [s.text for s in spans if s.color == "blue"]
        has_instruction = any(s.color == "red" and s.text.strip() for s in spans)
        has_outside_static = any(s.color == "black" and s.text.strip() for s in spans)
        if not (has_instruction and has_outside_static and len(blue) >= 2):
            continue
        fields["gbs"].append({"paragraph_index": paragraph_index, "match_text": blue[0]})
        fields["px"].append({"paragraph_index": paragraph_index, "match_text": blue[1]})
        branches += [
            {"id": f"with_recruitment_{paragraph_index}",
             "expression": "transaction_type == 'With recruitment'",
             "paragraph_index": paragraph_index, "match_text": blue[0],
             "compiled_from": "For transactions initiated with recruitment"},
            {"id": f"without_recruitment_{paragraph_index}",
             "expression": "transaction_type == 'Without recruitment'",
             "paragraph_index": paragraph_index, "match_text": blue[1],
             "compiled_from": "For transactions initiated without recruitment"},
        ]
    # A paragraph that is nothing but instruction is scaffolding, and a real
    # writer reports it as such. Included so the test isolates inline behaviour
    # rather than failing on residue it never set out to cover.
    scaffolding = sorted({
        i for i in {s.paragraph_index for s in scan.spans}
        if [s for s in scan.spans if s.paragraph_index == i and s.text.strip()]
        and all(s.color == "red" for s in scan.spans if s.paragraph_index == i and s.text.strip())
    })
    reading = {
        "fields": [
            {"id": fid, "type": "string", "required": False, "reason": "r", "occurrences": occ}
            for fid, occ in fields.items() if occ
        ],
        "conditions": [], "instruction_spans": [], "inline_branches": branches,
        "scaffolding_paragraphs": scaffolding, "language": "en", "notes": [],
    }
    return reading, len(branches)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_model_path_can_express_an_inline_switch(fixtures_dir, tmp_path, dialect):
    """A switch whose arms are runs inside one paragraph, with content on both
    sides. A paragraph-ranged block cannot say "keep this run, drop that one",
    and deleting the paragraph takes the sentence the switch is embedded in --
    which shipped from this pipeline with `qa_passed` True.
    """
    import dataclasses

    from app.compiler.llm_compiler import assemble
    from app.templates.parsers.docx_prescan import prescan

    template = _template(fixtures_dir, "ct036", dialect)
    scan = prescan(str(template))
    reading, branch_count = _inline_reading(scan)
    assert branch_count >= 2, "fixture no longer carries an inline switch"

    manifest = dataclasses.asdict(assemble(scan, reading, model="test"))
    inline_blocks = [b for b in manifest["blocks"] if b.get("start_span") is not None]
    assert len(inline_blocks) == branch_count, "every arm must be span-scoped, not paragraph-ranged"

    for value, kept, dropped in (
        ("With recruitment", "GBS", "PX"),
        ("Without recruitment", "PX", "GBS"),
    ):
        out = tmp_path / f"{dialect}-{kept}.docx"
        result = fill_template(
            str(template), str(out), manifest,
            {"transaction_type": value, "gbs": "GBS-VALUE", "px": "PX-VALUE"},
        )
        text = _text(out)
        assert f"{kept}-VALUE" in text
        assert f"{dropped}-VALUE" not in text, "both arms survived; the letter names two contacts"
        assert "e.g. Change Job" not in text, "instruction text reached the reader"
        assert "initiated" not in text, (
            "the unmarked word between two instruction phrases leaked -- the zone "
            "must be read from run colour, not from what the model enumerated"
        )
        assert not [n for n in result.qa_notes if "instruction" in n.lower()]
        assert "To signify your acceptance" in text, "the sentence the switch sits in must survive"
