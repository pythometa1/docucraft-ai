"""What makes a generated document reproducible: the hashes, and the closure.

§6 ends with the claim the whole audit story rests on -- production pins an
exact `(template_hash, manifest_hash)` pair, so any document can be rebuilt
from that pair plus the source record alone. Nothing computed those hashes.
The audit trail could name the manifest id that ran, which answers "which row"
and not "which bytes": the row's JSON columns can be edited in place, the
template binary can be re-saved from Word with a whitespace normalisation, and
a letter that was reproducible on Monday quietly is not on Tuesday, with
nothing to show that anything moved.

Three rules shape the hashing here.

The manifest hash covers what the run does -- objects, expressions, policy --
and nothing about who signed it. If `approved_by`, `approved_at` or the hash
itself were inside the digest, sealing a manifest would change the value being
sealed, and no two systems would ever agree on it.

The encoding is canonical, not `json.dumps`. Key order, `1` against `1.0`, and
a string that happens to contain a comma are all ways for two logically
identical manifests to hash differently, or -- worse -- for two different ones
to collide. Values are type-tagged and strings length-prefixed so neither can
happen, and anything the encoder does not recognise raises instead of being
stringified into a digest nobody can reproduce.

`required_source_fields` is computed, never taken on trust. §6 says so in as
many words, and the reason is drift detection: the list is what a batch is
validated against before it runs, so a hand-maintained one that forgot a field
added last month reports a source file clean and then blocks a thousand
documents one row at a time.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.expressions.token_parser import condition_inputs
from app.generation.resolution_engine import CyclicDependencyError
from app.generation.rule_engine import formula_identifiers
from app.manifests.models import (
    LEGACY_COLUMN_BY_OBJECT_TYPE, ManifestEnvelope, ManifestStatus, ObjectType,
    coerce_status, to_legacy_objects,
)

HASH_PREFIX = "sha256:"
_FILE_CHUNK = 1024 * 1024

#: §6 writes source references as `source.new_manager_name`. The record handed
#: to the renderer *is* the source row, so its keys carry no such prefix;
#: leaving it on would make every drift check compare a prefixed name against
#: an unprefixed column and declare every field missing.
SOURCE_NAMESPACE = "source."

#: The envelope fields the manifest hash covers: what the manifest *does*.
#:
#: Everything else is excluded on purpose. `approved_by`, `approved_at` and
#: `manifest_hash` are outputs of sealing, and hashing them would make the seal
#: change what it seals. `manifest_id`, `manifest_version`, `status`,
#: `organization_id`, `template_family_id`, `template_version_id` and
#: `supersedes` are identity and lifecycle: two orgs running byte-identical
#: rules should hash identically, which is what makes "this template family's
#: manifests are all the same contract" a checkable statement. `template_hash`
#: stays out because §6 pins it *alongside* this hash rather than inside it,
#: and `required_source_fields` stays out because it is a projection of the
#: objects -- including a derived value would let a stale copy of it change the
#: digest of an unchanged manifest.
HASHED_ENVELOPE_FIELDS = (
    "expression_lang",
    "objects",
    "qa_policy",
    "renderer_contract",
    "source_schema_hash",
    "source_schema_ref",
)


# ---- canonical encoding ----

def canonical_form(value) -> str:
    """A byte-exact encoding of JSON-shaped data, stable across runs and hosts.

    Type-tagged so `1`, `1.0`, `"1"` and `true` cannot collide, length-prefixed
    on strings so `["a", "b"]` cannot encode the same as `["a,b"]`, and dict
    keys sorted so insertion order stops mattering. Unsupported types raise:
    a digest computed over `str(some_object)` is reproducible only by the
    process that happened to build it.
    """
    if value is None:
        return "n"
    # Before int: bool is an int in Python, and "true" is not 1.
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"Cannot hash a non-finite number: {value!r}.")
        # An integral float is the same quantity as the int, and manifests
        # acquire one or the other depending on whether the value went through
        # JSON. `repr` is the shortest round-tripping form for the rest.
        if value.is_integer():
            return f"i:{int(value)}"
        return f"f:{value!r}"
    if isinstance(value, str):
        return f"s:{len(value)}:{value}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_form(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError(f"Manifest keys must be strings, got {type(key).__name__}.")
            parts.append(f"{canonical_form(key)}={canonical_form(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise TypeError(
        f"Cannot canonicalise {type(value).__name__} for hashing; manifests hold "
        "only JSON values (null, bool, int, float, str, list, dict)."
    )


def sha256_of(value) -> str:
    """`sha256:<hex>` over the canonical encoding of `value`."""
    digest = hashlib.sha256(canonical_form(value).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


# ---- the three hashes ----

def template_hash(path) -> str:
    """`sha256:<hex>` of the approved template binary, read in chunks.

    The bytes, not the parse. A DOCX re-saved by Word with identical visible
    content is a different file, and it has to be: the anchors in the manifest
    are run paths and ordinals into that exact package, so "looks the same" is
    not the property production needs to pin.
    """
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(_FILE_CHUNK):
            digest.update(chunk)
    return f"{HASH_PREFIX}{digest.hexdigest()}"


def source_schema_hash(columns) -> str:
    """Pin the source schema this manifest was compiled against.

    Accepts a mapping of column -> declared type, a plain sequence of column
    names, or a sequence of `{"name": ..., "type": ...}` dicts -- whichever the
    caller happens to hold. Column order is normalised away because a source
    export that reorders its columns has not changed schema; names keep their
    case, because a renamed or re-cased column is exactly the drift this hash
    exists to catch.

    An empty schema raises. Pinning "no columns" produces a hash that matches
    every other empty pin, which reads as agreement between two systems that
    have never actually agreed on anything.
    """
    pairs: dict[str, str] = {}

    def declare(name, declared_type) -> None:
        key = str(name).strip()
        if not key:
            raise ValueError("A source schema column must have a non-empty name.")
        value = "" if declared_type is None else str(declared_type).strip()
        if key in pairs and pairs[key] != value:
            raise ValueError(
                f"Source schema declares column {key!r} twice with different types: "
                f"{pairs[key]!r} and {value!r}."
            )
        pairs[key] = value

    if isinstance(columns, dict):
        for name, declared_type in columns.items():
            declare(name, declared_type)
    else:
        for entry in columns or ():
            if isinstance(entry, dict):
                declare(entry.get("name"), entry.get("type"))
            else:
                declare(entry, None)

    if not pairs:
        raise ValueError("Cannot pin an empty source schema; no columns were declared.")

    return sha256_of([[name, pairs[name]] for name in sorted(pairs)])


def manifest_hash(envelope: ManifestEnvelope) -> str:
    """`sha256:<hex>` over the objects, expressions and policy of `envelope`.

    Objects are sorted by (type, id) first. Two manifests that list the same
    objects in a different order are the same contract -- each object addresses
    the document through its own anchor, not through its position in a list --
    and letting a re-serialisation reorder them would produce a "changed"
    manifest that renders identically, which trains everyone to ignore the
    diff that matters.

    Each object is hashed whole, per-object `status`, `confidence`, `evidence`
    and approval stamps included. Those are attributes of the object §6 says
    must carry them, and "any change to an object changes the hash" is the
    property the review trail leans on; the approval that must stay outside the
    digest is the envelope's own, which sealing writes.
    """
    payload = {}
    for name in HASHED_ENVELOPE_FIELDS:
        if name == "objects":
            payload["objects"] = [
                obj.to_dict() for obj in
                sorted(envelope.objects, key=lambda o: (o.object_type.value, o.object_id))
            ]
        else:
            payload[name] = getattr(envelope, name)
    return sha256_of(payload)


def manifest_hash_matches(envelope: ManifestEnvelope) -> bool:
    """Whether the recorded hash still describes the objects it was sealed over."""
    return bool(envelope.manifest_hash) and envelope.manifest_hash == manifest_hash(envelope)


def pin_failures(envelope: ManifestEnvelope, template_path=None) -> list:
    """Every reason this manifest's `(template_hash, manifest_hash)` pin fails.

    Empty means the document this manifest produces can be reproduced from the
    pin plus the source record. Pass `template_path` to re-hash the template
    binary as well; without it the template side is unchecked, which is
    reported rather than assumed to be fine.
    """
    reasons: list = []
    if not envelope.manifest_hash:
        reasons.append("Manifest carries no manifest_hash, so its objects are not pinned.")
    elif not manifest_hash_matches(envelope):
        reasons.append(
            f"Manifest content no longer hashes to its recorded manifest_hash "
            f"({envelope.manifest_hash} recorded, {manifest_hash(envelope)} computed)."
        )
    if not envelope.template_hash:
        reasons.append("Manifest carries no template_hash, so its template binary is not pinned.")
    elif template_path is not None:
        actual = template_hash(template_path)
        if actual != envelope.template_hash:
            reasons.append(
                f"Template binary no longer hashes to its recorded template_hash "
                f"({envelope.template_hash} recorded, {actual} computed)."
            )
    return reasons


# ---- the required-source-field closure ----

def _source_name(entry: dict) -> str:
    """The source column one field object reads.

    `source_ref` when the object declares one, its id otherwise -- which is what
    the compiler produces today, where a field's id *is* the name a binding
    resolves.
    """
    ref = entry.get("source_ref")
    name = str(ref).strip() if ref else str(entry.get("id") or "").strip()
    if name.startswith(SOURCE_NAMESPACE):
        name = name[len(SOURCE_NAMESPACE):]
    return name


def _is_derived(entry: dict) -> bool:
    """Whether this field object produces a value rather than reading one."""
    if str(entry.get("object_type") or "").upper() == ObjectType.CALCULATION.value:
        return True
    if entry.get("kind") == "computed":
        return True
    return bool(entry.get("formula"))


def _derived_inputs(entry: dict) -> list:
    """What a calculation reads: its declared inputs, or its formula's names.

    §6 has CALCULATION carry `input_fields`; the compiler in this build writes
    `inputs`; a manifest that declares neither still names its dependencies
    inside the expression, so the identifiers are read out of it rather than
    treating the calculation as depending on nothing.
    """
    declared = entry.get("input_fields") or entry.get("inputs")
    if declared:
        return [str(name) for name in declared]
    expression = entry.get("formula") or entry.get("expression") or ""
    return formula_identifiers(str(expression))


def _referenced_names(manifest: dict) -> list:
    """Every name the manifest mentions, before derived ones are resolved away."""
    names: list = []

    for entry in manifest.get("fields") or ():
        if _is_derived(entry):
            names.extend(_derived_inputs(entry))
        else:
            names.append(_source_name(entry))

    for entry in manifest.get("conditions") or ():
        names.extend(condition_inputs(str(entry.get("expression") or "")))
        # A condition may also declare inputs the expression does not spell out
        # -- §6's CONDITION object carries `input_fields` -- and a "context"
        # condition names earlier units in `depends_on`.
        names.extend(str(n) for n in (entry.get("input_fields") or ()))
        names.extend(str(n) for n in (entry.get("depends_on") or ()))

    for entry in manifest.get("blocks") or ():
        # A repeated region reads the collection it repeats over; without it the
        # region silently renders once, or not at all.
        for key in ("repeat_over", "iterate_over"):
            if entry.get(key):
                names.append(str(entry[key]))
        column_refs = entry.get("column_refs")
        if isinstance(column_refs, dict):
            names.extend(str(ref) for ref in column_refs.values())
        elif isinstance(column_refs, (list, tuple)):
            names.extend(str(ref) for ref in column_refs)

    return names


def _as_stored_manifest(manifest) -> dict:
    if isinstance(manifest, ManifestEnvelope):
        stored = to_legacy_objects(manifest)
        # A SIGNATURE reads a source field too -- who signs -- and it is one of
        # the types the legacy projection has no list for, so without this the
        # closure under-reports by exactly the field a letter cannot be signed
        # without. NARRATIVE is not added: its grounding sources are retrieval
        # corpora, not columns of the source record.
        for obj in manifest.objects_of_type(ObjectType.SIGNATURE):
            ref = obj.attributes.get("signer_source_ref")
            if ref:
                stored["fields"].append({"id": obj.object_id, "source_ref": ref})
        return stored
    if isinstance(manifest, dict):
        return manifest
    raise TypeError(
        f"Expected a ManifestEnvelope or a stored manifest dict, got {type(manifest).__name__}."
    )


def required_source_fields(manifest) -> tuple:
    """The computed closure of every source field this manifest needs.

    Takes either a `ManifestEnvelope` or the stored
    `{fields, conditions, blocks}` dict. Three things make it a closure rather
    than a list of names:

    A calculation's own id is not required from the source -- the manifest
    produces it -- but its inputs are, and so are *their* inputs when one
    calculation feeds another.

    A name that matches a field object resolves to that field's `source_ref`,
    so renaming the column a field reads changes this list even though no id
    moved.

    A name that matches nothing in the manifest is a bare source column and
    stays as it is. That case is not an edge: the Hospira conditions all hinge
    on `colleague_type`, which has no placeholder anywhere in the document and
    therefore no field object, and a closure that dropped it would report a
    source file complete while every conditional block in the letter was about
    to be deleted.

    A cycle between calculations raises `CyclicDependencyError` rather than
    looping, matching what the resolution engine does with the same shape.
    """
    stored = _as_stored_manifest(manifest)

    by_id: dict[str, dict] = {}
    for entry in stored.get("fields") or ():
        entry_id = str(entry.get("id") or "").strip()
        if entry_id:
            by_id[entry_id] = entry

    resolved: set = set()

    def resolve(name: str, trail: tuple) -> None:
        name = name.strip()
        if not name:
            return
        if name in trail:
            cycle = " -> ".join(trail[trail.index(name):] + (name,))
            raise CyclicDependencyError(
                f"Circular dependency between manifest calculations: {cycle}"
            )
        entry = by_id.get(name)
        if entry is None:
            resolved.add(name)  # a bare source column
            return
        if _is_derived(entry):
            for dependency in _derived_inputs(entry):
                resolve(str(dependency), trail + (name,))
            return
        source = _source_name(entry)
        if source:
            resolved.add(source)

    for referenced in _referenced_names(stored):
        resolve(str(referenced), ())

    return tuple(sorted(resolved))


# ---- lock and supersede semantics ----

def is_locked(status) -> bool:
    """Whether this status means immutable-and-runnable.

    Accepts a `ManifestStatus`, a §6 name or a stored legacy string, because
    the callers that need to ask are on both sides of that bridge: a row holds
    "approved", an envelope holds LOCKED, and both mean the same thing.
    """
    return coerce_status(status) is ManifestStatus.LOCKED


def is_editable(status) -> bool:
    """Whether a manifest in this state may still be changed in place.

    LOCKED and SUPERSEDED are both immutable -- one because production is
    pinned to it, the other because a document generated last year has to
    remain explicable by the manifest that produced it.
    """
    return coerce_status(status) in (ManifestStatus.DRAFT, ManifestStatus.IN_REVIEW)


def lock(envelope: ManifestEnvelope, *, approved_by: str, approved_at: datetime | None = None) -> ManifestEnvelope:
    """Seal a manifest as the production contract, and return the sealed copy.

    Recomputes `required_source_fields` on the way through: §6 calls it a
    computed closure, so locking is exactly the moment a hand-edited list stops
    being anyone's problem. Then computes `manifest_hash` over the sealed
    content and stamps the approval.

    Refuses without a `template_hash`. A locked manifest whose template is not
    pinned cannot reproduce its own output, which is the single guarantee
    locking exists to make.
    """
    if not is_editable(envelope.status):
        raise ValueError(
            f"Manifest {envelope.manifest_id} is {envelope.status.value} and cannot be locked; "
            "edit it as a new version instead."
        )
    if not str(approved_by or "").strip():
        raise ValueError("Locking a manifest requires the id of the person approving it.")
    if not envelope.template_hash:
        raise ValueError(
            f"Manifest {envelope.manifest_id} pins no template_hash, so a document generated "
            "from it could not be reproduced. Hash the template version first."
        )

    sealed = replace(
        envelope,
        status=ManifestStatus.LOCKED,
        required_source_fields=required_source_fields(envelope),
        approved_by=str(approved_by),
        approved_at=approved_at or datetime.now(timezone.utc),
        manifest_hash=None,
    )
    return replace(sealed, manifest_hash=manifest_hash(sealed))


@dataclass(frozen=True)
class Supersession:
    """The two envelopes a manifest edit produces.

    Both, not one: §6's lock semantics are that the successor is a new DRAFT at
    version n+1 *and* the predecessor becomes SUPERSEDED and is never deleted.
    Returning them together is what stops a caller writing the successor and
    forgetting to retire what production is still pinned to -- which is how a
    template ends up with two manifests that both look live.
    """

    superseded: ManifestEnvelope
    successor: ManifestEnvelope


def supersede(envelope: ManifestEnvelope, *, manifest_id: str) -> Supersession:
    """Open version n+1 as a DRAFT and retire the version it replaces.

    The successor carries the predecessor's objects and pins, and none of its
    approval: no `approved_by`, no `approved_at`, and no `manifest_hash`, which
    is recomputed when it is locked in its turn. `manifest_id` is a parameter
    because ids are minted by the store, not by this module.
    """
    if envelope.status is ManifestStatus.SUPERSEDED:
        raise ValueError(
            f"Manifest {envelope.manifest_id} is already superseded; supersede its successor "
            "instead, or two drafts will claim the same predecessor."
        )
    if not str(manifest_id or "").strip():
        raise ValueError("A superseding manifest needs its own manifest_id.")

    successor = replace(
        envelope,
        manifest_id=str(manifest_id),
        manifest_version=envelope.manifest_version + 1,
        status=ManifestStatus.DRAFT,
        manifest_hash=None,
        approved_by=None,
        approved_at=None,
        supersedes=envelope.manifest_id,
    )
    return Supersession(
        superseded=replace(envelope, status=ManifestStatus.SUPERSEDED),
        successor=successor,
    )


def unstorable_object_types() -> tuple:
    """Object types §6 defines that the current table has no column for.

    Named here so a caller can check before it writes rather than discovering
    the loss afterwards. `to_row_values` reports the affected object ids; this
    is the same fact stated once, in terms of types.
    """
    return tuple(t for t in ObjectType if t not in LEGACY_COLUMN_BY_OBJECT_TYPE)
