"""§11's family workflow: what a new template inherits, and what it must not.

The doc draws it as five lines:

    New template
      -> structural fingerprint
      -> nearest approved family
         |- similarity very high -> reuse manifest + diff changed objects
         |- similarity medium    -> reuse mapping evidence + targeted review
         `- no useful family     -> compile as a new family

Every part of that existed here except the arrows. `fingerprint.py` computes the
fingerprint and was exercised only by its own tests; `family_matcher.py` grouped
an estate at upload time and then forgot it; nothing carried an approved
manifest across to the next template. So the estate paid the no-family price on
every single file, which §18 puts at ~15-40 model calls against ~2-6 for a
strong match, and then says plainly: "family reuse is the primary cost lever,
not model selection."

The dangerous half of this module is the inheriting, not the matching. An
inherited mapping arrives wearing an approval that a human gave to a *different
document*. Three rules keep that from becoming an escaped error:

  * Inherited objects come back PROPOSED, never APPROVED. §13's bands decide
    whether a reviewer has to look at them, exactly as they would for a mapping
    the compiler had just invented; inheritance contributes a signal
    (`family_inheritance`, weight 0.65 scaled by measured similarity), not a
    verdict.
  * Every anchor is re-resolved against the new template version. An anchor that
    no longer resolves exactly once comes back flagged for review rather than
    dropped -- dropping it is how a required field silently stops being filled,
    which §19 ranks as a legally defective letter.
  * Inheritance never crosses a tenant. §19 lists "cross-tenant leakage via
    family matching" as low likelihood and potentially existential, and this is
    the function where that would happen, so the check lives here and raises.
"""

from dataclasses import dataclass, fields as dataclass_fields, replace
from enum import Enum

from app.manifests.models import ManifestEnvelope, ManifestStatus
from app.templates.fingerprint import StructuralFingerprint, structural_similarity
from app.templates.semantic_model import (
    ANCHOR_STYLE_ID, Anchor, AnchorError, PROPOSED, STATIC, TemplateInventory,
    document_text, resolve, static_text_hash,
)

# ---- the two thresholds ----

#: "similarity very high" -- reuse the approved manifest whole and let the diff
#: say what to review.
#:
#: 0.90 is derived, not chosen for looking decisive. §13 combines independent
#: signals as `confidence = 1 - ∏(1 - sᵢ·wᵢ)` and gives `family_inheritance` a
#: weight of 0.65 "scaled by measured family similarity". An inherited FIELD
#: that also matches its source column by name (`exact_name_match`, w = 0.55)
#: therefore scores
#:
#:     1 - (1 - 0.90 × 0.65) × (1 - 0.55) = 0.813
#:
#: which is inside §13's Confirm band (0.80 - 0.97): "presented pre-selected
#: with evidence; one click to accept". That is what "reuse manifest + diff
#: changed objects" means at the review desk. Lower the bar to 0.85 and the same
#: object scores 0.799 and falls into Review, where §13 says it must be shown
#: with ranked alternatives -- at which point the branch has saved nobody
#: anything and is misnaming what it does.
REUSE_MANIFEST_SIMILARITY = 0.90

#: "similarity medium" -- reuse the family's mapping evidence, then review the
#: objects it lands on.
#:
#: 0.60 is `cluster_by_structure`'s own family threshold. Below it two templates
#: were never grouped into a family in the first place, so "inherit from the
#: nearest family" would mean inheriting from a stranger that happened to be
#: closest.
#:
#: The cost arithmetic agrees. §18's saving is real only while most inherited
#: mappings survive review, and correcting a wrong mapping costs a reviewer more
#: attention than making a fresh one -- a reviewer who has been handed three bad
#: suggestions stops reading the fourth. So the floor sits where reuse still
#: pays rather than as low as a match can be found.
TARGETED_REVIEW_SIMILARITY = 0.60


class InheritanceBranch(str, Enum):
    """Which of §11's three arrows was taken."""

    REUSE_MANIFEST = "REUSE_MANIFEST"    # similarity very high
    REUSE_EVIDENCE = "REUSE_EVIDENCE"    # similarity medium
    NEW_FAMILY = "NEW_FAMILY"            # no useful family


# ---- fingerprints as they come back out of the database ----

#: The attributes `StructuralFingerprint.as_dict()` writes, and the ones read
#: back. Named here so an unknown key raises instead of being ignored: a stored
#: fingerprint with a key this build does not understand was written by a newer
#: one, and matching against the half of it we recognise would produce a
#: similarity score that is quietly wrong rather than absent.
_FINGERPRINT_SET_FIELDS = ("mergefield_codes", "bracket_tokens")
_FINGERPRINT_TUPLE_FIELDS = ("table_shapes",)


def fingerprint_from_dict(data) -> StructuralFingerprint:
    """Rebuild a `StructuralFingerprint` from its stored JSON form.

    `as_dict` turns the frozensets into sorted lists and the tuple into a list,
    because JSON has neither; this puts them back, so a stored fingerprint and a
    freshly computed one compare identically rather than by luck of type.
    """
    if not isinstance(data, dict):
        raise TypeError(
            f"A stored fingerprint must be a mapping as written by "
            f"StructuralFingerprint.as_dict(), got {type(data).__name__}."
        )
    known = {f.name for f in dataclass_fields(StructuralFingerprint)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(
            "Unknown structural fingerprint keys: " + ", ".join(unknown)
            + f". Known keys are {', '.join(sorted(known))}."
        )
    payload = dict(data)
    for name in _FINGERPRINT_SET_FIELDS:
        if name in payload:
            payload[name] = frozenset(payload[name] or ())
    for name in _FINGERPRINT_TUPLE_FIELDS:
        if name in payload:
            payload[name] = tuple(payload[name] or ())
    return StructuralFingerprint(**payload)


@dataclass(frozen=True)
class FamilyRecord:
    """One candidate family, as the matcher needs it.

    `approved_manifest_id` is the field that decides whether this family can be
    inherited from at all. §11 says "nearest *approved* family", and the word is
    load-bearing: a family whose representative manifest is still a draft has no
    approved knowledge to lend, so structural closeness to it buys nothing.
    """

    family_id: str
    name: str
    fingerprint: StructuralFingerprint
    representative_template_version_id: str | None = None
    approved_manifest_id: str | None = None

    def __post_init__(self):
        if not str(self.family_id or "").strip():
            raise ValueError("A family record must carry a non-empty family_id.")
        if not isinstance(self.fingerprint, StructuralFingerprint):
            raise TypeError(
                f"Family {self.family_id!r} needs a StructuralFingerprint, got "
                f"{type(self.fingerprint).__name__}; read a stored one through "
                "fingerprint_from_dict()."
            )

    @classmethod
    def from_row(cls, row, *, approved_manifest_id: str | None = None) -> "FamilyRecord":
        """Read a `TemplateFamily` row (or anything shaped like one).

        Duck-typed so this module never imports the ORM, which is what lets the
        whole §11 workflow be tested without a database.
        """
        return cls(
            family_id=row.id,
            name=row.name,
            fingerprint=fingerprint_from_dict(row.fingerprint or {}),
            representative_template_version_id=row.representative_template_version_id,
            approved_manifest_id=approved_manifest_id,
        )


@dataclass(frozen=True)
class FamilyCandidate:
    """A family, scored against the template being onboarded."""

    family_id: str
    name: str
    similarity: float
    representative_template_version_id: str | None = None
    approved_manifest_id: str | None = None

    @property
    def is_approved(self) -> bool:
        return bool(self.approved_manifest_id)

    def as_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "name": self.name,
            "similarity": self.similarity,
            "representative_template_version_id": self.representative_template_version_id,
            "approved_manifest_id": self.approved_manifest_id,
        }


@dataclass(frozen=True)
class InheritanceDecision:
    """Which branch of §11 was taken, against which family, and why.

    `family_id` and `branch` answer two different questions and can disagree.
    The branch says what to do about the *manifest*; `family_id` says which
    family this template *belongs to*. A template that is structurally a member
    of a family whose manifest nobody has approved yet takes the NEW_FAMILY
    branch -- there is nothing to inherit -- while still joining that family
    rather than founding a second one for the same document type. `reason` names
    that case explicitly, because a decision the reader has to reconstruct from
    two fields is a decision that gets misread.
    """

    branch: InheritanceBranch
    reason: str
    similarity: float = 0.0
    family_id: str | None = None
    family_name: str | None = None
    parent_manifest_id: str | None = None
    considered: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "considered", tuple(self.considered))
        if not str(self.reason or "").strip():
            raise ValueError("An inheritance decision must say why it went the way it did.")
        if self.branch is not InheritanceBranch.NEW_FAMILY and not self.parent_manifest_id:
            raise ValueError(
                f"The {self.branch.value} branch reuses an approved manifest, so it must name "
                "the manifest it reuses."
            )

    @property
    def reuses_manifest(self) -> bool:
        """Whether the approved manifest is carried over object for object."""
        return self.branch is InheritanceBranch.REUSE_MANIFEST

    @property
    def reuses_evidence(self) -> bool:
        """Whether the family's mappings are evidence for a targeted review."""
        return self.branch is InheritanceBranch.REUSE_EVIDENCE

    @property
    def joins_existing_family(self) -> bool:
        return self.family_id is not None

    def as_dict(self) -> dict:
        return {
            "branch": self.branch.value,
            "reason": self.reason,
            "similarity": self.similarity,
            "family_id": self.family_id,
            "family_name": self.family_name,
            "parent_manifest_id": self.parent_manifest_id,
            "considered": [c.as_dict() for c in self.considered],
        }


def rank_candidates(fingerprint: StructuralFingerprint, families) -> tuple:
    """Every family scored against `fingerprint`, closest first.

    Ties break on family id so the ranking is stable across runs; a match that
    reorders itself between two calls makes an onboarding decision that cannot
    be explained afterwards.
    """
    if not isinstance(fingerprint, StructuralFingerprint):
        raise TypeError(
            f"Matching needs a StructuralFingerprint, got {type(fingerprint).__name__}."
        )
    scored = [
        FamilyCandidate(
            family_id=record.family_id,
            name=record.name,
            similarity=structural_similarity(fingerprint, record.fingerprint),
            representative_template_version_id=record.representative_template_version_id,
            approved_manifest_id=record.approved_manifest_id,
        )
        for record in families
    ]
    return tuple(sorted(scored, key=lambda c: (-c.similarity, c.family_id)))


def decide_inheritance(fingerprint: StructuralFingerprint, families) -> InheritanceDecision:
    """Run §11's workflow for one new template and say which arrow it took.

    `families` is a sequence of `FamilyRecord`, already filtered to this tenant
    by the caller -- this function has no way to check that, and §19's
    cross-tenant leakage risk is a query-layer concern, not a scoring one.
    """
    ranked = rank_candidates(fingerprint, families)
    nearest = ranked[0] if ranked else None
    approved = next((c for c in ranked if c.is_approved), None)

    if nearest is None:
        return InheritanceDecision(
            branch=InheritanceBranch.NEW_FAMILY,
            reason="This tenant has no template families yet, so there is nothing to inherit from.",
            considered=ranked,
        )

    # The family this template *belongs to*, whether or not it can lend a
    # manifest. Joining it means the next upload of the same document type finds
    # one family with a growing membership rather than a row per upload.
    home = nearest if nearest.similarity >= TARGETED_REVIEW_SIMILARITY else None

    if approved is None:
        return InheritanceDecision(
            branch=InheritanceBranch.NEW_FAMILY,
            reason=(
                f"The nearest family {nearest.name!r} scores {nearest.similarity:.2f}, but no "
                "family in this tenant has an approved manifest to inherit, so this template "
                "is compiled from scratch and its manifest becomes the family's first."
            ),
            similarity=nearest.similarity,
            family_id=home.family_id if home else None,
            family_name=home.name if home else None,
            considered=ranked,
        )

    if approved.similarity >= REUSE_MANIFEST_SIMILARITY:
        return InheritanceDecision(
            branch=InheritanceBranch.REUSE_MANIFEST,
            reason=(
                f"Structural similarity {approved.similarity:.2f} is at or above "
                f"{REUSE_MANIFEST_SIMILARITY:.2f}, so this is a revision of {approved.name!r} "
                "rather than a new document: its approved manifest is inherited whole and only "
                "the objects the diff reports as changed need review."
            ),
            similarity=approved.similarity,
            family_id=approved.family_id,
            family_name=approved.name,
            parent_manifest_id=approved.approved_manifest_id,
            considered=ranked,
        )

    if approved.similarity >= TARGETED_REVIEW_SIMILARITY:
        return InheritanceDecision(
            branch=InheritanceBranch.REUSE_EVIDENCE,
            reason=(
                f"Structural similarity {approved.similarity:.2f} sits between "
                f"{TARGETED_REVIEW_SIMILARITY:.2f} and {REUSE_MANIFEST_SIMILARITY:.2f}: close "
                f"enough that {approved.name!r}'s approved mappings are evidence for this "
                "template's objects, not close enough to carry them over unreviewed."
            ),
            similarity=approved.similarity,
            family_id=approved.family_id,
            family_name=approved.name,
            parent_manifest_id=approved.approved_manifest_id,
            considered=ranked,
        )

    return InheritanceDecision(
        branch=InheritanceBranch.NEW_FAMILY,
        reason=(
            f"The nearest approved family {approved.name!r} scores {approved.similarity:.2f}, "
            f"below the {TARGETED_REVIEW_SIMILARITY:.2f} floor at which reuse still saves a "
            "reviewer more than it costs them. Compiling this as a new family is cheaper than "
            "correcting mappings inherited from a different document."
        ),
        similarity=approved.similarity,
        family_id=home.family_id if home else None,
        family_name=home.name if home else None,
        considered=ranked,
    )


# ---- inheriting the manifest ----

#: Object attributes written by `inherit_manifest`, and read by everything
#: downstream that needs to tell an inherited object from a compiled one.
INHERITED_FROM = "inherited_from"
NEEDS_REVIEW = "needs_review"
INHERITANCE_NOTE = "inheritance_note"

#: The evidence tag §6's own worked FIELD example carries
#: (`"family_inheritance:0.98"`), and the name §13 gives the signal.
FAMILY_EVIDENCE_PREFIX = "family_inheritance:"

#: Types whose §6 contract makes an anchor mandatory. An inherited object of one
#: of these types that declares no anchor cannot be shown to address anything in
#: the new template, so it comes back for review rather than being carried over
#: on the strength of having been approved somewhere else.
ANCHOR_BEARING_TYPES = frozenset({
    "FIELD", "CONDITION", "SECTION", "TABLE_ROW", "HEADER", "FOOTER", "SIGNATURE",
})

_ANCHOR_FIELD_NAMES = frozenset(f.name for f in dataclass_fields(Anchor))


@dataclass(frozen=True)
class NewTemplateVersion:
    """The template version an approved manifest is being pointed at.

    Carries the inventory rather than a file path because re-resolving anchors
    is the point: without the new template's text there is no way to tell an
    object that still addresses something from one that does not, and an
    inheritance that skipped the check would hand a reviewer a manifest of
    anchors into a document that no longer contains them.
    """

    template_version_id: str
    inventory: TemplateInventory
    template_hash: str | None = None
    template_family_id: str | None = None
    organization_id: str | None = None

    def __post_init__(self):
        if not str(self.template_version_id or "").strip():
            raise ValueError("An inherited manifest must name the template version it targets.")
        if not isinstance(self.inventory, TemplateInventory):
            object.__setattr__(self, "inventory", TemplateInventory.of(self.inventory))


def _anchor_from_dict(data: dict) -> Anchor:
    """Rebuild an `Anchor` from `Anchor.as_dict()` output."""
    unknown = sorted(set(data) - _ANCHOR_FIELD_NAMES)
    if unknown:
        raise ValueError(
            "Unknown anchor keys: " + ", ".join(unknown)
            + f". Known keys are {', '.join(sorted(_ANCHOR_FIELD_NAMES))}."
        )
    return Anchor(**data)


def _resolve_one(data, inventory: TemplateInventory, label: str) -> str | None:
    """None when this anchor still resolves exactly once; the reason otherwise."""
    if not isinstance(data, dict) or "kind" not in data:
        return (
            f"Its {label} is stored as {data!r} rather than a §6 anchor, so it addresses a "
            "position rather than a reference and cannot be re-validated against a different "
            "template version."
        )
    try:
        anchor = _anchor_from_dict(data)
    except (TypeError, ValueError) as exc:
        return f"Its {label} could not be read as an anchor: {exc}"
    if anchor.kind == ANCHOR_STYLE_ID:
        # §6: colour and style are onboarding evidence, never the address. An
        # inherited object anchored that way was never production-safe, and
        # inheritance is not the place to start trusting it.
        return (
            f"Its {label} is a style_id, which §6 calls evidence rather than an address; it "
            "must be re-anchored before this object can be approved."
        )
    try:
        resolve(anchor, inventory)
    except AnchorError as exc:
        return f"Its {label} no longer resolves against the new template version: {exc}"
    return None


def _static_still_present(text_hash, inventory: TemplateInventory) -> str | None:
    """Whether the approved immutable text this STATIC object pins still exists."""
    if not isinstance(text_hash, str) or not text_hash.strip():
        return "It is a STATIC object with no text_hash, so nothing pins the text it approves."
    candidates = list(inventory.paragraph_texts) + [document_text(inventory.paragraph_texts)]
    if any(static_text_hash(text) == text_hash for text in candidates):
        return None
    return (
        "The approved static text it pins does not appear byte-for-byte in the new template "
        "version; §6 says a changed STATIC region forces a new template version rather than a "
        "re-approval."
    )


def _token_still_unique(token: str, inventory: TemplateInventory) -> str | None:
    """The legacy fallback: a positional slot that at least carries its token.

    §6's rule is that an address must resolve exactly once. A bare token is a
    weaker address than an anchor, but counting its occurrences answers the same
    question, and answering it badly is better than not asking -- the manifests
    this build compiled before anchors existed are addressed exactly this way.
    """
    occurrences = sum(text.count(token) for text in inventory.paragraph_texts)
    if occurrences == 1:
        return None
    if occurrences == 0:
        return (
            f"Its token {token!r} does not appear in the new template version, so there is "
            "nothing here for it to fill."
        )
    return (
        f"Its token {token!r} appears {occurrences} times in the new template version, so "
        "nothing can tell which occurrence the inherited mapping meant."
    )


def revalidation_note(obj, inventory: TemplateInventory) -> str | None:
    """Why this object needs review against the new template, or None.

    Checked in order of how strong the address is: a §6 anchor first, then an
    anchor range, then a STATIC text hash, then the legacy token. An object of a
    type §6 says must carry an anchor and which carries none at all is the last
    case, and it is not silently accepted.
    """
    attributes = obj.attributes
    if "anchor" in attributes:
        return _resolve_one(attributes["anchor"], inventory, "anchor")

    for key in ("anchor_range", "anchor_row"):
        if key in attributes:
            region = attributes[key]
            # Two spellings reach here: the constructor's own
            # `{"start": ..., "end": ...}` and `AnchorRange.as_dict()`'s
            # `{"from": ..., "to": ...}`. Both endpoints have to resolve, since
            # a region whose end has moved governs different paragraphs than the
            # one that was approved.
            for pair in (("start", "end"), ("from", "to")):
                if isinstance(region, dict) and set(pair) <= set(region):
                    for end_label in pair:
                        note = _resolve_one(region[end_label], inventory, f"{key} {end_label}")
                        if note:
                            return note
                    return None
            return _resolve_one(region, inventory, key)

    if obj.object_type.value == STATIC:
        return _static_still_present(attributes.get("text_hash"), inventory)

    token = attributes.get("token") or attributes.get("template_token")
    if isinstance(token, str) and token.strip():
        return _token_still_unique(token, inventory)

    if obj.object_type.value in ANCHOR_BEARING_TYPES:
        return (
            f"§6 requires a {obj.object_type.value} to carry an anchor, and this one carries "
            "neither an anchor nor a token, so there is no way to check that it still addresses "
            "anything in the new template version."
        )
    return None


def _inherit_object(obj, parent: ManifestEnvelope, inventory: TemplateInventory, similarity: float):
    """One approved object, carried over as a proposal against a new template."""
    attributes = dict(obj.attributes)

    # The approval belonged to the parent template's review, not this one.
    # Carrying the stamps across would make the audit trail claim a person
    # signed off a document they have never seen.
    attributes.pop("approved_by", None)
    attributes.pop("approved_at", None)
    if obj.object_type.value != STATIC:
        # §6: "Every non-STATIC object carries status APPROVED or an explicit
        # NOT_APPLICABLE rule" before a manifest can be locked. PROPOSED is what
        # review has to clear, and it is what inheritance produces -- §13's
        # bands decide what a reviewer actually has to look at.
        attributes["status"] = PROPOSED

    evidence = [str(e) for e in (attributes.get("evidence") or ())
                if not str(e).startswith(FAMILY_EVIDENCE_PREFIX)]
    # §13's family_inheritance signal is "scaled by measured family similarity",
    # so the measurement travels with the evidence rather than the reviewer
    # being told only that something was inherited.
    evidence.append(f"{FAMILY_EVIDENCE_PREFIX}{similarity:.2f}")
    attributes["evidence"] = evidence

    attributes[INHERITED_FROM] = {
        "manifest_id": parent.manifest_id,
        "manifest_version": parent.manifest_version,
        "template_version_id": parent.template_version_id,
        "template_family_id": parent.template_family_id,
        "object_id": obj.object_id,
        "similarity": similarity,
    }

    note = revalidation_note(obj, inventory)
    attributes[NEEDS_REVIEW] = note is not None
    if note is None:
        attributes.pop(INHERITANCE_NOTE, None)
    else:
        attributes[INHERITANCE_NOTE] = note

    return type(obj)(object_id=obj.object_id, object_type=obj.object_type, attributes=attributes)


def inherit_manifest(parent_manifest, new_template_version, *,
                     manifest_id: str, similarity: float) -> ManifestEnvelope:
    """Build the DRAFT manifest a new template inherits from an approved one.

    Every object of the parent is carried over -- none is dropped, including the
    ones whose anchors no longer resolve, which come back carrying
    `needs_review` and a note saying what broke. Dropping them would be the
    worst available outcome: the reviewer would see a shorter, cleaner manifest
    and no indication that a paragraph the parent filled is now unaddressed.

    The result is DRAFT at version 1. It is not a supersession: the parent
    belongs to a different template version and stays LOCKED and runnable, so
    `supersedes` is deliberately empty. `manifest_hash` is unset because a hash
    seals a manifest at lock time and this one has not been reviewed yet.
    """
    if not isinstance(parent_manifest, ManifestEnvelope):
        raise TypeError(
            f"The parent manifest must be a ManifestEnvelope, got "
            f"{type(parent_manifest).__name__}."
        )
    if not isinstance(new_template_version, NewTemplateVersion):
        raise TypeError(
            f"The target must be a NewTemplateVersion carrying the template's inventory, got "
            f"{type(new_template_version).__name__}."
        )
    if parent_manifest.status is not ManifestStatus.LOCKED:
        raise ValueError(
            f"Manifest {parent_manifest.manifest_id} is {parent_manifest.status.value}; §11 "
            "inherits from the nearest *approved* family, and inheriting from an unapproved "
            "manifest would spread mappings nobody has signed off across the whole family."
        )
    if not str(manifest_id or "").strip():
        raise ValueError("An inherited manifest needs its own manifest_id, minted by the store.")
    if not isinstance(similarity, (int, float)) or isinstance(similarity, bool):
        raise TypeError(f"similarity must be a number, got {type(similarity).__name__}.")
    if not 0.0 <= float(similarity) <= 1.0:
        raise ValueError(f"similarity is a 0..1 structural score, got {similarity!r}.")

    target_org = new_template_version.organization_id
    if target_org is not None and str(target_org) != parent_manifest.organization_id:
        # §19: "cross-tenant leakage via family matching -- contract breach;
        # potentially existential". This is the exact call that would do it.
        raise ValueError(
            f"Manifest {parent_manifest.manifest_id} belongs to organisation "
            f"{parent_manifest.organization_id} and cannot be inherited into a template owned "
            f"by {target_org}; family matching never crosses a tenant boundary."
        )

    similarity = float(similarity)
    inventory = new_template_version.inventory
    objects = tuple(
        _inherit_object(obj, parent_manifest, inventory, similarity)
        for obj in parent_manifest.objects
    )

    return ManifestEnvelope(
        manifest_id=str(manifest_id),
        manifest_version=1,
        status=ManifestStatus.DRAFT,
        organization_id=parent_manifest.organization_id,
        template_version_id=new_template_version.template_version_id,
        template_family_id=(new_template_version.template_family_id
                            or parent_manifest.template_family_id),
        template_hash=new_template_version.template_hash,
        # The source contract is what makes the mappings meaningful, so it is
        # inherited with them. If the new template reads a different extract,
        # that is a change a reviewer has to make deliberately rather than one
        # an empty pin hides.
        source_schema_ref=parent_manifest.source_schema_ref,
        source_schema_hash=parent_manifest.source_schema_hash,
        expression_lang=parent_manifest.expression_lang,
        renderer_contract=dict(parent_manifest.renderer_contract),
        # Objects are carried one for one, so the parent's computed closure is
        # exactly right for this draft. `lock()` recomputes it anyway, which is
        # what keeps it honest once review has changed something.
        required_source_fields=parent_manifest.required_source_fields,
        qa_policy=dict(parent_manifest.qa_policy),
        objects=objects,
        manifest_hash=None,
        approved_by=None,
        approved_at=None,
        supersedes=None,
    )


#: The note every object carries on the medium-similarity branch. Spelled out
#: rather than left implicit because the reviewer's question is always "why is
#: this in front of me", and "inherited from a template that is only mostly the
#: same one" is a different answer from "its anchor broke".
EVIDENCE_ONLY_NOTE = (
    "Inherited as evidence rather than as an approved mapping: the family match is in §11's "
    "medium band, so this object's source mapping is a strong suggestion that still needs "
    "confirming against this template."
)


def as_evidence_only(manifest: ManifestEnvelope) -> ManifestEnvelope:
    """§11's medium branch: the same carried objects, none of them waved through.

    "Reuse mapping evidence + targeted review" is not "reuse the manifest with a
    shorter review". At medium similarity the two templates are the same
    document type but not the same document, so an object that re-anchors
    cleanly has still only proved that a token exists in both -- which is
    evidence about the mapping, not confirmation of it. Every inherited object
    therefore comes back flagged; an object that failed re-validation keeps the
    sharper note explaining what actually broke.
    """
    objects = []
    for obj in manifest.objects:
        if not obj.attributes.get(INHERITED_FROM) or obj.attributes.get(NEEDS_REVIEW):
            objects.append(obj)
            continue
        attributes = dict(obj.attributes)
        attributes[NEEDS_REVIEW] = True
        attributes[INHERITANCE_NOTE] = EVIDENCE_ONLY_NOTE
        objects.append(type(obj)(object_id=obj.object_id, object_type=obj.object_type,
                                 attributes=attributes))
    return replace(manifest, objects=tuple(objects))


def objects_needing_review(manifest: ManifestEnvelope) -> tuple:
    """The inherited objects a reviewer has to look at before this can lock."""
    return tuple(o for o in manifest.objects if o.attributes.get(NEEDS_REVIEW))


def inheritance_summary(manifest: ManifestEnvelope) -> dict:
    """A countable summary of what inheritance produced.

    The two numbers a reviewer is deciding on: how much was carried over, and
    how much of it survived re-validation against the new template.
    """
    inherited = tuple(o for o in manifest.objects if o.attributes.get(INHERITED_FROM))
    flagged = objects_needing_review(manifest)
    return {
        "objects_total": len(manifest.objects),
        "objects_inherited": len(inherited),
        "objects_needing_review": len(flagged),
        "needs_review": [
            {"object_id": o.object_id, "object_type": o.object_type.value,
             "note": o.attributes.get(INHERITANCE_NOTE, "")}
            for o in flagged
        ],
    }


def with_family(manifest: ManifestEnvelope, family_id: str | None) -> ManifestEnvelope:
    """The same manifest, recorded as belonging to `family_id`.

    A separate function because `ManifestEnvelope` is frozen -- §6's lock
    semantics mean nothing about a manifest is edited in place, family
    membership included.
    """
    return replace(manifest, template_family_id=family_id)
