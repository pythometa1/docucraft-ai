"""Whether a template a person just wrote will actually work.

The gap this closes is narrow and expensive. `validate_manifest` decides whether
a manifest may be *approved*, and it runs at approval -- by which point the
template has been read, edited, reviewed and signed off, and a defect means all
of that again. `assertions` asks the document what is still wrong, but only
inside the compile loop, where the audience is a model rather than the person
who can actually fix it. Nothing ran while a template was being written.

So this module runs those same checks at authoring time, against the body in the
editor. It **calls them; it does not restate them.** A second copy of "a field
with no slot cannot be filled" is a second thing to keep in step, and the two
would disagree exactly when it mattered.

What it adds is the handful of findings the code already makes computable and
nobody surfaces. Each one is a real defect that survives approval today:

  * a condition reading an identifier no field supplies -- `validate_manifest`
    catches `condition_reads_nothing` but not this, so a clause keyed on
    `temporary_assignment` when nothing supplies it approves cleanly and then
    blocks every letter at the generation gate;
  * a conditional row whose block covers its whole table, which the fill engine
    empties and then removes -- measured on a compensation template as sixteen
    rows of somebody's salary breakdown;
  * two ids that slug to the same key, where column assignment is exclusive so
    the collision displaces a real pairing;
  * a block boundary the compiler admitted it guessed at.

Severity is `blocking`, `warning` or `advisory`, and only `blocking` stops a
publish. That split is the point: a template with warnings is a template someone
should look at, and one with blockers is a template that will produce wrong
letters. Conflating them trains people to ignore both.
"""

from dataclasses import dataclass, field as dataclass_field

from app.compiler import assertions as A
from app.compiler.rule_compiler import BOUNDARY_CONFIDENCE, _slug
from app.expressions.token_parser import condition_inputs
from app.manifests.validator import validate_manifest
from app.templates import blueprint as bp
from app.templates.semantic_model import (
    Anchor, ConditionObject, FieldObject, SectionObject, TemplateInventory, lock_blockers,
    source_field_name,
)

BLOCKING = "blocking"
WARNING = "warning"
ADVISORY = "advisory"

def template_inventory(body: dict) -> TemplateInventory:
    """What an anchor resolves against: the text, and the merge field codes.

    Passing bare paragraph texts is a caller mistake `semantic_model` refuses
    loudly rather than guessing at -- `None` means "you did not give me an
    inventory" and `()` means "this template has no merge fields", and collapsing
    them would turn a wiring bug into a template bug. It refused this linter
    first, on seven salary fields of a real offer letter.
    """
    codes = []
    for index, block, _in_table in bp.walk_paragraphs(body):
        for seg in block.get("segments") or ():
            if seg.get("role") == bp.MERGEFIELD and (seg.get("code") or "").strip():
                codes.append((index, seg["code"].strip()))
    return TemplateInventory(paragraph_texts=tuple(bp.paragraph_texts(body)),
                             mergefield_codes=tuple(codes))


#: Blockers `lock_blockers` reports that are not a reason to refuse a *draft*.
#: An object still in PROPOSED is the normal state of a template being written --
#: refusing to publish because nobody has approved the objects yet would make the
#: gate unpassable rather than useful. Approval is the manifest's own step.
_NOT_A_PUBLISH_BLOCKER = frozenset({"object_not_approved"})

#: Lock blockers that are worth saying and not worth refusing a publish over.
#:
#: An anchor is drift detection: it is how a manifest notices that the template
#: moved underneath it. The fill engine does not read anchors at all -- it fills
#: through `slots` -- so a template whose anchors are ambiguous still produces
#: correct letters today, and will merely fail to warn if somebody edits the
#: document later. Measured on a real offer letter: two fields sit in boilerplate
#: repeated verbatim at paragraphs 179 and 202, with identical text for sixty
#: characters either side, so nothing in the document can tell the two apart.
#: That is a limitation of addressing text by its surroundings, not a mistake the
#: author made, and there is no edit that would fix it -- which is the test for
#: whether a finding should block.
_ANCHOR_RULES = frozenset({"anchor_does_not_resolve_once"})

#: Handled here instead of taken from `lock_blockers`, because an `AnchorRange`
#: can only say which *paragraphs* an object claims and some objects are finer
#: than that. The two arms of an inline switch -- "for transactions initiated
#: with recruitment" against "without recruitment" -- are two spans of one
#: paragraph, and the compiler says so: both carry `start_span`/`end_span` and a
#: `boundary_method` of `inline_zone`, and the fill engine drops one by span
#: rather than by paragraph. Read at paragraph granularity they look like two
#: objects fighting over the same region, which is how a real offer letter
#: produced two blockers for the arrangement that is correct.
_RANGE_RULES = frozenset({"overlapping_anchor_ranges"})


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    detail: str
    object_id: str | None = None
    paragraph_index: int | None = None
    #: A typed operation that would resolve this, when one exists. The editor's
    #: "Apply fix" button, the co-pilot and an API caller all post the same
    #: payload, so there is one code path and one set of guards rather than three
    #: implementations of the same repair.
    fix: dict | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "detail": self.detail,
                "object_id": self.object_id, "paragraph_index": self.paragraph_index,
                "fix": self.fix}


@dataclass
class LintReport:
    findings: list = dataclass_field(default_factory=list)

    @property
    def blocking(self) -> list:
        return [f for f in self.findings if f.severity == BLOCKING]

    def can_publish(self, dispositions=()) -> bool:
        answered = set(dispositions or ())
        return not [f for f in self.blocking if f.code not in answered]

    def as_dict(self) -> dict:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "blocking": len(self.blocking),
            "can_publish": self.can_publish(),
        }


# ---- the legacy projection every existing check reads ----

def legacy_manifest(objects, *, delete_always=()) -> dict:
    """`{fields, conditions, blocks, delete_always}` from the stored objects.

    The shape `validate_manifest`, `assertions`, `orphaned_fields` and the fill
    engine all take. Built here rather than stored twice, so there is no way for
    the two to drift.
    """
    out = {"fields": [], "conditions": [], "blocks": [], "delete_always": list(delete_always)}
    column = {"FIELD": "fields", "CALCULATION": "fields", "CONDITION": "conditions",
              "SECTION": "blocks", "TABLE_ROW": "blocks"}
    for obj in objects or ():
        target = column.get(obj.get("object_type"))
        if target:
            entry = {k: v for k, v in obj.items() if k != "object_id"}
            entry["id"] = obj.get("object_id")
            out[target].append(entry)
    return out


# ---- §6 typed objects, built on demand ----

def typed_objects(body: dict, objects) -> tuple:
    """`(typed, findings)` -- the `semantic_model` classes for these bags.

    The typed classes are the linter's instrument, not the storage format. They
    refuse a great deal at construction, and each refusal is a real answer: a
    condition with no test case is a rule nobody has run, a field whose DEFAULT
    policy has no default renders nothing and calls it deliberate. Collecting
    those as findings rather than letting one raise is what keeps a single bad
    object from making the whole template unopenable.

    Anchor *ranges* are re-lifted from the paragraph range rather than rebuilt
    from the stored dict, because `AnchorRange.as_dict()` keeps only the two
    paths -- not the tokens, ordinals or context hashes an anchor needs. Which is
    the right way round anyway: the linter asks whether the template says what
    the objects claim *now*, and a re-lift against the current body is exactly
    that question.
    """
    from app.templates.semantic_model import lift_block_to_anchor_range

    texts = bp.paragraph_texts(body)
    typed, findings = [], []

    for obj in objects or ():
        object_id = obj.get("object_id")
        kind = obj.get("object_type")
        try:
            if kind == "FIELD":
                anchor = obj.get("anchor")
                if not anchor:
                    continue  # already reported as field_without_slot / unliftable
                typed.append(FieldObject(
                    object_id=object_id, status=obj.get("status") or "PROPOSED",
                    anchor=Anchor(**anchor), source_ref=obj.get("source_ref") or "",
                    format=obj.get("format"), on_missing=obj.get("on_missing") or "BLANK",
                    value_type=obj.get("value_type") or "string",
                    default=obj.get("default")))
            elif kind in ("CONDITION", "SECTION"):
                # The lift already walked a boundary that landed on a blank line
                # inward and recorded where it moved to. Re-lifting from the raw
                # range would rediscover the same empty paragraph and report a
                # template as unlockable that the lift had already repaired.
                adjusted = (obj.get("boundary_adjusted") or {}).get("to")
                if adjusted:
                    start, end = adjusted
                else:
                    start, end = obj.get("start_paragraph"), obj.get("end_paragraph")
                if not isinstance(start, int) or not isinstance(end, int):
                    continue  # reported at lift time as unanchorable
                anchor_range = lift_block_to_anchor_range(
                    {"id": object_id, "start_paragraph": start, "end_paragraph": end}, texts)
                if kind == "CONDITION":
                    typed.append(ConditionObject(
                        object_id=object_id, status=obj.get("status") or "PROPOSED",
                        anchor_range=anchor_range, expression=obj.get("expression") or "",
                        on_true=obj.get("on_true") or "KEEP",
                        on_false=obj.get("on_false") or "REMOVE_BLOCK",
                        test_cases=tuple(obj.get("test_cases") or ())))
                else:
                    typed.append(SectionObject(
                        object_id=object_id, status=obj.get("status") or "PROPOSED",
                        anchor_range=anchor_range, repeat_over=obj.get("repeat_over"),
                        ordering=obj.get("ordering"),
                        empty_behaviour=obj.get("empty_behaviour") or "REMOVE"))
        except (ValueError, TypeError) as exc:
            findings.append(Finding(
                "object_not_lockable", BLOCKING,
                f"{kind} {object_id!r} cannot be expressed as a manifest object: {exc}",
                object_id=object_id))

    return typed, findings


# ---- the findings nobody surfaces ----

def _supplied_names(objects) -> set:
    """Every name the source record will carry, from the objects that read one."""
    supplied = set()
    for obj in objects or ():
        if obj.get("object_type") in ("FIELD", "CALCULATION"):
            ref = obj.get("source_ref") or obj.get("object_id") or ""
            if ref:
                supplied.add(source_field_name(ref))
            if obj.get("object_id"):
                supplied.add(obj["object_id"])
    return supplied


def conditions_reading_unsupplied_identifiers(objects) -> list:
    """Names a condition needs that no field in the document fills.

    Advisory, and it took a real template to see why. `colleague_type ==
    'Full time'` reads a name that appears nowhere as a placeholder, and that is
    not a defect -- it is a **control column**: the source spreadsheet supplies
    it, `build_workbook` puts it in the workbook, and no letter ever prints it.
    Five of five conditions on a real offer letter are that case, so reporting it
    as a blocker would have made the gate unpassable on the template the whole
    engine was built against.

    What it is worth saying is what the source will have to carry, before
    somebody discovers it at the binding screen.
    """
    supplied = _supplied_names(objects)
    out = []
    for obj in objects or ():
        if obj.get("object_type") != "CONDITION":
            continue
        for name in condition_inputs(obj.get("expression") or "") or ():
            if source_field_name(name) in supplied:
                continue
            out.append(Finding(
                "condition_needs_a_source_column", ADVISORY,
                f"Condition {obj.get('object_id')!r} reads {name!r}, which nothing in the document "
                "fills, so the source spreadsheet has to carry a column for it.",
                object_id=obj.get("object_id")))
    return out


def _is_span_scoped(obj: dict) -> bool:
    """Whether this object claims spans of a paragraph rather than paragraphs."""
    return isinstance(obj.get("start_span"), int) and isinstance(obj.get("end_span"), int)


def overlapping_regions(objects) -> list:
    """Two paragraph-scoped objects claiming the same paragraph.

    §6's rule, minus the case it cannot express. Span-scoped blocks are excluded
    because their regions are finer than a paragraph and cannot be compared at
    this granularity -- see `_RANGE_RULES`.
    """
    ranged = [
        (obj.get("object_id"), obj.get("start_paragraph"), obj.get("end_paragraph"))
        for obj in objects or ()
        if obj.get("object_type") in ("SECTION", "TABLE_ROW") and not _is_span_scoped(obj)
        and isinstance(obj.get("start_paragraph"), int)
        and isinstance(obj.get("end_paragraph"), int)
    ]
    out = []
    for i, (left, left_start, left_end) in enumerate(ranged):
        for right, right_start, right_end in ranged[i + 1:]:
            if left_start <= right_end and right_start <= left_end:
                out.append(Finding(
                    "overlapping_anchor_ranges", BLOCKING,
                    f"Blocks {left!r} and {right!r} both claim paragraphs "
                    f"{max(left_start, right_start)} to {min(left_end, right_end)}. Dropping one "
                    "would take the other's content with it.",
                    object_id=left, paragraph_index=max(left_start, right_start)))
    return out


def duplicate_slugs(objects) -> list:
    """Two ids that normalise to the same key.

    Binding assigns each column to one field, so a collision does not merely
    duplicate -- it displaces. The slug is the compiler's own, NFKC-normalised,
    which is how a CJK master's fullwidth placeholders match their ASCII twins.
    """
    seen: dict = {}
    out = []
    for obj in objects or ():
        object_id = obj.get("object_id")
        if not object_id:
            continue
        key = _slug(str(object_id))
        if key in seen and seen[key] != object_id:
            out.append(Finding(
                "duplicate_slug", BLOCKING,
                f"{object_id!r} and {seen[key]!r} both normalise to {key!r}. Binding assigns a "
                "column to one field, so one of these would take the other's.",
                object_id=object_id,
                fix={"op": "rename_field", "id": object_id}))
        seen.setdefault(key, object_id)
    return out


def guessed_block_boundaries(objects) -> list:
    """Blocks whose end the compiler admitted it could not find.

    `BOUNDARY_CONFIDENCE` puts `lookahead_cap` at 0.5 -- "nothing said where this
    ends, so we stopped looking". Block-boundary resolution is the one genuinely
    hard sub-problem in the compile, and this is the compiler saying so about a
    specific block rather than in general.
    """
    out = []
    for obj in objects or ():
        method = obj.get("boundary_method")
        if not method or BOUNDARY_CONFIDENCE.get(method, 1.0) >= 1.0:
            continue
        out.append(Finding(
            "block_boundary_guessed", WARNING,
            f"Block {obj.get('object_id')!r} ends where it does because nothing in the template "
            f"said where it should ({method}). Confirm the range before publishing.",
            object_id=obj.get("object_id"),
            paragraph_index=obj.get("start_paragraph"),
            fix={"op": "set_block_end", "id": obj.get("object_id")}))
    return out


# `whole_table_deletions` was here, and is deliberately gone.
#
# It checked for a conditional block covering every paragraph of a table, on the
# grounds that the fill engine would empty the table and then remove it. That was
# true of an older renderer and is the damage its comments describe: sixteen rows
# of a salary breakdown, basic through gratuity, lost from a letter whose subject
# is the recipient's pay.
#
# `docx_renderer` now removes a dropped paragraph's *row* and removes a table only
# once it has no rows left, so the wholly-conditional case -- a Full Time
# remuneration table standing beside a Part Time one -- works by construction. The
# check therefore fired on exactly the arrangement that is correct, twice on a real
# offer letter, and blocked it. A check for a bug that has been fixed is worse than
# no check: it costs the author attention and teaches them the gate is wrong.


def fields_without_precedent(objects, dictionary=()) -> list:
    """Fields the organisation's vocabulary has never seen.

    Advisory, not a defect. It is the difference between a template that binds
    itself from the field dictionary and one that needs a mapping session, and it
    is worth knowing before publishing rather than at the binding screen.
    """
    known = {_slug(str(name)) for name in dictionary or ()}
    if not known:
        return []
    out = []
    for obj in objects or ():
        if obj.get("object_type") != "FIELD":
            continue
        object_id = obj.get("object_id")
        if object_id and _slug(str(object_id)) not in known:
            out.append(Finding(
                "field_not_in_dictionary", ADVISORY,
                f"{object_id!r} is not in this organisation's field dictionary, so it will need "
                "mapping by hand rather than binding itself.",
                object_id=object_id))
    return out


# ---- the whole report ----

def lint(body: dict, objects, *, emitted_path: str | None = None, dictionary=(),
         delete_always=()) -> LintReport:
    """Every reason this template would not work, and what would fix it.

    `emitted_path` is a `.docx` written from this body. When it is given the
    document itself is asked what is still wrong, through the same `assertions`
    the compile loop uses -- which is the strongest of these checks, because it
    reads the file rather than the description of it. When it is not, the
    structural checks still run, so an editor can lint on every keystroke and
    pay for the file only on save.
    """
    findings: list = []
    manifest = legacy_manifest(objects, delete_always=delete_always)

    # 1. What approval would refuse. Run now rather than at approval, when the
    #    template has already been read, edited and reviewed.
    for failure in validate_manifest(manifest):
        findings.append(Finding(failure.rule, BLOCKING, failure.detail,
                                object_id=failure.object_id))

    # 2. What the object model refuses to lock.
    typed, construction = typed_objects(body, objects)
    findings.extend(construction)
    for blocker in lock_blockers(typed, template=template_inventory(body)):
        if blocker.rule in _NOT_A_PUBLISH_BLOCKER:
            continue
        if blocker.rule in _RANGE_RULES:
            continue  # answered by `overlapping_regions`, which knows about spans
        severity = WARNING if blocker.rule in _ANCHOR_RULES else BLOCKING
        findings.append(Finding(blocker.rule, severity, blocker.detail,
                                object_id=blocker.object_id))

    # 3. What nobody surfaces.
    findings.extend(conditions_reading_unsupplied_identifiers(objects))
    findings.extend(duplicate_slugs(objects))
    findings.extend(overlapping_regions(objects))
    findings.extend(guessed_block_boundaries(objects))
    findings.extend(fields_without_precedent(objects, dictionary))

    # 4. What the document says, when there is a document to ask.
    if emitted_path:
        from app.templates.parsers.docx_prescan import prescan

        scan = prescan(emitted_path)
        faults, warnings = A.collect_with_warnings(scan, manifest)
        for fault in faults:
            findings.append(Finding(fault.check, BLOCKING, fault.detail,
                                    object_id=fault.object_id,
                                    paragraph_index=fault.paragraph_index))
        for warning in warnings:
            findings.append(Finding(
                warning.get("code") or "compiler_warning", WARNING,
                warning.get("message") or warning.get("detail") or "needs review",
                paragraph_index=warning.get("paragraph_index")))

    return LintReport(findings=findings)
