"""What a template reading looks like from outside.

A stored manifest carries the engine's working: who or what read it
(`compiled_by`), how sure it was (`confidence`, per manifest and per item), the
model's reasoning for each item (`compiled_from`, `source_hint`), how a block's
edges were found (`boundary_method`), the colour counts the scan used, and the
whole review transcript. That is how the product works, and none of it is what a
customer needs to check a reading, fill it, or approve it.

So a manifest leaves the API through an allow-list, not a filter on the full
row: a column added to the table later stays private until somebody decides
otherwise. Items (fields, conditions, blocks) are copied with a small set of
internal keys removed, because the screens need everything else on them --
slots for highlighting, ids, types, the plain-English sentence.

The reverse direction matters as much. A reviewer's screen sends back what it
was given, so a PATCH would silently erase the internal keys it never saw;
`restore_internal` puts them back from the stored row before anything is saved.
"""

from __future__ import annotations

import copy

from app.expressions.plain_english import annotate_conditions

#: Keys removed from every field, condition and block, at any depth. The
#: model's reasoning, the scorer's numbers and the inheritance measurement.
INTERNAL_ITEM_KEYS = frozenset({
    "confidence", "source_hint", "compiled_from", "boundary_method",
    "evidence", "score", "band", "similarity", "weight", "weights",
})


def scrub(value):
    """A deep copy of `value` with every `INTERNAL_ITEM_KEYS` entry removed."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in INTERNAL_ITEM_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _restore_into(incoming: dict, stored: dict) -> None:
    for key, stored_value in stored.items():
        if key in INTERNAL_ITEM_KEYS:
            incoming.setdefault(key, copy.deepcopy(stored_value))
        elif isinstance(stored_value, dict) and isinstance(incoming.get(key), dict):
            _restore_into(incoming[key], stored_value)


def _names_internal_key(path) -> bool:
    return isinstance(path, str) and any(
        part.split("[")[0] in INTERNAL_ITEM_KEYS for part in path.split("."))


def public_diff(diff: dict | None) -> dict | None:
    """A manifest diff without the lines about internal keys.

    A diff names what changed by path ("inherited_from.similarity") and carries
    both values, so scrubbing keys is not enough: a change whose path touches an
    internal key is dropped whole. The counts stay as computed -- they count
    objects, and an object that changed is still an object that changed.
    """
    if diff is None:
        return None

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in INTERNAL_ITEM_KEYS}
        if isinstance(value, list):
            return [clean(v) for v in value
                    if not (isinstance(v, dict) and _names_internal_key(v.get("field")))]
        return value

    return clean(diff)


def restore_internal(incoming: list | None, stored: list | None, *, id_key: str = "id") -> list | None:
    """`incoming` with the internal keys the client never saw put back.

    Matched on `id_key`. An item the client added has no stored twin and keeps
    only what it was sent; an item the client removed is simply gone.
    """
    if incoming is None:
        return None
    by_id = {item.get(id_key): item for item in (stored or []) if isinstance(item, dict)}
    out = []
    for item in incoming:
        if isinstance(item, dict) and item.get(id_key) in by_id:
            item = dict(item)
            _restore_into(item, by_id[item.get(id_key)])
        out.append(item)
    return out


def document_summary(prescan_summary: dict | None) -> dict:
    """Counts a reviewer can check against their own document, in its words.

    The scan counts runs by colour; a customer knows their template as
    placeholders, instructions and Word fields, so the same numbers are
    published under those names and nothing else from the scan is.
    """
    ps = prescan_summary or {}

    def count(key):
        value = ps.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {
        "paragraph_count": count("paragraph_count"),
        "placeholder_count": count("blue_spans"),
        "instruction_count": count("red_spans"),
        "word_field_count": count("mergefields"),
    }


#: The one failure sentence written for a customer at the source: no AI is
#: configured. It already carries the neutral wording from `LLM_NOT_CONFIGURED_MESSAGE`.
_USER_FACING_PREFIXES = ("No AI is available",)

GENERIC_READ_FAILURE = (
    "This template could not be read completely. Check that its placeholders and "
    "conditional sections are written consistently, then read it again."
)


def public_failure_reason(prescan_summary: dict | None) -> str:
    """Why a reading failed, without the pipeline that failed.

    The stored note names chunks, review rounds and the model; the reader needs
    to know it failed and what to try, so only a note written for them passes
    through.
    """
    notes = [n for n in (prescan_summary or {}).get("notes") or [] if isinstance(n, str)]
    first = notes[0].strip() if notes else ""
    if first.startswith(_USER_FACING_PREFIXES):
        return first
    return GENERIC_READ_FAILURE


def public_manifest(m) -> dict:
    """The allow-listed shape every manifest endpoint returns."""
    return {
        "id": m.id,
        "template_file_id": m.template_file_id,
        "template_version_id": m.template_version_id,
        "version_no": m.version_no,
        "status": m.status,
        "fields": scrub(m.fields or []),
        # §7: the approver signs the meaning, not the syntax. Rendered from the
        # stored expression on every read so it cannot drift from what will run.
        "conditions": scrub(annotate_conditions(m.conditions or [])),
        "blocks": scrub(m.blocks or []),
        "document_summary": document_summary(m.prescan_summary),
        "failure_reason": public_failure_reason(m.prescan_summary) if m.status == "failed" else None,
        "created_at": m.created_at,
        "approved_by": m.approved_by,
        "approved_at": m.approved_at,
        # The reviewer's job is these two. `code` stays: a disposition is keyed
        # on it.
        "warnings": m.warnings or [],
        "warning_dispositions": m.warning_dispositions or {},
    }


# ---- template families ----

#: The inheritance branch in plain words. The engine's enum names the method
#: ("reuse evidence"); a customer only needs to know what happened.
REUSED, REVIEW, NEW = "reused", "review", "new"

_REASON = {
    REUSED: "Matches an approved template closely; its reading was reused.",
    REVIEW: "Similar to an approved template; used as a starting point for review.",
    NEW: "No approved template is close enough to reuse, so this one will be read on its own.",
}


def public_decision(decision, *, outcome: str) -> dict:
    """An inheritance decision with no similarity figure and no threshold.

    `outcome` is what the endpoint actually did, which an explicit parent can
    make differ from the branch the matcher chose.
    """
    return {
        "outcome": outcome,
        "reason": _REASON[outcome],
        "family_id": decision.family_id,
        "family_name": decision.family_name,
        "parent_manifest_id": decision.parent_manifest_id,
        "considered": [
            {"family_id": c.family_id, "name": c.name,
             "approved_manifest_id": c.approved_manifest_id}
            for c in decision.considered
        ],
    }

