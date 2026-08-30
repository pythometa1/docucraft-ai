"""What a manifest must satisfy before it can be approved for production.

Approval used to be a status assignment: the endpoint set `status="approved"`
and wrote an audit line. Nothing checked that the thing being approved could
actually run -- a condition referencing a field no slot ever fills, a block id
that does not exist, two fields claiming the same id, an `on_missing` policy
spelled wrong. All of those compile, store and approve cleanly, and fail one
record at a time in production instead.

These are the rules that are computable from the manifest and the template as
they exist today. The architecture record lists more -- type-checking every
expression against a pinned source schema, a passing test case per expression,
anchors re-validated against a template hash -- and those need the schema
pinning and hashing that do not exist yet. Rules are added here as the material
to enforce them arrives; what must not happen is the set staying empty because
it cannot yet be complete.
"""

from dataclasses import dataclass

from app.qa.orphaned_fields import ORPHANED_FIELD, orphaned_fields
from app.generation.missing_policy import ON_MISSING_VALUES
from app.expressions.token_parser import condition_inputs, evaluate_condition


@dataclass(frozen=True)
class ValidationFailure:
    rule: str
    detail: str
    object_id: str | None = None

    def as_dict(self) -> dict:
        return {"rule": self.rule, "detail": self.detail, "object_id": self.object_id}


def _duplicate_ids(objects, label: str) -> list[ValidationFailure]:
    seen, failures = set(), []
    for obj in objects:
        oid = obj.get("id")
        if oid in seen:
            failures.append(ValidationFailure(
                "duplicate_id", f"More than one {label} carries the id {oid!r}.", oid,
            ))
        seen.add(oid)
    return failures


def validate_manifest(manifest: dict, *, warnings=None, dispositions=None) -> list[ValidationFailure]:
    """Every reason this manifest cannot be approved. Empty means it can be.

    `manifest` is the stored shape: fields, conditions, blocks, delete_always.
    """
    fields = manifest.get("fields") or []
    conditions = manifest.get("conditions") or []
    blocks = manifest.get("blocks") or []

    failures: list[ValidationFailure] = []

    # A compile that could not read the template is not a manifest with problems
    # -- it is the absence of a manifest, recorded. The row exists so a reviewer
    # can read the transcript and retry, and the one thing it must never do is
    # become approvable by having its warnings dispositioned away.
    if str(manifest.get("status") or "").strip().lower() == "failed":
        failures.append(ValidationFailure(
            "compile_failed",
            "This compile did not produce a reading of the template, so there is nothing to "
            "approve. See compile_transcript for the round it stopped on, and re-compile.",
            None,
        ))

    # A field whose every slot sits in a paragraph the compile deletes is the
    # same defect as a field with no slot at all, one step later: the compiler
    # found a name, placed it, and then guaranteed the placement could never
    # survive. It matters more than the no-slot case because it is invisible --
    # the field reaches the data template, somebody fills in a spreadsheet
    # column for it, and the value is silently dropped at generation. Measured
    # on a real client template: 8 of 26 fields, and the reviewer found out by
    # reading the finished letter.
    failures += [
        ValidationFailure(ORPHANED_FIELD, f.detail, None)
        for f in orphaned_fields(manifest)
    ]
    failures += _duplicate_ids(fields, "field")
    failures += _duplicate_ids(conditions, "condition")
    failures += _duplicate_ids(blocks, "block")

    block_ids = {b.get("id") for b in blocks}

    for f in fields:
        fid = f.get("id")
        if not fid:
            failures.append(ValidationFailure("field_without_id", "A field carries no id.", None))
            continue
        # A field with nowhere to go is the compiler telling you it found a name
        # it could not place. Approving it promises a value the letter never shows.
        if not f.get("slots"):
            failures.append(ValidationFailure(
                "field_without_slot",
                f"Field {fid!r} has no slot in the document, so its value would never appear.",
                fid,
            ))
        declared = str(f.get("on_missing") or "").strip().upper()
        if declared and declared not in ON_MISSING_VALUES:
            failures.append(ValidationFailure(
                "unknown_on_missing_policy",
                f"Field {fid!r} declares on_missing={f.get('on_missing')!r}; "
                f"expected one of {', '.join(ON_MISSING_VALUES)}.",
                fid,
            ))
        if declared == "DEFAULT" and f.get("default") is None:
            failures.append(ValidationFailure(
                "default_policy_without_default",
                f"Field {fid!r} falls back to a default that the manifest does not declare.",
                fid,
            ))

    for c in conditions:
        cid = c.get("id")
        expression = c.get("expression") or ""
        if not expression.strip():
            failures.append(ValidationFailure("condition_without_expression", f"Condition {cid!r} has no expression.", cid))
            continue
        # Parseability is checked by evaluating against a record that supplies
        # every referenced name, so the only way back is `unparseable`.
        probe = {name: "" for name in condition_inputs(expression)}
        if evaluate_condition(expression, probe).reason == "unparseable":
            failures.append(ValidationFailure(
                "condition_does_not_parse",
                f"Condition {cid!r} cannot be parsed: {expression!r}.",
                cid,
            ))
        if not condition_inputs(expression):
            failures.append(ValidationFailure(
                "condition_reads_nothing",
                f"Condition {cid!r} references no source field, so its verdict can never change.",
                cid,
            ))
        for referenced in c.get("keeps_blocks") or []:
            if referenced not in block_ids:
                failures.append(ValidationFailure(
                    "condition_targets_unknown_block",
                    f"Condition {cid!r} governs block {referenced!r}, which this manifest does not define.",
                    cid,
                ))

    for b in blocks:
        start, end = b.get("start_paragraph"), b.get("end_paragraph")
        if start is None or end is None:
            failures.append(ValidationFailure("block_without_range", f"Block {b.get('id')!r} has no paragraph range.", b.get("id")))
        elif end < start:
            failures.append(ValidationFailure(
                "block_range_inverted",
                f"Block {b.get('id')!r} ends at paragraph {end}, before it starts at {start}.",
                b.get("id"),
            ))

    # The compiler's warnings are not advisory. Each one names a paragraph a
    # human has to look at, and approval is exactly the moment to insist.
    answered = set(dispositions or {})
    for w in warnings or []:
        code = w.get("code")
        if code not in answered:
            where = w.get("paragraph_index")
            location = f" (paragraph {where})" if isinstance(where, int) and where >= 0 else ""
            failures.append(ValidationFailure(
                "unresolved_compiler_warning",
                f"{code}{location}: {w.get('message') or w.get('detail') or 'needs review'}",
                code,
            ))

    return failures
