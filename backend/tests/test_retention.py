"""Retention, the deletion cascade, and the certificate that proves it happened.

§16's "Retention and deletion" clause, pinned failure by failure.

The failures these tests describe are not hypothetical. A payroll extract left
on disk after the batch that needed it is the single most sensitive artefact
this system holds and the least useful to keep. An embedding built from a
deleted salary column is still built from that salary. A signed offer letter
deleted on a schedule we invented rather than the customer's is a records
incident we caused. And a deletion certificate whose hash nobody can recompute
is a promise, not evidence.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import os

import pytest
from sqlalchemy import select

def _on_postgres() -> bool:
    """Whether this run is against PostgreSQL rather than the SQLite default.

    Some tests here assert the *inert* half of a control -- what happens on a
    backend that cannot express row-level security. Those are real assertions
    worth keeping (the suite runs on SQLite by default, and a no-op that started
    raising would take it down), but they describe SQLite, so they are skipped
    when TEST_DATABASE_URL points the same suite at PostgreSQL.
    """
    return os.environ.get("DATABASE_URL", "").startswith("postgresql")

from app import retention
from app.db import SessionLocal
from app.models import (
    Conversation, DeletionCertificate, DocumentReview, DocumentVersion, GeneratedDocument,
    LookupValue, ManifestBinding, Organization, OrgDataPolicy, Project, ReviewComment,
    ReviewTask, SourceChunk, SourceFile, SourceVersion, TemplateFile, TemplateManifest,
    TemplateVersion, User,
)
from app.security import hash_password
from app.storage import abs_path


#: bcrypt is deliberately slow, and this module makes a dozen throwaway
#: organisations. Hashing the same throwaway password once keeps the cost at one
#: KDF run for the file rather than one per test; nothing here is testing the KDF.
_THROWAWAY_PASSWORD_HASH = hash_password("pw")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _write_blob(rel: str, body: bytes = b"payroll") -> str:
    target = abs_path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return rel


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def throwaway_org(app_client):
    """An organisation created for one test and destroyed by it.

    Offboarding deletes everything an organisation has, so it cannot be run
    against the shared tenancy fixture without taking the rest of the suite with
    it. Each caller gets its own.
    """
    made = []

    def _make(tag: str, display_id: int):
        db = SessionLocal()
        try:
            org = Organization(name=f"Retention {tag}")
            db.add(org)
            db.flush()
            user = User(
                org_id=org.id, email=f"retention-{tag}@tenant.test", full_name=f"Ret {tag}",
                password_hash=_THROWAWAY_PASSWORD_HASH, role_key="org_admin",
            )
            db.add(user)
            db.flush()
            project = Project(
                org_id=org.id, display_id=display_id, name=f"P {tag}", region="Europe",
                function="Human Resources", document_type="Offer Letter", language="English",
                status="pending", created_by=user.id,
            )
            db.add(project)
            db.commit()
            made.append(org.id)
            return org.id, user.id, project.id
        finally:
            db.close()

    yield _make


#: Display ids are globally unique and the suite shares one database, so each
#: fixture instance takes the next one rather than a literal.
_display_ids = itertools.count(91200)


@pytest.fixture()
def source_with_derivations(app_client, throwaway_org):
    """One upload, its versions, its chunks, a binding, and files on disk.

    The shape §16 describes: a source file is never just a row, and the point of
    the cascade is everything that grew out of it.
    """
    org_id, user_id, project_id = throwaway_org(
        f"cascade-{uuid.uuid4().hex[:8]}", next(_display_ids)
    )
    db = SessionLocal()
    try:
        source = SourceFile(
            org_id=org_id, project_id=project_id, name="payroll.csv", file_type="csv",
            status="ready", created_by=user_id,
        )
        db.add(source)
        db.flush()
        version = SourceVersion(
            source_file_id=source.id, org_id=org_id, version_no=1,
            blob_path=_write_blob(f"sources/{project_id}/payroll.csv"), created_by=user_id,
        )
        db.add(version)
        db.flush()
        for index, text in enumerate([
            "employee_id: 44182; full_name: Dana Ruiz; annual_salary: 118400",
            "employee_id: 44183; full_name: Sam Okafor; annual_salary: 96250",
        ]):
            db.add(SourceChunk(
                project_id=project_id, source_version_id=version.id, org_id=org_id,
                chunk_index=index, element_type="row", heading_path="", text=text,
                token_count=8, content_sha256=f"sha-{index}",
            ))
        manifest = TemplateManifest(
            org_id=org_id, template_version_id="tv-1", version_no=1, status="approved",
            created_by=user_id,
        )
        db.add(manifest)
        db.flush()
        db.add(ManifestBinding(
            org_id=org_id, manifest_id=manifest.id, source_version_id=version.id,
            field_bindings={"full_name": "full_name"}, value_map={}, created_by=user_id,
        ))
        db.commit()
        return {
            "org_id": org_id, "user_id": user_id, "project_id": project_id,
            "source_id": source.id, "version_id": version.id, "blob": version.blob_path,
        }
    finally:
        db.close()


class _FakeVectorIndex:
    """Enough of the retrieval store to prove the cascade reaches it."""

    def __init__(self):
        self.forgotten_records: list = []
        self.forgotten_orgs: list = []

    def forget_records(self, *, org_id, record_ids):
        self.forgotten_records.append((org_id, list(record_ids)))
        return len(record_ids)

    def forget_org(self, org_id):
        self.forgotten_orgs.append(org_id)
        return 7


class _FakeMappingMemory:
    def __init__(self):
        self.forgotten_columns: list = []
        self.forgotten_orgs: list = []

    def forget_source_columns(self, *, org_id, source_columns):
        self.forgotten_columns.append((org_id, list(source_columns)))
        return len(source_columns)

    def forget_org(self, org_id):
        self.forgotten_orgs.append(org_id)
        return 3


# -------------------------------------------------------------------- policy


def test_an_organisation_with_no_recorded_policy_still_expires_its_uploads(app_client, throwaway_org):
    """A tenant that has never set a schedule must not thereby keep every
    payroll extract forever. §16 puts source uploads on a schedule by default;
    it is generated documents it refuses to default."""
    org_id, _user_id, _project_id = throwaway_org("nopolicy", 91102)
    db = SessionLocal()
    try:
        policy = retention.policy_for(db, org_id)
        assert policy.recorded is False
        assert policy.source_retention_days > 0
        assert policy.generated_document_retention_days is None
        assert policy.retains_documents_indefinitely is True
    finally:
        db.close()


def test_generated_documents_have_no_retention_period_until_the_customer_states_one(app_client, throwaway_org):
    """§16: retained "according to the customer's records policy, not a default
    of your choosing". Deleting a signed employment contract on a schedule we
    invented is a records incident we caused, not housekeeping."""
    org_id, _user_id, _project_id = throwaway_org("customerpolicy", 91103)
    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, source_retention_days=14)
        db.commit()
        assert retention.policy_for(db, org_id).generated_document_retention_days is None

        retention.set_policy(db, org_id, generated_document_retention_days=2555)
        db.commit()
        assert retention.policy_for(db, org_id).generated_document_retention_days == 2555

        # And withdrawing it is a thing the customer can say, distinct from
        # saying nothing.
        retention.set_policy(db, org_id, clear_document_retention=True)
        db.commit()
        assert retention.policy_for(db, org_id).retains_documents_indefinitely is True
    finally:
        db.close()


def test_a_retention_schedule_of_zero_days_is_refused(app_client, throwaway_org):
    """Zero deletes the upload before the batch that needs it has run, which
    presents as a generation failure nobody can reproduce."""
    org_id, _user_id, _project_id = throwaway_org("zerodays", 91104)
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="at least 1"):
            retention.set_policy(db, org_id, source_retention_days=0)
    finally:
        db.rollback()
        db.close()


def test_an_unrecognised_residency_is_refused_at_the_point_it_is_recorded(app_client, throwaway_org):
    """A region the boundary cannot check against a deployment is a requirement
    that will never be enforced, which is worse than not recording one."""
    org_id, _user_id, _project_id = throwaway_org("badregion", 91105)
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="not one of"):
            retention.set_policy(db, org_id, residency="Ireland")
    finally:
        db.rollback()
        db.close()


# -------------------------------------------------------- the deletion cascade


def test_deleting_a_source_upload_takes_its_chunks_versions_and_bindings(app_client, source_with_derivations):
    """§16: "an embedding derived from deleted data is still derived from it."
    The row is the least of it -- the salaries have by then been copied into
    chunks and referenced by bindings."""
    ctx = source_with_derivations
    db = SessionLocal()
    try:
        manifest = retention.delete_source_file(
            db, org_id=ctx["org_id"], source_file_id=ctx["source_id"],
        )
        db.commit()

        assert db.scalar(select(SourceFile).where(SourceFile.id == ctx["source_id"])) is None
        assert db.scalars(
            select(SourceVersion).where(SourceVersion.source_file_id == ctx["source_id"])
        ).all() == []
        assert db.scalars(
            select(SourceChunk).where(SourceChunk.source_version_id == ctx["version_id"])
        ).all() == []
        assert db.scalars(
            select(ManifestBinding).where(ManifestBinding.source_version_id == ctx["version_id"])
        ).all() == []

        assert manifest.counts["source_chunks"] == 2
        assert manifest.counts["manifest_bindings"] == 1
        assert manifest.counts["source_files"] == 1
    finally:
        db.close()


def test_deleting_a_source_upload_removes_the_file_from_disk(app_client, source_with_derivations):
    """A deletion that reports success while the spreadsheet is still on disk is
    the exact failure this module exists to prevent -- and the one a customer
    would never discover until it mattered."""
    ctx = source_with_derivations
    blob = abs_path(ctx["blob"])
    assert blob.exists(), "fixture did not write the blob"

    db = SessionLocal()
    try:
        manifest = retention.delete_source_file(
            db, org_id=ctx["org_id"], source_file_id=ctx["source_id"],
        )
        db.commit()
        assert not blob.exists()
        assert ctx["blob"] in manifest.blobs
    finally:
        db.close()


def test_the_cascade_reaches_the_embeddings_and_the_mapping_memory(app_client, source_with_derivations):
    """The clause §16 spends a whole sentence on. A vector built from a deleted
    chunk, and a mapping learned from a deleted column, are both still that
    customer's data."""
    ctx = source_with_derivations
    index, memory = _FakeVectorIndex(), _FakeMappingMemory()

    db = SessionLocal()
    try:
        manifest = retention.delete_source_file(
            db, org_id=ctx["org_id"], source_file_id=ctx["source_id"],
            vector_index=index, mapping_memory=memory,
        )
        db.commit()

        assert index.forgotten_records, "no embeddings were forgotten"
        forgotten_org, forgotten_ids = index.forgotten_records[0]
        assert forgotten_org == ctx["org_id"]
        assert len(forgotten_ids) == 2
        assert manifest.derived["embeddings"] == 2

        assert memory.forgotten_columns, "mapping memory was never asked to forget"
        _org, columns = memory.forgotten_columns[0]
        # Recovered from the flattened `"column: value"` chunks the ingest path
        # writes, because nothing links an approval back to its source file.
        assert {"employee_id", "full_name", "annual_salary"} <= set(columns)
    finally:
        db.close()


def test_deleting_another_tenants_upload_by_id_is_refused(app_client, source_with_derivations, throwaway_org):
    """Deleting by id without the tenant check is how one customer's offboarding
    destroys another customer's data."""
    ctx = source_with_derivations
    other_org, _user_id, _project_id = throwaway_org("otherorg", 91106)
    db = SessionLocal()
    try:
        with pytest.raises(LookupError, match="not in organisation"):
            retention.delete_source_file(db, org_id=other_org, source_file_id=ctx["source_id"])
    finally:
        db.rollback()
        db.close()


def test_the_cascade_covers_every_org_scoped_table_the_models_declare(app_client):
    """The list is computed from the schema rather than written down, so a table
    added next year is emptied on offboarding without anyone remembering this
    module exists."""
    from app.db import Base

    swept = {t.name for t in retention.cascade_tables()}
    org_scoped = {n for n, t in Base.metadata.tables.items() if "org_id" in t.columns}
    missing = org_scoped - swept - set(retention.IDENTITY_TABLES) - {retention.CERTIFICATE_TABLE}
    assert not missing, f"org-scoped tables an offboarding would leave behind: {sorted(missing)}"


def test_children_are_swept_before_the_rows_they_point_at(app_client):
    """PostgreSQL enforces referential integrity and SQLite mostly does not, so
    a wrong order here is green in the suite and a foreign-key violation in
    production."""
    order = [t.name for t in retention.cascade_tables()]
    for child, parent in [
        ("source_chunks", "source_versions"),
        ("source_versions", "source_files"),
        ("document_versions", "generated_documents"),
        ("chat_messages", "conversations"),
        ("template_sections", "template_versions"),
        ("template_versions", "template_files"),
        ("source_files", "projects"),
    ]:
        assert order.index(child) < order.index(parent), f"{child} must be deleted before {parent}"


# -------------------------------------------------------------------- sweeps


def test_the_sweep_deletes_uploads_past_the_schedule_and_keeps_the_rest(app_client, throwaway_org):
    """The schedule is the point. §16 wants source uploads gone on a timetable,
    not whenever somebody remembers."""
    org_id, user_id, project_id = throwaway_org("sweep", 91107)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)

    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, source_retention_days=30)
        stale = SourceFile(
            org_id=org_id, project_id=project_id, name="old.csv", file_type="csv",
            status="ready", created_by=user_id, created_at=old,
        )
        fresh = SourceFile(
            org_id=org_id, project_id=project_id, name="new.csv", file_type="csv",
            status="ready", created_by=user_id,
        )
        db.add_all([stale, fresh])
        db.commit()
        stale_id, fresh_id = stale.id, fresh.id

        manifest = retention.sweep_expired_sources(db, org_id=org_id)
        db.commit()

        assert db.get(SourceFile, stale_id) is None
        assert db.get(SourceFile, fresh_id) is not None
        assert manifest.counts["source_files"] == 1
    finally:
        db.close()


def test_the_document_sweep_deletes_nothing_and_says_why_when_no_policy_is_recorded(app_client, throwaway_org):
    """Silence would be indistinguishable from "there was nothing to delete".
    The one thing a compliance reviewer needs to know is that the customer has
    not stated a period."""
    org_id, _user_id, project_id = throwaway_org("nodocpolicy", 91108)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4000)
        document = GeneratedDocument(
            org_id=org_id, project_id=project_id, display_id=91108, language="en",
            status="draft", created_at=old,
        )
        db.add(document)
        db.commit()
        document_id = document.id

        manifest = retention.sweep_expired_documents(db, org_id=org_id)
        db.commit()

        assert manifest.total_rows == 0
        assert db.get(GeneratedDocument, document_id) is not None
        assert "generated_document_retention_days" in manifest.skipped
        assert "customer" in manifest.skipped["generated_document_retention_days"]
    finally:
        db.close()


def test_the_document_sweep_honours_the_period_the_customer_did_state(app_client, throwaway_org):
    """And once they have stated one, it is theirs and it is applied -- to the
    rendered file on disk as well as the row."""
    org_id, user_id, project_id = throwaway_org("docpolicy", 91109)
    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, generated_document_retention_days=365)
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
        document = GeneratedDocument(
            org_id=org_id, project_id=project_id, display_id=91110, language="en",
            status="approved", created_at=old,
        )
        db.add(document)
        db.flush()
        version = DocumentVersion(
            document_id=document.id, org_id=org_id, version_no=1,
            blob_path=_write_blob(f"documents/{project_id}/letter.docx"),
            status="approved", created_by=user_id,
        )
        db.add(version)
        db.commit()
        document_id, blob = document.id, abs_path(version.blob_path)
        assert blob.exists()

        manifest = retention.sweep_expired_documents(db, org_id=org_id)
        db.commit()

        assert db.get(GeneratedDocument, document_id) is None
        assert not blob.exists()
        assert manifest.counts["document_versions"] == 1
    finally:
        db.close()


# --------------------------------------------------------------- offboarding


def test_offboarding_empties_every_org_scoped_table(app_client, throwaway_org):
    """§16 asks for a tenant offboarding routine. A routine that leaves the chat
    transcripts and the lookup taxonomy behind has not offboarded anybody."""
    org_id, user_id, project_id = throwaway_org("offboard", 91111)
    db = SessionLocal()
    try:
        template = TemplateFile(
            org_id=org_id, project_id=project_id, name="offer.docx", status="parsed",
            created_by=user_id,
        )
        db.add(template)
        db.flush()
        db.add(TemplateVersion(
            template_file_id=template.id, org_id=org_id, version_no=1,
            blob_path=_write_blob(f"templates/{project_id}/offer.docx"), created_by=user_id,
        ))
        db.add(Conversation(org_id=org_id, project_id=project_id, user_id=user_id, title="c"))
        db.add(LookupValue(org_id=org_id, kind="region", value="Europe"))
        db.commit()

        actor = db.get(User, user_id)
        certificate, manifest = retention.offboard_organisation(db, org_id=org_id, actor=actor)
        db.commit()

        assert db.scalars(select(Project).where(Project.org_id == org_id)).all() == []
        assert db.scalars(select(TemplateFile).where(TemplateFile.org_id == org_id)).all() == []
        assert db.scalars(select(Conversation).where(Conversation.org_id == org_id)).all() == []
        assert db.scalars(select(LookupValue).where(LookupValue.org_id == org_id)).all() == []
        assert db.get(User, user_id) is None
        assert db.get(Organization, org_id) is None
        assert manifest.counts["users"] == 1
        assert manifest.counts["organizations"] == 1
        assert certificate.scope == retention.ORGANISATION
    finally:
        db.close()


def test_offboarding_deletes_the_blobs_as_well_as_the_rows(app_client, throwaway_org):
    """Object storage outlives the database. A tenant offboarding that empties
    tables and leaves the rendered contracts on disk is the version of this
    routine that reads as done and is not."""
    org_id, user_id, project_id = throwaway_org("offboardblobs", 91112)
    db = SessionLocal()
    try:
        template = TemplateFile(
            org_id=org_id, project_id=project_id, name="t.docx", status="parsed",
            created_by=user_id,
        )
        db.add(template)
        db.flush()
        version = TemplateVersion(
            template_file_id=template.id, org_id=org_id, version_no=1,
            blob_path=_write_blob(f"templates/{project_id}/gone.docx"), created_by=user_id,
        )
        db.add(version)
        db.commit()
        blob = abs_path(version.blob_path)
        assert blob.exists()

        _certificate, manifest = retention.offboard_organisation(db, org_id=org_id)
        db.commit()
        assert not blob.exists()
        assert manifest.blobs
    finally:
        db.close()


def test_the_certificate_survives_the_organisation_it_accounts_for(app_client, throwaway_org):
    """It is the only thing that does. `org_id` becomes a dangling reference by
    design, which is why the row carries the organisation's name -- a receipt
    that cannot say whose data it covers is not a receipt."""
    org_id, user_id, _project_id = throwaway_org("receipt", 91113)
    db = SessionLocal()
    try:
        actor = db.get(User, user_id)
        certificate, _manifest = retention.offboard_organisation(db, org_id=org_id, actor=actor)
        db.commit()
        certificate_id = certificate.id

        db.expire_all()
        stored = db.get(DeletionCertificate, certificate_id)
        assert stored is not None
        assert stored.org_name == "Retention receipt"
        assert stored.issued_by_email == "retention-receipt@tenant.test"
    finally:
        db.close()


def test_the_certificate_hash_verifies_against_the_manifest_it_was_issued_for(app_client, throwaway_org):
    """This is what makes it verifiable rather than a number in a row. An
    auditor holding the manifest we handed over recomputes the hash and either
    it matches or the certificate is worthless."""
    org_id, user_id, _project_id = throwaway_org("verify", 91114)
    db = SessionLocal()
    try:
        actor = db.get(User, user_id)
        certificate, manifest = retention.offboard_organisation(db, org_id=org_id, actor=actor)
        db.commit()
        assert retention.verify_manifest(manifest, certificate) is True
    finally:
        db.close()


def test_a_manifest_with_one_id_added_no_longer_matches_its_certificate(app_client, throwaway_org):
    """The property that makes the hash worth computing: it is over the ids, not
    over the counts, so a manifest quietly edited after the fact stops
    matching."""
    org_id, user_id, _project_id = throwaway_org("tamper", 91115)
    db = SessionLocal()
    try:
        actor = db.get(User, user_id)
        certificate, manifest = retention.offboard_organisation(db, org_id=org_id, actor=actor)
        db.commit()

        manifest.record("projects", ["a-row-that-was-never-deleted"])
        assert retention.verify_manifest(manifest, certificate) is False
    finally:
        db.close()


def test_the_manifest_hash_does_not_depend_on_the_order_rows_were_deleted_in():
    """Two runs that destroyed the same rows must produce the same hash, or the
    certificate proves nothing about anything."""
    first = retention.DeletionManifest(scope="organisation", scope_id="o", org_id="o")
    first.record("projects", ["b", "a"])
    first.record("source_files", ["z"])

    second = retention.DeletionManifest(scope="organisation", scope_id="o", org_id="o")
    second.record("source_files", ["z"])
    second.record("projects", ["a", "b"])

    assert first.digest() == second.digest()

    # And a table that turned out to have nothing in it does not appear at all,
    # so "deleted 0 chat_messages" never reads as a line item.
    first.record("chat_messages", [])
    assert "chat_messages" not in first.counts
    assert first.digest() == second.digest()


def test_offboarding_does_not_delete_the_receipts_of_earlier_deletions(app_client, throwaway_org):
    """The certificate table carries org_id like everything else, so the naive
    cascade would delete the evidence along with the data -- leaving a customer
    with our word for it."""
    org_id, user_id, project_id = throwaway_org("earlier", 91116)
    db = SessionLocal()
    try:
        source = SourceFile(
            org_id=org_id, project_id=project_id, name="s.csv", file_type="csv",
            status="ready", created_by=user_id,
        )
        db.add(source)
        db.commit()

        first = retention.delete_source_file(db, org_id=org_id, source_file_id=source.id)
        earlier = retention.certificate_for(db, first, actor=db.get(User, user_id))
        db.commit()
        earlier_id = earlier.id

        retention.offboard_organisation(db, org_id=org_id)
        db.commit()

        assert db.get(DeletionCertificate, earlier_id) is not None
    finally:
        db.close()


def test_offboarding_forgets_what_the_retrieval_stores_learned(app_client, throwaway_org):
    """§16 again: an embedding derived from deleted data is still derived from
    it, and mapping memory is the place a customer's field names live longest."""
    org_id, _user_id, _project_id = throwaway_org("forget", 91117)
    index, memory = _FakeVectorIndex(), _FakeMappingMemory()
    db = SessionLocal()
    try:
        _certificate, manifest = retention.offboard_organisation(
            db, org_id=org_id, vector_index=index, mapping_memory=memory,
        )
        db.commit()
        assert index.forgotten_orgs == [org_id]
        assert memory.forgotten_orgs == [org_id]
        assert manifest.derived == {"embeddings": 7, "mapping_memory": 3}
    finally:
        db.close()


# ------------------------------------------------------------- the endpoints


def test_the_data_policy_round_trips_through_the_api(app_client, two_orgs):
    token_a, _project_a, *_ = two_orgs

    updated = app_client.put(
        "/api/v1/admin/data-policy",
        json={"source_retention_days": 45, "residency": "eu", "zero_retention_required": True},
        headers=_auth(token_a),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["source_retention_days"] == 45
    assert body["residency"] == "EU"
    assert body["zero_retention_required"] is True
    assert body["generated_documents_retained_indefinitely"] is True

    read_back = app_client.get("/api/v1/admin/data-policy", headers=_auth(token_a))
    assert read_back.json()["residency"] == "EU"


def test_an_unrecognised_residency_is_a_four_hundred_not_a_five_hundred(app_client, two_orgs):
    """The caller mistyped a region; that is their mistake to fix, and the
    message has to name the regions that exist."""
    token_a, *_ = two_orgs
    refused = app_client.put(
        "/api/v1/admin/data-policy", json={"residency": "Atlantis"}, headers=_auth(token_a),
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["error"]["code"] == "INVALID_DATA_POLICY"


@pytest.mark.parametrize("path,method,body", [
    ("/api/v1/admin/data-policy", "get", None),
    ("/api/v1/admin/data-policy", "put", {"source_retention_days": 10}),
    ("/api/v1/admin/retention/sweep", "post", {}),
    ("/api/v1/admin/offboarding", "post", {"confirm_org_id": "x"}),
    ("/api/v1/admin/deletion-certificates", "get", None),
])
def test_retention_administration_needs_the_manage_users_capability(app_client, two_orgs, path, method, body):
    """§16's separation of duties reaches the destructive end of the product too.
    A `generator` runs production; deciding how long a customer's contracts are
    kept, or destroying them, is not running production."""
    from app.db import SessionLocal as _Session
    from app.models import Project as _Project
    from app.security import create_access_token

    _token_a, project_a, *_ = two_orgs
    db = _Session()
    try:
        org_id = db.get(_Project, project_a).org_id
        generator = db.scalar(select(User).where(User.email == "retention-generator@tenant.test"))
        if generator is None:
            generator = User(
                org_id=org_id, email="retention-generator@tenant.test", full_name="Gen",
                password_hash=_THROWAWAY_PASSWORD_HASH, role_key="generator",
            )
            db.add(generator)
            db.commit()
        token = create_access_token(generator.id, org_id)
    finally:
        db.close()

    call = getattr(app_client, method)
    response = call(path, headers=_auth(token)) if body is None else call(path, json=body, headers=_auth(token))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"]["code"] == "CAPABILITY_REQUIRED"


def test_offboarding_refuses_unless_the_caller_names_their_own_organisation(app_client, two_orgs):
    """The confirmation is not redundant with the token. This endpoint deletes
    everything the caller can see including the caller, so a request that
    arrived by accident should fail on the confirmation rather than succeed on
    the session."""
    token_a, *_ = two_orgs
    refused = app_client.post(
        "/api/v1/admin/offboarding", json={"confirm_org_id": "some-other-org"},
        headers=_auth(token_a),
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["error"]["code"] == "OFFBOARDING_NOT_CONFIRMED"


def test_the_sweep_endpoint_reports_what_it_did_and_what_it_declined_to_do(app_client, two_orgs):
    token_a, *_ = two_orgs
    app_client.put(
        "/api/v1/admin/data-policy", json={"source_retention_days": 30}, headers=_auth(token_a),
    )
    swept = app_client.post("/api/v1/admin/retention/sweep", headers=_auth(token_a))
    assert swept.status_code == 200, swept.text
    body = swept.json()
    assert "sources" in body and "generated_documents" in body
    assert body["generated_documents"]["skipped"], (
        "a sweep that deleted no documents because no policy was stated must say so"
    )


def test_offboarding_over_the_api_returns_a_certificate_and_the_manifest_once(app_client, throwaway_org):
    """The response is the only copy of the manifest. We keep the hash; the
    customer keeps the list."""
    from app.security import create_access_token

    org_id, user_id, _project_id = throwaway_org("apioffboard", 91118)
    token = create_access_token(user_id, org_id)

    done = app_client.post(
        "/api/v1/admin/offboarding", json={"confirm_org_id": org_id}, headers=_auth(token),
    )
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["certificate"]["manifest_sha256"] == body["manifest"]["manifest_sha256"]
    assert body["manifest"]["deleted_ids"]["organizations"] == [org_id]

    db = SessionLocal()
    try:
        stored = db.scalar(
            select(DeletionCertificate).where(DeletionCertificate.id == body["certificate"]["id"])
        )
        assert stored is not None
        # Counts and a hash, never the inventory.
        assert not hasattr(stored, "deleted_ids")
        assert stored.counts["organizations"] == 1
    finally:
        db.close()


# ------------------------------------------------------------- the edges


def test_a_policy_lookup_without_a_tenant_is_refused(app_client):
    """Every read in this module names an organisation. One that does not would
    be answering "what is everybody's retention policy", which is not a question
    this system should be able to ask."""
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="needs an org_id"):
            retention.policy_for(db, "")
    finally:
        db.close()


def test_a_document_retention_period_of_zero_days_is_refused(app_client, throwaway_org):
    """Zero would delete a contract the moment it was rendered. Whatever the
    customer meant, it was not that."""
    org_id, _user_id, _project_id = throwaway_org("zerodocs", 91119)
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="at least 1"):
            retention.set_policy(db, org_id, generated_document_retention_days=0)
    finally:
        db.rollback()
        db.close()


def test_a_blob_that_has_already_gone_does_not_stop_the_rest_being_removed():
    """Deletion has to be resumable. A cascade that aborts on the first missing
    file cannot be retried after a partial failure, which is exactly when it
    most needs to be."""
    present = _write_blob("retention/present.bin")
    removed = retention._remove_blobs([None, "", "retention/never-existed.bin", present])
    assert removed == [present]
    assert not abs_path(present).exists()


def test_a_sweep_with_no_expired_documents_reports_nothing_rather_than_failing(app_client, throwaway_org):
    """The common case, and the one a nightly job hits every night after the
    first. It must be cheap and silent, not an exception in a log nobody reads."""
    org_id, _user_id, _project_id = throwaway_org("nothingexpired", 91120)
    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, generated_document_retention_days=365)
        db.commit()
        manifest = retention.sweep_expired_documents(db, org_id=org_id)
        assert manifest.total_rows == 0
        assert manifest.skipped == {}
    finally:
        db.close()


def test_the_source_sweep_says_when_it_fell_back_to_the_platform_default(app_client, throwaway_org):
    """A tenant who never set a schedule still gets one, and the report says so.
    Applying our default silently would leave a customer discovering the
    timetable from the absence of their data."""
    org_id, user_id, project_id = throwaway_org("defaulted", 91121)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    index, memory = _FakeVectorIndex(), _FakeMappingMemory()
    db = SessionLocal()
    try:
        source = SourceFile(
            org_id=org_id, project_id=project_id, name="ancient.csv", file_type="csv",
            status="ready", created_by=user_id, created_at=old,
        )
        db.add(source)
        db.flush()
        version = SourceVersion(
            source_file_id=source.id, org_id=org_id, version_no=1,
            blob_path=_write_blob(f"sources/{project_id}/ancient.csv"), created_by=user_id,
        )
        db.add(version)
        db.flush()
        db.add(SourceChunk(
            project_id=project_id, source_version_id=version.id, org_id=org_id,
            chunk_index=0, element_type="row", heading_path="",
            text="employee_id: 1; annual_salary: 50000", token_count=4, content_sha256="s",
        ))
        db.commit()

        manifest = retention.sweep_expired_sources(
            db, org_id=org_id, vector_index=index, mapping_memory=memory,
        )
        db.commit()

        assert "source_retention_policy" in manifest.skipped
        assert "platform default" in manifest.skipped["source_retention_policy"]
        assert manifest.counts["source_files"] == 1
        assert manifest.derived["embeddings"] == 1
        assert manifest.blobs
    finally:
        db.close()


def test_earlier_certificates_are_listable_by_an_administrator(app_client, two_orgs):
    """A certificate nobody can read afterwards is a receipt filed in a drawer
    that does not open. The narrower deletions -- a sweep, one upload -- are the
    ones a DPIA asks about, and they happen far more often than offboarding."""
    token_a, _project_a, *_ = two_orgs
    listed = app_client.get("/api/v1/admin/deletion-certificates", headers=_auth(token_a))
    assert listed.status_code == 200, listed.text
    assert isinstance(listed.json()["items"], list)


def test_a_sweep_that_deleted_something_issues_a_certificate_for_it(app_client, throwaway_org):
    """Evidence at the point of deletion, not reconstructed from logs later."""
    from app.security import create_access_token

    org_id, user_id, project_id = throwaway_org("sweepcert", 91122)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    db = SessionLocal()
    try:
        db.add(SourceFile(
            org_id=org_id, project_id=project_id, name="old.csv", file_type="csv",
            status="ready", created_by=user_id, created_at=old,
        ))
        db.commit()
    finally:
        db.close()

    token = create_access_token(user_id, org_id)
    swept = app_client.post("/api/v1/admin/retention/sweep", headers=_auth(token))
    assert swept.status_code == 200, swept.text
    certificates = swept.json()["certificates"]
    assert len(certificates) == 1
    assert certificates[0]["counts"]["source_files"] == 1

    listed = app_client.get("/api/v1/admin/deletion-certificates", headers=_auth(token))
    assert [c["id"] for c in listed.json()["items"]] == [certificates[0]["id"]]


def test_a_table_with_no_single_column_primary_key_is_refused_rather_than_swept(app_client):
    """The manifest is a list of ids. A table whose rows cannot be named would
    be deleted and counted but not listed, and the certificate's hash would then
    cover less than was actually destroyed -- a receipt for part of a deletion,
    which is worse than no receipt."""
    from sqlalchemy import Column, MetaData, String, Table

    composite = Table(
        "composite_probe", MetaData(),
        Column("org_id", String, primary_key=True),
        Column("other", String, primary_key=True),
    )
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="no single-column primary key"):
            retention._delete_rows(
                db, composite, composite.c.org_id == "x",
                retention.DeletionManifest(scope="organisation", scope_id=None, org_id="x"),
            )
    finally:
        db.close()


@pytest.mark.skipif(
    _on_postgres(), reason="asserts the SQLite no-op path; PostgreSQL runs the real thing"
)
def test_the_maintenance_bypass_is_inert_where_there_is_no_row_level_security(app_client):
    """It has to be usable unconditionally by a data migration, and the suite's
    migrations run on SQLite. A version that emitted `SET LOCAL` there would
    take the whole chain down."""
    from app import tenancy

    db = SessionLocal()
    try:
        connection = db.get_bind()
        with tenancy.maintenance_bypass(connection) as opened:
            assert opened is connection
    finally:
        db.close()


# ------------------------------------------------- the persisted derivations


def test_the_cascade_deletes_the_stored_embeddings_built_from_the_upload(app_client, source_with_derivations):
    """§16's hardest sentence, against real rows rather than a process-local
    dictionary. An embedding of a chunk, and an embedding of that chunk's
    column, are both derived from the spreadsheet that has just been destroyed.
    """
    from app.models import DEFAULT_DIMENSIONS, Embedding

    ctx = source_with_derivations
    db = SessionLocal()
    try:
        chunk_ids = list(db.scalars(
            select(SourceChunk.id).where(SourceChunk.source_version_id == ctx["version_id"])
        ))
        db.add(Embedding(
            org_id=ctx["org_id"], kind="source_column_description", record_id=chunk_ids[0],
            text="employee_id", vector=[0.1] * DEFAULT_DIMENSIONS, provider="hashing", dims=DEFAULT_DIMENSIONS,
        ))
        db.add(Embedding(
            org_id=ctx["org_id"], kind="source_column_description", record_id="annual_salary",
            text="annual salary", vector=[0.2] * DEFAULT_DIMENSIONS, provider="hashing", dims=DEFAULT_DIMENSIONS,
        ))
        # A vector for something else entirely, which must survive.
        db.add(Embedding(
            org_id=ctx["org_id"], kind="field_context", record_id="a-template-field",
            text="reporting line", vector=[0.3] * DEFAULT_DIMENSIONS, provider="hashing", dims=DEFAULT_DIMENSIONS,
        ))
        db.commit()

        manifest = retention.delete_source_file(
            db, org_id=ctx["org_id"], source_file_id=ctx["source_id"],
        )
        db.commit()

        remaining = db.scalars(
            select(Embedding.record_id).where(Embedding.org_id == ctx["org_id"])
        ).all()
        assert remaining == ["a-template-field"]
        assert manifest.counts["embeddings"] == 2
    finally:
        db.close()


def test_the_cascade_forgets_what_was_learned_about_the_uploads_columns(app_client, source_with_derivations):
    """Mapping memory is where a customer's field names live longest -- §13
    treats a mapping approved forty-two times as the strongest evidence there
    is, which is precisely why it outlives the file it came from unless
    something goes and gets it."""
    from app.models import MappingMemoryEntry, ReviewerCorrection

    ctx = source_with_derivations
    db = SessionLocal()
    try:
        db.add(MappingMemoryEntry(
            org_id=ctx["org_id"], field_key="salary", source_column="annual_salary",
            source_column_key="annual salary", transform="none", on_missing="block",
            field_type="currency", approval_count=42,
        ))
        db.add(MappingMemoryEntry(
            org_id=ctx["org_id"], field_key="manager", source_column="Reporting To",
            source_column_key="reporting to", transform="none", on_missing="block",
            field_type="string", approval_count=3,
        ))
        db.add(ReviewerCorrection(
            org_id=ctx["org_id"], field_key="salary", accepted_column="annual_salary",
            rejected_column="Base Pay", corrected_by="someone",
        ))
        db.commit()

        retention.delete_source_file(db, org_id=ctx["org_id"], source_file_id=ctx["source_id"])
        db.commit()

        survivors = db.scalars(
            select(MappingMemoryEntry.source_column)
            .where(MappingMemoryEntry.org_id == ctx["org_id"])
        ).all()
        assert survivors == ["Reporting To"], (
            "a mapping learned from a column of the deleted file survived it"
        )
        assert db.scalars(
            select(ReviewerCorrection).where(ReviewerCorrection.org_id == ctx["org_id"])
        ).all() == []
    finally:
        db.close()


def test_the_cascade_scrubs_the_verbatim_source_row_out_of_a_generation(app_client, source_with_derivations):
    """§16: "an embedding derived from deleted data is still derived from it".

    A verbatim copy of the spreadsheet row is the least derived thing there is.
    `ManifestGeneration.source_record` held one -- salary, identifiers and name
    -- captured so a reviewer could see what the letter was filled from, and
    nothing linked it back to the upload, so destroying the source file on its
    retention schedule left the copy behind indefinitely.
    """
    from app.models import ManifestGeneration

    ctx = source_with_derivations
    db = SessionLocal()
    try:
        manifest_id = db.scalar(
            select(TemplateManifest.id).where(TemplateManifest.org_id == ctx["org_id"])
        )
        generation = ManifestGeneration(
            org_id=ctx["org_id"], manifest_id=manifest_id,
            source_version_id=ctx["version_id"], source_record_key="2",
            source_record={"employee_id": "44182", "full_name": "Dana Ruiz", "annual_salary": "118400"},
            field_lineage=[{"field_id": "full_name", "value": "Dana Ruiz", "source": "source_record"}],
            qa_passed=True, blob_path=None, created_by=ctx["user_id"],
        )
        db.add(generation)
        db.commit()
        generation_id = generation.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        manifest = retention.delete_source_file(
            db, org_id=ctx["org_id"], source_file_id=ctx["source_id"],
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        row = db.get(ManifestGeneration, generation_id)
    finally:
        db.close()

    assert row is not None, (
        "the generation record must survive: it is the audit trail for a letter that may "
        "already be signed, which §16 governs by the customer's records policy rather than "
        "by the source file's retention schedule"
    )
    assert "Dana Ruiz" not in str(row.source_record)
    assert "118400" not in str(row.source_record)
    assert "44182" not in str(row.source_record)

    # §17 asks lineage for "source file/version + row/record key", not for a copy
    # of every value, so the letter stays traceable after the row is gone.
    assert row.source_record_key == "2"
    assert row.field_lineage, "the per-field lineage is the audit artefact and must remain"

    # A scrub is reported as a scrub. Counting it as a deletion would tell a
    # customer a record is gone when it is still there, correctly.
    assert manifest.scrubbed.get("manifest_generations") == 1
    assert "manifest_generations" not in manifest.rows


def test_a_generation_from_a_different_upload_is_left_alone(app_client, source_with_derivations):
    """The scrub is scoped by source version. Emptying every generation in the
    org would destroy the audit trail of letters built from uploads that are
    still perfectly live."""
    from app.models import ManifestGeneration

    ctx = source_with_derivations
    db = SessionLocal()
    try:
        manifest_id = db.scalar(
            select(TemplateManifest.id).where(TemplateManifest.org_id == ctx["org_id"])
        )
        other = ManifestGeneration(
            org_id=ctx["org_id"], manifest_id=manifest_id,
            source_version_id="a-different-upload", source_record_key="1",
            source_record={"full_name": "Priya Raman"},
            qa_passed=True, created_by=ctx["user_id"],
        )
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        retention.delete_source_file(db, org_id=ctx["org_id"], source_file_id=ctx["source_id"])
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.get(ManifestGeneration, other_id).source_record == {"full_name": "Priya Raman"}
    finally:
        db.close()


# ------------------------------------- deleting one generated document by hand

def test_deleting_a_document_from_another_org_raises_rather_than_no_ops():
    """A quiet no-op here is the dangerous answer.

    `delete_generated_document` reports what it removed, so returning an empty
    manifest for a document belonging to somebody else would read as "there was
    nothing to delete" -- and the caller would tell its user the document is
    gone while it sits untouched in another tenant.
    """
    from app.db import SessionLocal
    from app.retention import delete_generated_document

    db = SessionLocal()
    try:
        with pytest.raises(LookupError, match="is not in organisation"):
            delete_generated_document(db, org_id="org-that-owns-nothing", document_id="no-such-document")
    finally:
        db.close()


def _document_with_a_review(db, *, org_id, user_id, project_id, display_id, created_at=None):
    """A letter somebody objected to, and the remark quoting a line of it back."""
    document = GeneratedDocument(
        org_id=org_id, project_id=project_id, display_id=display_id, language="en",
        status="changes_requested",
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id, org_id=org_id, version_no=1,
        blob_path=_write_blob(f"documents/{project_id}/{display_id}.docx"),
        status="changes_requested", created_by=user_id,
    )
    db.add(version)
    db.flush()
    review = DocumentReview(
        org_id=org_id, document_id=document.id, document_version_id=version.id,
        state="open", reason="the salary is wrong", requested_by=user_id,
        authored_by=user_id,
    )
    db.add(review)
    db.flush()
    comment = ReviewComment(
        org_id=org_id, review_id=review.id, author_id=user_id,
        body="this figure", paragraph_index=12, span_index=3,
        quoted_text="a base salary of GBP 48,000",
    )
    db.add(comment)
    # The engine's own parked question, which holds the source values verbatim.
    db.add(ReviewTask(
        org_id=org_id, project_id=project_id, unit_id="pro_rata", kind="calculation",
        question="What is the pro-rata bonus?", status="open",
        document_version_id=version.id, proposed_value="82000",
        context={"available": {"salary": "82000", "employee_id": "E-99123"}},
    ))
    db.flush()
    return document, version, review, comment


def test_deleting_a_document_takes_the_conversation_about_it(app_client, throwaway_org):
    """A comment quotes the letter back verbatim.

    `quoted_text` holds the run as it read when somebody remarked on it, so a
    review comment is a copy of the document's contents living in another table.
    Deleting the letter and leaving the remark that reproduces it would leave
    behind exactly the payload the deletion was for.
    """
    org_id, user_id, project_id = throwaway_org("reviewdel", 91130)
    db = SessionLocal()
    try:
        document, _version, review, comment = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91130)
        db.commit()
        document_id, review_id, comment_id = document.id, review.id, comment.id

        manifest = retention.delete_generated_document(
            db, org_id=org_id, document_id=document_id)
        db.commit()

        assert db.get(DocumentReview, review_id) is None
        assert db.get(ReviewComment, comment_id) is None
        assert manifest.counts["review_comments"] == 1
        assert manifest.counts["document_reviews"] == 1
    finally:
        db.close()


def test_the_document_sweep_takes_the_conversation_too(app_client, throwaway_org):
    """The scheduled cascade and the by-hand one agree, which is the point of
    both calling the same helper."""
    org_id, user_id, project_id = throwaway_org("reviewsweep", 91131)
    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, generated_document_retention_days=365)
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
        _document, _version, review, comment = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91132,
            created_at=old)
        db.commit()
        review_id, comment_id = review.id, comment.id

        retention.sweep_expired_documents(db, org_id=org_id)
        db.commit()

        assert db.get(DocumentReview, review_id) is None
        assert db.get(ReviewComment, comment_id) is None
    finally:
        db.close()


def test_deleting_a_document_nobody_objected_to_leaves_other_reviews_alone(
        app_client, throwaway_org):
    """The predicate is the version, not the org.

    Deleting one letter must not take the conversation about another -- which is
    the same class of mistake as the `blob_path` join that could destroy a second
    document's lineage.
    """
    org_id, user_id, project_id = throwaway_org("reviewscope", 91133)
    db = SessionLocal()
    try:
        doomed, _v1, _r1, _c1 = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91134)
        _kept, _v2, kept_review, kept_comment = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91135)
        db.commit()
        kept_ids = (kept_review.id, kept_comment.id)

        retention.delete_generated_document(db, org_id=org_id, document_id=doomed.id)
        db.commit()

        assert db.get(DocumentReview, kept_ids[0]) is not None
        assert db.get(ReviewComment, kept_ids[1]) is not None
    finally:
        db.close()


def test_deleting_a_document_takes_the_engines_parked_questions_too(
        app_client, throwaway_org):
    """A `ReviewTask` holds the source record, not a measurement of it.

    `resolution_engine` fills `context["available"]` with the resolved values --
    salary, identifiers -- and `proposed_value` with the figure written into the
    letter. Leaving those behind destroyed the document, its versions, its blob
    and its lineage row while the salary stayed in the database, on a row
    pointing at a `document_version_id` that no longer existed and absent from
    the manifest the certificate is issued against.
    """
    org_id, user_id, project_id = throwaway_org("taskdel", 91138)
    db = SessionLocal()
    try:
        document, version, _r, _c = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91138)
        db.commit()
        document_id, version_id = document.id, version.id

        manifest = retention.delete_generated_document(
            db, org_id=org_id, document_id=document_id)
        db.commit()

        left = db.query(ReviewTask).filter(
            ReviewTask.document_version_id == version_id).all()
        assert left == [], "a parked question kept the salary after the document was destroyed"
        assert manifest.counts["review_tasks"] == 1
    finally:
        db.close()


def test_the_sweep_takes_the_parked_questions_as_well(app_client, throwaway_org):
    """The scheduled cascade and the by-hand one agree, because both call the
    same helper -- which is the reason they share one."""
    org_id, user_id, project_id = throwaway_org("tasksweep", 91139)
    db = SessionLocal()
    try:
        retention.set_policy(db, org_id, generated_document_retention_days=365)
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
        _d, version, _r, _c = _document_with_a_review(
            db, org_id=org_id, user_id=user_id, project_id=project_id, display_id=91140,
            created_at=old)
        db.commit()
        version_id = version.id

        retention.sweep_expired_documents(db, org_id=org_id)
        db.commit()

        assert db.query(ReviewTask).filter(
            ReviewTask.document_version_id == version_id).all() == []
    finally:
        db.close()


def test_a_document_with_no_versions_still_deletes(app_client, throwaway_org):
    """The empty case: nothing to look reviews up by, and no query to run."""
    org_id, _user_id, project_id = throwaway_org("noversions", 91136)
    db = SessionLocal()
    try:
        document = GeneratedDocument(org_id=org_id, project_id=project_id,
                                     display_id=91136, language="en", status="draft")
        db.add(document)
        db.commit()
        document_id = document.id

        manifest = retention.delete_generated_document(db, org_id=org_id,
                                                       document_id=document_id)
        db.commit()

        assert db.get(GeneratedDocument, document_id) is None
        assert "document_reviews" not in manifest.counts
    finally:
        db.close()


def test_the_lineage_is_found_by_version_id_not_by_the_path_it_was_written_to(
        app_client, throwaway_org):
    """The `blob_path` join was already wrong before this feature touched it.

    `apply_version_text` writes a *different* path for an edited document, so its
    generation record was unreachable through the path and survived the deletion
    -- taking the resolved field lineage, which is a copy of the source row, with
    it.
    """
    from app.models import ManifestGeneration

    org_id, user_id, project_id = throwaway_org("lineagelink", 91137)
    db = SessionLocal()
    try:
        document = GeneratedDocument(org_id=org_id, project_id=project_id,
                                     display_id=91137, language="en", status="draft")
        db.add(document)
        db.flush()
        version = DocumentVersion(
            document_id=document.id, org_id=org_id, version_no=2,
            blob_path="documents/edited-v2.docx", status="draft", created_by=user_id)
        db.add(version)
        db.flush()
        # A real manifest row: `manifest_generations.manifest_id` is a foreign
        # key, which PostgreSQL enforces and SQLite does not.
        template_file = TemplateFile(org_id=org_id, project_id=project_id, name="t.docx",
                                     status="ready", created_by=user_id)
        db.add(template_file)
        db.flush()
        template_version = TemplateVersion(template_file_id=template_file.id, org_id=org_id,
                                           version_no=1, blob_path="t.docx",
                                           created_by=user_id)
        db.add(template_version)
        db.flush()
        template_manifest = TemplateManifest(
            org_id=org_id, template_file_id=template_file.id,
            template_version_id=template_version.id, version_no=1, status="draft",
            fields=[], conditions=[], blocks=[], delete_always=[], confidence=1.0,
            compiled_by="rule_based", prescan_summary={}, created_by=user_id)
        db.add(template_manifest)
        db.flush()

        generation = ManifestGeneration(
            org_id=org_id, manifest_id=template_manifest.id,
            source_record={"salary": "48000"},
            field_lineage=[], condition_lineage=[], qa_passed=True, qa_notes=[],
            # What v1 was rendered to. The edit wrote somewhere else.
            blob_path="documents/original-v1.docx",
            document_version_id=version.id, created_by=user_id)
        db.add(generation)
        db.commit()
        generation_id = generation.id

        retention.delete_generated_document(db, org_id=org_id, document_id=document.id)
        db.commit()

        assert db.get(ManifestGeneration, generation_id) is None
    finally:
        db.close()


def test_a_blank_column_name_contributes_no_spellings():
    """`_column_candidates` is what makes a deletion find the mapping-memory rows
    built from a column. A blank name would otherwise add the empty string to the
    match set, and an empty candidate matches far more than it should."""
    from app.retention import _column_candidates

    assert _column_candidates(["", "   ", None]) == set()
    assert "Annual Salary" in _column_candidates(["Annual Salary", ""])
