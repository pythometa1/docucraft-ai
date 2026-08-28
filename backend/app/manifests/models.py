"""The typed envelope a locked manifest actually is.

§6 of the architecture record calls the manifest "the production execution
contract", and the rest of the document leans on that phrase -- but until the
envelope is written down a manifest is whatever the row happened to contain
when it was saved. That is how two manifests render differently from the same
template and nobody can say which one a letter came out of: the row carries
fields, conditions and blocks, while everything that makes a run reproducible
-- which template binary, which source schema, which expression dialect, which
renderer, who signed it -- lives wherever somebody remembered to put it.

This module is the schema for that envelope, and data only: construction,
coercion, and round-tripping to and from plain JSON. Hashing, the
required-source-field closure and the lock/supersede transitions live in
`app.manifests.versioning`, which imports this module. Nothing here imports
that one, so the dependency runs one way and the envelope stays inert.

Two things it deliberately does not do.

It does not touch the database. `TemplateManifest` already carries every
envelope column §6 names except `template_family_id`, so `to_row_values` hands
back a plain column -> value mapping a caller can apply to a row, and names
what has nowhere to go instead of dropping it quietly.

It does not invent a status vocabulary. The doc has four states, the table has
five legacy strings, and they disagree about the one that matters: what §6
calls LOCKED is stored as "approved". That bridge is written out here rather
than re-guessed at every call site -- a call site that guesses wrong reads a
locked manifest as editable.
"""

from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum


# The dialect this build actually executes. §7 recommends CEL, and the envelope
# records the language so that recommendation can be adopted per manifest
# without silently reinterpreting rules approved under the old one -- but what
# runs today is the restricted-AST subset in `app.expressions.token_parser`,
# and claiming "cel/1.0" here would be a lie told to the audit trail. Matches
# the default already on `TemplateManifest.expression_lang`.
DEFAULT_EXPRESSION_LANG = "documind-expr/1.0"


# ---- status ----

class ManifestStatus(str, Enum):
    """The four states §6 defines, and only those.

    DRAFT       being compiled or edited; never executed in production
    IN_REVIEW   a human is looking at it; still editable
    LOCKED      immutable; the production execution contract
    SUPERSEDED  replaced by a later version; kept forever, never executed
    """

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    LOCKED = "LOCKED"
    SUPERSEDED = "SUPERSEDED"


#: §6 status -> the string `TemplateManifest.status` has always stored.
#: "approved" is what this schema wrote long before §6 named the state LOCKED,
#: and renaming a column value under a live table is a migration, not a
#: definition. The two names mean the same thing: signed off and immutable.
LEGACY_STATUS_BY_STATUS = {
    ManifestStatus.DRAFT: "draft",
    ManifestStatus.IN_REVIEW: "in_review",
    ManifestStatus.LOCKED: "approved",
    ManifestStatus.SUPERSEDED: "superseded",
}

#: The reverse, plus the one legacy string §6 has no state for. "deprecated"
#: means retired-and-not-runnable, which is exactly SUPERSEDED's guarantee, so
#: it maps there. The mapping is therefore deliberately lossy in that one
#: direction: a "deprecated" row read into an envelope and written back becomes
#: "superseded". Losing the distinction is safe; reading it as DRAFT, and so as
#: editable, would not be.
STATUS_BY_LEGACY_STATUS = {
    "draft": ManifestStatus.DRAFT,
    "in_review": ManifestStatus.IN_REVIEW,
    "approved": ManifestStatus.LOCKED,
    "superseded": ManifestStatus.SUPERSEDED,
    "deprecated": ManifestStatus.SUPERSEDED,
}


def to_legacy_status(status) -> str:
    """The string to write to `TemplateManifest.status` for this state."""
    return LEGACY_STATUS_BY_STATUS[coerce_status(status)]


def from_legacy_status(stored: str) -> ManifestStatus:
    """Read a stored status string as one of the four §6 states.

    An unrecognised string raises. Defaulting it to DRAFT would make an
    unreadable status mean "editable", and defaulting it to LOCKED would make a
    typo mean "run this in production"; both are worse than a loud failure at
    the one place that can still name the row.
    """
    try:
        return STATUS_BY_LEGACY_STATUS[str(stored).strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown stored manifest status {stored!r}; expected one of "
            f"{', '.join(sorted(STATUS_BY_LEGACY_STATUS))}."
        ) from None


def coerce_status(status) -> ManifestStatus:
    """Accept a ManifestStatus, a §6 name, or a stored legacy string."""
    if isinstance(status, ManifestStatus):
        return status
    text = str(status).strip()
    try:
        return ManifestStatus(text.upper())
    except ValueError:
        return from_legacy_status(text)


# ---- objects ----

class ObjectType(str, Enum):
    """The semantic object types §6 enumerates."""

    FIELD = "FIELD"
    CONDITION = "CONDITION"
    SECTION = "SECTION"
    TABLE_ROW = "TABLE_ROW"
    CALCULATION = "CALCULATION"
    NARRATIVE = "NARRATIVE"
    STATIC = "STATIC"
    HEADER = "HEADER"
    FOOTER = "FOOTER"
    SIGNATURE = "SIGNATURE"


#: What each object type "must carry", straight from §6's table. Presence is
#: the test, not truthiness: the doc's own worked FIELD example carries
#: `"format": null`, and a declared-and-empty attribute is a decision recorded
#: where an absent one is a question nobody answered.
MANDATORY_ATTRIBUTES = {
    ObjectType.FIELD: ("anchor", "source_ref", "format", "on_missing", "status"),
    ObjectType.CONDITION: ("anchor_range", "expression", "on_true", "on_false", "test_cases"),
    ObjectType.SECTION: ("anchor_range", "repeat_over", "ordering", "empty_behaviour"),
    # "per-column field refs" in the doc's table; `column_refs` here because an
    # attribute name has to be one token.
    ObjectType.TABLE_ROW: ("anchor_row", "iterate_over", "column_refs"),
    ObjectType.CALCULATION: ("expression", "input_fields", "output_type", "rounding", "test_cases"),
    ObjectType.NARRATIVE: ("prompt_ref", "grounding_sources", "citation_policy", "review_required"),
    ObjectType.STATIC: ("text_hash",),
    ObjectType.HEADER: ("anchor", "allowed_mutations"),
    ObjectType.FOOTER: ("anchor", "allowed_mutations"),
    ObjectType.SIGNATURE: ("anchor", "signer_source_ref", "image_policy", "esign_ref"),
}


class AnchorKind(str, Enum):
    """How an object addresses its place in the template (§6's anchor table)."""

    CONTENT_CONTROL = "content_control"   # SDT; highest stability
    MERGEFIELD = "mergefield"
    RUN_PATH = "run_path"                 # path + ordinal + context hash
    BOUNDING_BOX = "bounding_box"         # immutable PDF templates only
    STYLE_OR_COLOUR = "style_or_colour"   # onboarding hint; never production


#: Anchor kinds that may address a production object. Colour is missing on
#: purpose: §6 is explicit that colour coding is evidence, not an anchor, and a
#: single reformat in Word would silently repoint every mapping that trusted it.
PRODUCTION_ANCHOR_KINDS = frozenset({
    AnchorKind.CONTENT_CONTROL, AnchorKind.MERGEFIELD,
    AnchorKind.RUN_PATH, AnchorKind.BOUNDING_BOX,
})


def is_production_anchor(kind) -> bool:
    """Whether this anchor kind may survive into a locked manifest."""
    if kind is None:
        return False
    try:
        return AnchorKind(str(kind).strip().lower()) in PRODUCTION_ANCHOR_KINDS
    except ValueError:
        # An anchor kind nobody declared cannot be shown to re-resolve against
        # the pinned template, so it is not a production anchor either.
        return False


@dataclass(frozen=True)
class ManifestObject:
    """One semantic object: its identity, its type, and everything else.

    The remaining attributes stay an open mapping rather than ten separate
    dataclasses. Object shapes differ per type and per renderer, the compiler
    already emits keys this envelope has no opinion about (`slots`, `confidence`,
    `compiled_from`), and dropping them on the way through would make the
    envelope a lossy copy of the manifest rather than the manifest itself.
    `missing_attributes` is what enforces §6's per-type contract.
    """

    object_id: str
    object_type: ObjectType
    attributes: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.object_id is None or not str(self.object_id).strip():
            raise ValueError("A manifest object must carry a non-empty object_id.")
        object.__setattr__(self, "object_id", str(self.object_id))
        object.__setattr__(self, "object_type", coerce_object_type(self.object_type))
        if not isinstance(self.attributes, dict):
            raise TypeError(
                f"Object {self.object_id!r} attributes must be a mapping, got "
                f"{type(self.attributes).__name__}."
            )
        object.__setattr__(self, "attributes", dict(self.attributes))

    def missing_attributes(self) -> tuple:
        """Which of §6's mandatory attributes this object does not declare."""
        return tuple(
            name for name in MANDATORY_ATTRIBUTES[self.object_type]
            if name not in self.attributes
        )

    def to_dict(self) -> dict:
        """The §6 object shape: object_id, object_type, then its attributes."""
        return {
            "object_id": self.object_id,
            "object_type": self.object_type.value,
            **self.attributes,
        }

    def to_legacy_dict(self) -> dict:
        """The shape the existing `fields`/`conditions`/`blocks` columns hold.

        Same content, keyed by `id`, because that is what every reader of those
        columns already looks for -- the validator, the resolution engine, the
        fill engine. `object_type` rides along so `from_legacy_dict` can tell a
        SECTION from a TABLE_ROW when both come back out of `blocks`.
        """
        return {
            "id": self.object_id,
            "object_type": self.object_type.value,
            **self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict, *, default_type=None) -> "ManifestObject":
        if not isinstance(data, dict):
            raise TypeError(f"A manifest object must be a mapping, got {type(data).__name__}.")
        payload = dict(data)
        object_id = payload.pop("object_id", None) or payload.pop("id", None)
        declared = payload.pop("object_type", None) or default_type
        if declared is None:
            raise ValueError(
                f"Object {object_id!r} declares no object_type and none was implied by "
                "the list it came from."
            )
        payload.pop("id", None)  # both spellings were present; identity is settled
        return cls(object_id=object_id, object_type=declared, attributes=payload)

    @classmethod
    def from_legacy_dict(cls, data: dict, default_type) -> "ManifestObject":
        """Read one entry of a legacy `fields`/`conditions`/`blocks` list.

        `default_type` is what the list itself implies; an entry that names its
        own `object_type` wins, which is how a CALCULATION stored among the
        fields comes back as a CALCULATION.
        """
        return cls.from_dict(data, default_type=default_type)


def coerce_object_type(object_type) -> ObjectType:
    if isinstance(object_type, ObjectType):
        return object_type
    text = str(object_type).strip().upper()
    try:
        return ObjectType(text)
    except ValueError:
        raise ValueError(
            f"Unknown manifest object type {object_type!r}; expected one of "
            f"{', '.join(t.value for t in ObjectType)}."
        ) from None


# ---- envelope ----

@dataclass(frozen=True)
class ManifestEnvelope:
    """§6's top-level manifest structure.

    Frozen because §6's lock semantics are the whole point: editing a locked
    manifest produces version n+1 in DRAFT rather than mutating what production
    is pinned to. `app.manifests.versioning` returns new envelopes for every
    transition, and an accidental `envelope.status = LOCKED` raises here.

    `template_family_id` is ordered after the identifiers the table stores
    because `TemplateManifest` has no column for it yet -- see `to_row_values`.
    """

    manifest_id: str
    manifest_version: int
    status: ManifestStatus
    organization_id: str
    template_version_id: str
    template_family_id: str | None = None
    template_hash: str | None = None
    source_schema_ref: str | None = None
    source_schema_hash: str | None = None
    expression_lang: str = DEFAULT_EXPRESSION_LANG
    renderer_contract: dict = field(default_factory=dict)
    required_source_fields: tuple = ()
    qa_policy: dict = field(default_factory=dict)
    objects: tuple = ()
    manifest_hash: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    supersedes: str | None = None

    def __post_init__(self):
        for name in ("manifest_id", "organization_id", "template_version_id"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"A manifest envelope must carry a non-empty {name}.")
            object.__setattr__(self, name, str(getattr(self, name)))
        if not isinstance(self.manifest_version, int) or isinstance(self.manifest_version, bool):
            raise TypeError(
                f"manifest_version must be an int, got {type(self.manifest_version).__name__}."
            )
        if self.manifest_version < 1:
            raise ValueError(f"manifest_version starts at 1, got {self.manifest_version}.")
        object.__setattr__(self, "status", coerce_status(self.status))
        object.__setattr__(self, "objects", tuple(
            o if isinstance(o, ManifestObject) else ManifestObject.from_dict(o)
            for o in self.objects
        ))
        # Sorted and de-duplicated: the order of a computed closure carries no
        # meaning, and an unstable order turns every recompute into a diff.
        object.__setattr__(self, "required_source_fields", tuple(sorted(
            {str(name) for name in self.required_source_fields}
        )))
        object.__setattr__(self, "renderer_contract", dict(self.renderer_contract or {}))
        object.__setattr__(self, "qa_policy", dict(self.qa_policy or {}))
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", str(self.supersedes))
        if self.approved_at is not None and not isinstance(self.approved_at, datetime):
            object.__setattr__(self, "approved_at", parse_timestamp(self.approved_at))

        duplicates = _duplicate_object_ids(self.objects)
        if duplicates:
            # Two objects with one id cannot both be addressed, so one of them
            # is unreachable -- and which one depends on iteration order.
            raise ValueError(
                "A manifest envelope cannot carry two objects with the same object_id: "
                + ", ".join(duplicates)
            )

    # -- convenience --

    def objects_of_type(self, object_type) -> tuple:
        wanted = coerce_object_type(object_type)
        return tuple(o for o in self.objects if o.object_type is wanted)

    def object_by_id(self, object_id: str):
        for o in self.objects:
            if o.object_id == object_id:
                return o
        return None

    # -- serialisation --

    def to_dict(self) -> dict:
        """The envelope as JSON primitives, in §6's own key order.

        Every value here is a str, int, bool, None, list or dict, which is what
        lets the envelope live in the JSON columns that already exist rather
        than needing a typed table of its own.
        """
        return {
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "status": self.status.value,
            "organization_id": self.organization_id,
            "template_family_id": self.template_family_id,
            "template_version_id": self.template_version_id,
            "template_hash": self.template_hash,
            "source_schema_ref": self.source_schema_ref,
            "source_schema_hash": self.source_schema_hash,
            "expression_lang": self.expression_lang,
            "renderer_contract": dict(self.renderer_contract),
            "required_source_fields": list(self.required_source_fields),
            "qa_policy": dict(self.qa_policy),
            "objects": [o.to_dict() for o in self.objects],
            "manifest_hash": self.manifest_hash,
            "approved_by": self.approved_by,
            "approved_at": format_timestamp(self.approved_at),
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ManifestEnvelope":
        """Rebuild an envelope from `to_dict` output.

        Unknown top-level keys raise rather than being ignored. A manifest is
        the execution contract, and a key the reader does not understand is
        either a misspelling that silently dropped a policy or a newer writer
        this code cannot honour -- both of which are worse discovered here than
        during a batch.
        """
        if not isinstance(data, dict):
            raise TypeError(f"A manifest envelope must be a mapping, got {type(data).__name__}.")
        known = {f.name for f in dataclass_fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(
                "Unknown manifest envelope keys: " + ", ".join(unknown)
                + f". Known keys are {', '.join(sorted(known))}."
            )
        missing = sorted({"manifest_id", "manifest_version", "status",
                          "organization_id", "template_version_id"} - set(data))
        if missing:
            raise ValueError("Manifest envelope is missing required keys: " + ", ".join(missing))

        payload = dict(data)
        payload["objects"] = tuple(
            ManifestObject.from_dict(o) for o in (payload.get("objects") or ())
        )
        payload["approved_at"] = parse_timestamp(payload.get("approved_at"))
        payload["required_source_fields"] = tuple(payload.get("required_source_fields") or ())
        return cls(**payload)


def _duplicate_object_ids(objects) -> list:
    seen, duplicates = set(), []
    for o in objects:
        if o.object_id in seen and o.object_id not in duplicates:
            duplicates.append(o.object_id)
        seen.add(o.object_id)
    return duplicates


# ---- timestamps ----

def format_timestamp(value: datetime | None) -> str | None:
    """UTC, ISO 8601, `Z` suffix -- the format §6's envelope example uses.

    A naive datetime is read as UTC. Every timestamp this system writes comes
    from `models.now()`, which is aware UTC; SQLite hands the same value back
    without its tzinfo, and treating that as local time would move an approval
    by hours depending on where the reader is sitting.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value) -> datetime | None:
    """Read `format_timestamp` output (or a datetime) back to aware UTC."""
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Not an ISO 8601 timestamp: {value!r}.") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---- the bridge to what the table actually stores ----

#: Which legacy JSON column each object type decomposes into, and which type a
#: column implies when nothing says otherwise. CALCULATION joins FIELD in
#: `fields` because that is where the resolution engine already looks for a
#: derived value (`kind == "computed"`); SECTION and TABLE_ROW are both regions,
#: so both live in `blocks`.
LEGACY_COLUMN_BY_OBJECT_TYPE = {
    ObjectType.FIELD: "fields",
    ObjectType.CALCULATION: "fields",
    ObjectType.CONDITION: "conditions",
    ObjectType.SECTION: "blocks",
    ObjectType.TABLE_ROW: "blocks",
}

DEFAULT_OBJECT_TYPE_BY_LEGACY_COLUMN = {
    "fields": ObjectType.FIELD,
    "conditions": ObjectType.CONDITION,
    "blocks": ObjectType.SECTION,
}


@dataclass(frozen=True)
class RowValues:
    """Column values for a `TemplateManifest` row, and what would not fit.

    `unmapped` is not an error return -- it is the honest half of the mapping.
    §6 defines ten object types and the table has three lists, so a STATIC,
    NARRATIVE, HEADER, FOOTER or SIGNATURE object has nowhere to go until the
    row grows a single JSON `objects` column. A caller that writes `columns`
    and ignores a non-empty `unmapped` is dropping approved manifest content on
    the floor, which is why it is returned rather than logged.
    """

    columns: dict
    unmapped: tuple = ()

    def is_complete(self) -> bool:
        return not self.unmapped


def to_legacy_objects(envelope: ManifestEnvelope) -> dict:
    """The `{fields, conditions, blocks}` dict the existing readers consume.

    `app.manifests.validator.validate_manifest`, the source resolver and the
    fill engine all take this shape. Producing it from the envelope means the
    envelope can be introduced without rewriting any of them.
    """
    out = {"fields": [], "conditions": [], "blocks": []}
    for obj in envelope.objects:
        column = LEGACY_COLUMN_BY_OBJECT_TYPE.get(obj.object_type)
        if column:
            out[column].append(obj.to_legacy_dict())
    return out


def to_row_values(envelope: ManifestEnvelope) -> RowValues:
    """Map the envelope onto `TemplateManifest`'s columns.

    Returns column names verbatim so a caller can `setattr` them onto a row.
    `delete_always` is deliberately absent: it is a list of instruction spans
    the renderer removes, not one of §6's semantic objects, and a mapping that
    emitted it would overwrite the compiler's work with an empty list every
    time an envelope was written back.
    """
    lists = to_legacy_objects(envelope)
    unmapped = tuple(
        o.object_id for o in envelope.objects
        if o.object_type not in LEGACY_COLUMN_BY_OBJECT_TYPE
    )
    columns = {
        "id": envelope.manifest_id,
        "org_id": envelope.organization_id,
        "template_version_id": envelope.template_version_id,
        "version_no": envelope.manifest_version,
        "status": to_legacy_status(envelope.status),
        "fields": lists["fields"],
        "conditions": lists["conditions"],
        "blocks": lists["blocks"],
        "template_hash": envelope.template_hash,
        "manifest_hash": envelope.manifest_hash,
        "source_schema_ref": envelope.source_schema_ref,
        "source_schema_hash": envelope.source_schema_hash,
        "expression_lang": envelope.expression_lang,
        "renderer_contract": dict(envelope.renderer_contract),
        "required_source_fields": list(envelope.required_source_fields),
        "qa_policy": dict(envelope.qa_policy),
        "supersedes": envelope.supersedes,
        "approved_by": envelope.approved_by,
        "approved_at": envelope.approved_at,
    }
    return RowValues(columns=columns, unmapped=unmapped)


def envelope_from_row(row, *, template_family_id: str | None = None) -> ManifestEnvelope:
    """Read a `TemplateManifest` row (or anything shaped like one) as an envelope.

    Duck-typed on purpose: this module stays free of the ORM so the envelope can
    be built and tested without a database, and so importing it never drags in
    `app.models`.

    `template_family_id` is a parameter rather than a column read because there
    is no column -- family membership lives in `TemplateClusterMember`. Pass it
    from there when the caller knows it; the envelope records `None` rather than
    inventing one.
    """
    objects: list = []
    for column, default_type in DEFAULT_OBJECT_TYPE_BY_LEGACY_COLUMN.items():
        for entry in getattr(row, column, None) or ():
            objects.append(ManifestObject.from_legacy_dict(entry, default_type))

    return ManifestEnvelope(
        manifest_id=row.id,
        manifest_version=int(row.version_no or 1),
        status=from_legacy_status(row.status),
        organization_id=row.org_id,
        template_version_id=row.template_version_id,
        template_family_id=template_family_id,
        template_hash=getattr(row, "template_hash", None),
        source_schema_ref=getattr(row, "source_schema_ref", None),
        source_schema_hash=getattr(row, "source_schema_hash", None),
        expression_lang=getattr(row, "expression_lang", None) or DEFAULT_EXPRESSION_LANG,
        renderer_contract=getattr(row, "renderer_contract", None) or {},
        required_source_fields=tuple(getattr(row, "required_source_fields", None) or ()),
        qa_policy=getattr(row, "qa_policy", None) or {},
        objects=tuple(objects),
        manifest_hash=getattr(row, "manifest_hash", None),
        approved_by=getattr(row, "approved_by", None),
        approved_at=getattr(row, "approved_at", None),
        supersedes=getattr(row, "supersedes", None),
    )
