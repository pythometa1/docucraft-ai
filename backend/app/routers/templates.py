import re

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compile_progress import reading_state, running_readings
from app.db import get_db
from app.models import (
    TemplateManifest,
    Project, TemplateFile, TemplateLibrary, TemplateLibraryVersion, TemplateSection,
    TemplateVersion, User,
)
from app.security import error, get_current_user
from app.audit.service import log_audit
from app.ownership import owned_project, owned_template_file, owned_template_library, owned_template_version
from app.templates.ingest import parse_template_version
from app.templates.parsers.docx_safety import MalformedPackageError, UnsafePackageError, inspect_package
from app.expressions.token_parser import parse_tokens
from app.storage import abs_path, save_upload

router = APIRouter(tags=["templates"])


# ---------------------------------------------------------------- template files
@router.post("/projects/{project_id}/templates", status_code=201)
def upload_template(project_id: str, file: UploadFile = File(...), name: str | None = Form(None), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    if not (file.filename or "").lower().endswith((".docx", ".dotx")):
        raise error("UNSUPPORTED_TEMPLATE_TYPE", "Only .docx/.dotx templates are supported in this build", 422)

    rel_path, _size = save_upload(file, f"templates/{project_id}")

    # Before any parser opens it. A .docx is a zip from outside the trust
    # boundary, and the checks here (symlink entries, path traversal,
    # decompression ratio, entry count) are about the archive rather than the
    # document -- so they have to run ahead of python-docx, not inside it.
    try:
        inspect_package(abs_path(rel_path))
    except UnsafePackageError as exc:
        raise error("UNSAFE_TEMPLATE_PACKAGE", str(exc), 422) from exc
    except MalformedPackageError:
        # Broken, not hostile. Fall through so the upload is still recorded and
        # the parser reports what is wrong with it -- a file that vanishes on
        # upload tells the person who sent it nothing.
        pass

    tf = TemplateFile(org_id=user.org_id, project_id=project_id, name=name or file.filename, status="parsing", created_by=user.id)
    db.add(tf)
    db.flush()

    tv = TemplateVersion(template_file_id=tf.id, org_id=tf.org_id, version_no=1, blob_path=rel_path, created_by=user.id)
    db.add(tv)
    db.flush()

    parse_template_version(db, tf, tv)

    log_audit(db, user, "Uploaded template", "template_file", tf.id, project_id, "success" if tf.status == "ready" else "warning", tf.name)
    db.commit()
    db.refresh(tf)
    return _template_file_out(tf, tv, user.full_name)


def creator_name(db: Session, user_id: str | None) -> str | None:
    """Who actually uploaded this. The dashboard used to print a hardcoded name
    for every row regardless of who did the work, which is worse than blank --
    it is an attribution that looks authoritative and is simply untrue."""
    if not user_id:
        return None
    u = db.get(User, user_id)
    return u.full_name if u else None


def _manifest_summary(db: Session, template_file_id: str) -> dict:
    """Whether this template has been read yet, and what came out of it.

    Carried on the template rather than fetched per row by the client, because
    "has this been compiled?" is what decides whether a project can move past its
    first stage at all -- and a screen that has to ask a second endpoint before it
    can answer renders the wrong state first and corrects itself a moment later.
    """
    m = db.scalars(
        select(TemplateManifest)
        .where(TemplateManifest.template_file_id == template_file_id)
        .order_by(TemplateManifest.version_no.desc())
    ).first()
    if m is None:
        return {"manifest_id": None, "manifest_status": None, "field_count": 0,
                "condition_count": 0, "compile_error": None,
                "unfillable": [], "unfillable_count": 0}
    # A failed compile has to say why on the same payload that says it happened.
    # Without this the screen shows "Compiled -- 0 fields", which is what a
    # successful compile of a template with no placeholders looks like, and the
    # two are not remotely the same thing to act on.
    #
    # The stored note names the pipeline that failed (parts, review rounds, the
    # model), so the reader gets `public_failure_reason` instead.
    compile_error = None
    if m.status == "failed":
        from app.manifests.public import public_failure_reason

        compile_error = public_failure_reason(m.prescan_summary)

    # The placeholders nothing will fill, named on the template's own row.
    #
    # Every one of these is a document that will come back blocked with
    # "Leftover placeholder brackets" -- the fill engine leaves the literal text
    # where the value should be and QA refuses it. That was only discoverable by
    # running a batch: three canary rows fail, the run stops, and the reader is
    # four steps and one spreadsheet away from the template they would have to
    # change. It is known the moment the template is read, so it belongs here.
    #
    # Read off the stored column rather than re-derived, so listing a project's
    # templates does not re-parse every .docx on the row.
    unfillable = [
        {
            "code": w.get("code"),
            "paragraph_index": w.get("paragraph_index"),
            # The placeholder itself, when the check recorded one. Newlines are
            # flattened: Word splits these across lines and a raw token renders
            # as a broken three-line chip.
            "placeholder": " ".join((w.get("evidence") or "").split()) or None,
            "message": w.get("message") or w.get("detail"),
        }
        for w in (m.warnings or [])
        if w.get("code") in ("uncovered_placeholder", "W-SPLIT-PLACEHOLDER")
    ]
    return {
        "manifest_id": m.id,
        "manifest_status": m.status,
        "compile_error": compile_error,
        "field_count": len(m.fields or []),
        "condition_count": len(m.conditions or []),
        "unfillable": unfillable[:8],
        "unfillable_count": len(unfillable),
    }


def _blueprint_id_for(db: Session, tf: TemplateFile) -> str | None:
    """The blueprint already open on this template, if any.

    Sent with the row so the project can offer "continue editing" rather than
    "edit", and so clicking it does not have to guess whether one exists. The
    join key has been on `TemplateBlueprint` since it was written; nothing on the
    template side had ever looked at it.
    """
    from app.models import TemplateBlueprint

    return db.scalar(
        select(TemplateBlueprint.id)
        .where(TemplateBlueprint.org_id == tf.org_id,
               TemplateBlueprint.template_file_id == tf.id,
               TemplateBlueprint.deleted_at.is_(None))
        .order_by(TemplateBlueprint.updated_at.desc())
        .limit(1)
    )


def _template_file_out(tf: TemplateFile, tv: TemplateVersion | None, created_by_name: str | None = None,
                       manifest: dict | None = None, blueprint_id: str | None = None) -> dict:
    return {
        "id": tf.id, "name": tf.name, "status": tf.status, "parse_error": tf.parse_error,
        "current_version_id": tf.current_version_id,
        "blueprint_id": blueprint_id,
        "created_by_name": created_by_name,
        "section_count": tv.section_count if tv else None,
        "template_kind": tv.template_kind if tv else None,
        "jinja_vars": tv.jinja_vars if tv else [],
        "created_at": tf.created_at,
        **(manifest or {"manifest_id": None, "manifest_status": None, "field_count": 0, "condition_count": 0}),
    }


@router.get("/projects/{project_id}/templates")
def list_templates(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_project(db, project_id, user)
    rows = db.scalars(select(TemplateFile).where(TemplateFile.project_id == project_id, TemplateFile.deleted_at.is_(None))).all()
    # A reading in progress, per template, so a reload can re-attach to it
    # rather than showing "Not read yet" under a job that is still running.
    readings = running_readings(db, project_id=project_id)
    out = []
    for tf in rows:
        tv = db.get(TemplateVersion, tf.current_version_id) if tf.current_version_id else None
        row = _template_file_out(
            tf, tv, creator_name(db, tf.created_by), _manifest_summary(db, tf.id),
            blueprint_id=_blueprint_id_for(db, tf))
        job = readings.get(tf.id)
        row["reading"] = reading_state(job) if job is not None else None
        out.append(row)
    return {"items": out}


@router.get("/templates/{template_id}")
def get_template(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tf = db.get(TemplateFile, template_id)
    if not tf or tf.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    tv = db.get(TemplateVersion, tf.current_version_id) if tf.current_version_id else None
    return _template_file_out(tf, tv, creator_name(db, tf.created_by), _manifest_summary(db, tf.id))


def _section_tree(sections: list[TemplateSection]) -> list[dict]:
    by_level: dict[int, list[dict]] = {}
    nodes = []
    for s in sorted(sections, key=lambda x: x.order_index):
        node = {"id": s.id, "level": s.level, "title": s.title, "section_path": s.section_path, "order_index": s.order_index, "fillable": s.fillable, "children": []}
        nodes.append(node)
    # Build simple nesting by level using a stack (document order already correct).
    root: list[dict] = []
    stack: list[dict] = []
    for node in nodes:
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else root).append(node)
        stack.append(node)
    return root


@router.get("/template-versions/{version_id}/sections")
def get_template_sections(version_id: str, tree: bool = True, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_template_version(db, version_id, user)
    sections = db.scalars(select(TemplateSection).where(TemplateSection.template_version_id == version_id)).all()
    if tree:
        return {"items": _section_tree(sections)}
    return {"items": [{"id": s.id, "level": s.level, "title": s.title, "section_path": s.section_path} for s in sections]}


class BulkTemplateDelete(BaseModel):
    template_ids: list[str]


MAX_BULK_TEMPLATES = 50


@router.post("/templates:delete")
def delete_templates(body: BulkTemplateDelete, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Remove several template files, and say what happened to each.

    The same `deleted_at` stamp `DELETE /templates/{id}` applies. The manifests
    compiled from them are deliberately left in place: a manifest is the contract
    a finished letter was produced against, and §17 wants that letter traceable
    to it long after somebody tidied the template list.

    Ownership is checked per template, for the reason every bulk endpoint here
    checks it per item: a caller can put any id in a JSON array, and one that
    trusts the array is how one tenant reaches another's.

    **One commit for the whole request.** Unlike `documents:delete`, which unlinks
    files from disk and so must commit per document, this is a timestamp on rows:
    the request is atomic, and a failure leaves the workspace as it was.
    """
    from datetime import datetime, timezone

    if not body.template_ids:
        raise error("NO_TEMPLATES", "Select at least one template to delete.", 422)
    ids = list(dict.fromkeys(body.template_ids))
    if len(ids) > MAX_BULK_TEMPLATES:
        raise error(
            "TOO_MANY_TEMPLATES",
            f"{len(ids)} templates were selected; this endpoint removes at most "
            f"{MAX_BULK_TEMPLATES} at a time.",
            422,
        )

    stamped = datetime.now(timezone.utc)
    deleted, refused = [], []
    for template_id in ids:
        tf = db.get(TemplateFile, template_id)
        if not tf or tf.org_id != user.org_id or tf.deleted_at is not None:
            refused.append({"template_id": template_id, "code": "TEMPLATE_NOT_FOUND",
                            "name": None, "reason": "This template no longer exists."})
            continue
        tf.deleted_at = stamped
        log_audit(db, user, "Deleted template", "template_file", template_id,
                  project_id=tf.project_id, severity="warning",
                  target=f"{tf.name} (bulk of {len(ids)})")
        deleted.append({"template_id": template_id, "name": tf.name})

    db.commit()
    return {"requested": len(ids), "deleted": deleted, "refused": refused}


@router.delete("/templates/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from datetime import datetime, timezone
    tf = db.get(TemplateFile, template_id)
    if not tf or tf.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    tf.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------- template library (native token templates, spec §8)
class LibraryCreate(BaseModel):
    name: str
    category: str
    description: str | None = None
    content_html: str = "<h1>Untitled template</h1><p>Start writing…</p>"


def _library_out(db: Session, tl: TemplateLibrary) -> dict:
    tlv = db.get(TemplateLibraryVersion, tl.current_version_id) if tl.current_version_id else None
    return {
        "id": tl.id, "name": tl.name, "category": tl.category, "description": tl.description,
        "starred": tl.starred, "uses": tl.uses,
        "version": f"v{tlv.version_no}.0" if tlv else "v0.1",
        "updated_at": tl.updated_at,
    }


@router.get("/template-library")
def list_library(category: str | None = None, q: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(TemplateLibrary).where(TemplateLibrary.org_id == user.org_id, TemplateLibrary.deleted_at.is_(None))
    if category and category != "All":
        stmt = stmt.where(TemplateLibrary.category == category)
    rows = db.scalars(stmt.order_by(TemplateLibrary.updated_at.desc())).all()
    if q:
        rows = [r for r in rows if q.lower() in r.name.lower()]
    return {"items": [_library_out(db, r) for r in rows]}


@router.post("/template-library", status_code=201)
def create_library_entry(body: LibraryCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tl = TemplateLibrary(org_id=user.org_id, name=body.name, category=body.category, description=body.description, created_by=user.id)
    db.add(tl)
    db.flush()
    _, _tokens, fields = parse_tokens(body.content_html)
    tlv = TemplateLibraryVersion(template_library_id=tl.id, org_id=tl.org_id, version_no=1, content_html=body.content_html, source_fields=fields, created_by=user.id)
    db.add(tlv)
    db.flush()
    tl.current_version_id = tlv.id
    db.commit()
    db.refresh(tl)
    return _library_out(db, tl)


@router.get("/template-library/{library_id}/content")
def get_library_content(library_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tl = db.get(TemplateLibrary, library_id)
    if not tl or tl.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    tlv = db.get(TemplateLibraryVersion, tl.current_version_id)
    return {"content_html": tlv.content_html, "source_fields": tlv.source_fields, "version_no": tlv.version_no}


class LibraryContentPatch(BaseModel):
    content_html: str


@router.patch("/template-library/{library_id}/content")
def save_library_content(library_id: str, body: LibraryContentPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tl = db.get(TemplateLibrary, library_id)
    if not tl or tl.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    prev = db.get(TemplateLibraryVersion, tl.current_version_id)
    _, _tokens, fields = parse_tokens(body.content_html)
    tlv = TemplateLibraryVersion(
        template_library_id=tl.id, org_id=tl.org_id, version_no=(prev.version_no + 1 if prev else 1),
        content_html=body.content_html, source_fields=fields, created_by=user.id,
    )
    db.add(tlv)
    db.flush()
    tl.current_version_id = tlv.id
    db.commit()
    return {"content_html": tlv.content_html, "source_fields": tlv.source_fields, "version_no": tlv.version_no}


@router.get("/template-library/{library_id}/versions")
def list_library_versions(library_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_template_library(db, library_id, user)
    rows = db.scalars(select(TemplateLibraryVersion).where(TemplateLibraryVersion.template_library_id == library_id).order_by(TemplateLibraryVersion.version_no.desc())).all()
    return {"items": [{"version_no": r.version_no, "created_at": r.created_at} for r in rows]}


# ---------------------------------------------------------------- legacy conversion parity (spec §8.4)
_PRESET_PATTERNS = [
    (re.compile(r"\[AI:\s*([^\]]+)\]", re.IGNORECASE), "prompt"),
    (re.compile(r"\{\{?\s*([a-zA-Z_][a-zA-Z0-9_ ]{1,40})\s*\}?\}"), "source"),
    (re.compile(r"<<\s*([a-zA-Z_][a-zA-Z0-9_ ]{1,40})\s*>>"), "source"),
    (re.compile(r"\[([A-Z][A-Z0-9_ /-]{2,40})\]"), "source"),
]


class ConvertRequest(BaseModel):
    text: str


@router.post("/template-library:convert")
def convert_legacy_text(body: ConvertRequest, user: User = Depends(get_current_user)):
    """Server-side parity of the client-side wizard's regex detector (spec §8.4) --
    the shipped frontend wizard does this in-browser already; this exists for
    non-browser clients / bulk conversion / audit reproducibility."""
    candidates = []
    seen = set()
    for rx, kind in _PRESET_PATTERNS:
        for m in rx.finditer(body.text):
            raw = m.group(0)
            if raw in seen:
                continue
            seen.add(raw)
            field = re.sub(r"[^a-z0-9]+", "_", m.group(1).strip().lower()).strip("_") or "field"
            candidates.append({"raw": raw, "suggested_type": kind, "suggested_field": field})
    return {"candidates": candidates}
