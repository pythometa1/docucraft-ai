"""Choosing the evidence one CSR section may be written from.

Retrieval is the only thing standing between a drafting model and its own
memory. The section prompt licenses facts from the extracts it is handed and
from nowhere else, so a chunk that reaches this list is a chunk the model is
permitted to state as fact about this study. Two consequences shape the file.

The tenant and the project are filtered in SQL, before a single score is
computed -- never trimmed out of the ranked results afterwards. A ranking bug
can then only ever surface the wrong chunk of the RIGHT study; it cannot leak
one sponsor's numbers into another sponsor's report, which is the one failure
this module has to be structurally incapable of rather than merely careful
about.

Scoring is lexical, here, in pure Python. It has to work with no embedding
provider configured and no network, because a CSR that cannot be drafted
offline the night before a submission is a CSR that gets drafted by hand.
`app.retrieval.lexical` is deliberately not reused: it reads `.text` where
these chunks carry `.content`, it fits a scikit-learn vectorizer per query,
and -- the reason that actually decides it -- it drops zero-scoring chunks and
truncates to k before any caller can intervene. That would discard exactly the
numeric disposition table whose prose overlap with a section query is nil and
whose table id is the whole reason it belongs.
"""

import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.csr.ich_e3 import source_types_for
from app.models import CsrChunk

#: A prior CSR is retrievable for every section -- house voice, heading style,
#: the shape of a sentence -- and citable as fact for none of them. That is why
#: it is added to every doc_type filter below and why its extract header says
#: so in capitals above.
STYLE_REFERENCE_TYPE = "prior_csr"

#: The conventional ICH E3 post-text table ranges: disposition and baseline
#: tables are numbered 14.1.x, efficacy 14.2.x, safety 14.3.x. A section
#: drafting from a thousand-table TLF bundle wants its own range first.
TLF_RANGE_BY_SECTION = {"10": "14.1", "11": "14.2", "12": "14.3"}

#: Additive, and small on purpose. A disposition table is mostly digits and arm
#: labels, so its lexical overlap with a prose query is near nil however
#: exactly it answers the section -- this is what carries it past the
#: no-shared-terms cut below. It is about the score of a weak lexical match:
#: enough to rank a right-range table among the candidates, not enough to bury
#: a narrative chunk that plainly answers the section.
TLF_RANGE_BOOST = 0.15

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
    guidance), and what this study calls things (metadata values). The metadata
    matters more than it looks: "NSCLC" and the compound name are the terms
    that separate this study's chunks from a prior CSR's boilerplate about a
    different one.
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


def in_tlf_range(table_id: str | None, range_prefix: str) -> bool:
    """Whether a table id belongs to a section's conventional TLF range.

    On the segment boundary, not on the raw string: a bare `startswith("14.1")`
    hands Table 14.10 -- a safety table in some numbering schemes -- the boost
    that belongs to Section 10's disposition tables, and a boost given to the
    wrong table is a wrong table quoted in the report.
    """
    table = (table_id or "").strip()
    return bool(table) and (table == range_prefix or table.startswith(range_prefix + "."))


def retrieve_for_section(db: Session, *, csr_project_id: str, org_id: str,
                         section_number: str, section_title: str,
                         guidance_text: str | None, study_metadata: dict,
                         k: int = 16) -> list:
    """The chunks Section `section_number` may be written from, best first.

    Deterministic: the same section over the same indexed corpus retrieves the
    same evidence in the same order twice. Regenerating a section and getting a
    different set of sources makes the audit record ("what did the model
    actually see?") unanswerable, and makes a QC failure impossible to
    reproduce.
    """
    # Tenant and project first, in SQL. Everything after this line is ranking;
    # nothing after this line can widen what was selected here.
    statement = select(CsrChunk).where(
        CsrChunk.org_id == org_id,
        CsrChunk.csr_project_id == csr_project_id,
    )
    doc_types = source_types_for(section_number)
    if doc_types:
        # An empty map entry means "no filter"; a populated one always admits
        # the style reference as well, which is retrievable for every section
        # and citable for none.
        statement = statement.where(
            CsrChunk.doc_type.in_([*doc_types, STYLE_REFERENCE_TYPE]))

    candidates = list(db.scalars(statement.order_by(CsrChunk.id)))
    if not candidates or k <= 0:
        return []

    scores = score_chunks(
        build_query(section_number=section_number, section_title=section_title,
                    guidance_text=guidance_text, study_metadata=study_metadata),
        candidates,
    )
    range_prefix = TLF_RANGE_BY_SECTION.get((section_number or "").split(".")[0])

    ranked = []
    for chunk in candidates:
        score = scores.get(chunk.id, 0.0)
        if range_prefix and chunk.is_table and in_tlf_range(chunk.table_id, range_prefix):
            score += TLF_RANGE_BOOST
        if score <= 0:
            # After the stop list, sharing not one term with the section's
            # query means unrelated. Handing it over anyway would let a section
            # be "grounded" in text that has nothing to do with it -- and cited
            # as such, which is worse than the [DATA NEEDED] the model writes
            # when it is given nothing.
            continue
        ranked.append((chunk, score))

    # Tables win ties: on equal evidence a numbered table is the citable form
    # ("[S2, Table 14.1.1]") and prose paraphrasing it is not. The id is the
    # final key purely to make the order total.
    ranked.sort(key=lambda pair: (-pair[1], not pair[0].is_table, pair[0].id))
    return [chunk for chunk, _score in ranked[:k]]


def format_extracts(chunks) -> tuple:
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
        label = (f"[{marker} -- STYLE REFERENCE ONLY, never cite as fact]"
                 if chunk.doc_type == STYLE_REFERENCE_TYPE else f"[{marker}]")
        locators = [chunk.doc_type or "source"]
        if chunk.table_id:
            locators.append(f"Table {chunk.table_id}")
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
