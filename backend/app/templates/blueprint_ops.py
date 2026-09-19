"""One validated way to change a template, whoever is asking.

The editor's "Apply fix" button, the chat co-pilot and an API caller all post the
same payload through this module. That is the whole design: three
implementations of "rename this field" would be three sets of guards, and the
weakest of them would be the one that matters.

Every branch is guarded and every rejection is *returned*, never swallowed. The
pattern is `agentic_compiler._apply_corrections`, and the reason is the same one
written there: an operation that silently does nothing counts as progress. In a
loop that lets a model spin to its round limit; here it lets a person believe a
change landed when it did not, and then publish.

Text is validated through `text_edit.validate_run_text` before it can be applied
-- control characters, tabs, line breaks, a length cap. Word stores a tab or a
newline as an element rather than a character, so a run containing one renders as
nothing at all: the words silently vanish from the letter. That bug is why the
check exists, and routing every text-carrying operation through it is why it
cannot come back through a different door.
"""

from dataclasses import dataclass, field as dataclass_field

from app.public_errors import public_message
from app.templates import blueprint as bp

#: The vocabulary. Anything else is rejected by name rather than ignored, so a
#: caller that invents an operation is told rather than left to conclude it
#: worked.
OPERATIONS = (
    "set_segment_text",
    "set_segment_role",
    "set_segment_emit",
    "add_field",
    "remove_field",
    "rename_field",
    "retype_field",
    "set_on_missing",
    "rewrite_condition",
    "remove_condition",
    "set_block_range",
    "set_row_repeat",
    "remove_row_repeat",
)

#: What a repeating row may do when its collection is empty.
ROW_EMPTY_BEHAVIOURS = ("REMOVE_ROW", "REMOVE_TABLE")

EDITABLE_ROLES = (bp.STATIC, bp.PLACEHOLDER, bp.INSTRUCTION)

FIELD_TYPES = ("string", "currency", "date", "number", "percent")


@dataclass
class OperationResult:
    body: dict
    objects: list
    applied: list = dataclass_field(default_factory=list)
    rejected: list = dataclass_field(default_factory=list)

    def as_dict(self) -> dict:
        return {"applied": self.applied, "rejected": self.rejected,
                "body": self.body, "objects": self.objects}


def _reject(rejected, op, reason):
    rejected.append({"op": op, "reason": reason})


def _paragraph_at(body: dict, index):
    for i, block, _in_table in bp.walk_paragraphs(body):
        if i == index:
            return block
    return None


def _segment_at(body: dict, paragraph_index, span_index):
    """The segment holding a given span, or None.

    Addressed by *span* rather than by position in the segment list, because a
    span is what every other part of the system means by "the second run of
    paragraph 12" -- the manifest's slots, the fill engine, the pre-scanner.
    """
    block = _paragraph_at(body, paragraph_index)
    if block is None:
        return None
    for position, span in bp.span_plan(block["segments"]):
        if span == span_index:
            return block["segments"][position]
    return None


def _objects_by_id(objects):
    return {o.get("object_id"): o for o in objects}


def _token_in_table(body: dict, token: str) -> bool:
    """Whether `token` appears in a placeholder run inside a table."""
    for _index, block, in_table in bp.walk_paragraphs(body):
        if not in_table:
            continue
        for seg in block.get("segments") or ():
            if seg.get("role") == bp.PLACEHOLDER and token in (seg.get("text") or ""):
                return True
    return False


def apply_operations(body: dict, objects, ops) -> OperationResult:
    """Apply each operation, or say why it could not be.

    The body is normalised once at the end rather than after each operation.
    Normalising in between would renumber spans underneath operations that had
    not run yet, so a batch naming two spans of one paragraph would apply the
    first correctly and the second to the wrong run.
    """
    from app.generation.text_edit import EditRejected, validate_run_text

    body = {**body, "blocks": [b for b in (body.get("blocks") or ())]}
    objects = [dict(o) for o in objects or ()]
    result = OperationResult(body=body, objects=objects)

    for op in ops or ():
        kind = op.get("op")
        if kind not in OPERATIONS:
            _reject(result.rejected, op,
                    f"{kind!r} is not an operation this template understands; the ones that exist "
                    f"are {', '.join(OPERATIONS)}")
            continue

        try:
            if kind in ("set_segment_text", "set_segment_role", "set_segment_emit"):
                segment = _segment_at(body, op.get("paragraph_index"), op.get("span_index"))
                if segment is None:
                    _reject(result.rejected, op,
                            f"there is no run at paragraph {op.get('paragraph_index')}, span "
                            f"{op.get('span_index')}; the document may have changed since this was "
                            "proposed")
                    continue
                if segment["role"] in (bp.MERGEFIELD, bp.HYPERLINK):
                    _reject(result.rejected, op,
                            f"a {segment['role']} carries its meaning in the document's own "
                            "structure rather than in its text, so editing it here would break it")
                    continue

                if kind == "set_segment_text":
                    text = validate_run_text(op.get("text") or "")
                    if text == segment.get("text"):
                        _reject(result.rejected, op, "the run already reads exactly that")
                        continue
                    segment["text"] = text
                elif kind == "set_segment_role":
                    role = op.get("role")
                    if role not in EDITABLE_ROLES:
                        _reject(result.rejected, op,
                                f"a run can be {', '.join(EDITABLE_ROLES)}; {role!r} is not one of "
                                "them")
                        continue
                    if role == segment["role"]:
                        _reject(result.rejected, op, f"the run is already {role}")
                        continue
                    segment["role"] = role
                else:
                    segment["emit"] = False if op.get("emit") is False else None
                    if segment["emit"] is None:
                        segment.pop("emit")

            elif kind == "add_field":
                segment = _segment_at(body, op.get("paragraph_index"), op.get("span_index"))
                if segment is None:
                    _reject(result.rejected, op,
                            f"there is no run at paragraph {op.get('paragraph_index')}, span "
                            f"{op.get('span_index')}")
                    continue
                match_text = op.get("match_text") or segment.get("text") or ""
                if match_text not in (segment.get("text") or ""):
                    _reject(result.rejected, op,
                            f"{match_text!r} does not appear in that run, so the field would have "
                            "nowhere to go")
                    continue
                field_id = op.get("id")
                if not field_id:
                    _reject(result.rejected, op, "a field needs an id")
                    continue
                if field_id in _objects_by_id(objects):
                    _reject(result.rejected, op, f"a field called {field_id!r} already exists")
                    continue
                objects.append({
                    "object_id": field_id, "object_type": "FIELD",
                    "type": op.get("type") or "string",
                    "slots": [{"kind": "text_match", "text": match_text,
                               "paragraph_index": op["paragraph_index"],
                               "span_index": op["span_index"]}],
                    "source_ref": f"source.{field_id}", "format": None,
                    "on_missing": "BLANK", "value_type": op.get("type") or "string",
                    "status": "PROPOSED", "anchor": None})
                segment["role"] = bp.PLACEHOLDER

            elif kind in ("remove_field", "remove_condition"):
                wanted = "FIELD" if kind == "remove_field" else "CONDITION"
                target = _objects_by_id(objects).get(op.get("id"))
                if target is None or target.get("object_type") != wanted:
                    _reject(result.rejected, op,
                            f"this template has no {wanted.lower()} called {op.get('id')!r}")
                    continue
                objects.remove(target)

            elif kind == "rename_field":
                target = _objects_by_id(objects).get(op.get("id"))
                new_id = op.get("new_id")
                if target is None or target.get("object_type") != "FIELD":
                    _reject(result.rejected, op, f"no field called {op.get('id')!r}")
                    continue
                if not new_id:
                    _reject(result.rejected, op, "a rename needs a new id")
                    continue
                if new_id in _objects_by_id(objects):
                    _reject(result.rejected, op, f"{new_id!r} is already taken")
                    continue
                target["object_id"] = new_id
                # The source ref follows the name unless somebody has pointed it
                # somewhere deliberately; overwriting a chosen binding would
                # silently repoint a field at a different column.
                if target.get("source_ref") in (None, "", f"source.{op.get('id')}"):
                    target["source_ref"] = f"source.{new_id}"

            elif kind == "retype_field":
                target = _objects_by_id(objects).get(op.get("id"))
                new_type = op.get("type")
                if target is None or target.get("object_type") != "FIELD":
                    _reject(result.rejected, op, f"no field called {op.get('id')!r}")
                    continue
                if new_type not in FIELD_TYPES:
                    _reject(result.rejected, op,
                            f"the renderer can format {', '.join(FIELD_TYPES)}; not {new_type!r}")
                    continue
                target["type"] = target["value_type"] = new_type

            elif kind == "set_on_missing":
                target = _objects_by_id(objects).get(op.get("id"))
                policy = (op.get("on_missing") or "").upper()
                if target is None:
                    _reject(result.rejected, op, f"no object called {op.get('id')!r}")
                    continue
                from app.generation.missing_policy import ON_MISSING_VALUES

                if policy not in ON_MISSING_VALUES:
                    _reject(result.rejected, op,
                            f"{policy!r} is not a missing-value policy; expected one of "
                            f"{', '.join(ON_MISSING_VALUES)}")
                    continue
                if policy == "DEFAULT" and op.get("default") is None:
                    _reject(result.rejected, op,
                            "a DEFAULT policy needs a value to fall back to, or it renders nothing "
                            "and calls it deliberate")
                    continue
                target["on_missing"] = policy
                if policy == "DEFAULT":
                    target["default"] = op.get("default")

            elif kind == "rewrite_condition":
                from app.compiler.llm_compiler import _expression_is_executable

                target = _objects_by_id(objects).get(op.get("id"))
                expression = (op.get("expression") or "").strip()
                if target is None or target.get("object_type") != "CONDITION":
                    _reject(result.rejected, op, f"no condition called {op.get('id')!r}")
                    continue
                if not expression:
                    _reject(result.rejected, op,
                            "a condition with no expression governs nothing")
                    continue
                if not _expression_is_executable(expression):
                    _reject(result.rejected, op,
                            f"{expression!r} is not something the engine can run. Write it as a "
                            "comparison, such as region == 'EU'")
                    continue
                target["expression"] = expression

            elif kind == "set_row_repeat":
                from app.compiler.rule_compiler import _slug

                iterate_over = (op.get("iterate_over") or "").strip()
                if not iterate_over:
                    _reject(result.rejected, op,
                            "a repeating row needs the name of the collection it repeats over, "
                            "such as line_items")
                    continue
                raw_columns = [c for c in (op.get("columns") or ()) if isinstance(c, dict)]
                columns = []
                for c in raw_columns:
                    token = (c.get("token") or "").strip()
                    if not token:
                        continue
                    columns.append({
                        "token": token,
                        "field_id": c.get("field_id") or _slug(token[1:-1] if token.startswith("<") and token.endswith(">") else token),
                        "source_key": c.get("source_key") or c.get("field_id") or _slug(token[1:-1] if token.startswith("<") and token.endswith(">") else token),
                        "type": c.get("type") if c.get("type") in FIELD_TYPES else "string",
                        "format": c.get("format"),
                        "on_missing": (c.get("on_missing") or "BLANK").upper(),
                        "default": c.get("default"),
                    })
                if not columns:
                    _reject(result.rejected, op,
                            "a repeating row needs at least one column token, such as "
                            "<Item Description>, or repeating it would print the template row "
                            "unchanged")
                    continue
                missing_tokens = [c["token"] for c in columns if not _token_in_table(body, c["token"])]
                if missing_tokens:
                    _reject(result.rejected, op,
                            f"{', '.join(missing_tokens)} do(es) not appear as a placeholder "
                            "inside any table of this template, so the row to repeat cannot be "
                            "found")
                    continue
                empty_behaviour = (op.get("empty_behaviour") or "REMOVE_ROW").upper()
                if empty_behaviour not in ROW_EMPTY_BEHAVIOURS:
                    _reject(result.rejected, op,
                            f"a repeating row's empty_behaviour can be "
                            f"{', '.join(ROW_EMPTY_BEHAVIOURS)}; {empty_behaviour!r} is not one "
                            "of them")
                    continue
                row_id = op.get("id") or f"{iterate_over}_rows"
                existing = _objects_by_id(objects).get(row_id)
                if existing is not None and existing.get("object_type") != "TABLE_ROW":
                    _reject(result.rejected, op,
                            f"{row_id!r} already names a {existing.get('object_type')}, not a "
                            "repeating row")
                    continue
                spec = {
                    "object_id": row_id, "object_type": "TABLE_ROW",
                    "iterate_over": iterate_over,
                    "columns": columns,
                    "column_refs": {c["field_id"]: c["source_key"] for c in columns},
                    "anchor_row": {"kind": "run_path", "token": columns[0]["token"]},
                    "empty_behaviour": empty_behaviour,
                    "required": bool(op.get("required")),
                    "status": "PROPOSED",
                }
                if existing is not None:
                    objects[objects.index(existing)] = spec
                else:
                    objects.append(spec)

            elif kind == "remove_row_repeat":
                target = _objects_by_id(objects).get(op.get("id"))
                if target is None or target.get("object_type") != "TABLE_ROW":
                    _reject(result.rejected, op,
                            f"this template has no repeating row called {op.get('id')!r}")
                    continue
                objects.remove(target)

            elif kind == "set_block_range":
                target = _objects_by_id(objects).get(op.get("id"))
                start, end = op.get("start_paragraph"), op.get("end_paragraph")
                paragraphs = len(bp.walk_paragraphs(body))
                if target is None or target.get("object_type") != "SECTION":
                    _reject(result.rejected, op, f"no block called {op.get('id')!r}")
                    continue
                if not isinstance(start, int) or not isinstance(end, int):
                    _reject(result.rejected, op, "a block range needs two paragraph numbers")
                    continue
                if start > end:
                    _reject(result.rejected, op,
                            f"paragraph {start} to {end} runs backwards")
                    continue
                if not (0 <= start < paragraphs and 0 <= end < paragraphs):
                    _reject(result.rejected, op,
                            f"this template has {paragraphs} paragraphs, so {start} to {end} "
                            "addresses text that is not there")
                    continue
                target["start_paragraph"], target["end_paragraph"] = start, end
                target.pop("boundary_adjusted", None)
                target["boundary_method"] = "author"

        except EditRejected as exc:
            _reject(result.rejected, op, str(exc))
            continue
        except (ValueError, TypeError, KeyError) as exc:      # pragma: no cover - defensive
            _reject(result.rejected, op, public_message(
                exc, "This change could not be applied to the template."))
            continue

        result.applied.append(op)

    # Normalised once, at the end. Doing it between operations would renumber
    # spans underneath the ones that had not run yet, so a batch naming two spans
    # of one paragraph would apply the first correctly and the second to the
    # wrong run.
    result.body = bp.normalise_body(result.body)
    result.objects = objects
    return result
