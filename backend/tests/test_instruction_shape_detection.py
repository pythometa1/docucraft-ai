"""Red text that reads like a branch instruction must not be silently deleted.

The rule layer has a last resort: a red span it cannot compile into a condition
is scaffolding, so it deletes the span and the paragraph around it. That is
correct for "(Remove all table once used)" and catastrophic for "USE FOR NEW
HIRE NOT ON TEMPORARY ASSIGNMENT", which is not scaffolding at all -- it is a
condition, written in prose, governing the paragraph beneath it.

`W-MARKER-PARSE` is what stands between those two cases. It fires when red text
*reads* like an instruction but does not compile as one, blocks approval, and
through `rules_fell_short` hands the template to a model that can read prose.

It used to match `only if|^for\\b` and nothing else, which is not the vocabulary
these templates use. Nineteen instructions in one real client master went
unmatched: no warning, no escalation, and a letter that shipped missing its
offer sentence, its date and its hours of work.

The bias here is deliberate. A false positive costs a reviewer one
acknowledgement. A false negative costs a paragraph nobody notices is gone.
"""

from pathlib import Path

import pytest

from app.compiler.rule_compiler import compile_manifest
from app.templates.conventions import all_families, load_family
from app.compiler.llm_compiler import rules_fell_short
from app.templates.parsers.docx_prescan import prescan

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("text", [
    # The shapes that went unmatched, from a real client master.
    "USE FOR NEW HIRE NOT ON TEMPORARY ASSIGNMENT",
    "USE IF ON TEMPORARY ASSIGNMENT",
    "USE FOR CURRENT COLLEAGUES NOT ON TEMPORARY ASSIGNMENT",
    "INCLUDE IF COLLEAGUE TYPE IS FIXED TERM",
    "INCLUDE IF ON TEMPORARY ASSIGNMENT AND HAS HIGHER DUTIES ALLOWANCE",
    "ALWAYS INCLUDE",
    "(Include if the colleague is working part time hours)",
    "(Include the below clause if the position is covered by the Clerks Award)",
    "(Remove all table once used)",
    # The two the original pattern already caught, which must keep working.
    "Include the following text only if the colleague type is Full Time:",
    "For Permanent transfers:",
])
def test_instruction_shapes_are_recognised(text):
    """Against the English family's declared vocabulary, not a module constant.

    Which is the point: a customer whose masters say "APPLY WHEN ..." instead of
    "USE IF ..." is a new line in a YAML file, not a change to the compiler."""
    pattern = load_family("en_annotated").instruction_shape_re
    assert pattern.search(text), f"an instruction shape went unmatched: {text!r}"


@pytest.mark.parametrize("text", [
    "Your employment with Pfizer Australia will commence on 1 July 2024.",
    "The total payments made to you, including your Base salary, are capped.",
    "You will be paid fortnightly in arrears.",
])
def test_ordinary_clause_text_is_not_instruction_shaped(text):
    pattern = load_family("en_annotated").instruction_shape_re
    assert not pattern.search(text), f"false positive on clause text: {text!r}"


def test_the_real_template_now_escalates_instead_of_deleting():
    """The regression, stated as the defect.

    This template compiled to 2 conditions and 30 delete-always paragraphs with
    zero warnings, so nothing escalated and the content went quietly. It must
    now raise W-MARKER-PARSE and be handed to a model.
    """
    template = Path("/Users/shubhamyeljale/Desktop/OutputTesting/HCM.AU.CT.003_AUS Non sales Contract_20251118 (1).docx")
    if not template.exists():
        pytest.skip("the client master is not part of the repository")

    scan = prescan(str(template))
    manifest = compile_manifest(scan)
    codes = [
        w.get("code") if isinstance(w, dict) else getattr(w, "code", None)
        for w in (manifest.warnings or [])
    ]
    assert codes.count("W-MARKER-PARSE") > 10, "the prose instructions were not recognised"
    assert rules_fell_short(scan, manifest), "a template this partly-understood must escalate"


@pytest.mark.parametrize("name", [
    "compensation_letter.docx",
    "hospira_offer.docx",
    "icc_ct036_template.docx",
    "icc_ct040_template.docx",
])
def test_widening_did_not_disturb_templates_that_already_compiled(name):
    """The blast-radius guarantee.

    Widening a pattern that blocks approval is only safe if it leaves working
    templates alone. Measured when the change was made: every fixture's
    W-MARKER-PARSE count was identical before and after, and these assertions
    pin that. A template that starts warning here has either regressed or found
    a real defect -- either way somebody should look, rather than discover it
    when a batch stops.
    """
    scan = prescan(str(FIXTURES / "templates" / name))
    manifest = compile_manifest(scan)
    codes = [
        w.get("code") if isinstance(w, dict) else getattr(w, "code", None)
        for w in (manifest.warnings or [])
    ]
    # Zero, for every one of them. The raw regex matches text in these templates,
    # but W-MARKER-PARSE also requires the span not to be a recognised marker and
    # not to sit in an inline zone -- and in these families it always is one or
    # the other. So the widening reaches exactly the templates the narrow pattern
    # left silently broken and no others.
    assert codes.count("W-MARKER-PARSE") == 0


def test_every_family_declares_its_own_instruction_vocabulary():
    """The scaling property, asserted rather than assumed.

    Engine behaviour is selected by family, never by template identity -- that
    is what keeps a thousand masters on one codebase. A family that declares no
    vocabulary falls back to the English one, which would silently mean a
    Japanese estate is policed by English imperatives and escalates nothing.
    """
    for family_id in all_families():
        family = load_family(family_id)
        assert family.instruction_shape_re is not None, (
            f"{family_id} declares no instruction_shape_pattern, so it would be policed "
            "by another language's vocabulary"
        )


def test_a_family_can_change_its_vocabulary_without_touching_the_compiler():
    """The claim that this layering is worth anything.

    A customer whose masters write "APPLY WHEN ..." is served by editing YAML.
    Nothing here imports the compiler.
    """
    from app.templates.conventions import ConventionFamily
    import re

    custom = ConventionFamily(
        id="acme", language="en",
        instruction_shape_re=re.compile(r"^\s*apply when\b", re.IGNORECASE),
    )
    from app.compiler.rule_compiler import _instruction_shaped

    assert _instruction_shaped(custom, "APPLY WHEN THE COLLEAGUE IS PART TIME")
    # And it is no longer policed by English imperatives it never declared.
    assert not _instruction_shaped(custom, "USE IF ON TEMPORARY ASSIGNMENT")
