"""The writer/reviewer loop, and the guarantee that holds it together.

One property matters more than the rest and is asserted from several directions:
**a manifest is never returned as usable while the document still objects to it.**
The reviewer is a model marking work a model produced, so its verdict cannot be
the thing the loop trusts. The deterministic assertions are, and a reviewer
saying "approved" over an outstanding fault must not be enough.

The rest is failure handling. Every way this can go wrong -- no key, a writer
that errors, a reviewer that errors, a loop that stops improving -- has to end in
a recorded failure rather than a plausible-looking manifest, because that
substitution is the whole thing this compile path was rewritten to remove.
"""

from __future__ import annotations

import docx
import pytest

from app.compiler import agentic_compiler as AC
from app.compiler import llm_compiler as LC
from app.llm.provider import LLMNotConfiguredError, ResidencyUnscoped

PARAS = ["Dear <Name>,", "Your role is <Role>.", "Yours sincerely,"]


@pytest.fixture
def template(tmp_path):
    document = docx.Document()
    for text in PARAS:
        document.add_paragraph(text)
    path = tmp_path / "t.docx"
    document.save(str(path))
    return str(path)


def _occ(index, text):
    return {"paragraph_index": index, "match_text": text}


def _field(fid, index, text, type_="string"):
    return {"id": fid, "type": type_, "required": True, "reason": "r", "occurrences": [_occ(index, text)]}


FULL_READING = {
    "fields": [_field("name", 0, "<Name>"), _field("role", 1, "<Role>")],
    "conditions": [], "scaffolding_paragraphs": [], "language": "en", "notes": [],
}
PARTIAL_READING = {**FULL_READING, "fields": [_field("name", 0, "<Name>")]}


class _Result:
    def __init__(self, data, model="stub", error=None):
        self.data, self.model, self.error = data, model, error


def _review(verdict="approved", **extra):
    empty = {
        key: [] for key in (
            "fields_to_add", "fields_to_remove", "fields_to_retype", "fields_to_relocate",
            "conditions_to_add", "conditions_to_remove", "conditions_to_rewrite",
            "scaffolding_add", "scaffolding_remove", "notes",
        )
    }
    return _Result({**empty, **extra, "verdict": verdict})


ADD_ROLE = _review("needs_more_work", fields_to_add=[{
    "id": "role", "type": "string", "occurrences": [_occ(1, "<Role>")],
    "reason": "paragraph 1 carries <Role> and no field claims it",
}])


@pytest.fixture
def run(monkeypatch):
    """Drive the loop with scripted writer and reviewer replies."""
    def _run(template_path, writes, reviews, **kwargs):
        calls = {"write": 0, "review": 0}

        class _Provider:
            def structured(self, *, system, prompt, schema, purpose="generate"):
                if "You compile document templates" in system:
                    index = min(calls["write"], len(writes) - 1)
                    calls["write"] += 1
                    return writes[index]
                index = min(calls["review"], len(reviews) - 1)
                calls["review"] += 1
                return reviews[index]

            def generate(self, **kwargs):
                raise AssertionError("generate is not part of compiling")

        monkeypatch.setattr(AC, "get_llm_provider", lambda *a, **k: _Provider())
        monkeypatch.setattr(LC, "get_llm_provider", lambda *a, **k: _Provider())
        return _run_and_count(template_path, kwargs, calls)

    def _run_and_count(path, kwargs, calls):
        outcome = AC.compile_template(path, **kwargs)
        return outcome, calls

    return _run


def test_a_clean_reading_converges_without_corrections(template, run):
    outcome, calls = run(template, [_Result(FULL_READING)], [_review()])

    assert outcome.ok
    assert {f["id"] for f in outcome.manifest.fields} == {"name", "role"}
    assert calls["review"] == 0, "there was nothing to review; do not pay for a round"


def test_the_reviewer_fixes_what_the_writer_missed(template, run):
    outcome, calls = run(template, [_Result(PARTIAL_READING)], [ADD_ROLE, _review()])

    assert outcome.ok
    assert {f["id"] for f in outcome.manifest.fields} == {"name", "role"}
    assert calls["review"] == 1


def test_an_approved_verdict_cannot_release_a_manifest_with_faults(template, run):
    """The guarantee. The reviewer is a model marking a model's work; the
    document is what decides."""
    outcome, _calls = run(template, [_Result(PARTIAL_READING)], [_review("approved")])

    assert not outcome.ok
    assert outcome.manifest.fields == []
    assert "<Role>" in outcome.reason


def test_a_loop_that_stops_improving_gives_up_rather_than_running_to_the_ceiling(template, run, monkeypatch):
    monkeypatch.setattr(AC.settings, "compile_max_rounds", 50)
    outcome, calls = run(template, [_Result(PARTIAL_READING)], [_review("needs_more_work")])

    assert not outcome.ok
    assert outcome.manifest.compiled_by == "llm_unconverged"
    assert calls["review"] == AC.NON_PROGRESS_LIMIT


def test_a_writer_that_fails_produces_a_recorded_failure_not_a_manifest(template, run):
    outcome, _calls = run(template, [_Result(None, error="rate limited")], [_review()])

    assert not outcome.ok
    assert outcome.manifest.fields == []
    assert outcome.manifest.compiled_by == "llm_failed"
    assert "rate limited" in outcome.reason


def test_a_reviewer_that_fails_produces_a_recorded_failure(template, run):
    outcome, _calls = run(template, [_Result(PARTIAL_READING)], [_Result(None, error="timeout")])

    assert not outcome.ok
    assert outcome.manifest.fields == []
    assert "timeout" in outcome.reason


def test_no_model_configured_is_a_failed_compile_not_an_exception(template, monkeypatch):
    """It used to return the rule-based manifest, which reported fields and a
    confidence and was indistinguishable from a compile that worked."""
    def _boom(*args, **kwargs):
        raise LLMNotConfiguredError("Compiling", "openai")

    monkeypatch.setattr(AC, "get_llm_provider", _boom)
    outcome = AC.compile_template(template, llm_policy=object())

    assert not outcome.ok
    assert outcome.manifest.compiled_by == "llm_unavailable"
    assert outcome.manifest.fields == []


def test_a_residency_error_still_raises(template, monkeypatch):
    """Not a property of the template: a call site that forgot the tenant is a
    bug to fix, not a compile to record against this document."""
    def _boom(*args, **kwargs):
        raise ResidencyUnscoped("no tenant named")

    monkeypatch.setattr(AC, "get_llm_provider", _boom)
    with pytest.raises(ResidencyUnscoped):
        AC.compile_template(template, llm_policy=None)


def test_the_transcript_records_how_the_answer_was_reached(template, run):
    outcome, _calls = run(template, [_Result(PARTIAL_READING)], [ADD_ROLE, _review()])
    transcript = outcome.transcript_dicts()

    assert transcript[0]["stage"] == "scan"
    assert transcript[0]["chunks"] == 1
    rounds = [entry for entry in transcript if "round" in entry]
    assert rounds and rounds[0]["corrections_applied"] == 1
    assert rounds[-1]["assertion_count"] == 0


def test_a_test_fill_failure_reaches_the_reviewer_and_blocks_release(template, run):
    """The strongest assertion available: fill the manifest and run the gates."""
    outcome, calls = run(
        template, [_Result(FULL_READING)], [_review("approved")],
        test_fill=lambda manifest: ["Leftover placeholder brackets: ['Yes/No']"],
    )
    assert not outcome.ok
    # The stub reports the same failure every round, so the loop correctly stops
    # on non-progress rather than on the reviewer's say-so.
    assert calls["review"] == AC.NON_PROGRESS_LIMIT, "the fill failure must reach the reviewer"
    assert "Yes/No" in outcome.reason


def test_a_test_fill_that_raises_is_a_finding_not_a_crash(template, run):
    def _explode(manifest):
        raise ValueError("template is corrupt")

    outcome, _calls = run(
        template, [_Result(FULL_READING)], [_review("approved")], test_fill=_explode,
    )
    assert not outcome.ok
    assert "template is corrupt" in outcome.reason


def test_readings_from_several_chunks_are_merged_on_id(template, run, monkeypatch):
    """Chunks overlap, so the same field arrives more than once; occurrences
    union rather than the second reading replacing the first."""
    monkeypatch.setattr(AC.settings, "compile_chunk_budget_chars", 20)

    first = {**FULL_READING, "fields": [_field("name", 0, "<Name>")]}
    second = {**FULL_READING, "fields": [
        {**_field("name", 0, "<Name>"), "type": "string"},
        _field("role", 1, "<Role>"),
    ]}
    outcome, _calls = run(
        template, [_Result(first), _Result(second), _Result(second)],
        [_review(), _review()],
    )
    assert outcome.ok
    assert {f["id"] for f in outcome.manifest.fields} == {"name", "role"}


# ---- the schemas themselves -------------------------------------------------

def _object_schemas(schema, path="root"):
    """Every object schema nested anywhere inside, with a readable path."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        yield path, schema
        for key, value in (schema.get("properties") or {}).items():
            yield from _object_schemas(value, f"{path}.{key}")
    if schema.get("type") == "array":
        yield from _object_schemas(schema.get("items") or {}, f"{path}[]")


@pytest.mark.parametrize("name", ["COMPILE_SCHEMA", "REVIEW_SCHEMA", "RECONCILE_SCHEMA"])
def test_every_structured_output_schema_is_strict_mode_compliant(name):
    """Strict structured outputs have no optional properties: `required` must
    name every key in `properties`, and `additionalProperties` must be false.

    This is not a style rule. A schema that breaks it is accepted locally and
    rejected by the provider with an HTTP 400 at call time -- which is how it was
    found: two of five templates in a live acceptance run failed to compile, on
    the review step, for a reason nothing in the test suite could see.
    """
    schema = getattr(AC, name, None) or getattr(LC, name)

    for path, obj in _object_schemas(schema, name):
        properties = sorted(obj.get("properties") or {})
        required = sorted(obj.get("required") or [])
        assert properties == required, (
            f"{path}: properties {properties} but required {required}; "
            f"strict mode rejects optional properties"
        )
        assert obj.get("additionalProperties") is False, f"{path}: additionalProperties must be false"


def test_a_correction_naming_a_field_that_does_not_exist_is_not_progress(template, run):
    """An applied-count that includes no-ops lets a stalled loop run to its
    ceiling instead of stopping."""
    nonsense = _review("needs_more_work", fields_to_remove=[{"id": "nope", "reason": "x"}])
    outcome, calls = run(template, [_Result(PARTIAL_READING)], [nonsense])

    assert not outcome.ok
    assert calls["review"] == AC.NON_PROGRESS_LIMIT
    rounds = [e for e in outcome.transcript_dicts() if "round" in e]
    assert all(r["corrections_applied"] == 0 for r in rounds)


def test_the_writer_prompt_names_every_array_it_asks_for():
    """A capability the prompt describes but never names is a capability the
    model answers in prose.

    Both new arrays were added to the schema and explained in the system prompt,
    and neither was ever populated: the model wrote "Two inline contact routes
    appear twice (paragraphs 161 and 186), controlled by one decision field" into
    `notes` and left `inline_branches` empty. The manifest compiled with zero
    inline blocks and every document was blocked on the instruction text the
    switch would have removed. The intro also still said "Identify two things"
    after growing to five.
    """
    import re

    from app.compiler.llm_compiler import COMPILE_SCHEMA, COMPILE_SYSTEM

    numbered = re.findall(r"^(\d)\. ([A-Z][A-Z ]+)-> `(\w+)`", COMPILE_SYSTEM, re.M)
    assert [n for n, _label, _field in numbered] == ["1", "2", "3", "4", "5"]

    named = {field for _n, _label, field in numbered}
    required = set(COMPILE_SCHEMA["required"]) - {"language", "notes"}
    assert required <= named, f"the prompt never names: {sorted(required - named)}"

    assert f"Identify {len(numbered)} things" in COMPILE_SYSTEM.replace("five", "5"), (
        "the intro must count the same number of things the prompt lists"
    )


# ---- the invariant behind four rounds of the same bug ------------------------

def test_the_reviewer_can_express_everything_the_writer_can():
    """A capability on one side of the loop and not the other is a fault nobody
    can clear.

    `inline_branches` and `instruction_spans` were added to the writer's schema,
    then to the writer's prompt, then given an assertion that fires when they are
    missing -- and the loop still could not converge, because the REVIEWER's
    correction schema had no way to add either. It spent every round applying
    corrections that changed nothing and parked as unconverged, on a template
    whose only defect it had been told about in detail.

    Four rounds of the same mistake in different layers. This is the check that
    makes the fifth one loud.
    """
    from app.compiler.agentic_compiler import REVIEW_SCHEMA
    from app.compiler.llm_compiler import COMPILE_SCHEMA

    writer = set(COMPILE_SCHEMA["required"]) - {"language", "notes"}
    reviewer = {k for k in REVIEW_SCHEMA["required"] if k not in ("verdict", "notes")}

    # Stated, not inferred: a heuristic here would pass on a near-miss, which is
    # the failure mode this test exists to catch.
    corrections_for = {
        "fields": {"fields_to_add", "fields_to_remove", "fields_to_retype", "fields_to_relocate"},
        "conditions": {"conditions_to_add", "conditions_to_remove", "conditions_to_rewrite"},
        "instruction_spans": {"instruction_spans_to_add"},
        "inline_branches": {"inline_branches_to_add"},
        "scaffolding_paragraphs": {"scaffolding_add", "scaffolding_remove"},
    }
    assert writer == set(corrections_for), (
        f"the writer's schema changed; record how the reviewer corrects "
        f"{sorted(writer ^ set(corrections_for))}"
    )
    for array, corrections in corrections_for.items():
        missing = corrections - reviewer
        assert not missing, (
            f"the writer can produce {array!r} and the reviewer is missing {sorted(missing)}; "
            f"any fault that needs one is unfixable and the loop will park"
        )


def test_every_assertion_kind_has_a_correction_that_can_address_it():
    """The same invariant from the other end: an assertion nothing can fix is a
    guaranteed non-convergence, not a gate."""
    from app.compiler import assertions as A
    from app.compiler.agentic_compiler import REVIEW_SCHEMA

    reviewer = {k for k in REVIEW_SCHEMA["required"] if k not in ("verdict", "notes")}
    remedy = {
        A.UNCOVERED_PLACEHOLDER: "fields_to_add",
        A.UNCOVERED_MERGEFIELD: "fields_to_add",
        A.UNEXECUTABLE_CONDITION: "conditions_to_rewrite",
        A.FIELD_WITHOUT_SLOT: "fields_to_relocate",
        A.ORPHANED_FIELD: "scaffolding_remove",
        A.SURVIVING_INSTRUCTION: "instruction_spans_to_add",
        A.PARAGRAPH_SCOPED_SWITCH: "inline_branches_to_add",
        A.SCAFFOLDING_CONFLICT: "scaffolding_remove",
        A.TEST_FILL_FAILURE: "conditions_to_rewrite",
    }
    kinds = {
        v for k, v in vars(A).items()
        if k.isupper() and isinstance(v, str) and not k.startswith("_")
    }
    assert kinds <= set(remedy), f"no remedy recorded for: {sorted(kinds - set(remedy))}"
    for kind, array in remedy.items():
        assert array in reviewer, f"{kind} needs {array!r}, which the reviewer cannot produce"


def test_the_reviewer_prompt_tells_it_how_to_fix_the_two_hardest_faults():
    """Naming the array is not enough; the model has to know which fault it
    answers. It described the recruitment switch in prose for two compiles."""
    from app.compiler.agentic_compiler import REVIEW_SYSTEM

    assert "inline_branches_to_add" in REVIEW_SYSTEM
    assert "instruction_spans_to_add" in REVIEW_SYSTEM
    assert "blocks_to_remove" in REVIEW_SYSTEM
    assert "deletes the entire line" in REVIEW_SYSTEM
