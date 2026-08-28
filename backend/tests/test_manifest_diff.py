"""Diffing two manifests is what makes "review changed objects only" affordable.

§11's family workflow claims a new revision of a known template should "detect
delta; review changed objects only". The failure it prevents is not a crash: it
is a reviewer being handed sixty identical mappings to re-approve in order to
find the two that moved, skimming by the fifteenth, and waving through the one
that changed.
"""

import pytest

from app.manifests.diff import ABSENT, FieldChange, diff_manifests, field_changes
from app.manifests.models import ManifestEnvelope, ManifestObject, ManifestStatus


def _field(object_id: str, **attributes) -> ManifestObject:
    base = {
        "anchor": {"kind": "run_path", "path": "body/p[4]/r[1]", "ordinal": 1,
                   "token": "<Salary>", "context_hash": "sha256:" + "a" * 64},
        "source_ref": "source.annual_salary",
        "format": None,
        "on_missing": "BLOCK",
        "status": "APPROVED",
    }
    base.update(attributes)
    return ManifestObject(object_id=object_id, object_type="FIELD", attributes=base)


def _envelope(*objects, manifest_id="mf_1", **envelope) -> ManifestEnvelope:
    payload = {
        "manifest_id": manifest_id,
        "manifest_version": 1,
        "status": ManifestStatus.LOCKED,
        "organization_id": "org_a",
        "template_version_id": "tv_1",
        "objects": objects,
    }
    payload.update(envelope)
    return ManifestEnvelope(**payload)


def test_an_untouched_object_is_reported_unchanged():
    left = _envelope(_field("obj_1"))
    right = _envelope(_field("obj_1"), manifest_id="mf_2")

    result = diff_manifests(left, right)

    assert result.counts() == {"added": 0, "removed": 0, "changed": 0, "unchanged": 1}
    assert result.review_set() == ()


def test_an_object_that_moved_is_reported_with_the_field_that_moved():
    """A reviewer told only "obj_1 changed" has to open the object and compare it
    by eye. Naming `source_ref` is the difference between a decision and a hunt."""
    left = _envelope(_field("obj_1"))
    right = _envelope(_field("obj_1", source_ref="source.base_salary"), manifest_id="mf_2")

    changed = diff_manifests(left, right).changed
    assert len(changed) == 1
    assert [c.field for c in changed[0].changes] == ["source_ref"]
    assert changed[0].changes[0].before == "source.annual_salary"
    assert changed[0].changes[0].after == "source.base_salary"


def test_anchor_drift_is_named_by_its_dotted_path():
    """§19 ranks anchor drift after a template re-save as the most likely failure
    in the register. A diff that reported "the anchor changed" would hide which
    part -- a repointed path is a different repair from a stale context hash."""
    left = _envelope(_field("obj_1"))
    moved = dict(_field("obj_1").attributes["anchor"], context_hash="sha256:" + "b" * 64)
    right = _envelope(_field("obj_1", anchor=moved), manifest_id="mf_2")

    changed = diff_manifests(left, right).changed[0]
    assert [c.field for c in changed.changes] == ["anchor.context_hash"]


def test_an_object_only_on_the_right_is_added_and_only_on_the_left_is_removed():
    left = _envelope(_field("obj_1"), _field("obj_gone"))
    right = _envelope(_field("obj_1"), _field("obj_new"), manifest_id="mf_2")

    result = diff_manifests(left, right)

    assert [o.object_id for o in result.added] == ["obj_new"]
    assert [o.object_id for o in result.removed] == ["obj_gone"]
    assert [o.object_id for o in result.review_set()] == ["obj_new", "obj_gone"]


def test_a_removed_object_is_in_the_review_set():
    """A mapping that vanished is a paragraph that will not be filled -- §19's
    "silently blank required field", which it costs a legally defective letter.
    Reporting removals and then excluding them from review would be worse than
    not reporting them."""
    left = _envelope(_field("obj_1"), _field("obj_gone"))
    right = _envelope(_field("obj_1"), manifest_id="mf_2")

    assert [o.object_id for o in diff_manifests(left, right).review_set()] == ["obj_gone"]


def test_unchanged_means_hash_identical_not_merely_equal_looking():
    """`1` and `1.0` are the same number to Python's `==` and different values to
    the manifest hash. A differ that disagreed with the hash would call an object
    changed whose manifest_hash never moved, and reviewers would learn to ignore
    it."""
    left = _envelope(_field("obj_1", validation={"max_len": 80, "pattern": None}))
    # Same content, dict written in a different key order, integer as a float.
    right = _envelope(
        _field("obj_1", validation={"pattern": None, "max_len": 80.0}), manifest_id="mf_2"
    )

    assert diff_manifests(left, right).counts()["unchanged"] == 1


def test_a_declared_and_empty_attribute_is_not_the_same_as_an_absent_one():
    """§6's own worked FIELD example carries `"format": null`. Declared-and-empty
    is a decision somebody recorded; absent is a question nobody answered."""
    left = _envelope(_field("obj_1"))
    without = dict(_field("obj_1").attributes)
    without.pop("format")
    right = _envelope(
        ManifestObject(object_id="obj_1", object_type="FIELD", attributes=without),
        manifest_id="mf_2",
    )

    change = diff_manifests(left, right).changed[0].changes[0]
    assert change.field == "format"
    assert change.before is None and change.after is ABSENT
    assert change.removed and not change.added
    assert change.as_dict()["declared_before"] is True
    assert change.as_dict()["declared_after"] is False


def test_the_envelope_is_diffed_separately_from_the_objects():
    """Two manifests can carry byte-identical objects and still not be the same
    contract: a renderer upgrade changes what those objects do. Burying that
    among object changes, or omitting it, both make a real change look like a
    no-op review."""
    left = _envelope(_field("obj_1"), renderer_contract={"format": "DOCX", "version": "3.1.0"})
    right = _envelope(_field("obj_1"), manifest_id="mf_2",
                      renderer_contract={"format": "DOCX", "version": "3.2.0"})

    result = diff_manifests(left, right)

    assert result.counts()["changed"] == 0
    assert [c.field for c in result.envelope_changes] == ["renderer_contract"]
    assert not result.identical


def test_identical_reports_the_manifest_hashes_agreeing():
    left = _envelope(_field("obj_1"))
    right = _envelope(_field("obj_1"), manifest_id="mf_2")

    result = diff_manifests(left, right)
    assert result.identical
    assert result.as_dict()["identical"] is True
    assert result.as_dict()["review_object_ids"] == []


def test_diffing_a_loose_dict_is_refused_rather_than_guessed_at():
    """Two dicts would be compared on whatever keys they happened to carry, and
    a diff computed over the wrong shape reports everything as changed."""
    with pytest.raises(TypeError, match="ManifestEnvelope"):
        diff_manifests({"objects": []}, _envelope(_field("obj_1")))


def test_a_field_absent_on_both_sides_is_not_a_change():
    with pytest.raises(ValueError, match="absent on both sides"):
        FieldChange("format")


def test_nested_mappings_flatten_and_lists_do_not():
    """`anchor.context_hash` is worth naming. `test_cases[2].input` is not, because
    aligning two lists positionally is a guess this module has no basis for."""
    changes = field_changes(
        {"anchor": {"path": "p[1]"}, "test_cases": [{"input": 1}]},
        {"anchor": {"path": "p[2]"}, "test_cases": [{"input": 2}]},
    )
    assert [c.field for c in changes] == ["anchor.path", "test_cases"]
