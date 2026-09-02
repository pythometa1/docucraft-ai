"""Tenancy guards.

Multi-tenancy here is enforced per handler rather than by row-level security or a
query-filter mixin, which means a handler that forgets the check reads across the
tenant boundary. Several list endpoints did exactly that -- they filtered on the
path parameter alone (`WHERE project_id = :id`), so a known UUID from another org
returned its rows.

Centralising the checks means there is one place to audit, and adding a new
endpoint is a matter of asking for the entity through here rather than
remembering to re-implement the comparison. Each helper raises the same 404 the
handlers already returned, so a cross-tenant probe is indistinguishable from a
genuinely missing row.
"""

from sqlalchemy.orm import Session

from app.models import (
    Conversation, DocumentVersion, DraftDocument, GeneratedDocument, Project,
    SourceFile, SourceVersion, TemplateBlueprint, TemplateFile,
    TemplateLibrary, TemplateManifest, TemplateVersion, User,
)
from app.security import error


def owned_project(db: Session, project_id: str, user: User, *, allow_deleted: bool = False) -> Project:
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id or (project.deleted_at and not allow_deleted):
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    return project


def owned_template_file(db: Session, template_id: str, user: User) -> TemplateFile:
    tf = db.get(TemplateFile, template_id)
    if not tf or tf.org_id != user.org_id or tf.deleted_at:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    return tf


def owned_template_version(db: Session, version_id: str, user: User) -> TemplateVersion:
    tv = db.get(TemplateVersion, version_id)
    if tv is None:
        raise error("TEMPLATE_NOT_FOUND", "Template version not found", 404)
    owned_template_file(db, tv.template_file_id, user)  # org lives on the parent
    return tv


def owned_source_file(db: Session, source_id: str, user: User) -> SourceFile:
    sf = db.get(SourceFile, source_id)
    if not sf or sf.org_id != user.org_id or sf.deleted_at:
        raise error("SOURCE_NOT_FOUND", "Source file not found", 404)
    return sf


def owned_source_version(db: Session, version_id: str, user: User) -> SourceVersion:
    sv = db.get(SourceVersion, version_id)
    if sv is None:
        raise error("SOURCE_NOT_FOUND", "Source version not found", 404)
    owned_source_file(db, sv.source_file_id, user)
    return sv


def owned_draft(db: Session, draft_id: str, user: User) -> DraftDocument:
    draft = db.get(DraftDocument, draft_id)
    if not draft or draft.org_id != user.org_id or draft.deleted_at:
        raise error("DRAFT_NOT_FOUND", "Draft not found", 404)
    return draft


def owned_document(db: Session, document_id: str, user: User) -> GeneratedDocument:
    gd = db.get(GeneratedDocument, document_id)
    if not gd or gd.org_id != user.org_id:
        raise error("DOCUMENT_NOT_FOUND", "Document not found", 404)
    return gd


def owned_document_version(db: Session, version_id: str, user: User) -> tuple[DocumentVersion, GeneratedDocument]:
    dv = db.get(DocumentVersion, version_id)
    if dv is None:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)
    return dv, owned_document(db, dv.document_id, user)


def owned_manifest(db: Session, manifest_id: str, user: User) -> TemplateManifest:
    m = db.get(TemplateManifest, manifest_id)
    if not m or m.org_id != user.org_id:
        raise error("MANIFEST_NOT_FOUND", "Manifest not found", 404)
    return m


def owned_conversation(db: Session, conversation_id: str, user: User) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.org_id != user.org_id:
        raise error("CONVERSATION_NOT_FOUND", "Conversation not found", 404)
    return conversation


def owned_template_library(db: Session, library_id: str, user: User) -> TemplateLibrary:
    library = db.get(TemplateLibrary, library_id)
    if not library or library.org_id != user.org_id or library.deleted_at:
        raise error("TEMPLATE_NOT_FOUND", "Template not found", 404)
    return library


def owned_blueprint(db: Session, blueprint_id: str, user: User) -> TemplateBlueprint:
    blueprint = db.get(TemplateBlueprint, blueprint_id)
    if not blueprint or blueprint.org_id != user.org_id or blueprint.deleted_at:
        raise error("BLUEPRINT_NOT_FOUND", "Template blueprint not found", 404)
    return blueprint


