"""Retrieval that survives a restart, and a tenant filter that is a WHERE clause.

Both halves of the retrieval layer were dictionaries. `InMemoryVectorStore` and
`InMemoryMappingMemoryStore` are honest reference implementations -- they hold
the same contract, they fail closed the same way -- but they hold it in the
process, so a deploy erased every embedding the system had built and every
mapping a reviewer had approved. §10 is explicit about where those belong:
PostgreSQL is the system of record and pgvector is "semantic memory within the
same data platform", and approved mappings are persisted "as structured
relational/JSONB data, not only as an embedding" because a vector can rank a
candidate but cannot tell you a human accepted it forty-two times.

This module is the second implementation of both protocols. Nothing above it
changes: `VectorIndex` and `MappingMemory` already took their store as a
constructor argument, and they still compute exactly what they computed before.
What changes is where the rows come from.

The tenant filter is the reason this file is worth reading carefully. §16:

    "pgvector searches apply the tenant filter before similarity, never as a
    post-filter on results."

Every read here is a `SELECT ... WHERE org_id = :org` and there is no method
that reads without one. The similarity computation upstream therefore never sees
a row belonging to another organisation -- not to score it, not to rank it, not
to count it against `k`. A post-filter would be observationally identical right
up until the discard step is wrong, and by then another customer's corpus has
already influenced the result. `test_org_a_cannot_reach_org_bs_row_through_the_
database_backed_store` is the standing evidence §16 asks for, since it says in
as many words that a code review is not sufficient for this one.

Two smaller decisions worth stating. The stores take a `Session` and never
commit: the caller's unit of work decides when the transaction ends, which is
what lets an approval and the audit row recording it land together or not at
all. And `shared_structural_keys` does its anonymisation in SQL -- it selects
the three closed-vocabulary columns and nothing else -- so another tenant's
`MappingEntry` is never materialised in this process at all, rather than being
materialised and then carefully not returned.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Embedding as EmbeddingRow
from app.models import MappingMemoryEntry as MappingMemoryRow
from app.models import MappingMemorySharing as SharingRow
from app.models import ReviewerCorrection as CorrectionRow
from app.retrieval.embeddings import decode_vector, encode_vector
from app.retrieval.mapping_memory import (
    CorrectionEvent,
    MappingEntry,
    StructuralKey,
)
from app.retrieval.vector import (
    DEFAULT_DIMENSIONS,
    TenantScopeRequired,
    VectorRecord,
    normalise_text,
)


def _require_org(org_id: str, what: str) -> str:
    """No read and no write here happens without a tenant to scope it to."""
    if not org_id or not str(org_id).strip():
        raise TenantScopeRequired(f"{what} requires an org_id")
    return str(org_id)


def _as_utc(moment: datetime | None) -> datetime | None:
    """Timestamps come back naive from both SQLite and a plain `TIMESTAMP` column.

    The in-memory store hands out timezone-aware UTC, and two stores that
    disagree about whether a datetime carries a zone produce a `TypeError` at
    the first comparison in a caller that worked fine against the other one.
    Everything written here is UTC, so re-attaching the zone on read is
    restoring information the column dropped, not inventing it.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ------------------------------------------------------------------- vectors


class SqlVectorStore:
    """`VectorStore` over the `embeddings` table.

    Satisfies the protocol in `app.retrieval.vector` exactly, including the part
    that matters most: there is no read that spans tenants and returns rows.
    `size()` spans the estate but returns an integer, which is a capacity figure
    rather than anybody's data.
    """

    def __init__(self, session: Session, *, dimensions: int = DEFAULT_DIMENSIONS):
        self._session = session
        self._dimensions = int(dimensions)

    # ------------------------------------------------------------- writing

    def put(self, record: VectorRecord) -> None:
        """Insert or replace one embedding, keyed on (org, record_id).

        Re-indexing a field must not accumulate rows: the second embedding of
        the same source column is a correction of the first, and keeping both
        would let a stale vector out-rank the current one purely by being
        returned twice.
        """
        coordinates = encode_vector(record.vector, self._dimensions)
        existing = self._session.execute(
            select(EmbeddingRow).where(
                EmbeddingRow.org_id == record.org_id,
                EmbeddingRow.record_id == record.record_id,
            )
        ).scalar_one_or_none()

        if existing is None:
            self._session.add(EmbeddingRow(
                org_id=record.org_id,
                kind=record.kind,
                record_id=record.record_id,
                text=record.text,
                vector=coordinates,
                provider=record.provider,
                dims=len(coordinates),
                doc_type=record.doc_type,
                meta=dict(record.metadata or {}),
            ))
        else:
            existing.kind = record.kind
            existing.text = record.text
            existing.vector = coordinates
            existing.provider = record.provider
            existing.dims = len(coordinates)
            existing.doc_type = record.doc_type
            existing.meta = dict(record.metadata or {})
        # Flushed rather than committed: the row has to be visible to the next
        # read in this transaction, and a unique-constraint violation has to
        # surface at the statement that caused it rather than at some later
        # commit in an unrelated part of the request.
        self._session.flush()

    # ------------------------------------------------------------- reading

    def scoped_rows(self, *, org_id: str, doc_type: str | None = None, kind: str | None = None) -> list[VectorRecord]:
        """This organisation's rows, filtered in SQL before anything is scored.

        The `doc_type` predicate is a SQL equality, so an unclassified row --
        `doc_type IS NULL` -- does not satisfy it. That matches the in-memory
        store and it is the behaviour you want: an unclassified row is not
        evidence for a typed query, and letting NULL match would quietly stop
        the filter filtering the moment someone forgets to classify an upload.
        """
        org_id = _require_org(org_id, "scoped_rows")
        stmt = select(EmbeddingRow).where(EmbeddingRow.org_id == org_id)
        if doc_type is not None:
            stmt = stmt.where(EmbeddingRow.doc_type == doc_type)
        # Narrowing by kind in SQL rather than in Python: an organisation's index
        # holds every kind it has ever embedded, and pulling all of them across
        # to discard most is the difference between reading 203 rows and 470.
        if kind is not None:
            stmt = stmt.where(EmbeddingRow.kind == kind)
        # Deterministic order in, deterministic order out. The index breaks
        # score ties on record_id, and starting from a stable order means a
        # golden test does not depend on the database's physical row order.
        stmt = stmt.order_by(EmbeddingRow.record_id)
        return [self._to_record(row) for row in self._session.execute(stmt).scalars()]

    def _to_record(self, row: EmbeddingRow) -> VectorRecord:
        return VectorRecord(
            record_id=row.record_id,
            org_id=row.org_id,
            kind=row.kind,
            text=row.text,
            vector=decode_vector(row.vector),
            provider=row.provider,
            doc_type=row.doc_type,
            metadata=dict(row.meta or {}),
        )

    # ----------------------------------------------------------- retention

    def forget_org(self, org_id: str) -> int:
        """§16 retention: "deletion must cascade to embeddings"."""
        org_id = _require_org(org_id, "forget_org")
        result = self._session.execute(
            delete(EmbeddingRow).where(EmbeddingRow.org_id == org_id)
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def size(self) -> int:
        return int(self._session.execute(select(func.count()).select_from(EmbeddingRow)).scalar_one())


# ------------------------------------------------------------ mapping memory


class SqlMappingMemoryStore:
    """`MappingMemoryStore` over `mapping_memory`, `mapping_memory_sharing` and
    `reviewer_corrections`.

    The uniqueness rule is the same one the in-memory store enforces with a
    tuple key: one row per (org, field, normalised source column, transform).
    Here it is a database constraint as well, so two concurrent approvals of the
    same mapping cannot split the count across two rows -- and §13's largest
    single-signal weight is that count.
    """

    def __init__(self, session: Session):
        self._session = session

    # ------------------------------------------------------------- writing

    def upsert(self, entry: MappingEntry) -> MappingEntry:
        row = self._row_for(
            org_id=entry.org_id,
            field_key=entry.field_key,
            source_column=entry.source_column,
            transform=entry.transform,
        )
        if row is None:
            row = MappingMemoryRow(
                id=entry.entry_id,
                org_id=entry.org_id,
                field_key=entry.field_key,
                source_column=entry.source_column,
                source_column_key=normalise_text(entry.source_column),
                transform=entry.transform,
                on_missing=entry.on_missing,
                field_type=entry.field_type,
                context_text=entry.context_text or "",
                doc_type=entry.doc_type,
                template_family_id=entry.template_family_id,
                approval_count=entry.approval_count,
                rejection_count=entry.rejection_count,
                first_seen_at=entry.first_seen_at,
                last_approved_at=entry.last_approved_at,
                last_approved_by=entry.last_approved_by,
            )
            self._session.add(row)
        else:
            # `first_seen_at` is deliberately not written back. It records when
            # this organisation first arrived at the mapping, and an update that
            # moved it forward would erase the one field that says how long the
            # convention has been in use.
            row.on_missing = entry.on_missing
            row.field_type = entry.field_type
            row.context_text = entry.context_text or ""
            row.doc_type = entry.doc_type
            row.template_family_id = entry.template_family_id
            row.approval_count = entry.approval_count
            row.rejection_count = entry.rejection_count
            row.last_approved_at = entry.last_approved_at
            row.last_approved_by = entry.last_approved_by
        self._session.flush()
        return entry

    def record_correction(self, event: CorrectionEvent) -> None:
        self._session.add(CorrectionRow(
            org_id=event.org_id,
            field_key=event.field_key,
            accepted_column=event.accepted_column,
            rejected_column=event.rejected_column,
            corrected_by=event.corrected_by,
            created_at=event.at,
        ))
        self._session.flush()

    # ------------------------------------------------------------- reading

    def find(
        self, *, org_id: str, field_key: str, source_column: str, transform: str,
    ) -> MappingEntry | None:
        org_id = _require_org(org_id, "find")
        row = self._row_for(
            org_id=org_id, field_key=field_key, source_column=source_column, transform=transform,
        )
        return None if row is None else self._to_entry(row)

    def entries_for_org(self, org_id: str) -> list:
        org_id = _require_org(org_id, "entries_for_org")
        rows = self._session.execute(
            select(MappingMemoryRow)
            .where(MappingMemoryRow.org_id == org_id)
            .order_by(MappingMemoryRow.field_key, MappingMemoryRow.source_column_key)
        ).scalars()
        return [self._to_entry(row) for row in rows]

    def corrections_for_org(self, org_id: str) -> list:
        org_id = _require_org(org_id, "corrections_for_org")
        rows = self._session.execute(
            select(CorrectionRow)
            .where(CorrectionRow.org_id == org_id)
            .order_by(CorrectionRow.created_at, CorrectionRow.id)
        ).scalars()
        return [
            CorrectionEvent(
                org_id=row.org_id,
                field_key=row.field_key,
                accepted_column=row.accepted_column,
                rejected_column=row.rejected_column,
                corrected_by=row.corrected_by,
                at=_as_utc(row.created_at),
            )
            for row in rows
        ]

    def _row_for(
        self, *, org_id: str, field_key: str, source_column: str, transform: str,
    ) -> MappingMemoryRow | None:
        return self._session.execute(
            select(MappingMemoryRow).where(
                MappingMemoryRow.org_id == org_id,
                MappingMemoryRow.field_key == field_key,
                MappingMemoryRow.source_column_key == normalise_text(source_column),
                MappingMemoryRow.transform == transform,
            )
        ).scalar_one_or_none()

    def _to_entry(self, row: MappingMemoryRow) -> MappingEntry:
        return MappingEntry(
            entry_id=row.id,
            org_id=row.org_id,
            field_key=row.field_key,
            field_type=row.field_type,
            source_column=row.source_column,
            transform=row.transform,
            on_missing=row.on_missing,
            context_text=row.context_text or "",
            doc_type=row.doc_type,
            template_family_id=row.template_family_id,
            approval_count=row.approval_count,
            rejection_count=row.rejection_count,
            first_seen_at=_as_utc(row.first_seen_at),
            last_approved_at=_as_utc(row.last_approved_at),
            last_approved_by=row.last_approved_by,
        )

    # ------------------------------------------------- cross-tenant sharing

    def set_sharing_opt_in(self, org_id: str, *, opted_in: bool, actor: str) -> None:
        org_id = _require_org(org_id, "sharing policy")
        if not actor:
            # Opting a customer's data into a shared pool is a consent decision.
            # An unattributed one cannot be evidenced later, which is the whole
            # point of recording it.
            raise ValueError("cross-tenant sharing must record who opted in")
        row = self._session.execute(
            select(SharingRow).where(SharingRow.org_id == org_id)
        ).scalar_one_or_none()
        if row is None:
            self._session.add(SharingRow(org_id=org_id, opted_in=bool(opted_in), actor=actor))
        else:
            row.opted_in = bool(opted_in)
            row.actor = actor
        self._session.flush()

    def is_opted_in(self, org_id: str) -> bool:
        """Absent means opted out. A pool that defaults to sharing shares
        whatever nobody has got round to configuring."""
        if not org_id or not str(org_id).strip():
            return False
        row = self._session.execute(
            select(SharingRow.opted_in).where(SharingRow.org_id == org_id)
        ).scalar_one_or_none()
        return bool(row)

    def shared_structural_keys(self, *, exclude_org_id: str) -> list:
        """`(org_id, StructuralKey, approvals)` from opted-in organisations only.

        The projection is the anonymisation. This selects `field_type`,
        `transform` and `on_missing` -- three closed vocabularies -- plus the
        contributing org id and a count, and nothing else: no source column, no
        context text, no field key, no template family. Another tenant's
        `MappingEntry` is therefore never constructed in this process, so there
        is no object anybody has to be careful with. The org ids exist so the
        caller can count distinct contributors against the anonymity floor, and
        are consumed by that count.

        Both the opt-in join and the `net approvals > 0` predicate run in SQL. A
        contributor that has not opted in never has a row read at all, which is
        a stronger statement than reading it and declining to use it.
        """
        net_approvals = MappingMemoryRow.approval_count - MappingMemoryRow.rejection_count
        stmt = (
            select(
                MappingMemoryRow.org_id,
                MappingMemoryRow.field_type,
                MappingMemoryRow.transform,
                MappingMemoryRow.on_missing,
                net_approvals.label("net_approvals"),
            )
            .join(SharingRow, SharingRow.org_id == MappingMemoryRow.org_id)
            .where(
                SharingRow.opted_in.is_(True),
                MappingMemoryRow.org_id != exclude_org_id,
                net_approvals > 0,
            )
            .order_by(MappingMemoryRow.org_id, MappingMemoryRow.field_type)
        )
        return [
            (
                org_id,
                StructuralKey(field_type=field_type, transform=transform, on_missing=on_missing),
                int(approvals),
            )
            for org_id, field_type, transform, on_missing, approvals
            in self._session.execute(stmt).all()
        ]

    # ----------------------------------------------------------- retention

    def forget_org(self, org_id: str) -> int:
        """§16 retention: offboarding deletes the memory too, not just the rows.

        All three tables, in one transaction. Leaving the correction log behind
        would leave a record of which columns a departed customer's reviewers
        accepted and rejected -- which is their data, described field by field.
        """
        org_id = _require_org(org_id, "forget_org")
        result = self._session.execute(
            delete(MappingMemoryRow).where(MappingMemoryRow.org_id == org_id)
        )
        self._session.execute(delete(CorrectionRow).where(CorrectionRow.org_id == org_id))
        self._session.execute(delete(SharingRow).where(SharingRow.org_id == org_id))
        self._session.flush()
        return int(result.rowcount or 0)
