"""RAG + LLM orchestration (spec §10). Unifies the two fill-plan sources -- template
sections filled via a draft's mappings, and inline tokens parsed from a native
template-library entry -- behind one retrieval/prompt/validate path, exactly as
described in docs/BACKEND_SPEC.md §10.
"""

import re

from app.models import SourceChunk
from app.expressions import token_parser
from app.llm.provider import LLMProvider
from app.retrieval.lexical import retrieve

_FIELD_LINE_RE_CACHE: dict[str, re.Pattern] = {}


def _field_regex(field: str) -> re.Pattern:
    if field not in _FIELD_LINE_RE_CACHE:
        _FIELD_LINE_RE_CACHE[field] = re.compile(rf"{re.escape(field)}\s*:\s*([^;\n]+)", re.IGNORECASE)
    return _FIELD_LINE_RE_CACHE[field]


def extract_field_value(chunks: list[SourceChunk], field: str) -> str | None:
    """Pull a `field: value` style fact straight out of structured source chunks
    (csv/xlsx ingestion emits exactly this "col: val; col2: val2" shape, §9.1)."""
    rx = _field_regex(field)
    for c in chunks:
        m = rx.search(c.text)
        if m:
            return m.group(1).strip()
    return None


def build_fact_sheet(chunks: list[SourceChunk], field_names: list[str]) -> dict:
    sheet = {}
    for field in field_names:
        value = extract_field_value(chunks, field)
        if value is not None:
            sheet[field] = value
    return sheet


def _grounding_score(blocks: list[dict]) -> float:
    if not blocks:
        return 0.0
    cited = sum(1 for b in blocks if b.get("citations"))
    return round(cited / len(blocks), 2)


def resolve_token_unit(
    *,
    token: token_parser.TemplateToken,
    chunks: list[SourceChunk],
    fact_sheet: dict,
    llm: LLMProvider,
) -> str:
    """Resolves one native-template token to its final rendered string (spec §8.3)."""
    if token.token_type == "source":
        field = token.attrs.get("field", "")
        value = fact_sheet.get(field) or extract_field_value(chunks, field)
        return value if value is not None else token.attrs.get("fallback", "")

    if token.token_type == "prompt":
        prompt_text = token.attrs.get("prompt", "")
        ranked = retrieve(chunks, prompt_text, k=6)
        context = [{"id": c.id, "text": c.text} for c, _ in ranked]
        result = llm.generate(instructions=prompt_text, context_chunks=context, fact_sheet=fact_sheet, max_words=80)
        return " ".join(b["text"] for b in result.blocks)

    if token.token_type == "conditional":
        condition = token.attrs.get("condition", "")
        return token.attrs.get("body", "") if token_parser.safe_eval_condition(condition, fact_sheet) else ""

    if token.token_type == "repeat":
        collection_field = token.attrs.get("collection", "")
        variable = token.attrs.get("variable", "item")
        body = token.attrs.get("body", "")
        rows = _collection_rows(chunks, collection_field)
        rendered = [body.replace(f"{{{variable}}}", row) for row in rows]
        return "".join(rendered) if rendered else ""

    return ""


def _collection_rows(chunks: list[SourceChunk], collection_field: str) -> list[str]:
    """A `repeat` token's collection is a source field name expected to resolve to
    several rows -- every chunk carrying that field contributes one row (spec §8.3)."""
    rx = _field_regex(collection_field)
    rows = []
    for c in chunks:
        m = rx.search(c.text)
        if m:
            rows.append(m.group(1).strip())
    return rows
