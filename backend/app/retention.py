"""When customer data is destroyed, and proof that it was.

§16's "Retention and deletion" is four sentences long and every one of them is a
thing this system could not do.

    "Source uploads deleted on a per-tenant schedule; they are the most
    sensitive artefact and the least useful to retain."

A source upload is a payroll extract: every salary, every national insurance
number, every home address, in the clear, on disk, forever. Nothing expired it.
`sweep_expired_sources` does, on a schedule the tenant sets.

    "Generated documents retained according to the customer's records policy,
    not a default of your choosing."

So there is deliberately no default here. `OrgDataPolicy.generated_document_
retention_days` is NULL until a customer states a period, and while it is NULL
`sweep_expired_documents` deletes nothing and says so. Deleting somebody's
signed employment contracts on a schedule we invented is not a tidy-up, it is a
records-management incident we caused.

    "Deletion must cascade to embeddings, preview renders, QA artefacts and
    cached outputs -- an embedding derived from deleted data is still derived
    from it."

This is the sentence that makes deletion hard. A `DELETE FROM source_files` is
one row; the salary it held has by then been copied into chunks, embedded into
vectors, learned into mapping memory, rendered into previews and recorded in QA
artefacts. `delete_source_file` removes the derivations too, and the set of
derivations is computed from the schema rather than hand-listed, so a table
somebody adds next year that records where it came from is swept without anyone
remembering to add it here.

    "A tenant offboarding routine that produces a verifiable deletion
    certificate."

`offboard_organisation` empties every org-scoped table, deletes the blobs, and
returns a `DeletionCertificate` carrying counts plus `manifest_sha256` -- a hash
over the canonical list of every id destroyed. The list itself is handed back to
the caller and never stored: retaining an inventory of everything we just
deleted would be a smaller copy of the thing we said we deleted, and the hash
proves the manifest without keeping it.

Over-deletion is the safe direction throughout. A mapping-memory entry wrongly
forgotten costs a reviewer one re-approval; a row wrongly retained is a record
we told a customer had been destroyed, and there is no undo for that sentence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, Text, cast
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base
from app.models import (
    DeletionCertificate, DocumentVersion, GeneratedDocument, ManifestBinding,
    ManifestGeneration, Organization, OrgDataPolicy, SourceChunk, SourceFile,
    SourceVersion, User,
)
from app.storage import abs_path

# ------------------------------------------------------------------- scopes

ORGANISATION = "organisation"
SOURCE_FILE = "source_file"
GENERATED_DOCUMENTS = "generated_documents"

#: Tables that hold identity rather than customer records. They are emptied last
#: and only when offboarding is asked to purge identity, because until they are
#: gone the certificate still has an actor and an organisation name to quote.
IDENTITY_TABLES = ("users", "organizations")

#: The one org-scoped table an org-wide purge must not touch. Deleting the
#: receipts along with the data would leave a customer with our word for it.
CERTIFICATE_TABLE = "deletion_certificates"

#: Column name that means "there is a file on disk behind this row". Matched by
#: name across the whole schema so a new table that stores a blob is swept
#: without anyone editing this module.
BLOB_COLUMN = "blob_path"

#: Columns that make a row a derivation of one source upload. A table that
#: records either of these is, by definition, holding something computed from a
#: customer's spreadsheet, and §16 says that is still the customer's data.
SOURCE_DERIVATION_COLUMNS = ("source_version_id", "source_file_id")

#: Columns holding the id of the thing a row was computed from. `embeddings`
#: files a vector under the `record_id` of whatever was embedded, so a vector
#: built from a deleted chunk -- or from a deleted column's description -- is
#: found by matching here.
DERIVED_RECORD_COLUMNS = ("record_id",)

#: Columns holding the *name* of a source column rather than a row id. A mapping
#: memory entry keyed on "Annual Salary", and the reviewer correction that
#: produced it, are both derived from the file that had an "Annual Salary"
#: column. Nothing links them back to that file, so the name is the link.
SOURCE_COLUMN_COLUMNS = (
    "source_column", "source_column_key", "accepted_column", "rejected_column",
)


# ------------------------------------------------------------------- results


@dataclass
class DeletionManifest:
    """Exactly what was destroyed, before it is reduced to counts and a hash.

    Held in memory and returned to the caller; never written to a table. It is
    the evidence a customer can be handed at the moment of deletion, and the
    thing `manifest_sha256` is computed over.
    """

    scope: str
    scope_id: str | None
    org_id: str
    rows: dict[str, list[str]] = field(default_factory=dict)
    blobs: list[str] = field(default_factory=list)
    derived: dict[str, int] = field(default_factory=dict)
    #: Populated when a sweep declined to act. §16 asks for the customer's
    #: records policy; "they have not given us one" is an answer, not an error,
    #: and it has to be reported rather than swallowed.
    skipped: dict[str, str] = field(default_factory=dict)
    #: Rows kept but emptied of customer payload, table -> count. Distinct from
    #: `rows` on purpose: a certificate that reported a scrub as a deletion would
    #: be claiming a record is gone when it is still there, correctly, holding
    #: the lineage for a letter somebody may have signed.
    scrubbed: dict[str, int] = field(default_factory=dict)

    def record(self, table: str, ids) -> None:
        if not ids:
            return
        self.rows.setdefault(table, []).extend(str(i) for i in ids)

    @property
    def counts(self) -> dict[str, int]:
        return {table: len(ids) for table, ids in sorted(self.rows.items()) if ids}

    @property
    def total_rows(self) -> int:
        return sum(len(ids) for ids in self.rows.values())

    def canonical(self) -> str:
        """The manifest in one deterministic form, so the hash is reproducible.

        Sorted keys, sorted ids, no whitespace. Two runs that deleted the same
        rows must produce the same string or the certificate proves nothing.
        """
        payload = {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "org_id": self.org_id,
            "rows": {table: sorted(set(ids)) for table, ids in sorted(self.rows.items())},
            "blobs": sorted(set(self.blobs)),
            # Inside the hash, because a scrub is part of what was destroyed. A
            # certificate that covered only deleted rows would let the verbatim
            # source rows this pass emptied go unaccounted for.
            "scrubbed": dict(sorted(self.scrubbed.items())),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def verify_manifest(manifest: DeletionManifest, certificate: DeletionCertificate) -> bool:
    """Whether a certificate really accounts for this manifest.

    The point of the hash: an auditor holding the manifest handed back at
    deletion time can recompute it and check the row, without us having kept a
    list of the identifiers we destroyed.
    """
    return manifest.digest() == certificate.manifest_sha256


# ------------------------------------------------------------------- policy


@dataclass(frozen=True)
class RetentionPolicy:
    """One tenant's schedules, with the platform default filled in where it may be.

    `generated_document_retention_days` is `None` when the customer has not
    stated one, and stays None -- there is no default to fill in, because §16
    says the period is theirs to choose.
    """

    org_id: str
    source_retention_days: int
    generated_document_retention_days: int | None
    recorded: bool

    @property
    def retains_documents_indefinitely(self) -> bool:
        return self.generated_document_retention_days is None


def policy_for(db: Session, org_id: str) -> RetentionPolicy:
    """The recorded policy, or the platform default where §16 permits one."""
    if not org_id or not str(org_id).strip():
        raise ValueError("policy_for needs an org_id")
    row = db.scalar(select(OrgDataPolicy).where(OrgDataPolicy.org_id == org_id))
    if row is None:
        return RetentionPolicy(
            org_id=org_id,
            source_retention_days=settings.default_source_retention_days,
            generated_document_retention_days=None,
            recorded=False,
        )
    return RetentionPolicy(
        org_id=org_id,
        source_retention_days=int(row.source_retention_days),
        generated_document_retention_days=(
            None if row.generated_document_retention_days is None
            else int(row.generated_document_retention_days)
        ),
        recorded=True,
    )


def set_policy(
    db: Session,
    org_id: str,
    *,
    source_retention_days: int | None = None,
    generated_document_retention_days: int | None = None,
    residency: str | None = None,
    zero_retention_required: bool | None = None,
    updated_by: str | None = None,
    clear_document_retention: bool = False,
) -> OrgDataPolicy:
    """Record or amend a tenant's policy. Returns the stored row.

    `clear_document_retention` exists because None already means "leave this
    field alone" here, and a customer withdrawing a retention period -- going
    back to keeping their contracts indefinitely -- needs a way to say so that
    is not the same word as saying nothing.
    """
    from app.tenancy import RESIDENCIES  # local: tenancy imports models lazily too

    row = db.scalar(select(OrgDataPolicy).where(OrgDataPolicy.org_id == org_id))
    if row is None:
        row = OrgDataPolicy(org_id=org_id, source_retention_days=settings.default_source_retention_days)
        db.add(row)

    if source_retention_days is not None:
        if int(source_retention_days) < 1:
            raise ValueError(
                "source_retention_days must be at least 1: a schedule of zero deletes an "
                "upload before the batch that needs it has run."
            )
        row.source_retention_days = int(source_retention_days)

    if clear_document_retention:
        row.generated_document_retention_days = None
    elif generated_document_retention_days is not None:
        if int(generated_document_retention_days) < 1:
            raise ValueError("generated_document_retention_days must be at least 1")
        row.generated_document_retention_days = int(generated_document_retention_days)

    if residency is not None:
        normalised = residency.strip().upper()
        if normalised not in RESIDENCIES:
            raise ValueError(
                f"residency={residency!r} is not one of {sorted(RESIDENCIES)}. An unrecognised "
                "region cannot be checked against a deployment at the model boundary."
            )
        row.residency = normalised

    if zero_retention_required is not None:
        row.zero_retention_required = bool(zero_retention_required)

    row.updated_by = updated_by
    db.flush()
    return row


# ------------------------------------------------------- schema introspection


def _org_tables():
    """Every mapped table carrying `org_id`, children before parents.

    `sorted_tables` orders by foreign-key dependency, parents first; reversed,
    a child is always deleted before the row it points at, which is what
    PostgreSQL's referential integrity requires and what SQLite would let us get
    wrong silently.

    Derived from the metadata rather than listed, because the list is the thing
    that goes stale. A table added by a future migration is swept the day it
    appears.
    """
    return [t for t in reversed(Base.metadata.sorted_tables) if "org_id" in t.columns]


def cascade_tables():
    """The org-scoped tables an offboarding empties, in a safe order."""
    skip = set(IDENTITY_TABLES) | {CERTIFICATE_TABLE}
    return [t for t in _org_tables() if t.name not in skip]


def _blob_column(table):
    return table.columns.get(BLOB_COLUMN)


def _primary_key_column(table):
    keys = list(table.primary_key.columns)
    return keys[0] if len(keys) == 1 else None


# ---------------------------------------------------------------- blob removal


def _remove_blobs(paths) -> list[str]:
    """Unlink the files behind a set of rows. Returns the ones that were there.

    A path that has already gone is not an error -- a retry after a partial
    failure has to be able to finish. Every other failure is: a permission
    error, or a `blob_path` that turns out to name a directory, means a file we
    told a customer we destroyed is still there, and reporting success over that
    is the failure mode this whole module exists to prevent.
    """
    removed = []
    for rel in paths:
        if not rel:
            continue
        target = abs_path(rel)
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        removed.append(str(rel))
    return removed


# ------------------------------------------------------- the deletion cascade


def _delete_rows(db: Session, table, where, manifest: DeletionManifest) -> list[str]:
    """Delete matching rows, recording their ids and collecting their blobs.

    The ids are read before the delete because they are the manifest, and a
    manifest assembled after the rows are gone is a manifest of nothing.
    """
    pk = _primary_key_column(table)
    if pk is None:
        # A composite or absent primary key leaves nothing to name the deleted
        # rows by, so the manifest would under-report and the certificate's hash
        # would cover less than was actually destroyed. Refusing is the only
        # honest option: a receipt for part of a deletion is worse than none.
        raise ValueError(
            f"{table.name} has no single-column primary key, so its rows cannot be listed on "
            "a deletion manifest. Give it one, or exclude it deliberately."
        )
    blob = _blob_column(table)

    columns = [c for c in (pk, blob) if c is not None]
    rows = db.execute(select(*columns).where(where)).all()
    if not rows:
        return []

    ids, blobs = [], []
    for row in rows:
        ids.append(str(row[0]))
        if blob is not None and row[1]:
            blobs.append(row[1])

    db.execute(delete(table).where(where))
    manifest.record(table.name, ids)
    manifest.blobs.extend(blobs)
    return ids


def delete_source_file(
    db: Session,
    *,
    org_id: str,
    source_file_id: str,
    vector_index=None,
    mapping_memory=None,
) -> DeletionManifest:
    """Destroy one source upload and everything computed from it.

    §16's hardest sentence: "an embedding derived from deleted data is still
    derived from it". So this removes the upload's versions and chunks, the
    blobs on disk, every row in any table that records which source version it
    came from -- manifest bindings today, whatever a later migration adds
    tomorrow -- and the embeddings and mapping-memory entries built from those
    chunks.

    What it deliberately does NOT touch is generated documents. They are
    governed by a different clause of the same section -- the customer's records
    policy -- and an offer letter that has already been signed does not stop
    being a record because the spreadsheet it was built from has expired.

    The `vector_index` and `mapping_memory` arguments are the retrieval stores
    to forget from. They are parameters rather than module-level singletons
    because these stores are constructed per caller; pass the ones the process
    is actually using, or the cascade will report a truthful zero.
    """
    manifest = DeletionManifest(scope=SOURCE_FILE, scope_id=source_file_id, org_id=org_id)

    source = db.scalar(
        select(SourceFile).where(SourceFile.id == source_file_id, SourceFile.org_id == org_id)
    )
    if source is None:
        raise LookupError(
            f"source file {source_file_id!r} is not in organisation {org_id!r}. Deleting by "
            "id without the tenant check is how one customer's request destroys another's data."
        )

    version_ids = [
        v for v in db.scalars(
            select(SourceVersion.id).where(SourceVersion.source_file_id == source_file_id)
        )
    ]

    # The columns this file's chunks were built from. Mapping memory records the
    # column a reviewer approved, and nothing links an approval back to the file
    # it came from -- so the link is reconstructed from the flattened
    # `"column: value"` chunks `source_ingestion` writes. Over-inclusive by
    # design: forgetting one mapping too many costs a re-approval, keeping one
    # too few leaves a customer's field names in a memory we said we cleared.
    columns = _columns_in_chunks(db, version_ids)

    chunk_ids = list(
        db.scalars(select(SourceChunk.id).where(SourceChunk.source_version_id.in_(version_ids)))
    ) if version_ids else []

    if version_ids:
        _delete_rows(db, SourceChunk.__table__, SourceChunk.source_version_id.in_(version_ids), manifest)
        _delete_rows(
            db, ManifestBinding.__table__,
            ManifestBinding.source_version_id.in_(version_ids), manifest,
        )
        _delete_rows(db, SourceVersion.__table__, SourceVersion.id.in_(version_ids), manifest)

    _sweep_source_derivations(
        db, org_id, manifest,
        version_ids=version_ids, source_file_id=source_file_id,
        chunk_ids=chunk_ids, columns=columns,
    )
    _delete_rows(db, SourceFile.__table__, SourceFile.id == source_file_id, manifest)

    manifest.blobs = _remove_blobs(manifest.blobs)
    _forget_derived_records(
        manifest,
        vector_index=vector_index,
        mapping_memory=mapping_memory,
        org_id=org_id,
        record_ids=chunk_ids,
        source_columns=columns,
    )
    db.flush()
    return manifest


def _columns_in_chunks(db: Session, version_ids) -> set[str]:
    """Column names visible in a set of chunks' flattened text.

    `source_ingestion` writes one chunk per spreadsheet row as
    `"employee_id: 44182; full_name: Dana Ruiz"`, so the column names are
    recoverable from the text with the same split the LLM boundary uses. Prose
    chunks yield the occasional false positive, which deletes a mapping-memory
    entry that did not need deleting -- the direction this module errs in on
    purpose.
    """
    if not version_ids:
        return set()
    found: set[str] = set()
    texts = db.scalars(
        select(SourceChunk.text).where(SourceChunk.source_version_id.in_(version_ids))
    )
    for text in texts:
        for segment in (text or "").split(";"):
            column, sep, value = segment.partition(":")
            if sep and column.strip() and value.strip():
                found.add(column.strip())
    return found


def _column_candidates(columns) -> set:
    """Every spelling of a source column name a derived row might have stored.

    Three of them, because three different stores wrote them: the reviewer's
    own spelling ("Annual Salary"), the lowercase form, and the normalised key
    `mapping_memory` files its uniqueness constraint under. Matching the
    normaliser that produced the key -- rather than reimplementing it here --
    is what stops "Joining Dt" surviving a deletion that removed "joining_dt".
    """
    from app.retrieval.vector import normalise_text  # local: numpy is heavy to import

    candidates = set()
    for column in columns:
        name = (column or "").strip()
        if not name:
            continue
        candidates.update({name, name.lower(), normalise_text(name)})
    return {c for c in candidates if c}


def _sweep_source_derivations(
    db: Session,
    org_id: str,
    manifest: DeletionManifest,
    *,
    version_ids,
    source_file_id: str,
    chunk_ids,
    columns,
) -> None:
    """Delete every row anywhere in the schema that was computed from this upload.

    Three ways a row can be a derivation, and all three are matched by column
    name across the whole schema rather than by a list of tables. A list is the
    thing that goes stale, and the failure when it does is silent: a table added
    next year holds a customer's field names, nobody adds it here, and the
    deletion reports success.

      * it names the upload         -- `source_version_id`, `source_file_id`
      * it names something in it    -- `record_id`, which is how `embeddings`
                                       files a vector against the chunk or the
                                       column description it was built from
      * it names one of its columns -- `source_column`, `accepted_column` and
                                       friends, which is where a customer's
                                       field names live longest

    The third is deliberately over-inclusive: two files with a column called
    "Start Date" are indistinguishable here, so deleting one forgets the
    mappings learned from both. That costs a reviewer one re-approval. The
    other direction costs a customer a field name we said we had erased.
    """
    # Scrubbed rather than deleted, so it is exempt from the sweep below and
    # handled explicitly afterwards. A generation record is the audit trail for a
    # letter that may already be signed, and §16 governs those by the customer's
    # records policy rather than by the source file's retention schedule. What
    # cannot stay is the verbatim copy of the row inside it.
    scrub_only = {ManifestGeneration.__table__.name}
    already = {SourceChunk.__table__.name, ManifestBinding.__table__.name, SourceVersion.__table__.name}
    candidates = _column_candidates(columns)
    record_candidates = set(chunk_ids) | candidates

    for table in _org_tables():
        if table.name in already or table.name in scrub_only or table.name == SourceFile.__table__.name:
            continue
        scoped = table.c.org_id == org_id

        for column_name in SOURCE_DERIVATION_COLUMNS:
            column = table.columns.get(column_name)
            if column is None:
                continue
            values = version_ids if column_name == "source_version_id" else [source_file_id]
            if values:
                _delete_rows(db, table, scoped & column.in_(values), manifest)

        for column_name in DERIVED_RECORD_COLUMNS:
            column = table.columns.get(column_name)
            if column is not None and record_candidates:
                _delete_rows(db, table, scoped & column.in_(record_candidates), manifest)

        for column_name in SOURCE_COLUMN_COLUMNS:
            column = table.columns.get(column_name)
            if column is not None and candidates:
                _delete_rows(db, table, scoped & column.in_(candidates), manifest)

    _scrub_generation_records(db, org_id, manifest, version_ids=version_ids)


def _scrub_generation_records(db: Session, org_id: str, manifest: DeletionManifest, *, version_ids) -> None:
    """Empty the verbatim source row out of every generation built from this upload.

    `ManifestGeneration.source_record` is a whole spreadsheet row -- salary,
    identifiers, name -- copied at generation time so a reviewer could see what
    the letter was filled from. §16's rule is unambiguous about what that makes
    it once the upload is gone: "an embedding derived from deleted data is still
    derived from it", and a verbatim copy is the least derived thing there is.

    The row itself stays. It is the audit record for a letter that may already
    have been signed, and §17 wants that letter traceable to the record that
    produced it. What §17 actually asks for is the "source file/version + row
    key", which survives in `source_record_key` -- so lineage is intact and the
    payload is not.
    """
    if not version_ids:
        return

    table = ManifestGeneration.__table__
    rows = db.execute(
        select(table.c.id).where(
            (table.c.org_id == org_id)
            & table.c.source_version_id.in_(version_ids)
            # Cast to text rather than comparing the JSON column directly:
            # PostgreSQL defines no equality operator for `json` (only for
            # `jsonb`), so `source_record != {}` is a runtime error there and
            # silently fine on SQLite. The cast means the same predicate runs on
            # both, which is the whole point of the column being portable.
            & (cast(table.c.source_record, Text) != "{}")
        )
    ).scalars().all()
    if not rows:
        return

    db.execute(
        table.update()
        .where(table.c.id.in_(rows))
        .values(source_record={"_scrubbed": "source upload deleted under the retention policy"})
    )
    manifest.scrubbed[table.name] = manifest.scrubbed.get(table.name, 0) + len(rows)


def _forget_derived_records(
    manifest: DeletionManifest,
    *,
    vector_index,
    mapping_memory,
    org_id: str,
    record_ids,
    source_columns,
) -> None:
    """Also forget from an in-process retrieval index, if one was handed over.

    The persistent `embeddings` and `mapping_memory` tables are already covered
    by `_sweep_source_derivations`, which deletes their rows in SQL. This is for
    the in-process index a worker may still be holding: its rows are not in the
    database, so nothing above can reach them, and a warm cache that keeps
    serving a deleted customer's vectors is a deletion that did not happen.

    Duck-typed on purpose. The retrieval layer is mid-migration between the two
    shapes, and a cascade written against one of them would quietly stop
    cascading on the day the other landed.
    """
    if vector_index is not None and record_ids:
        forget = getattr(vector_index, "forget_records", None)
        if callable(forget):
            manifest.derived["embeddings"] = int(
                forget(org_id=org_id, record_ids=list(record_ids)) or 0
            )

    if mapping_memory is not None and source_columns:
        forget = getattr(mapping_memory, "forget_source_columns", None)
        if callable(forget):
            manifest.derived["mapping_memory"] = int(
                forget(org_id=org_id, source_columns=sorted(source_columns)) or 0
            )


# ------------------------------------------------------------------- sweeps


def _cutoff(days: int, now: datetime | None = None) -> datetime:
    reference = now or datetime.now(timezone.utc)
    # The timestamps in this schema are naive UTC (`DateTime`, no timezone), so
    # the cutoff has to be naive too or every comparison raises.
    if reference.tzinfo is not None:
        reference = reference.astimezone(timezone.utc).replace(tzinfo=None)
    return reference - timedelta(days=days)


def sweep_expired_sources(
    db: Session,
    *,
    org_id: str,
    now: datetime | None = None,
    vector_index=None,
    mapping_memory=None,
) -> DeletionManifest:
    """Delete source uploads past this tenant's schedule, with their derivations.

    §16 puts source uploads first for a reason: they are the artefact that holds
    every salary and every identifier in the clear, and once a manifest has been
    compiled and the batch has run there is very little left to want from them.
    """
    policy = policy_for(db, org_id)
    cutoff = _cutoff(policy.source_retention_days, now)
    manifest = DeletionManifest(scope=SOURCE_FILE, scope_id=None, org_id=org_id)
    if not policy.recorded:
        manifest.skipped["source_retention_policy"] = (
            f"no recorded policy; applied the platform default of "
            f"{policy.source_retention_days} days"
        )

    expired = list(db.scalars(
        select(SourceFile.id).where(SourceFile.org_id == org_id, SourceFile.created_at < cutoff)
    ))
    for source_file_id in expired:
        one = delete_source_file(
            db, org_id=org_id, source_file_id=source_file_id,
            vector_index=vector_index, mapping_memory=mapping_memory,
        )
        for table, ids in one.rows.items():
            manifest.record(table, ids)
        manifest.blobs.extend(one.blobs)
        for store, count in one.derived.items():
            manifest.derived[store] = manifest.derived.get(store, 0) + count
    return manifest


def sweep_expired_documents(db: Session, *, org_id: str, now: datetime | None = None) -> DeletionManifest:
    """Delete generated documents past the period the CUSTOMER stated.

    And nothing at all until they state one. §16: retained "according to the
    customer's records policy, not a default of your choosing". A signed offer
    letter deleted on our schedule is not housekeeping.
    """
    policy = policy_for(db, org_id)
    manifest = DeletionManifest(scope=GENERATED_DOCUMENTS, scope_id=None, org_id=org_id)

    if policy.retains_documents_indefinitely:
        manifest.skipped["generated_document_retention_days"] = (
            "this organisation has not stated a records-retention period, so nothing was "
            "deleted. §16 leaves that period to the customer rather than defaulting it."
        )
        return manifest

    cutoff = _cutoff(policy.generated_document_retention_days, now)
    expired = list(db.scalars(
        select(GeneratedDocument.id).where(
            GeneratedDocument.org_id == org_id, GeneratedDocument.created_at < cutoff
        )
    ))
    if not expired:
        return manifest

    # Versions first: they carry the rendered DOCX on disk, and the parent row
    # is what makes them findable.
    _delete_rows(db, DocumentVersion.__table__, DocumentVersion.document_id.in_(expired), manifest)
    # The per-generation audit record is a QA artefact in §16's sense -- it holds
    # the resolved field lineage, which means it holds the source values. It
    # expires with the document it describes rather than outliving it.
    _delete_rows(
        db, ManifestGeneration.__table__,
        (ManifestGeneration.org_id == org_id) & (ManifestGeneration.created_at < cutoff),
        manifest,
    )
    _delete_rows(db, GeneratedDocument.__table__, GeneratedDocument.id.in_(expired), manifest)
    manifest.blobs = _remove_blobs(manifest.blobs)
    db.flush()
    return manifest


def delete_generated_document(
    db: Session,
    *,
    org_id: str,
    document_id: str,
) -> DeletionManifest:
    """Destroy one generated document at a reviewer's request.

    The same cascade `sweep_expired_documents` performs on a schedule, narrowed
    to one row. It is a hard delete rather than a `deleted_at` flag because
    `generated_documents` has no such column, and adding one would be the wrong
    fix: §16 requires deletion to reach the blob, and a soft-deleted row leaves
    the rendered DOCX on disk while telling the customer it is gone.

    The per-generation record goes too. `ManifestGeneration` holds the resolved
    field lineage for the render, which means it holds the source values that
    were written into the letter -- keeping it would leave the document's
    contents behind in the table that exists to explain them.

    `qa_failure_logs` deliberately stays, matching `sweep_expired_documents`.
    Those rows are a measurement of the system rather than a copy of the
    customer's data, and §22 computes the escaped-error rate from them: deleting
    the log of a check that fired would quietly improve that number every time
    somebody tidied up a document.

    Refuses across tenants rather than returning an empty manifest: deleting by
    id without the tenant check is how one customer's request destroys
    another's data.
    """
    manifest = DeletionManifest(scope=GENERATED_DOCUMENTS, scope_id=document_id, org_id=org_id)

    document = db.scalar(
        select(GeneratedDocument).where(
            GeneratedDocument.id == document_id, GeneratedDocument.org_id == org_id
        )
    )
    if document is None:
        raise LookupError(
            f"generated document {document_id!r} is not in organisation {org_id!r}."
        )

    # `manifest_generations` has no document_id. The link is the blob: the
    # generate handler writes one rendered path and stores it on both the
    # version and the generation record, so the paths are the join. Read them
    # before the versions are deleted, or there is nothing left to match on.
    blob_paths = [
        path for path in db.scalars(
            select(DocumentVersion.blob_path).where(DocumentVersion.document_id == document_id)
        ) if path
    ]

    _delete_rows(db, DocumentVersion.__table__, DocumentVersion.document_id == document_id, manifest)
    if blob_paths:
        _delete_rows(
            db, ManifestGeneration.__table__,
            (ManifestGeneration.org_id == org_id) & (ManifestGeneration.blob_path.in_(blob_paths)),
            manifest,
        )
    _delete_rows(db, GeneratedDocument.__table__, GeneratedDocument.id == document_id, manifest)
    manifest.blobs = _remove_blobs(manifest.blobs)
    db.flush()
    return manifest


# -------------------------------------------------------------- offboarding


def offboard_organisation(
    db: Session,
    *,
    org_id: str,
    actor: User | None = None,
    purge_identity: bool = True,
    vector_index=None,
    mapping_memory=None,
) -> tuple[DeletionCertificate, DeletionManifest]:
    """Empty a tenant and hand back proof.

    Every org-scoped table, children before parents, then the blobs, then the
    retrieval stores, then -- when `purge_identity` -- the people and the
    organisation row itself. The users are personal data too: names, email
    addresses and job titles belonging to a customer's employees, and leaving
    them behind so the audit trail stays tidy would be us keeping the part of
    the payload that identifies people.

    The certificate survives all of it. Its `org_id` becomes a dangling
    reference by design, which is why it carries `org_name`, and it is the one
    org-scoped row an offboarding never deletes.
    """
    org = db.get(Organization, org_id)
    manifest = DeletionManifest(scope=ORGANISATION, scope_id=org_id, org_id=org_id)

    # Read the identity facts the certificate needs before anything is deleted.
    org_name = org.name if org is not None else None
    issued_by = actor.id if actor is not None else None
    issued_by_email = actor.email if actor is not None else None

    for table in cascade_tables():
        _delete_rows(db, table, table.c.org_id == org_id, manifest)

    if purge_identity:
        for name in IDENTITY_TABLES:
            table = Base.metadata.tables[name]
            key = table.c.org_id if "org_id" in table.columns else table.c.id
            _delete_rows(db, table, key == org_id, manifest)

    manifest.blobs = _remove_blobs(manifest.blobs)

    for store, label in ((vector_index, "embeddings"), (mapping_memory, "mapping_memory")):
        if store is None:
            continue
        forget = getattr(store, "forget_org", None)
        if callable(forget):
            manifest.derived[label] = int(forget(org_id) or 0)

    certificate = DeletionCertificate(
        org_id=org_id,
        org_name=org_name,
        scope=ORGANISATION,
        scope_id=org_id,
        counts=manifest.counts,
        blobs_deleted=len(manifest.blobs),
        derived_forgotten=dict(manifest.derived),
        manifest_sha256=manifest.digest(),
        issued_by=issued_by,
        issued_by_email=issued_by_email,
        issued_at=datetime.now(timezone.utc),
    )
    db.add(certificate)
    db.flush()
    return certificate, manifest


def certificate_for(db: Session, manifest: DeletionManifest, *, actor: User | None = None) -> DeletionCertificate:
    """Record a certificate for a narrower deletion -- one file, one sweep.

    Offboarding is not the only deletion a customer may want evidence of. A
    retention sweep that removed four hundred payroll extracts is exactly the
    kind of thing a DPIA asks to see, and a certificate is cheaper to produce
    now than to reconstruct from logs later.
    """
    org = db.get(Organization, manifest.org_id)
    certificate = DeletionCertificate(
        org_id=manifest.org_id,
        org_name=org.name if org is not None else None,
        scope=manifest.scope,
        scope_id=manifest.scope_id,
        counts=manifest.counts,
        blobs_deleted=len(manifest.blobs),
        derived_forgotten=dict(manifest.derived),
        manifest_sha256=manifest.digest(),
        issued_by=actor.id if actor is not None else None,
        issued_by_email=actor.email if actor is not None else None,
        issued_at=datetime.now(timezone.utc),
    )
    db.add(certificate)
    db.flush()
    return certificate
