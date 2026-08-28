"""Snapshots of what the pre-scanner sees and what the compiler decides.

The goldens in `test_fill_goldens.py` prove the *output* is stable. These prove
the two stages before it are stable, which is where a change is easiest to make
by accident and hardest to notice: a tweak to run-merging shifts every span
index, a tweak to `_find_block_end` moves a block boundary by one paragraph, and
in both cases the filled document is still a plausible letter.

Snapshots are plain sorted JSON so a diff is readable in a pull request.
Regenerate with `pytest --update-goldens`.
"""

from __future__ import annotations

import json

import pytest

from app.templates.parsers.docx_prescan import prescan
from app.compiler.rule_compiler import (
    BOUNDARY_CONFIDENCE,
    UNKNOWN_BOUNDARY_CONFIDENCE,
    compile_manifest,
)

TEMPLATES = ["hospira_offer", "compensation_letter"]


def _assert_snapshot(actual: dict, path, *, update: bool) -> None:
    rendered = json.dumps(actual, indent=1, sort_keys=True)
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        if not update:
            raise AssertionError(f"snapshot created at {path} -- review it, then re-run")
        return
    expected = path.read_text()
    assert rendered == expected, (
        f"snapshot differs: {path.name}\n"
        "(run `pytest --update-goldens` to accept, and say why in the PR)"
    )


def _prescan_summary(scan) -> dict:
    """Everything downstream coordinates depend on, and nothing that churns."""
    return {
        "paragraph_count": len(scan.paragraphs),
        "blue_spans": sum(1 for s in scan.spans if s.color == "blue" and s.text.strip()),
        "red_spans": sum(1 for s in scan.spans if s.color == "red" and s.text.strip()),
        "hyperlink_spans": sum(1 for s in scan.spans if s.in_hyperlink),
        # Order matters: these are matched positionally against the document.
        "mergefield_codes": [mf.code for mf in scan.mergefields],
        "mergefield_paragraphs": [mf.paragraph_index for mf in scan.mergefields],
        "table_paragraph_indices": sorted(scan.table_paragraph_indices),
    }


@pytest.mark.parametrize("name", TEMPLATES)
def test_prescan_inventory_is_stable(fixtures_dir, goldens_dir, update_goldens, name):
    scan = prescan(str(fixtures_dir / "templates" / f"{name}.docx"))
    _assert_snapshot(_prescan_summary(scan),
                     goldens_dir.parent / "snapshots" / f"prescan_{name}.json",
                     update=update_goldens)


def test_hospira_manifest_is_stable(fixtures_dir, goldens_dir, update_goldens):
    """Only the rule-compiled template is snapshotted here. The compensation
    manifest comes from a model, so re-running its compiler is not a test --
    it is a purchase. Its reviewed snapshot lives in fixtures/manifests/."""
    compiled = compile_manifest(prescan(str(fixtures_dir / "templates" / "hospira_offer.docx")))
    summary = {
        "confidence": compiled.confidence,
        "compiled_by": compiled.compiled_by,
        "fields": [{"id": f["id"], "type": f["type"], "slots": len(f["slots"])} for f in compiled.fields],
        "conditions": [{"id": c["id"], "expression": c["expression"], "keeps_blocks": c["keeps_blocks"]}
                       for c in compiled.conditions],
        "blocks": [{k: b[k] for k in ("id", "start_paragraph", "end_paragraph", "boundary_method")}
                   for b in compiled.blocks],
        "delete_always_count": len(compiled.delete_always),
    }
    _assert_snapshot(summary, goldens_dir.parent / "snapshots" / "manifest_hospira.json",
                     update=update_goldens)


def test_every_block_boundary_carries_a_known_confidence(fixtures_dir):
    """`BOUNDARY_CONFIDENCE` sorts the review queue, so an unrecognised method
    silently downgrading to 0.5 would bury a block a reviewer should see."""
    compiled = compile_manifest(prescan(str(fixtures_dir / "templates" / "hospira_offer.docx")))
    methods = {b["boundary_method"] for b in compiled.blocks}

    assert methods, "no blocks compiled -- this test is vacuous"
    unknown = methods - set(BOUNDARY_CONFIDENCE)
    assert not unknown, f"boundary methods with no declared confidence: {unknown}"


@pytest.mark.parametrize("method,expected", sorted(BOUNDARY_CONFIDENCE.items()))
def test_the_confidence_scale_is_what_the_review_ui_assumes(method, expected):
    """Pinned deliberately: the Studio badges these values, so changing one
    changes what a reviewer is told without changing any UI code."""
    assert BOUNDARY_CONFIDENCE[method] == expected
    assert 0.0 < expected <= 1.0


def test_an_unrecognised_boundary_method_is_treated_as_least_trusted():
    assert BOUNDARY_CONFIDENCE.get("something_new", UNKNOWN_BOUNDARY_CONFIDENCE) == 0.5
