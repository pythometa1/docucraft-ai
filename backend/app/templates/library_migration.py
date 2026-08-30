"""Rescue the token library into something that can actually fill a document.

The `/templates` page used to author templates out of four coloured tokens which
serialised to `<span data-token="…">` HTML. That HTML is stored, versioned and
readable, and it can never become a manifest: the fill engine works on OOXML runs
addressed by `(paragraph_index, span_index)`, and an HTML span is neither. So
`POST /template-library/{id}/generate` existed, no screen ever called it, and
every template authored that way was work that could not be used.

This turns each one into a blueprint. The four tokens map onto §6 objects with
very little argument -- a `source` token is a FIELD, a `conditional` is a
CONDITION -- and the two places it is not obvious are both places where the old
model let something through that the new one refuses:

  * a `conditional`'s expression was free text, so it may be prose rather than
    something the evaluator can run. It is parse-probed, and prose becomes a
    finding rather than a stored condition that silently evaluates false forever.
  * a `prompt` token has no grounding source, and `NarrativeObject` requires one.
    It arrives needing review rather than being quietly turned into a field.

Nothing is deleted. The library rows stay exactly where they are; this reads
them.
"""

from bs4 import BeautifulSoup

from app.compiler.rule_compiler import _slug
from app.templates import blueprint as bp

#: `data-token` value -> what it becomes. `static` is absent because untagged
#: text is static by default and always was.
TOKEN_KINDS = ("source", "prompt", "conditional", "repeat")

#: Block-level HTML the editor produced. Anything else is treated as inline.
BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "li", "blockquote", "div")

#: Heading tag -> the Word style name the emitter writes.
STYLE_BY_TAG = {"h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3", "h4": "Heading 4"}


def _finding(code, detail, *, severity="warning", object_id=None):
    return {"code": code, "severity": severity, "detail": detail,
            "object_id": object_id, "paragraph_index": None}


def _placeholder_for(field: str) -> str:
    """`full_name` -> `<full_name>`.

    Angle brackets because that is what the pre-scanner's `BRACKET_RE` reads and
    what a legacy template writes, so a migrated template and a legacy one are
    the same kind of document from the first second.
    """
    return f"<{field}>"


def _expression_runs(expression: str) -> bool:
    """Whether the evaluator can actually execute this.

    The old editor's condition field was free text, so it holds things like
    "if the employee is eligible" as often as `region == 'EU'`. Storing prose as
    a condition gives a rule that evaluates false for every record and removes
    its section from every document, silently.
    """
    from app.expressions.token_parser import condition_inputs, evaluate_condition

    if not (expression or "").strip():
        return False
    probe = {name: "" for name in condition_inputs(expression)}
    return evaluate_condition(expression, probe).reason != "unparseable"


def migrate(content_html: str, *, status: str = "PROPOSED") -> tuple:
    """`(body, objects, findings)` for one template-library entry."""
    soup = BeautifulSoup(content_html or "", "html.parser")
    blocks: list = []
    objects: list = []
    findings: list = []
    by_field: dict = {}
    paragraph_index = 0

    containers = soup.find_all(BLOCK_TAGS) or ([soup] if content_html else [])
    for container in containers:
        segments: list = []
        span_index = 0

        for node in container.descendants:
            if getattr(node, "name", None) is None:                     # a text node
                if node.parent is not container and node.parent.get("data-token"):
                    continue                                            # handled with its span
                text = str(node)
                if text.strip():
                    segments.append(bp.segment(bp.STATIC, text))
                continue
            if node.name != "span":
                continue
            kind = node.get("data-token")
            if kind not in TOKEN_KINDS:
                continue

            if kind == "source":
                field = (node.get("field") or "").strip()
                if not field:
                    findings.append(_finding(
                        "token_without_field",
                        "A source token names no field, so nothing could ever fill it. It was "
                        "kept as static text."))
                    segments.append(bp.segment(bp.STATIC, node.get_text() or ""))
                    continue
                field_id = _slug(field)
                token = _placeholder_for(field)
                segments.append(bp.segment(bp.PLACEHOLDER, token))
                slot = {"kind": "text_match", "text": token,
                        "paragraph_index": paragraph_index, "span_index": span_index}
                if field_id in by_field:
                    by_field[field_id]["slots"].append(slot)
                else:
                    obj = {"object_id": field_id, "object_type": "FIELD", "type": "string",
                           "slots": [slot], "source_ref": f"source.{field_id}",
                           "format": None, "on_missing": "BLANK", "value_type": "string",
                           "status": status, "anchor": None}
                    if node.get("fallback"):
                        obj["on_missing"], obj["default"] = "DEFAULT", node.get("fallback")
                    by_field[field_id] = obj
                    objects.append(obj)

            elif kind == "prompt":
                # A NARRATIVE with no grounding source, which `NarrativeObject`
                # refuses. Kept as an instruction the author can see and decide
                # about rather than turned into a field nobody asked for.
                prompt = (node.get("prompt") or "").strip()
                segments.append(bp.segment(bp.INSTRUCTION, f"[AI: {prompt}]" if prompt else "[AI]"))
                findings.append(_finding(
                    "narrative_without_grounding",
                    f"An AI prompt token ({prompt[:60] or 'no instruction'}) has no grounding "
                    "source, so nothing constrains what it would write. Convert it to a field, or "
                    "give it a source to ground on."))

            elif kind == "conditional":
                expression = (node.get("condition") or "").strip()
                body_text = node.get("body") or ""
                segments.append(bp.segment(bp.STATIC, body_text))
                if _expression_runs(expression):
                    objects.append({
                        "object_id": f"cond_{_slug(expression)[:40]}", "object_type": "CONDITION",
                        "expression": expression, "keeps_blocks": [],
                        "on_true": "KEEP", "on_false": "REMOVE_BLOCK",
                        "start_paragraph": paragraph_index, "end_paragraph": paragraph_index,
                        "test_cases": [], "status": status})
                else:
                    findings.append(_finding(
                        "condition_does_not_parse", severity="blocking",
                        detail=(
                            f"The condition {expression[:60]!r} is not something the engine can "
                            "run, so its text was kept unconditionally. Rewrite it as a "
                            "comparison, such as region == 'EU'.")))

            elif kind == "repeat":
                collection = (node.get("collection") or "").strip()
                segments.append(bp.segment(bp.STATIC, node.get("body") or ""))
                findings.append(_finding(
                    "repeat_needs_a_table",
                    f"A repeat over {collection or 'an unnamed collection'} was kept as plain "
                    "text. Repetition is a table row in a Word template; put the row in a table "
                    "and mark it as repeating."))

            span_index += 1

        if not segments:
            segments = [bp.segment(bp.STATIC, container.get_text() or "")]
        blocks.append(bp.paragraph(segments, style=STYLE_BY_TAG.get(container.name)))
        paragraph_index += 1

    body = bp.normalise_body({"blocks": blocks, "sect_pr_from": None})

    # Normalising can merge segments, which moves span indices. Re-derive rather
    # than trusting the numbering from before the merge -- the same rule as
    # everywhere else here: a position is never carried across a rewrite.
    from app.templates.lift import reslot_against

    objects, reslot_findings = reslot_against(objects, body)
    findings.extend(reslot_findings)
    return body, objects, findings
