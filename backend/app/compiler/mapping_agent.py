"""Compile → test-fill → read the failures → revise. Once per template family.

This is the only agentic loop in the system, and it is here rather than at
generation time for one reason: it has a verifiable success signal. The fill
engine's QA gates already report leftover placeholders, leftover MERGEFIELD
codes, and leftover instruction prose — objective ground truth to optimise
against. Without that an "agent" is just an LLM call in a `for` loop.

At generation time the opposite holds: a free-running agent would mean
non-determinism and variable cost per document, which is exactly what a
regulated document factory must not have. So generation stays deterministic and
the iteration happens once, at onboarding, where it makes review-by-exception
viable across thousands of templates.

The test fill also reports what its *binding* got wrong, not only what the fill
got wrong. A letter can pass every QA gate with a salary drawn from a column of
prose: the placeholder is filled, nothing is left over, and the document is
wrong. Those are the mappings §13 vetoes, and an onboarding log that recorded
only "QA passed" would send them on to a reviewer with nothing marking them.
"""

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field

from app.compiler import confidence as cf
from app.generation.source_resolver import apply_binding, suggest_bindings
from app.templates.parsers.docx_prescan import PreScanResult, prescan
from app.generation.docx_renderer import fill_template
from app.llm.provider import get_llm_provider, llm_configured
from app.compiler.llm_compiler import compile_manifest_llm, choose_compiler, rules_fell_short
from app.compiler.rule_compiler import CompiledManifest, compile_manifest, refine_with_llm

log = logging.getLogger(__name__)

MAX_ITERATIONS = 3

REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "block_corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "end_paragraph": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["block_id", "end_paragraph", "reason"],
                "additionalProperties": False,
            },
        },
        "delete_paragraphs": {"type": "array", "items": {"type": "integer"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["block_corrections", "delete_paragraphs", "notes"],
    "additionalProperties": False,
}

REVISE_SYSTEM = (
    "You are fixing a compiled document-template manifest that failed its quality gates. "
    "You are given the QA failures from a real test fill and the manifest that produced "
    "them. Leftover placeholder brackets usually mean a field slot was missed or its block "
    "boundary is wrong; leftover instruction text means a paragraph that should have been "
    "deleted was kept. Propose the smallest change that clears the failure. Return empty "
    "arrays if you cannot identify a confident fix -- a wrong correction is worse than an "
    "unresolved warning a human will see."
)


@dataclass
class Iteration:
    number: int
    qa_passed: bool
    qa_notes: list = field(default_factory=list)
    unresolved_fields: list = field(default_factory=list)
    action: str = ""
    #: Mappings §13 refuses to clear, as "field -> column: veto". Separate from
    #: `qa_notes` because a vetoed mapping usually passes QA -- the placeholder
    #: is filled, and the value is simply the wrong one.
    binding_vetoes: list = field(default_factory=list)


@dataclass
class AgenticResult:
    manifest: CompiledManifest
    iterations: list = field(default_factory=list)
    converged: bool = False

    def log(self) -> list[dict]:
        return [asdict(i) for i in self.iterations]


#: The one veto the onboarding log stays quiet about. Nothing has precedent on a
#: template nobody has onboarded yet, so it fires on every candidate of every
#: template and would bury the vetoes that describe a defect in the mapping
#: itself. It is not suppressed anywhere a human approves a mapping -- see the
#: binding-suggestions endpoint, where it is exactly the point.
_COLD_START_VETO = cf.VETO_NO_PRECEDENT


def _binding_vetoes(plan) -> list[str]:
    """Proposed mappings §13 will not clear, in the order a human reads them."""
    out = []
    for suggestion in plan.suggestions:
        blocking = [v for v in suggestion.vetoes if v != _COLD_START_VETO]
        if suggestion.column and blocking:
            out.append(f"{suggestion.field_id} -> {suggestion.column}: {', '.join(blocking)}")
    return sorted(out)


def _test_fill(template_path: str, manifest: CompiledManifest, records: list[dict], columns: list[str]):
    """Fill against real sample rows in a temp dir; persist nothing."""
    manifest_dict = {
        "fields": manifest.fields, "conditions": manifest.conditions,
        "blocks": manifest.blocks, "delete_always": manifest.delete_always,
    }
    # The sample rows are passed so the type gate reads the columns' real
    # values: a spreadsheet stores every cell as text, and a mapping that has
    # never been type-checked is the one that puts a name where an amount goes.
    plan = suggest_bindings(manifest_dict, columns, records=records)
    bindings = plan.as_field_bindings()

    notes: list[str] = []
    unresolved: set[str] = set()
    passed = True
    with tempfile.TemporaryDirectory() as tmp:
        for record in records:
            out = os.path.join(tmp, f"probe_{record.get('_row_index', 0)}.docx")
            try:
                result = fill_template(template_path, out, manifest_dict, apply_binding(record, bindings))
            except Exception as exc:
                passed = False
                # Logged in full; the notes are returned to the client.
                log.warning("Probe fill raised during mapping", exc_info=exc)
                notes.append("Filling a sample row failed before it produced a document.")
                continue
            if not result.qa_passed:
                passed = False
                notes.extend(result.qa_notes)
            filled = {entry["field_id"] for entry in result.field_lineage if entry.get("value")}
            unresolved.update(f["id"] for f in manifest.fields if f["id"] not in filled)
    return passed, sorted(set(notes)), sorted(unresolved), _binding_vetoes(plan)


def _escalate_if_rules_missed_the_logic(scan: PreScanResult, manifest: CompiledManifest, paragraph_texts: list[str], *, llm_policy=None, evidence=None) -> CompiledManifest:
    """Hand a template to the model when the rules plainly could not read it.

    Shared by both compile entry points so the Studio's "Compile" and
    "Compile & self-verify" buttons cannot disagree about which templates the
    rule layer is competent for.

    With no model configured this deliberately keeps the rule manifest rather
    than raising -- its fields are real and useful -- but drops the confidence
    and records why, because a manifest reporting 50 fields and 0 conditions
    would otherwise look like a clean compile of a template that has fifteen
    conditional blocks.
    """
    reason = rules_fell_short(scan, manifest)
    if not reason:
        return manifest

    if not llm_configured("compile"):
        manifest.confidence = min(manifest.confidence, 0.3)
        manifest.notes = [*(manifest.notes or []), f"{reason.rsplit('.', 2)[0]}. No model is configured to read them, so its conditional logic is missing from this manifest."]
        return manifest

    escalated = compile_manifest_llm(scan, paragraph_texts, llm_policy=llm_policy, evidence=evidence)
    if not escalated.conditions and manifest.fields:
        # The model did no better -- it may have failed outright. Keep the rule
        # result, which at least has the fields, and carry its reason through:
        # discarding it left "0 conditions" looking like a considered answer
        # rather than a call that errored or was truncated.
        manifest.confidence = min(manifest.confidence, 0.3)
        why = " ".join(escalated.notes) if escalated.notes else "It returned no conditions."
        manifest.notes = [*(manifest.notes or []), f"{reason} {why} This template still needs a human."]
        return manifest

    escalated.notes = [*(escalated.notes or []), reason]
    return escalated


def compile_agentic(
    template_path: str,
    sample_records: list[dict] | None = None,
    columns: list[str] | None = None,
    max_iterations: int = MAX_ITERATIONS,
    *,
    llm_policy=None,
    evidence=None,
) -> AgenticResult:
    """Compile, verify against real data, revise, repeat.

    Without sample rows there is nothing to verify against, so this degrades to
    a single compile pass rather than pretending to iterate.
    """
    scan: PreScanResult = prescan(template_path)
    paragraph_texts = _paragraph_texts(scan)

    if choose_compiler(scan) == "rules":
        manifest = compile_manifest(scan)
        manifest = _escalate_if_rules_missed_the_logic(scan, manifest, paragraph_texts, llm_policy=llm_policy, evidence=evidence)
        if manifest.compiled_by.startswith("rule"):
            manifest = refine_with_llm(manifest, "", paragraphs=paragraph_texts, llm_policy=llm_policy)
    else:
        manifest = compile_manifest_llm(scan, paragraph_texts, llm_policy=llm_policy, evidence=evidence)

    result = AgenticResult(manifest=manifest)
    if not sample_records or not columns:
        result.iterations.append(Iteration(1, True, action="compiled (no sample data to verify against)"))
        return result

    # Verification is deterministic (fill + QA gates), so the first iteration is
    # worth running with or without a model; only the revision step needs one.
    can_revise = llm_configured("compile")
    provider = get_llm_provider("Agentic revision", policy=llm_policy) if can_revise else None
    for iteration in range(1, max_iterations + 1):
        passed, notes, unresolved, binding_vetoes = _test_fill(
            template_path, manifest, sample_records, columns
        )
        step = Iteration(iteration, passed, notes, unresolved, binding_vetoes=binding_vetoes)

        if passed:
            step.action = "QA passed"
            result.iterations.append(step)
            result.converged = True
            break
        if not can_revise:
            step.action = "cannot revise: no language model configured -- handing to human review"
            result.iterations.append(step)
            break
        if iteration == max_iterations:
            step.action = "iteration limit reached"
            result.iterations.append(step)
            break

        revision = provider.structured(
            system=REVISE_SYSTEM,
            prompt=(
                f"QA FAILURES:\n{chr(10).join(notes)}\n\n"
                f"UNRESOLVED FIELDS: {unresolved}\n\n"
                f"BLOCKS: {[{k: b[k] for k in ('id', 'start_paragraph', 'end_paragraph')} for b in manifest.blocks]}\n\n"
                f"PARAGRAPHS:\n" + "\n".join(f"[{i}] {t}" for i, t in enumerate(paragraph_texts) if t.strip())[:20000]
            ),
            schema=REVISE_SCHEMA,
            purpose="compile",
        )
        if revision.data is None:
            step.action = f"could not revise ({revision.error})"
            result.iterations.append(step)
            break

        applied = _apply_revision(manifest, revision.data)
        step.action = f"applied {applied} correction(s)"
        result.iterations.append(step)
        if applied == 0:
            break

    manifest.notes = [*(manifest.notes or []), *[f"iteration {i.number}: {i.action}" for i in result.iterations]]
    return result


def _paragraphs_carrying_a_field(manifest: CompiledManifest) -> set[int]:
    """Every paragraph some field has a slot in."""
    return {
        slot.get("paragraph_index")
        for f in manifest.fields
        for slot in (f.get("slots") or [])
        if isinstance(slot.get("paragraph_index"), int)
    }


def _apply_revision(manifest: CompiledManifest, revision: dict) -> int:
    applied = 0
    by_id = {b["id"]: b for b in manifest.blocks}
    for correction in revision.get("block_corrections", []):
        block = by_id.get(correction["block_id"])
        if block and correction["end_paragraph"] >= block["start_paragraph"]:
            block["end_paragraph"] = correction["end_paragraph"]
            block["boundary_method"] = "agent_revised"
            applied += 1

    # A deletion may not remove a paragraph that carries a field.
    #
    # This schema offers exactly two moves -- widen or narrow a block, or delete
    # a paragraph -- and neither of them is "add the field that is missing". So
    # when the QA failure is a placeholder nobody claimed, the only move
    # available is deleting the line it sits on, and the model takes it. On a
    # real payroll notification that removed the Legal Entity and Fixed Term rows
    # from the letter outright, label and value together, to clear a warning
    # about a bracket. The gate caught it one iteration later as
    # `resolved_value_absent` -- but by then the manifest said to delete content
    # a document needs, and the failure had moved from a visible `<Yes/No>` a
    # reviewer would spot to a paragraph that is simply not there.
    #
    # Refusing here rather than catching it afterwards keeps the loop honest: an
    # unclaimed placeholder stays unclaimed, stays visible, and stays a fault
    # somebody has to answer.
    protected = _paragraphs_carrying_a_field(manifest)
    existing = {(d["paragraph_index"], d.get("span_index")) for d in manifest.delete_always}
    for paragraph_index in revision.get("delete_paragraphs", []):
        if paragraph_index in protected:
            continue
        if (paragraph_index, None) not in existing:
            manifest.delete_always.append({"paragraph_index": paragraph_index, "span_index": None})
            applied += 1
    return applied


def _paragraph_texts(scan: PreScanResult) -> list[str]:
    texts = [""] * len(scan.paragraphs)
    for span in scan.spans:
        if span.paragraph_index < len(texts):
            texts[span.paragraph_index] += span.text
    return texts
