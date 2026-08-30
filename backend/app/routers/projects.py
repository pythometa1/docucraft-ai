from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from datetime import datetime, timezone

from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.value_format import resolved_locale
from app.models import (
    Counter, DraftDocument, GeneratedDocument, LookupValue, Project, SourceFile,
    TemplateFile, User,
)
from app.security import error, get_current_user
from app.audit.service import log_audit

router = APIRouter(tags=["projects"])


@router.get("/lookups")
def list_lookups(kind: str, parent: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(LookupValue).where(LookupValue.org_id == user.org_id, LookupValue.kind == kind, LookupValue.is_active == True)
    if parent is not None:
        stmt = stmt.where(LookupValue.parent_value == parent)
    rows = db.scalars(stmt.order_by(LookupValue.sort_order)).all()
    return {"items": [r.value for r in rows]}


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    region: str
    function: str
    document_type: str
    language: str = "English"
    # How dates and amounts are written -- `en_AU`, `en_GB`, `de_DE`. Separate
    # from `region`, which is continental and answers a different question:
    # "Asia Pacific" contains Australia, Japan and India and is not a locale.
    locale: str | None = None


def _next_display_id(db: Session, counter_name: str, start: int) -> int:
    counter = db.get(Counter, counter_name)
    if counter is None:
        counter = Counter(name=counter_name, value=start)
        db.add(counter)
    counter.value += 1
    db.flush()
    return counter.value


def _project_out(db: Session, p: Project) -> dict:
    has_templates = db.scalar(select(func.count()).select_from(TemplateFile).where(TemplateFile.project_id == p.id, TemplateFile.deleted_at.is_(None))) > 0
    has_sources = db.scalar(select(func.count()).select_from(SourceFile).where(SourceFile.project_id == p.id, SourceFile.deleted_at.is_(None))) > 0
    has_drafts = db.scalar(select(func.count()).select_from(DraftDocument).where(DraftDocument.project_id == p.id, DraftDocument.deleted_at.is_(None))) > 0
    has_generated = db.scalar(select(func.count()).select_from(GeneratedDocument).where(GeneratedDocument.project_id == p.id)) > 0
    return {
        "id": p.id, "display_id": p.display_id, "name": p.name, "description": p.description,
        "region": p.region, "function": p.function, "document_type": p.document_type, "language": p.language,
        "locale": p.locale, "effective_locale": resolved_locale(region=p.region, project_locale=p.locale)[0],
        "locale_source": resolved_locale(region=p.region, project_locale=p.locale)[1],
        "status": p.status, "generation_settings": p.generation_settings,
        "created_at": p.created_at, "updated_at": p.updated_at,
        "has_templates": has_templates, "has_sources": has_sources,
        "has_generation_method": bool(p.generation_settings.get("method")),
        "has_drafts": has_drafts, "has_generated_documents": has_generated,
    }


@router.get("/projects")
def list_projects(
    q: str | None = None,
    status_: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One page of this organisation's projects.

    `total` counts the rows matching the current filters, not every project in
    the organisation. It used to count the latter, which made the header read
    "Content studio (48)" above a search that had matched three -- and it is
    also the number a pager divides to decide whether a next page exists, so a
    filtered list offered pages that were always empty.
    """
    filters = [Project.org_id == user.org_id, Project.deleted_at.is_(None)]
    if q:
        like = f"%{q}%"
        # `display_id` is cast because it is the number printed on every row and
        # the one people actually quote to each other ("what happened to 51003?").
        # Searching without it sent that query to "No projects match your search".
        filters.append(or_(
            Project.name.ilike(like),
            Project.document_type.ilike(like),
            Project.function.ilike(like),
            cast(Project.display_id, String).ilike(like),
        ))
    if status_:
        filters.append(Project.status == status_)

    rows = db.scalars(
        select(Project).where(*filters).order_by(Project.updated_at.desc()).limit(limit).offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Project).where(*filters))
    return {"items": [_project_out(db, p) for p in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    display_id = _next_display_id(db, "project_display_id", 51000)
    project = Project(
        org_id=user.org_id, display_id=display_id, name=body.name, description=body.description,
        region=body.region, function=body.function, document_type=body.document_type,
        language=body.language, locale=(body.locale or "").strip().replace("-", "_") or None,
        status="pending", created_by=user.id,
    )
    db.add(project)
    db.flush()
    log_audit(db, user, "Created project", "project", project.id, project.id, "success", f"{project.name} ({project.display_id})")
    db.commit()
    db.refresh(project)
    return _project_out(db, project)


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.org_id != user.org_id or p.deleted_at:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    return _project_out(db, p)


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    generation_settings: dict | None = None
    locale: str | None = None


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    if body.name is not None:
        p.name = body.name
    if body.description is not None:
        p.description = body.description
    if body.status is not None:
        p.status = body.status
    if body.generation_settings is not None:
        p.generation_settings = {**p.generation_settings, **body.generation_settings}
    if body.locale is not None:
        # Rejected here rather than at render time. A locale babel cannot parse
        # falls back silently during generation, which is the failure this whole
        # field exists to remove.
        from babel import Locale, UnknownLocaleError

        candidate = body.locale.strip().replace("-", "_")
        if candidate:
            try:
                Locale.parse(candidate)
            except (UnknownLocaleError, ValueError):
                raise error(
                    "UNKNOWN_LOCALE",
                    f"{body.locale!r} is not a locale this system can format with. "
                    f"Use a code like 'en_AU', 'en_GB' or 'de_DE'.",
                    422,
                )
        p.locale = candidate or None
    db.commit()
    db.refresh(p)
    return _project_out(db, p)


@router.post("/projects/{project_id}/archive")
def archive_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    p.status = "archived"
    db.commit()
    return {"status": "archived"}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Remove a project and everything reached through it from the workspace.

    A `deleted_at` stamp rather than a DROP. The column already exists and
    `list_projects` and `get_project` already honour it, so the schema was built
    for this; and §16 puts the destruction of blobs and embeddings on the
    retention sweep, which knows the customer's stated period. A button that
    shredded a signed offer letter and its audit lineage on one click would take
    that decision away from the customer.

    The stamp is pushed down to the children that carry the same column.
    Without it the project vanishes while `GET /projects/{id}/templates` still
    answers for it, so anything holding a template id -- an open tab, a bookmark,
    a queued job -- keeps working against a project the user believes is gone.

    `generated_documents` has no such column and is deliberately left alone: the
    documents become unreachable because every route to them runs through the
    project, and destroying records outright is `delete_generated_document`'s
    job, per document, with the blob cascade that requires.
    """
    p = db.get(Project, project_id)
    if not p or p.org_id != user.org_id or p.deleted_at is not None:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)

    stamped = datetime.now(timezone.utc)
    p.deleted_at = stamped
    for model in (TemplateFile, SourceFile, DraftDocument):
        db.execute(
            update(model)
            .where(model.project_id == project_id, model.deleted_at.is_(None))
            .values(deleted_at=stamped)
        )
    log_audit(
        db, user, "Deleted project", "project", project_id,
        project_id=project_id, severity="warning", target=p.name,
    )
    db.commit()
    return {"status": "deleted"}
