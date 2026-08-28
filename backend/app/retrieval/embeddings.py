"""The column an embedding actually lives in, on two databases that disagree about vectors.

§10 puts semantic memory "within the same data platform" as transactional truth:
PostgreSQL is the system of record and pgvector is the index beside it, so a
tenant filter, a backup and a deletion request all reach the vectors by the same
route they reach the rows. Nothing here reached anything. `InMemoryVectorStore`
held the whole corpus in a dict, so a deploy, a crash or an ordinary worker
restart erased every embedded field description the system had built, and the
next template compiled against a blank history.

Persisting them needs one column type that behaves on both databases this project
actually runs on. Production is PostgreSQL with pgvector, where an embedding is a
first-class `vector(1024)` an HNSW index can order by distance. The test suite is
SQLite, which has no such type at all -- and a persistence layer that only exists
on the production database is a persistence layer nobody exercises before a
deploy. So `VectorColumn` renders as pgvector's type on PostgreSQL and as a JSON
array of floats everywhere else, and the suite runs the second path on every
commit while CI runs the first against a real Postgres 16.

The pgvector import is deliberately guarded. `pgvector.sqlalchemy` is required by
exactly one code path -- compiling a column against the PostgreSQL dialect -- and
making it a hard dependency of *importing the application* means a developer
without the package, or an image built before it was added to requirements, gets
an ImportError at start-up for a feature they are not using. It is imported once
here, its absence is recorded rather than raised, and the failure surfaces where
it is actionable: the first time something asks for a vector column on
PostgreSQL, naming both halves of the fix (the Python package, and the extension
inside the database).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

from app.retrieval.vector import DEFAULT_DIMENSIONS

try:  # pragma: no cover - exercised by test_the_absence_is_reported_not_raised
    from pgvector.sqlalchemy import Vector as _PgVector
except ImportError:  # pragma: no cover - the guard is the point; see the docstring
    _PgVector = None

#: Whether the PostgreSQL vector path can be compiled in this process. Read it
#: rather than re-importing: a caller that wants to branch on availability
#: should branch on the same fact `VectorColumn` branches on.
PGVECTOR_AVAILABLE = _PgVector is not None

#: The operator class the HNSW index is built for.
#:
#: `VectorIndex` ranks by cosine on L2-normalised rows, so the index has to be
#: built for cosine distance too. A mismatch here does not error -- Postgres
#: simply declines to use the index and falls back to a sequential scan, which
#: is the worst kind of failure: correct answers, quietly and increasingly slow,
#: with nothing in the logs to say the index the migration created is dead
#: weight.
HNSW_OPS = "vector_cosine_ops"


class PgvectorUnavailable(RuntimeError):
    """A PostgreSQL vector column was asked for without pgvector installed."""


def encode_vector(value: Any, dimensions: int) -> list[float]:
    """A stored row's coordinates, validated against the column's declared width.

    pgvector columns are fixed-width: a 768-dimensional vector in a
    `vector(1024)` column is rejected by the database with an error naming
    neither the record nor the provider that produced it. Checking here means
    the message names both, at the call that made the mistake.
    """
    if value is None:
        raise ValueError("a vector column cannot hold NULL; an embedding with no coordinates is not an embedding")
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"expected a one-dimensional vector, got shape {array.shape}")
    if array.shape[0] != dimensions:
        raise ValueError(
            f"vector has {array.shape[0]} dimensions but the column is declared "
            f"vector({dimensions}); re-embed with a provider of the right width "
            "rather than truncating or padding, which would silently change what was indexed"
        )
    if not np.all(np.isfinite(array)):
        raise ValueError("vector contains NaN or infinity; a distance computed against it is meaningless")
    return [float(x) for x in array]


def decode_vector(raw: Any) -> np.ndarray:
    """Coordinates back out of either storage shape, as the float64 array the index scores."""
    return np.asarray(raw, dtype=np.float64)


class VectorColumn(TypeDecorator):
    """`vector(n)` on PostgreSQL, a JSON array of floats elsewhere.

    The decorator exists so both dialects hand the application the same thing --
    a one-dimensional float64 numpy array -- because the alternative is every
    read site branching on `dialect.name`, which is the branch someone
    eventually forgets.

    `cache_ok` is true: the type is fully described by `dimensions`, so
    SQLAlchemy's statement cache can key on it safely.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS):
        if int(dimensions) < 1:
            raise ValueError(f"dimensions={dimensions} does not describe a vector")
        self.dimensions = int(dimensions)
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            if _PgVector is None:
                raise PgvectorUnavailable(
                    "this column needs pgvector on PostgreSQL and the package is not installed. "
                    "Two things are required and neither implies the other: `pip install pgvector` "
                    "for the Python type, and `CREATE EXTENSION IF NOT EXISTS vector` inside the "
                    "database for the column type itself. On SQLite no extension is needed -- the "
                    "column falls back to a JSON array of floats."
                )
            return dialect.type_descriptor(_PgVector(self.dimensions))
        # SQLite and anything else: a JSON array. Slower to scan and impossible
        # to index for distance, which is exactly why it is the fallback and not
        # the production path.
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # One definition of "a valid vector" for both dialects. On PostgreSQL
        # pgvector's own bind processor turns this list into the wire format;
        # on SQLite the JSON impl serialises it as an array.
        return encode_vector(value, self.dimensions)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decode_vector(value)


def hnsw_index_ddl(table: str, column: str, *, name: str | None = None) -> str:
    """The PostgreSQL-only index §18 asks to have planned before it is needed.

    §18 lists "pgvector recall degrades as mapping memory grows -- plan an HNSW
    index strategy and per-tenant partitioning before the corpus approaches the
    low millions of vectors" as a scaling limit worth watching. The cost of
    building it now is one statement in a migration; the cost of adding it later
    is an index build over a live table that every tenant is reading.

    Emitted only where it means something. SQLite has no `USING hnsw`, and a
    migration that tried would fail the suite rather than the deploy.
    """
    index_name = name or f"ix_{table}_{column}_hnsw"
    return (
        f'CREATE INDEX IF NOT EXISTS {index_name} '
        f'ON {table} USING hnsw ("{column}" {HNSW_OPS})'
    )


def drop_hnsw_index_ddl(table: str, column: str, *, name: str | None = None) -> str:
    index_name = name or f"ix_{table}_{column}_hnsw"
    return f"DROP INDEX IF EXISTS {index_name}"


__all__ = [
    "HNSW_OPS",
    "PGVECTOR_AVAILABLE",
    "PgvectorUnavailable",
    "VectorColumn",
    "decode_vector",
    "drop_hnsw_index_ddl",
    "encode_vector",
    "hnsw_index_ddl",
]
