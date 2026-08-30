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

KIT_DIR = Path(__file__).parent / "kits"

#: The order they are offered in. `blank` first because an author who knows what
#: they are writing should not have to delete somebody else's prose first.
KIT_ORDER = ("blank", "offer", "contract", "clinical", "medaff")


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
    path = KIT_DIR / f"{name}.yaml"
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
