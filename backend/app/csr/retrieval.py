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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.csr.ich_e3 import source_types_for
from app.docgen.ranking import build_query, in_id_range, score_chunks
from app.docgen.ranking import format_extracts as _format_extracts
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
        if range_prefix and chunk.is_table and in_id_range(chunk.table_id, range_prefix):
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
    """The numbered extract block and its source map, with THIS module's
    style-reference type bound in.

    Bound here rather than defaulted in the shared function: a prior CSR that
    silently stopped being labelled "never cite as fact" would still produce a
    readable draft, and the only sign would be a sentence sourced to somebody
    else's approved dossier.
    """
    return _format_extracts(chunks, style_reference_type=STYLE_REFERENCE_TYPE,
                            table_label="Table")
