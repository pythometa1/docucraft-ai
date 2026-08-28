"""Retrieval over source chunks.

Simplification vs. spec §10.5/§16: no pgvector/HNSW and no external embedding
provider. Instead we fit a TF-IDF vectorizer over a project's chunks at query time
and rank by cosine similarity -- a real, working stand-in for the `EmbeddingProvider`
interface described in the spec, swappable for Voyage/OpenAI embeddings + pgvector
HNSW without changing any caller (see docs/BACKEND_SPEC.md §16 DSA table).
"""

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.models import SourceChunk


def retrieve(chunks: list[SourceChunk], query: str, k: int = 8) -> list[tuple[SourceChunk, float]]:
    if not chunks:
        return []
    corpus = [c.text for c in chunks]
    try:
        vectorizer = TfidfVectorizer(stop_words="english", max_features=4096)
        matrix = vectorizer.fit_transform(corpus + [query])
    except ValueError:
        # corpus too small / all-stopword query -- fall back to naive keyword overlap
        scored = []
        q_terms = set(query.lower().split())
        for c in chunks:
            overlap = len(q_terms & set(c.text.lower().split()))
            scored.append((c, float(overlap)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    query_vec = matrix[-1]
    doc_vecs = matrix[:-1]
    sims = cosine_similarity(query_vec, doc_vecs).flatten()
    ranked = sorted(zip(chunks, sims), key=lambda x: x[1], reverse=True)
    # No `or ranked[:1]`: when nothing scores above zero, the best chunk is not
    # a weak match, it is an unrelated one. Handing it back would let a section
    # be "grounded" in text that has nothing to do with it, and cited as such.
    # An empty result is the truthful answer, and callers already treat it as
    # "no matching source content" rather than crashing.
    return [(c, float(s)) for c, s in ranked[:k] if s > 0]
