"""Splitting a template across model calls without losing what spans the cut.

Every template is read by a model now, and a long one does not fit in a single
call. The path this replaces did not split at all -- it built its prompt as
`"\\n".join(paragraphs)[:60000]` and sent that. Everything past the cut was never
read, and nothing in the result said so: a 400-paragraph contract compiled from
its first half and reported a manifest that looked finished.

Two properties make chunking safe here, and both are load-bearing.

**Global indices, verbatim.** Every downstream coordinate is a paragraph index
into the real document -- blocks are ranges of them, `delete_always` is a list of
them, and `_locate` snaps a model's `match_text` onto a span inside one. So a
chunk renumbering its paragraphs from zero would produce a manifest that
addresses the wrong lines, and it would look entirely plausible. Chunks carry the
document's own indices and never renumber.

**Overlap, because conditions straddle the cut.** A conditional block is an
instruction followed by the clause it governs. Split between the two and the
writer sees a clause with nothing marking it conditional, so it compiles as
static text and the finished letter carries a paragraph its data never asked for.
Overlapping the window by a few paragraphs means at least one chunk holds the
whole construct.

Neither of those is enough on its own for the case the compiler cares most about.
Alternatives have to share a field -- `USE FOR NEW HIRE` and `USE FOR CURRENT
COLLEAGUES` are two values of `employee_status`, not two independent booleans, and
as booleans both can be true and the letter gets both openings. Recognising that
requires seeing both instructions, which may be forty paragraphs apart. So each
chunk is prefixed with an **outline** of every instruction-shaped line in the
whole template, not just its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.compiler.rule_compiler import FALLBACK_INSTRUCTION_SHAPE_RE
from app.templates.conventions import DEFAULT_FAMILY, load_family

# `[[IF x]]`, `{% if x %}`, `{{#if x}}`, `<!-- IF x -->` and the English prose
# form. Imported rather than redefined: the same set decides elsewhere whether a
# template has conditional logic at all, and two copies would drift.
from app.compiler.llm_compiler import CONDITIONAL_MARKER_RE

_DEFAULT_FAMILY = load_family(DEFAULT_FAMILY)

# A line that reads as an instruction to whoever assembles the letter, rather
# than as letter content. Deliberately generous: the outline is context handed to
# a model, so a false positive costs a few tokens and a false negative costs the
# cross-chunk reasoning this exists to enable.
_HEADING_MAX_LEN = 120


def _collapse(text: str) -> str:
    """Whitespace-normalised, so an outline entry is one readable line."""
    return re.sub(r"\s+", " ", text or "").strip()


def _looks_instructional(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if CONDITIONAL_MARKER_RE.search(stripped):
        return True
    if FALLBACK_INSTRUCTION_SHAPE_RE.search(stripped):
        return True
    if _DEFAULT_FAMILY.include_re is not None and _DEFAULT_FAMILY.include_re.search(stripped):
        return True
    # A short line in full caps is how most of these estates shout an
    # instruction: "USE IF ON TEMPORARY ASSIGNMENT", "PRIVATE & CONFIDENTIAL".
    # The second is not an instruction, but it is cheap to carry and the model
    # is asked to interpret, not to trust, the outline.
    letters = [c for c in stripped if c.isalpha()]
    if letters and len(stripped) <= _HEADING_MAX_LEN:
        if sum(1 for c in letters if c.isupper()) / len(letters) >= 0.8:
            return True
    return False


@dataclass
class Chunk:
    """One model call's worth of template.

    `paragraphs` holds `(global_index, text)` pairs -- never a local index. The
    prompt renders them as `[47] ...`, so an index the model returns addresses the
    real document with no translation step to get wrong.
    """

    number: int
    total: int
    #: (global_paragraph_index, text), in document order.
    paragraphs: list[tuple[int, str]] = field(default_factory=list)
    #: Global indices this chunk shares with its neighbours.
    overlap_indices: set[int] = field(default_factory=set)

    @property
    def start_index(self) -> int:
        return self.paragraphs[0][0] if self.paragraphs else 0

    @property
    def end_index(self) -> int:
        return self.paragraphs[-1][0] if self.paragraphs else 0

    def render(self, outline: str = "") -> str:
        """The chunk as the model sees it: outline first, then the paragraphs."""
        body = "\n".join(f"[{i}] {t}" for i, t in self.paragraphs if t.strip())
        if self.total == 1:
            # A template that fits in one call must produce exactly what the
            # single-call path produced, so small templates are unaffected by
            # chunking existing at all.
            return f"{outline}{body}" if outline else body
        header = (
            f"This is part {self.number} of {self.total} of one template. "
            f"It covers paragraphs {self.start_index} to {self.end_index}. "
            "Paragraph indices are the whole document's, not this part's -- report them as given. "
            "Report only what you can see here; the other parts are read separately and merged.\n\n"
        )
        return f"{header}{outline}{body}"


def build_outline(paragraph_texts: list[str], *, max_entries: int = 200) -> str:
    """Every instruction-shaped line in the WHOLE template, for every chunk.

    Without this a chunk cannot tell an alternative from a standalone flag,
    because the sibling instruction it needs to compare against is in a different
    call. `COMPILE_SYSTEM` already instructs the writer to "look across the whole
    template for the set an instruction belongs to"; this is what makes that
    instruction satisfiable once the template is split.
    """
    entries = [
        f"[{i}] {_collapse(t)[:160]}"
        for i, t in enumerate(paragraph_texts)
        if _looks_instructional(t)
    ]
    if not entries:
        return ""
    truncated = entries[:max_entries]
    tail = (
        f"\n(+{len(entries) - len(truncated)} more instruction-shaped lines not listed)"
        if len(entries) > len(truncated) else ""
    )
    return (
        "Every instruction-shaped line in the COMPLETE template, for context. Use it to tell "
        "alternatives apart from independent flags: two instructions that are alternatives of "
        "each other must share one condition field and differ only in its value.\n"
        + "\n".join(truncated) + tail + "\n\n--- the paragraphs to compile ---\n"
    )


def chunk_paragraphs(
    paragraph_texts: list[str],
    *,
    budget_chars: int,
    overlap_paragraphs: int,
) -> list[Chunk]:
    """Split into overlapping windows that each fit `budget_chars`.

    A single paragraph longer than the budget is never split -- it goes into a
    chunk of its own and overruns. Cutting mid-paragraph would break `_locate`,
    which matches the model's `match_text` against a whole span; half a sentence
    matches nothing and the field is silently dropped. An overrun costs one
    oversized call, which the provider reports honestly if it cannot serve it.
    """
    indexed = [(i, t) for i, t in enumerate(paragraph_texts)]
    if not indexed:
        return [Chunk(number=1, total=1)]

    budget = max(1, budget_chars)
    overlap = max(0, overlap_paragraphs)

    windows: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for entry in indexed:
        # +8 approximates the "[123] " prefix and the newline the prompt adds.
        cost = len(entry[1]) + 8
        if current and size + cost > budget:
            windows.append(current)
            # Re-seed the next window with the tail of this one. Capped at half
            # the window so a small budget cannot make consecutive windows
            # identical and loop forever.
            carry = current[-min(overlap, max(1, len(current) // 2)):] if overlap else []
            current = list(carry)
            size = sum(len(t) + 8 for _i, t in current)
        current.append(entry)
        size += cost
    if current:
        windows.append(current)

    total = len(windows)
    seen_before: set[int] = set()
    chunks: list[Chunk] = []
    for number, window in enumerate(windows, start=1):
        indices = {i for i, _t in window}
        chunks.append(Chunk(
            number=number,
            total=total,
            paragraphs=window,
            overlap_indices=indices & seen_before,
        ))
        seen_before |= indices
    return chunks
