"""Starter templates: a document to begin from, not a list of field names.

These are the five presets the browser-side conversion wizard carried, moved to
the server and turned into something larger. There they were field *names* --
`["full_name", "position_title", …]` -- used to bias a regex over text somebody
had already written. Here a kit is a whole blueprint: real prose with real
placeholders in it, which is what "start from scratch" has to mean if the result
is going to be a document rather than a form.

A kit's placeholders are written in the same angle brackets a legacy template
uses, in the same blue the pre-scanner classifies as a placeholder. So a template
started from a kit and a template read from a customer's file are the same kind
of object from the first second, and everything downstream -- the linter, the
publish gate, the fill engine -- treats them identically. There is no
"from-scratch" branch anywhere, which is the point.

**No legend or key page.** It is tempting to open a kit with a paragraph
explaining that `<Like This>` is a placeholder. `llm_compiler.assemble` deletes
fields whose slots all sit in scaffolding, and a legend documenting the syntax
produces real-looking placeholders that exist nowhere in the letter -- so the
explanation belongs in the editor's UI, not in the document.
"""

from pathlib import Path

import yaml

from app.templates import blueprint as bp
from app.verticals import KIT_DIRS, KIT_IDS

#: The kits no vertical owns -- just `blank`. Every other kit lives in its
#: service's own folder and reaches this loader through the registry.
KIT_DIR = Path(__file__).parent / "kits"

#: The order they are offered in: `blank` first, because an author who knows
#: what they are writing should not have to delete somebody else's prose
#: first; then every service's kits in registry order (app/verticals.py).
KIT_ORDER = ("blank",) + KIT_IDS


def _kit_path(name: str) -> Path:
    """Where a kit's YAML lives: its owning service's folder, or the shared one."""
    return KIT_DIRS.get(name, KIT_DIR) / f"{name}.yaml"


class UnknownKit(ValueError):
    """A kit nobody ships."""


def _block_from(entry: dict) -> dict:
    kind = entry.get("kind", "paragraph")
    if kind == "table":
        return bp.table([[[_block_from(b) for b in cell] for cell in row]
                         for row in entry.get("rows") or ()])
    return bp.paragraph(
        [bp.segment(seg["role"], seg.get("text", ""), **{
            k: v for k, v in seg.items() if k not in ("role", "text")})
         for seg in entry.get("segments") or ()],
        style=entry.get("style"),
    )


def load_kit(name: str) -> dict:
    """`{name, description, fields, body}` for one kit.

    The body comes back normalised, so a kit whose YAML happens to put two static
    segments side by side is stored the way Word would store it -- one run, one
    span -- rather than describing a document that cannot exist.
    """
    path = _kit_path(name)
    if name not in KIT_ORDER or not path.exists():
        raise UnknownKit(
            f"there is no template kit called {name!r}; the ones that exist are "
            f"{', '.join(KIT_ORDER)}")
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        "id": name,
        "name": raw.get("name") or name,
        "description": raw.get("description") or "",
        "fields": list(raw.get("fields") or ()),
        "body": bp.normalise_body({
            "blocks": [_block_from(entry) for entry in raw.get("blocks") or ()],
            "sect_pr_from": None,
        }),
        # §6 TABLE_ROW declarations: which table row repeats, over what, and how
        # its column tokens are typed. Optional; most kits have none.
        "table_rows": list(raw.get("table_rows") or ()),
        # Per-token typing for the FIELD objects `objects_for` derives -- so an
        # `<Amount>` in a kit renders as currency rather than as a string.
        "field_types": dict(raw.get("field_types") or {}),
    }


def list_kits() -> list:
    """Every kit, without its body -- a picker does not need the document."""
    out = []
    for name in KIT_ORDER:
        kit = load_kit(name)
        out.append({"id": kit["id"], "name": kit["name"], "description": kit["description"],
                    "field_count": len(kit["fields"]),
                    "paragraph_count": len(bp.walk_paragraphs(kit["body"]))})
    return out


def table_row_objects(body: dict, specs, *, status: str = "PROPOSED") -> list:
    """TABLE_ROW objects for a kit's `table_rows:` declarations.

    Each spec names the collection to iterate over and the column tokens of the
    one row that repeats. Tokens the body does not carry inside a table are
    dropped from the spec rather than kept as promises the fill engine will
    report broken -- the same defence `objects_for` mounts against the kit's
    own `fields:` list.
    """
    from app.compiler.rule_compiler import _slug

    in_table_tokens: set = set()
    for _index, block, in_table in bp.walk_paragraphs(body):
        if not in_table:
            continue
        for seg in block.get("segments") or ():
            if seg.get("role") == bp.PLACEHOLDER:
                in_table_tokens.add(seg.get("text") or "")

    out = []
    for spec in specs or ():
        iterate_over = str(spec.get("iterate_over") or "").strip()
        if not iterate_over:
            continue
        columns = []
        for c in spec.get("columns") or ():
            token = str((c or {}).get("token") or "").strip()
            if not token or token not in in_table_tokens:
                continue
            inner = token[1:-1] if token.startswith("<") and token.endswith(">") else token
            field_id = c.get("field_id") or _slug(inner)
            columns.append({
                "token": token,
                "field_id": field_id,
                "source_key": c.get("source_key") or field_id,
                "type": c.get("type") or "string",
                "format": c.get("format"),
                "on_missing": str(c.get("on_missing") or "BLANK").upper(),
                "default": c.get("default"),
            })
        if not columns:
            continue
        out.append({
            "object_id": spec.get("id") or f"{iterate_over}_rows",
            "object_type": "TABLE_ROW",
            "iterate_over": iterate_over,
            "columns": columns,
            "column_refs": {c["field_id"]: c["source_key"] for c in columns},
            "anchor_row": {"kind": "run_path", "token": columns[0]["token"]},
            "empty_behaviour": str(spec.get("empty_behaviour") or "REMOVE_ROW").upper(),
            "required": bool(spec.get("required")),
            "status": status,
        })
    return out


def kit_objects(kit: dict, *, status: str = "PROPOSED") -> list:
    """Every object a kit starts with: its typed fields, then its table rows."""
    fields = objects_for(kit["body"], status=status)
    for obj in fields:
        declared = (kit.get("field_types") or {}).get(obj["object_id"])
        if declared in ("string", "currency", "date", "number", "percent"):
            obj["type"] = obj["value_type"] = declared
    return fields + table_row_objects(kit["body"], kit.get("table_rows"), status=status)


def objects_for(body: dict, *, status: str = "PROPOSED") -> list:
    """FIELD objects for every placeholder the kit's prose contains.

    Derived from the document rather than from the kit's `fields:` list, because
    the document is what will be filled. A name in that list with no placeholder
    in the prose would be a field that can never appear -- which is exactly the
    `orphaned_field` defect the publish gate refuses, and it would be this
    module's own fault rather than the author's.
    """
    from app.compiler.rule_compiler import _slug

    objects, seen = [], {}
    for index, block, _in_table in bp.walk_paragraphs(body):
        for position, span_index in bp.span_plan(block["segments"]):
            seg = block["segments"][position]
            if seg.get("role") != bp.PLACEHOLDER:
                continue
            token = seg.get("text") or ""
            inner = token[1:-1] if token.startswith("<") and token.endswith(">") else token
            field_id = _slug(inner)
            slot = {"kind": "text_match", "text": token,
                    "paragraph_index": index, "span_index": span_index}
            if field_id in seen:
                seen[field_id]["slots"].append(slot)
                continue
            obj = {
                "object_id": field_id, "object_type": "FIELD", "type": "string",
                "slots": [slot], "source_ref": f"source.{field_id}", "format": None,
                "on_missing": "BLANK", "value_type": "string", "status": status,
                "anchor": None,
            }
            seen[field_id] = obj
            objects.append(obj)
    return objects
