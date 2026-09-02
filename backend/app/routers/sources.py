from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, SourceChunk, SourceFile, SourceVersion, User
from app.security import error, get_current_user
from app.audit.service import log_audit
from app.ownership import owned_project, owned_source_file, owned_source_version
from app.generation.source_ingestion import content_sha256, extract
from app.generation.source_ingestion import RECORD_FILE_TYPES, extract_records
from app.retrieval.indexing import index_source_columns
from app.retrieval.store import SqlVectorStore
from app.retrieval.vector import VectorIndex
from app.storage import abs_path, save_upload

router = APIRouter(tags=["sources"])

_EXT_TO_TYPE = {".docx": "docx", ".pdf": "pdf", ".xlsx": "xlsx", ".csv": "csv", ".txt": "txt", ".json": "txt", ".html": "txt"}


def _source_out(sf: SourceFile) -> dict:
    return {
        "id": sf.id, "name": sf.name, "file_type": sf.file_type, "status": sf.status,
        "ingest_error": sf.ingest_error, "page_count": sf.page_count, "chunk_count": sf.chunk_count,
        # Every caller that wants to *read* a source addresses it by version, not
        # by file. Omitting this silently broke the mapping wizard, where
        # `source_version_ids` always resolved to [] and generation quietly fell
        # back to every chunk in the project rather than the file the user chose.
        "current_version_id": sf.current_version_id,
        "created_at": sf.created_at,
    }


@router.post("/projects/{project_id}/sources", status_code=201)
def upload_source(project_id: str, file: UploadFile = File(...), name: str | None = Form(None), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)

    filename = file.filename or "source"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = _EXT_TO_TYPE.get(ext)
    if not file_type:
        raise error("UNSUPPORTED_SOURCE_TYPE", f"Unsupported source file extension: {ext}", 422)

    rel_path, _size = save_upload(file, f"sources/{project_id}")
    sf = SourceFile(org_id=user.org_id, project_id=project_id, name=name or filename, file_type=file_type, status="chunking", created_by=user.id)
    db.add(sf)
    db.flush()
    sv = SourceVersion(source_file_id=sf.id, org_id=sf.org_id, version_no=1, blob_path=rel_path, created_by=user.id)
    db.add(sv)
    db.flush()

    try:
        raw_chunks = extract(str(abs_path(rel_path)), file_type)
        for idx, rc in enumerate(raw_chunks):
            db.add(SourceChunk(
                project_id=project_id, source_version_id=sv.id, org_id=sv.org_id, chunk_index=idx,
                element_type=rc.element_type, heading_path=rc.heading_path,
                text=rc.text, token_count=max(1, len(rc.text.split())),
                content_sha256=content_sha256(rc.text),
            ))
        sf.chunk_count = len(raw_chunks)
        sf.status = "ready"
        sf.current_version_id = sv.id

        # §10 semantic memory. The columns, not the rows: one vector per column
        # rather than one per employee, and the sample in each description is a
        # synthetic value from `llm.redaction`. Embedding the rows would put a
        # second copy of the payroll in the vector store, which a deletion then
        # has to find and a tenant filter is the only thing guarding.
        #
        # Best-effort on purpose. A file the customer can already use must not
        # fail to upload because the index was unavailable; the miss is recorded
        # and the file stays re-indexable.
        if file_type in RECORD_FILE_TYPES:
            try:
                columns, records = extract_records(str(abs_path(rel_path)), file_type)
                index_source_columns(
                    VectorIndex(store=SqlVectorStore(db)),
                    org_id=sv.org_id, source_version_id=sv.id,
                    columns=columns, records=records,
                    doc_type=project.document_type if project else None,
                )
            except Exception as exc:  # noqa: BLE001 - never fail an upload over the index
                sf.ingest_error = f"indexed: no ({type(exc).__name__}: {exc})"
    except Exception as exc:
        sf.status = "failed"
        sf.ingest_error = str(exc)

    log_audit(db, user, "Uploaded source", "source_file", sf.id, project_id, "success" if sf.status == "ready" else "warning", sf.name)
    db.commit()
    db.refresh(sf)
    return _source_out(sf)


@router.get("/projects/{project_id}/sources")
def list_sources(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_project(db, project_id, user)
    rows = db.scalars(select(SourceFile).where(SourceFile.project_id == project_id, SourceFile.deleted_at.is_(None))).all()
    return {"items": [_source_out(sf) for sf in rows]}


@router.get("/sources/{source_id}")
def get_source(source_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sf = db.get(SourceFile, source_id)
    if not sf or sf.org_id != user.org_id:
        raise error("SOURCE_NOT_FOUND", "Source file not found", 404)
    return _source_out(sf)


@router.get("/sources/{source_id}/fields")
def source_fields(source_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Distinct `key: value` field names found across a source's chunks -- backs the
    mapping wizard's "map template variable -> source field" dropdown (spec §2.4)."""
    sf = db.get(SourceFile, source_id)
    if not sf or sf.org_id != user.org_id:
        raise error("SOURCE_NOT_FOUND", "Source file not found", 404)
    if not sf.current_version_id:
        return {"items": []}
    chunks = db.scalars(select(SourceChunk).where(SourceChunk.source_version_id == sf.current_version_id)).all()
    fields: set[str] = set()
    for c in chunks:
        for part in c.text.split(";"):
            if ":" in part:
                fields.add(part.split(":", 1)[0].strip())
    return {"items": sorted(fields)}


@router.get("/source-versions/{version_id}/chunks")
def preview_chunks(version_id: str, page: int = 1, page_size: int = 20, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_source_version(db, version_id, user)
    stmt = select(SourceChunk).where(SourceChunk.source_version_id == version_id).order_by(SourceChunk.chunk_index)
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"chunk_index": c.chunk_index, "element_type": c.element_type, "heading_path": c.heading_path, "text": c.text} for c in rows]}


class BulkSourceDelete(BaseModel):
    source_ids: list[str]


MAX_BULK_SOURCES = 50


@router.post("/sources:delete")
def delete_sources(body: BulkSourceDelete, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Remove several source uploads, and say what happened to each.

    The same `deleted_at` stamp `DELETE /sources/{id}` applies, and it carries the
    same known limitation: the stamp does not reach the chunks, the embeddings or
    what the mapping memory learned from the columns. `retention.delete_source_file`
    is the cascade that does, and it runs on the sweep. Selecting ten uploads
    instead of one does not change that, and pretending otherwise here would be a
    worse answer than the honest one -- see §17 of APPLICATION_FLOW.

    Ownership is checked per source, for the reason every bulk endpoint here
    checks it per item: a caller can put any id in a JSON array, and one that
    trusts the array is how one tenant reaches another's.

    **One commit for the whole request.** Unlike `documents:delete`, which unlinks
    files from disk and so must commit per document, this is a timestamp on rows:
    the request is atomic, and a failure leaves the workspace as it was.
    """
    from datetime import datetime, timezone

    if not body.source_ids:
        raise error("NO_SOURCES", "Select at least one source file to delete.", 422)
    ids = list(dict.fromkeys(body.source_ids))
    if len(ids) > MAX_BULK_SOURCES:
        raise error(
            "TOO_MANY_SOURCES",
            f"{len(ids)} source files were selected; this endpoint removes at most "
            f"{MAX_BULK_SOURCES} at a time.",
            422,
        )

    stamped = datetime.now(timezone.utc)
    deleted, refused = [], []
    for source_id in ids:
        sf = db.get(SourceFile, source_id)
        if not sf or sf.org_id != user.org_id or sf.deleted_at is not None:
            refused.append({"source_id": source_id, "code": "SOURCE_NOT_FOUND",
                            "name": None, "reason": "This source file no longer exists."})
            continue
        sf.deleted_at = stamped
        log_audit(db, user, "Deleted source file", "source_file", source_id,
                  project_id=sf.project_id, severity="warning",
                  target=f"{sf.name} (bulk of {len(ids)})")
        deleted.append({"source_id": source_id, "name": sf.name})

    db.commit()
    return {"requested": len(ids), "deleted": deleted, "refused": refused}


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from datetime import datetime, timezone
    sf = db.get(SourceFile, source_id)
    if not sf or sf.org_id != user.org_id:
        raise error("SOURCE_NOT_FOUND", "Source file not found", 404)
    sf.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "deleted"}
