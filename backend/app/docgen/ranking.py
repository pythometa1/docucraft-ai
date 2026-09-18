"""Ranking chunks against a section query, and numbering the survivors.

Shared by every document module. Two jobs, both pure and both offline:

Scoring is lexical, in pure Python, because a dossier that cannot be drafted
with no embedding provider and no network the night before a submission is a
dossier that gets drafted by hand. `app.retrieval.lexical` is deliberately not
reused -- it reads `.text` where these chunks carry `.content`, fits a
scikit-learn vectorizer per query, and (the reason that actually decides it)
drops zero-scoring chunks and truncates to k before any caller can intervene.
That would discard exactly the numeric table whose prose overlap with a
section query is nil and whose table id is the whole reason it belongs.

Numbering is `format_extracts`: it writes the `[S1] ... [S2] ...` block the
prompt sees AND the parallel source map the citation parser resolves markers
through, in one pass, so the two cannot drift apart.

What is NOT here: which doc types a section may read, and which chunks deserve
a boost. Those are module policy -- the clinical module boosts a table whose
id falls in its section's ICH E3 range, a CMC module boosts by material -- and
they arrive already resolved.
"""

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./\-][a-z0-9]+)*")

#: Grammatical words only. Domain words that happen to be everywhere ("study",
#: "patients") are left in and discounted by IDF instead -- a stop list that
#: guesses at the domain is how "Disposition of Patients" stops matching the
#: disposition tables.
_STOPWORDS = frozenset("""
a an and are as at be been being by for from had has have if in into is it its
of on or than that the their then there these this those to was were which
while with within
""".split())


def _tokens(text: str) -> list:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS]


def _chunk_terms(chunk) -> list:
    """The terms a chunk is matched on: its text plus its own metadata.

    The table id and the heading hint are included because they are part of
    what the chunk IS. A TLF chunk has to be findable by "14.1.1" even when the
    ingested header line spells the id differently, and a heading hint is often
    the only word a narrative chunk shares with the section that wants it.
    """
    parts = (chunk.content or "", chunk.table_id or "", chunk.section_hint or "")
    return _tokens(" ".join(p for p in parts if p))


def build_query(*, section_number: str, section_title: str,
                guidance_text: str | None, study_metadata: dict) -> str:
    """The text a section is matched against.

    What the section IS (number and title), what it must contain (the template
    guidance), and what this project calls things (metadata values). The
    metadata matters more than it looks: "NSCLC" and the compound name are the
    terms that separate this study's chunks from a prior dossier's boilerplate
    about a different one.

    The keyword is still `study_metadata` because the clinical module's tests
    call it by name; a module with products rather than studies passes its own
    dict under the same key.
    """
    parts = [section_number or "", section_title or "", guidance_text or ""]
    for value in (study_metadata or {}).values():
        # Scalars only. A nested dict or a list stringifies into punctuation and
        # row ids, which is noise dressed as evidence; booleans stringify into
        # "true", which matches nothing and dilutes the vector.
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
    return " ".join(p for p in parts if p)


def score_chunks(query: str, chunks) -> dict:
    """`{chunk_id: cosine similarity}` for one query over one candidate set.

    Pure: no session, no network, no provider. TF-IDF cosine, with the IDF
    fitted over the candidates themselves, so the terms that distinguish this
    project's chunks from each other are the ones that carry weight.
    """
    documents = {c.id: Counter(_chunk_terms(c)) for c in chunks}
    if not documents:
        return {}

    total = len(documents)
    document_frequency: Counter = Counter()
    for counts in documents.values():
        document_frequency.update(counts.keys())

    # Smoothed IDF, so no term is ever weightless. The textbook log(N/df) gives
    # every term of a single-chunk project an IDF of exactly zero, and a project
    # with one indexed document would then retrieve nothing at all -- a failure
    # that only appears on the smallest, newest project, which is the first one
    # anybody tries.
    idf = {term: math.log((1 + total) / (1 + df)) + 1
           for term, df in document_frequency.items()}

    query_counts = Counter(t for t in _tokens(query) if t in idf)
    if not query_counts:
        return {chunk_id: 0.0 for chunk_id in documents}
    query_vector = {t: c * idf[t] for t, c in query_counts.items()}
    query_norm = math.sqrt(sum(w * w for w in query_vector.values()))

    scores = {}
    for chunk_id, counts in documents.items():
        norm = math.sqrt(sum((c * idf[t]) ** 2 for t, c in counts.items()))
        if not norm:
            scores[chunk_id] = 0.0
            continue
        dot = sum(weight * counts[term] * idf[term]
                  for term, weight in query_vector.items() if term in counts)
        scores[chunk_id] = dot / (query_norm * norm)
    return scores


def in_id_range(table_id: str | None, range_prefix: str) -> bool:
    """Whether a table id belongs to a section's conventional id range.

    On the segment boundary, not on the raw string: a bare `startswith("14.1")`
    hands Table 14.10 -- a safety table in some numbering schemes -- the boost
    that belongs to Section 10's disposition tables, and a boost given to the
    wrong table is a wrong table quoted in the report.
    """
    table = (table_id or "").strip()
    return bool(table) and (table == range_prefix or table.startswith(range_prefix + "."))


#: The clinical module named it after the TLF bundle it was written for.
in_tlf_range = in_id_range


#: What a reference-only extract is labelled, unless a module says otherwise.
STYLE_REFERENCE_LABEL = "STYLE REFERENCE ONLY, never cite as fact"


def format_extracts(chunks, *, style_reference_type: str | None = None,
                    table_label: str = "Table",
                    reference_label: str = STYLE_REFERENCE_LABEL) -> tuple:
    """The numbered `[S1] ...` block for the prompt, and the map that turns a
    marker back into a row.

    Built in one pass and returned together because they must not be able to
    disagree. A source map assembled separately from the text the model was
    shown is how [S3] comes to resolve to the chunk that was [S4] in the
    prompt: a citation that points confidently at the wrong page, which is
    worse than no citation at all.
    """
    blocks = []
    source_map = []
    for index, chunk in enumerate(chunks, start=1):
        marker = f"S{index}"
        # Rule 4 of the drafting prompt refuses facts from a style reference.
        # It can only refuse what it can see, so the warning goes in the header
        # of the extract itself rather than in a legend further up the prompt.
        # `reference_label` is the module's to choose: CSR and CMC mean "this
        # is somebody else's document, copy its tone and nothing else", while a
        # periodic safety report's prior report is a real source whose figures
        # belong to a different interval -- the same header, different warning.
        label = (f"[{marker} -- {reference_label}]"
                 if style_reference_type and chunk.doc_type == style_reference_type
                 else f"[{marker}]")
        locators = [chunk.doc_type or "source"]
        if chunk.table_id:
            locators.append(f"{table_label} {chunk.table_id}")
        if chunk.page is not None:
            locators.append(f"p.{chunk.page}")
        blocks.append(f"{label} {' | '.join(locators)}\n{(chunk.content or '').strip()}")
        source_map.append({
            "marker": marker,
            "chunk_id": chunk.id,
            "document_id": chunk.document_id,
            "page": chunk.page,
            "table_id": chunk.table_id,
            "doc_type": chunk.doc_type,
            "is_table": bool(chunk.is_table),
        })
    return "\n\n".join(blocks), source_map
