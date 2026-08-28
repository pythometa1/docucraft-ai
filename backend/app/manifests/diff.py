"""What changed between two manifests, object by object.

§11's whole claim about scale rests on one row of its own table: a new revision
of a known template should "detect delta; review changed objects only" instead
of "remap all fields". Without a diff there is no delta, so a reviewer handed a
revision of a 60-object offer letter re-approves all sixty of them to find the
two that moved -- and the sixty-object review is exactly the cost §11 exists to
remove. Worse, it is the review that gets skimmed: nobody reads the fifty-eighth
identical mapping carefully, which is how a changed one goes through unnoticed.

Three decisions shape this module.

**"Unchanged" means hash-identical, not eyeball-identical.** Each object is
compared through `app.manifests.versioning.sha256_of`, the same canonical
encoding the manifest hash is built on. That is what stops `1` and `1.0`, a
re-ordered dict, or a JSON round trip from being reported as a change nobody
made -- and, in the other direction, stops two genuinely different objects from
being called the same because a shallow `==` happened to agree.

**A changed object carries the fields that changed.** "Object obj_0117 changed"
sends the reviewer back to the full object to work out what moved; "its anchor
context hash changed and its source_ref did not" is a decision they can make in
one glance. Nested mappings are flattened to dotted paths for the same reason:
`anchor.context_hash` is the single most useful line this module can print,
because it is anchor drift -- §19's most likely failure mode -- named directly.

**The envelope is diffed too, and separately.** Two manifests can carry
byte-identical objects and still not be the same contract: a different source
schema hash, a different expression dialect or a different renderer contract all
change what the objects *do*. Reporting those alongside object changes would
bury them; reporting them not at all would let a renderer upgrade look like a
no-op review.
"""

from dataclasses import dataclass
from enum import Enum

from app.manifests.models import ManifestEnvelope
from app.manifests.versioning import (
    HASHED_ENVELOPE_FIELDS, canonical_form, manifest_hash, sha256_of,
)


class _Absent:
    """The value a field has when it is not there at all.

    `None` cannot serve: §6's own worked FIELD example carries `"format": null`,
    so "declared and empty" is a real state and distinguishing it from "never
    declared" is the difference between a decision somebody recorded and a
    question nobody answered.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT = _Absent()


#: The envelope fields worth diffing: everything the manifest hash covers except
#: the objects (which get their own comparison), plus the two pins §6 says
#: production runs against. `manifest_id`, `manifest_version` and `status` are
#: left out on purpose -- they differ between any two manifests by definition,
#: and a diff whose first three lines are always noise teaches people to skip
#: the rest.
DIFFED_ENVELOPE_FIELDS = tuple(
    name for name in HASHED_ENVELOPE_FIELDS if name != "objects"
) + ("template_hash", "template_version_id", "template_family_id")


class ChangeKind(str, Enum):
    """What happened to one object between the two manifests."""

    ADDED = "ADDED"          # present on the right only
    REMOVED = "REMOVED"      # present on the left only
    CHANGED = "CHANGED"      # present on both, different hash
    UNCHANGED = "UNCHANGED"  # present on both, identical hash


#: The three that need a human. `ManifestDiff.review_set` is this filter, and it
#: is the point of the module: "review changed objects only" is only a saving if
#: something can say which objects those are. REMOVED is in the set because a
#: mapping that vanished is a paragraph that will not be filled, which is a
#: silently blank required field -- §19 ranks that as a legally defective letter.
REVIEWABLE = (ChangeKind.ADDED, ChangeKind.REMOVED, ChangeKind.CHANGED)


@dataclass(frozen=True)
class FieldChange:
    """One attribute that differs, named by its dotted path.

    `before` or `after` is `ABSENT` when the attribute exists on only one side.
    """

    field: str
    before: object = ABSENT
    after: object = ABSENT

    def __post_init__(self):
        if self.before is ABSENT and self.after is ABSENT:
            raise ValueError(
                f"Field {self.field!r} is absent on both sides, so it is not a change."
            )

    @property
    def added(self) -> bool:
        return self.before is ABSENT

    @property
    def removed(self) -> bool:
        return self.after is ABSENT

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "before": None if self.before is ABSENT else self.before,
            "after": None if self.after is ABSENT else self.after,
            "declared_before": self.before is not ABSENT,
            "declared_after": self.after is not ABSENT,
        }


@dataclass(frozen=True)
class ObjectDiff:
    """One object's verdict, and -- when it changed -- what changed about it."""

    object_id: str
    change: ChangeKind
    object_type: str | None = None
    changes: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "changes", tuple(self.changes))
        if self.change is ChangeKind.CHANGED and not self.changes:
            # A CHANGED verdict comes from the hashes disagreeing, so something
            # must differ. An empty list here would mean the flattener lost it,
            # and a reviewer told "this changed, we cannot say how" has learned
            # nothing they can act on.
            raise ValueError(
                f"Object {self.object_id!r} is reported CHANGED with no field changes; "
                "the comparison and the hash disagree, which is a bug in the differ."
            )
        if self.change is ChangeKind.UNCHANGED and self.changes:
            raise ValueError(
                f"Object {self.object_id!r} is reported UNCHANGED but carries "
                f"{len(self.changes)} field change(s)."
            )

    @property
    def needs_review(self) -> bool:
        return self.change in REVIEWABLE

    def as_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "object_type": self.object_type,
            "change": self.change.value,
            "changes": [c.as_dict() for c in self.changes],
        }


@dataclass(frozen=True)
class ManifestDiff:
    """The full comparison of two manifests.

    `identical` is the manifest hashes agreeing, which is a stronger statement
    than "no object changed": it also covers the qa policy, the expression
    dialect and the renderer contract.
    """

    left_manifest_id: str
    right_manifest_id: str
    left_manifest_hash: str
    right_manifest_hash: str
    objects: tuple = ()
    envelope_changes: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "objects", tuple(self.objects))
        object.__setattr__(self, "envelope_changes", tuple(self.envelope_changes))

    @property
    def identical(self) -> bool:
        return self.left_manifest_hash == self.right_manifest_hash

    def _of(self, kind: ChangeKind) -> tuple:
        return tuple(o for o in self.objects if o.change is kind)

    @property
    def added(self) -> tuple:
        return self._of(ChangeKind.ADDED)

    @property
    def removed(self) -> tuple:
        return self._of(ChangeKind.REMOVED)

    @property
    def changed(self) -> tuple:
        return self._of(ChangeKind.CHANGED)

    @property
    def unchanged(self) -> tuple:
        return self._of(ChangeKind.UNCHANGED)

    def review_set(self) -> tuple:
        """The objects a reviewer actually has to look at.

        §11's "review changed objects only", as a value rather than a slogan.
        Order is added, then changed, then removed -- reading order for someone
        working through a revision, not alphabetical.
        """
        return self.added + self.changed + self.removed

    def counts(self) -> dict:
        return {
            "added": len(self.added), "removed": len(self.removed),
            "changed": len(self.changed), "unchanged": len(self.unchanged),
        }

    def as_dict(self) -> dict:
        return {
            "left_manifest_id": self.left_manifest_id,
            "right_manifest_id": self.right_manifest_id,
            "left_manifest_hash": self.left_manifest_hash,
            "right_manifest_hash": self.right_manifest_hash,
            "identical": self.identical,
            "counts": self.counts(),
            "review_object_ids": [o.object_id for o in self.review_set()],
            "envelope_changes": [c.as_dict() for c in self.envelope_changes],
            "objects": [o.as_dict() for o in self.objects],
        }


# ---- comparison ----

def _same(left, right) -> bool:
    """Whether two JSON values are the same value, by the hashing encoding.

    Not `==`. `1 == 1.0` and `True == 1` are both true in Python, and both are
    false to `canonical_form`; a differ that disagreed with the hash would call
    an object CHANGED whose hash never moved, or the reverse.
    """
    return canonical_form(left) == canonical_form(right)


def _flatten(value, prefix: str = "") -> dict:
    """Mapping values expanded to dotted paths; everything else left whole.

    Dicts recurse because that is where the useful detail lives -- `anchor` is a
    mapping, and `anchor.context_hash` changing is anchor drift named exactly.
    Lists do not: a list is ordered content, and reporting `test_cases[2].input`
    would need a positional alignment this module has no basis to guess at.
    """
    if not isinstance(value, dict):
        return {prefix: value}
    if not value:
        # An empty mapping at the top level is "this object has no fields", not
        # a field called "". Nested, it is a real value: `validation: {}` is a
        # declared-and-empty rule, and losing it would hide its removal.
        return {} if not prefix else {prefix: value}
    out: dict = {}
    for key in value:
        path = f"{prefix}.{key}" if prefix else str(key)
        out.update(_flatten(value[key], path))
    return out


def field_changes(before: dict, after: dict) -> tuple:
    """Every dotted path on which two object dicts differ, in path order."""
    flat_before = _flatten(before)
    flat_after = _flatten(after)
    changes = []
    for path in sorted(set(flat_before) | set(flat_after)):
        left = flat_before.get(path, ABSENT)
        right = flat_after.get(path, ABSENT)
        if left is ABSENT or right is ABSENT:
            if left is ABSENT and right is ABSENT:
                continue
            changes.append(FieldChange(path, left, right))
        elif not _same(left, right):
            changes.append(FieldChange(path, left, right))
    return tuple(changes)


def _require_envelope(value, label: str) -> ManifestEnvelope:
    if not isinstance(value, ManifestEnvelope):
        raise TypeError(
            f"{label} must be a ManifestEnvelope, got {type(value).__name__}. Read a row "
            "through `app.manifests.models.envelope_from_row` first; diffing two loose "
            "dicts would compare whatever keys they happened to carry."
        )
    return value


def diff_manifests(left, right) -> ManifestDiff:
    """Compare two manifests object by object.

    `left` is the baseline and `right` is the candidate, so ADDED means "the
    right-hand manifest has an object the left one did not". Objects are matched
    by `object_id` -- an id is the manifest's own name for a mapping, and it is
    what survives a re-compile that moved the object's position in the list.

    An object present on both sides is UNCHANGED only when the two sides hash
    identically under the manifest hashing encoding, so "unchanged" here is the
    same statement `manifest_hash` makes, restricted to one object.
    """
    left = _require_envelope(left, "The left manifest")
    right = _require_envelope(right, "The right manifest")

    left_by_id = {o.object_id: o for o in left.objects}
    right_by_id = {o.object_id: o for o in right.objects}

    diffs: list = []
    for object_id in sorted(set(left_by_id) | set(right_by_id)):
        before = left_by_id.get(object_id)
        after = right_by_id.get(object_id)

        if before is None:
            diffs.append(ObjectDiff(object_id, ChangeKind.ADDED,
                                    object_type=after.object_type.value,
                                    changes=field_changes({}, after.to_dict())))
        elif after is None:
            diffs.append(ObjectDiff(object_id, ChangeKind.REMOVED,
                                    object_type=before.object_type.value,
                                    changes=field_changes(before.to_dict(), {})))
        elif sha256_of(before.to_dict()) == sha256_of(after.to_dict()):
            diffs.append(ObjectDiff(object_id, ChangeKind.UNCHANGED,
                                    object_type=before.object_type.value))
        else:
            diffs.append(ObjectDiff(
                object_id, ChangeKind.CHANGED,
                # The right-hand type: a reviewer is deciding about what the
                # manifest is becoming. A type that moved is itself listed as a
                # field change, so nothing is hidden by the choice.
                object_type=after.object_type.value,
                changes=field_changes(before.to_dict(), after.to_dict()),
            ))

    envelope_changes = []
    for name in DIFFED_ENVELOPE_FIELDS:
        before_value = getattr(left, name)
        after_value = getattr(right, name)
        if not _same(before_value, after_value):
            envelope_changes.append(FieldChange(name, before_value, after_value))

    return ManifestDiff(
        left_manifest_id=left.manifest_id,
        right_manifest_id=right.manifest_id,
        left_manifest_hash=left.manifest_hash or manifest_hash(left),
        right_manifest_hash=right.manifest_hash or manifest_hash(right),
        objects=tuple(diffs),
        envelope_changes=tuple(envelope_changes),
    )
