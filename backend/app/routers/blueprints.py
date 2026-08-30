"""Template authoring: put a legacy `.docx` in, get an editable template back.

The product could read a template and it could fill one. It could not let anybody
*change* one. A compile that got a block boundary wrong could be argued with
through `PATCH /template-manifests/{id}` and raw JSON, and that is the whole of
it -- the document itself was never editable, so a template that was nearly right
stayed nearly right.

These endpoints are the loop that was missing:

    upload  ->  compile  ->  read into a body  ->  edit  ->  save  ->  download

Every step but the compile is deterministic. `compile_agentic_template` is the
one model call, it happens once, and everything after it is placement and
editing. That matters for the same reason the fill engine makes no model call:
the parts that must be reproducible are, and the part that needs judgement is
isolated where a person can see what it decided.

Versions are never mutated, like every other version row in this schema, which is
what lets `revert-to` fork from any earlier state rather than apologise. And
`source_template_version_id` pins the uploaded original for the life of the
blueprint -- however far the editing goes, the bytes the customer sent are still
there, which is what makes "put it back" an operation rather than a promise.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.compile_progress import CompileProgress
from app.compiler.agentic_compiler import compile_template as compile_agentic_template
from app.db import get_db
from app.llm.provider import LLMNotConfiguredError
from app.metrics import AGENTIC_COMPILE, timed
from app.manifests.models import ManifestEnvelope, to_row_values
from app.models import (
    TemplateBlueprint, TemplateBlueprintVersion, TemplateFile, TemplateManifest,
    TemplateVersion, User, now, uid,
)
from app.ownership import owned_blueprint, owned_project, owned_template_file
from app.security import error, get_current_user
from app.storage import abs_path, save_bytes
from app.templates import blueprint as bp
from app.templates.emit_docx import EmitError, emit, emit_from_base
from app.templates.kits import UnknownKit, list_kits, load_kit, objects_for
from app.templates.blueprint_lint import Finding as LintFinding
from app.templates.blueprint_lint import lint as lint_blueprint
from app.templates.lift import (
    mark_instructions, objects_from_compile, reslot_against,
)
from app.templates.read_docx import read_body
from app.tenancy import llm_policy_for

router = APIRouter(tags=["blueprints"])

NO_MODEL_MESSAGE = (
    "Reading a template needs a language model, and none is configured. The template is still "
    "uploaded; configure a provider and try again."
)


def _blueprint_out(row: TemplateBlueprint, version: TemplateBlueprintVersion | None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "project_id": row.project_id,
        "source_template_version_id": row.source_template_version_id,
        "template_file_id": row.template_file_id,
        "current_version_id": row.current_version_id,
        "version_no": version.version_no if version else None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _version_out(version: TemplateBlueprintVersion) -> dict:
    return {
        "id": version.id,
        "blueprint_id": version.blueprint_id,
        "version_no": version.version_no,
        "body": version.body,
        "objects": version.objects,
        "findings": version.findings,
        "provenance": version.provenance,
        "manifest_id": version.manifest_id,
        "change_summary": version.change_summary,
        "created_at": version.created_at,
    }


def _current_version(db: Session, row: TemplateBlueprint) -> TemplateBlueprintVersion | None:
    if row.current_version_id:
        return db.get(TemplateBlueprintVersion, row.current_version_id)
    return None


def _next_version_no(db: Session, blueprint_id: str) -> int:
    existing = db.scalars(
        select(TemplateBlueprintVersion.version_no)
        .where(TemplateBlueprintVersion.blueprint_id == blueprint_id)
    ).all()
    return max(existing, default=0) + 1


def _require_version(db: Session, row: TemplateBlueprint) -> TemplateBlueprintVersion:
    version = _current_version(db, row)
    if version is None:
        raise error("BLUEPRINT_HAS_NO_VERSION",
                    "This blueprint has no saved state yet.", 409)
    return version


# ---- (a) from a legacy template ----

class FromTemplateRequest(BaseModel):
    template_file_id: str
    name: str | None = None
    progress_token: str | None = None


@router.post("/template-blueprints:from-template", status_code=201)
def from_template(body: FromTemplateRequest, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Compile an uploaded template and hand back something editable.

    The compile is the model's one job here and it has already been built and
    tested; what this adds is the placement, which is deterministic. If the
    compile cannot run -- no key configured -- that is reported rather than
    worked around, because a blueprint built from an empty reading would look
    exactly like one built from a good reading of a template with nothing in it.
    """
    template_file = owned_template_file(db, body.template_file_id, user)
    version = db.get(TemplateVersion, template_file.current_version_id)
    if version is None:
        raise error("TEMPLATE_NOT_PARSED",
                    "This template has not been parsed yet, so there is nothing to read.", 409)

    path = str(abs_path(version.blob_path))
    progress = CompileProgress(body.progress_token, org_id=user.org_id,
                               project_id=template_file.project_id, user_id=user.id)

    try:
        # Timed like every other compile. Without this the two most expensive
        # compiles in the product -- this one and the bulk cluster auto-compile
        # -- are the two `slo_report` cannot see, so the duration it reports is
        # measured over the cheap ones and its sample count is a lie.
        with timed(db, org_id=user.org_id, operation=AGENTIC_COMPILE):
            outcome = compile_agentic_template(
                path,
                llm_policy=llm_policy_for(
                    db, user.org_id, project_id=template_file.project_id, user_id=user.id,
                    subject_type="template_file", subject_id=template_file.id),
                progress=progress)
    except LLMNotConfiguredError:  # pragma: no cover - the compiler catches its own
        raise error("LLM_NOT_CONFIGURED", NO_MODEL_MESSAGE, 503)

    # `compile_template` does not raise when it cannot reach a model; it returns
    # an empty manifest tagged `llm_unavailable` so a row can still be written
    # recording the attempt. That is right for the compile endpoint, which stores
    # the failure, and wrong here: a blueprint carrying an empty reading is
    # indistinguishable from a good reading of a template that asks for nothing,
    # and the user would edit it believing it had been read.
    if outcome.manifest.compiled_by == "llm_unavailable":
        raise error("LLM_NOT_CONFIGURED", NO_MODEL_MESSAGE, 503)
    if outcome.manifest.compiled_by == "llm_failed":
        raise error(
            "TEMPLATE_NOT_READ",
            "The model did not return a usable reading of this template "
            f"({outcome.reason or 'no reason recorded'}). The template is still uploaded; "
            "try again.", 502)

    document_body, read_notes = read_body(path)
    objects, findings = objects_from_compile(document_body, outcome.manifest)
    document_body, cleaned = mark_instructions(document_body, outcome.manifest)

    findings = list(findings) + [
        {"code": "content_not_modelled", "severity": "advisory", "detail": note,
         "object_id": None, "paragraph_index": None}
        for note in read_notes
    ]
    if not outcome.ok:
        reason = outcome.reason or "no reason recorded"
        findings.append({
            "code": "compile_did_not_converge", "severity": "blocking",
            "detail": (
                f"The reading of this template did not settle ({reason}). What is here is the "
                "best reading reached; check it before publishing."),
            "object_id": None, "paragraph_index": None})

    row = TemplateBlueprint(
        org_id=user.org_id, project_id=template_file.project_id,
        name=body.name or template_file.name, kind="legacy", status="draft",
        source_template_version_id=version.id, template_file_id=template_file.id,
        created_by=user.id,
    )
    db.add(row)
    db.flush()

    first = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=1,
        body=document_body, objects=objects, findings=findings,
        provenance={
            "kind": "legacy",
            "source_template_version_id": version.id,
            "compiled_by": outcome.manifest.compiled_by,
            "confidence": outcome.manifest.confidence,
            "instructions_cleaned": cleaned,
        },
        created_by=user.id,
        change_summary=f"Read from {template_file.name}; {cleaned} author instruction(s) removed.",
    )
    db.add(first)
    db.flush()
    row.current_version_id = first.id

    log_audit(db, user, "Created a template blueprint from an upload", "template_blueprint",
              row.id, template_file.project_id, "info", template_file.name)
    db.commit()
    db.refresh(row)
    db.refresh(first)
    return {**_blueprint_out(row, first), "version": _version_out(first)}


@router.get("/template-blueprint-kits")
def blueprint_kits(user: User = Depends(get_current_user)):
    """The documents you can start from.

    A kit is a whole blueprint -- prose with placeholders already in it -- not a
    list of field names. Starting from a form and writing the letter around it is
    the wrong way round: the letter is the thing, and the fields are the holes in
    it.
    """
    return {"items": list_kits()}


class FromKitRequest(BaseModel):
    name: str
    kit: str = "blank"
    project_id: str | None = None


@router.post("/template-blueprints", status_code=201)
def create_from_kit(request: FromKitRequest, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Start a template from scratch.

    The result is the same kind of object as one read from a customer's file --
    same body shape, same objects, same gate -- so nothing downstream has a
    from-scratch branch. What differs is only how it is written back out: there
    is no original to edit, so publishing builds a fresh package.
    """
    if request.project_id:
        owned_project(db, request.project_id, user)
    try:
        kit = load_kit(request.kit)
    except UnknownKit as exc:
        raise error("UNKNOWN_KIT", str(exc), 422)

    row = TemplateBlueprint(
        org_id=user.org_id, project_id=request.project_id, name=request.name or kit["name"],
        kind="kit", status="draft", created_by=user.id)
    db.add(row)
    db.flush()

    first = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=1,
        body=kit["body"], objects=objects_for(kit["body"]), findings=[],
        provenance={"kind": "kit", "kit": kit["id"]},
        change_summary=f"Started from the {kit['name']} kit.", created_by=user.id)
    db.add(first)
    db.flush()
    row.current_version_id = first.id

    log_audit(db, user, "Started a template from a kit", "template_blueprint", row.id,
              request.project_id, "info", kit["name"])
    db.commit()
    db.refresh(row)
    db.refresh(first)
    return {**_blueprint_out(row, first), "version": _version_out(first)}


class FromLibraryRequest(BaseModel):
    library_id: str
    project_id: str | None = None


@router.post("/template-blueprints:from-library", status_code=201)
def from_library(request: FromLibraryRequest, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Rescue a token-library entry into something that can fill a document.

    Those entries were authored as coloured tokens serialising to
    `<span data-token>` HTML, which the fill engine cannot execute -- it works on
    OOXML runs addressed by position, and an HTML span is neither. So the work is
    real and unusable, and this is how it stops being unusable.

    Nothing is deleted: the library row stays where it is and this reads it.
    """
    from app.models import TemplateLibrary, TemplateLibraryVersion
    from app.ownership import owned_template_library
    from app.templates.library_migration import migrate

    library = owned_template_library(db, request.library_id, user)
    content = db.get(TemplateLibraryVersion, library.current_version_id) \
        if library.current_version_id else None
    if content is None:
        raise error("TEMPLATE_NOT_FOUND", "This library entry has no saved content.", 404)

    project_id = request.project_id
    if project_id:
        owned_project(db, project_id, user)

    body, objects, findings = migrate(content.content_html)

    row = TemplateBlueprint(
        org_id=user.org_id, project_id=project_id, name=library.name, kind="library",
        status="draft", created_by=user.id)
    db.add(row)
    db.flush()
    first = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=1, body=body, objects=objects,
        findings=findings,
        provenance={"kind": "library", "library_id": library.id,
                    "library_version_no": content.version_no},
        change_summary=f"Migrated from the token library entry {library.name!r}.",
        created_by=user.id)
    db.add(first)
    db.flush()
    row.current_version_id = first.id

    log_audit(db, user, "Migrated a token-library template", "template_blueprint", row.id,
              project_id, "info", library.name)
    db.commit()
    db.refresh(row)
    db.refresh(first)
    return {**_blueprint_out(row, first), "version": _version_out(first)}


# ---- reading ----

@router.get("/template-blueprints")
def list_blueprints(project_id: str | None = None, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    query = select(TemplateBlueprint).where(
        TemplateBlueprint.org_id == user.org_id, TemplateBlueprint.deleted_at.is_(None))
    if project_id:
        owned_project(db, project_id, user)
        query = query.where(TemplateBlueprint.project_id == project_id)
    rows = db.scalars(query.order_by(TemplateBlueprint.updated_at.desc())).all()
    return {"items": [_blueprint_out(r, _current_version(db, r)) for r in rows]}


@router.get("/template-blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    row = owned_blueprint(db, blueprint_id, user)
    version = _current_version(db, row)
    return {**_blueprint_out(row, version),
            "version": _version_out(version) if version else None}


@router.get("/template-blueprints/{blueprint_id}/versions")
def list_versions(blueprint_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    owned_blueprint(db, blueprint_id, user)
    rows = db.scalars(
        select(TemplateBlueprintVersion)
        .where(TemplateBlueprintVersion.blueprint_id == blueprint_id)
        .order_by(TemplateBlueprintVersion.version_no.desc())
    ).all()
    # The bodies are large and a version list is a picker, not a diff.
    return {"items": [{
        "id": v.id, "version_no": v.version_no, "change_summary": v.change_summary,
        "created_at": v.created_at, "created_by": v.created_by,
        "finding_count": len(v.findings or []), "manifest_id": v.manifest_id,
    } for v in rows]}


# ---- saving ----

class SaveVersionRequest(BaseModel):
    body: dict
    objects: list | None = None
    change_summary: str | None = None
    #: The version the editor was showing. A save that names a version other than
    #: the current one is a save built on a state somebody has since replaced, and
    #: applying it would silently discard their work.
    expected_version_no: int | None = None


@router.post("/template-blueprints/{blueprint_id}/versions", status_code=201)
def save_version(blueprint_id: str, request: SaveVersionRequest,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = owned_blueprint(db, blueprint_id, user)
    current = _require_version(db, row)

    if (request.expected_version_no is not None
            and request.expected_version_no != current.version_no):
        raise error(
            "BLUEPRINT_VERSION_CONFLICT",
            f"This template has moved on to version {current.version_no} since you opened it. "
            "Reload it and reapply your change so neither edit is lost.", 409)

    try:
        normalised, notes = bp.normalise_with_notes(request.body)
    except bp.BlueprintError as exc:
        raise error("BLUEPRINT_INVALID", str(exc), 422)

    saved = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id,
        version_no=_next_version_no(db, row.id),
        body=normalised,
        objects=request.objects if request.objects is not None else current.objects,
        findings=[{"code": "formatting_lost_to_span_merge", "severity": "advisory",
                   "detail": note, "object_id": None, "paragraph_index": None}
                  for note in notes],
        provenance={"kind": "edit", "parent_version_id": current.id},
        parent_version_id=current.id,
        change_summary=request.change_summary or "Edited in the template studio.",
        created_by=user.id,
    )
    db.add(saved)
    db.flush()
    row.current_version_id = saved.id
    row.updated_at = now()

    log_audit(db, user, "Saved a template blueprint", "template_blueprint", row.id,
              row.project_id, "info", saved.change_summary)
    db.commit()
    db.refresh(saved)
    return _version_out(saved)


class RevertRequest(BaseModel):
    version_no: int


@router.post("/template-blueprints/{blueprint_id}:revert-to", status_code=201)
def revert_to(blueprint_id: str, request: RevertRequest, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Fork a new version from an earlier one.

    A fork rather than a rewind: the versions in between are what somebody did,
    and a revert that erased them would make the history a record of the current
    opinion rather than of the work.
    """
    row = owned_blueprint(db, blueprint_id, user)
    target = db.scalars(
        select(TemplateBlueprintVersion).where(
            TemplateBlueprintVersion.blueprint_id == blueprint_id,
            TemplateBlueprintVersion.version_no == request.version_no)
    ).first()
    if target is None:
        raise error("BLUEPRINT_VERSION_NOT_FOUND",
                    f"This template has no version {request.version_no}.", 404)

    forked = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=_next_version_no(db, row.id),
        body=target.body, objects=target.objects, findings=target.findings,
        provenance={"kind": "revert", "reverted_to_version_no": target.version_no,
                    "parent_version_id": target.id},
        parent_version_id=target.id,
        change_summary=f"Reverted to version {target.version_no}.",
        created_by=user.id,
    )
    db.add(forked)
    db.flush()
    row.current_version_id = forked.id
    row.updated_at = now()

    log_audit(db, user, "Reverted a template blueprint", "template_blueprint", row.id,
              row.project_id, "info", forked.change_summary)
    db.commit()
    db.refresh(forked)
    return _version_out(forked)


# ---- the artifact ----

def _write_docx(body: dict, base_version) -> bytes:
    """The template as bytes: edited from its original, or built fresh.

    Which of the two is not a preference. A template read from a customer's file
    is *edited* -- every part of the package except `word/document.xml` is copied
    byte for byte -- because the body is a faithful model of the text and a lossy
    model of section properties, headers, numbering and cell borders. Rebuilding
    one would hand back a document that reads the same and lays out differently.
    A template written from scratch has no such original, so there is nothing to
    preserve and a fresh package is the honest answer.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as workspace:
        out = str(Path(workspace) / "emitted.docx")
        try:
            if base_version is not None:
                emit_from_base(body, str(abs_path(base_version.blob_path)), out)
            else:
                emit(body, out)
        except EmitError as exc:
            raise error("BLUEPRINT_NOT_EMITTABLE", str(exc), 422)
        return Path(out).read_bytes()


@router.post("/template-blueprints/{blueprint_id}:emit", status_code=201)
def emit_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Write the current state to a `.docx` and record it as a template version.

    A blueprint read from a customer's file is published by editing that file --
    every part of the package except `word/document.xml` is copied byte for byte
    -- because the body is a faithful model of the text and a lossy model of
    section properties, headers, numbering and cell borders. Rebuilding would
    hand back a document that reads the same and lays out differently.
    """
    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)

    base_version = None
    if row.source_template_version_id:
        base_version = db.get(TemplateVersion, row.source_template_version_id)
        if base_version is None:
            raise error("TEMPLATE_NOT_FOUND", "The template this was read from is gone.", 404)

    payload = _write_docx(version.body, base_version)

    template_file = db.get(TemplateFile, row.template_file_id) if row.template_file_id else None
    if template_file is None:
        # A template written from scratch has no file to attach a version to
        # until the first time it is written out. Created here rather than at
        # creation so a blueprint nobody publishes leaves nothing behind.
        if not row.project_id:
            raise error(
                "BLUEPRINT_HAS_NO_PROJECT",
                "A template has to belong to a project before it can be written out, because that "
                "is where its file and its source data live.", 409)
        template_file = TemplateFile(
            org_id=row.org_id, project_id=row.project_id, name=row.name,
            status="ready", created_by=user.id)
        db.add(template_file)
        db.flush()
        row.template_file_id = template_file.id
    elif template_file.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "The template this belongs to is gone.", 404)

    rel_path = save_bytes(payload, f"templates/{template_file.project_id}", ".docx")
    emitted = TemplateVersion(
        template_file_id=template_file.id, org_id=row.org_id,
        version_no=1 + max(db.scalars(select(TemplateVersion.version_no).where(
            TemplateVersion.template_file_id == template_file.id)).all(), default=0),
        blob_path=rel_path, created_by=user.id,
    )
    db.add(emitted)
    db.flush()
    version.emitted_template_version_id = emitted.id

    db.flush()
    template_file.current_version_id = emitted.id

    log_audit(db, user, "Emitted a template from a blueprint", "template_blueprint", row.id,
              row.project_id, "info", f"{len(payload)} bytes")
    db.commit()
    return {
        "blueprint_id": row.id,
        "version_no": version.version_no,
        "template_version_id": emitted.id,
        "template_file_id": template_file.id,
    }


@router.get("/template-blueprints/{blueprint_id}/docx")
def download_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """The template as a file, written fresh from the current state.

    Emitted on demand rather than served from the last publish, because the point
    of downloading is to see what you have now -- opening a stale file in Word,
    editing it, and finding the studio disagrees is the confusion this avoids.
    """
    from fastapi.responses import Response

    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)

    base = None
    if row.source_template_version_id:
        base = db.get(TemplateVersion, row.source_template_version_id)
        if base is None:
            raise error("TEMPLATE_NOT_FOUND", "The template this was read from is gone.", 404)
    payload = _write_docx(version.body, base)

    filename = (row.name or "template").replace('"', "") or "template"
    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/template-blueprints/{blueprint_id}")
def delete_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    row = owned_blueprint(db, blueprint_id, user)
    published = db.scalars(
        select(TemplateManifest.id).where(
            TemplateManifest.org_id == user.org_id,
            TemplateManifest.template_file_id == row.template_file_id,
            TemplateManifest.status == "approved")
    ).first()
    if published:
        raise error(
            "BLUEPRINT_IN_USE",
            "An approved manifest was published from this template, and documents generated from "
            "it name the version it came from. Archive it instead.", 409)

    row.deleted_at = now()
    log_audit(db, user, "Deleted a template blueprint", "template_blueprint", row.id,
              row.project_id, "warning", row.name)
    db.commit()
    return {"status": "deleted"}


# ---- the gate ----

def _lint_current(db: Session, row: TemplateBlueprint, version: TemplateBlueprintVersion,
                  *, emitted_path: str | None = None, objects=None):
    from app.models import FieldDictionary

    dictionary = db.scalars(
        select(FieldDictionary.canonical_id).where(FieldDictionary.org_id == row.org_id)
    ).all()
    return lint_blueprint(version.body, objects if objects is not None else version.objects,
                          emitted_path=emitted_path, dictionary=dictionary)


@router.get("/template-blueprints/{blueprint_id}/lint")
def lint_endpoint(blueprint_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Every reason this template would not work, and what would fix it.

    The cheap stages only -- no document is written. Structure, the object model
    and approval's own rules all answer from the body, which is what lets an
    editor call this on every save. The expensive check, asking the emitted file
    what is still wrong, happens at publish where a file exists anyway.
    """
    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)
    return _lint_current(db, row, version).as_dict()


class PublishRequest(BaseModel):
    #: Blocking findings the publisher has looked at and accepted, by code. The
    #: same idiom as `POST /template-manifests/{id}/warnings:resolve`: a finding
    #: leaves the way by being answered, not by being ignored.
    dispositions: list[str] = []


@router.post("/template-blueprints/{blueprint_id}:publish", status_code=201)
def publish(blueprint_id: str, request: PublishRequest | None = None,
            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Write the template and the manifest that fills it, if it will work.

    The order matters and is not obvious. The document is written *first*, then
    the objects are re-addressed against it, and only then is anything checked --
    because a published template is not the file that was read. Author
    instructions the compile removes are written as empty runs, an empty run is
    invisible to the pre-scanner, and the runs either side of it therefore merge.
    So publishing renumbers spans, and a manifest carrying the numbering from
    before would fill the span to the left of every slot for the rest of its
    paragraph. Measuring again on the far side is the only version of this that
    is safe.
    """
    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)
    dispositions = (request.dispositions if request else []) or []

    emitted = emit_blueprint(blueprint_id, db=db, user=user)
    emitted_version = db.get(TemplateVersion, emitted["template_version_id"])
    path = str(abs_path(emitted_version.blob_path))

    published_body, _notes = read_body(path)
    objects, reslot_findings = reslot_against(version.objects, published_body)

    report = _lint_current(db, row, version, emitted_path=path, objects=objects)
    report.findings.extend(
        LintFinding(f["code"], f["severity"], f["detail"], object_id=f.get("object_id"),
                    paragraph_index=f.get("paragraph_index"))
        for f in reslot_findings)

    if not report.can_publish(dispositions):
        raise error(
            "BLUEPRINT_NOT_PUBLISHABLE",
            "This template would not produce correct documents yet. "
            + "; ".join(f.detail for f in report.blocking[:3]),
            422, details={"lint": report.as_dict()})

    envelope = ManifestEnvelope(
        manifest_id=uid(), manifest_version=1, status="DRAFT", organization_id=row.org_id,
        template_version_id=emitted_version.id, objects=objects)
    columns = to_row_values(envelope, objects_column=True).columns
    columns["version_no"] = 1 + max(db.scalars(
        select(TemplateManifest.version_no).where(
            TemplateManifest.org_id == row.org_id,
            TemplateManifest.template_file_id == row.template_file_id)).all(), default=0)

    manifest = TemplateManifest(
        **columns, template_file_id=row.template_file_id,
        delete_always=[], compiled_by=f"blueprint:{row.id}", confidence=1.0,
        prescan_summary={"published_from_blueprint": row.id,
                         "blueprint_version_no": version.version_no},
        created_by=user.id)
    db.add(manifest)
    db.flush()

    version.manifest_id = manifest.id
    version.findings = [f.as_dict() for f in report.findings]
    row.status = "published"
    row.updated_at = now()

    log_audit(db, user, "Published a template from a blueprint", "template_blueprint", row.id,
              row.project_id, "info", f"manifest {manifest.id}")
    db.commit()
    return {
        "blueprint_id": row.id,
        "template_version_id": emitted_version.id,
        "manifest_id": manifest.id,
        "lint": report.as_dict(),
    }


# ---- editing by asking ----

class CopilotRequest(BaseModel):
    message: str
    #: Explicit, never inferred. Guessing between "explain this condition" and
    #: "change this condition" is the class of guess this codebase refuses
    #: everywhere else, and the wrong guess edits a legal template.
    mode: str = "author"


@router.post("/template-blueprints/{blueprint_id}/copilot")
def copilot(blueprint_id: str, request: CopilotRequest, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    """Propose changes, or explain what the template does. Never both, never applied.

    What comes back from the model is a list of typed operations, and this runs
    them through the same applier a human edit goes through -- so the response
    already knows which of them would be refused and why. The caller shows the
    diff; `POST .../operations` is what actually writes.
    """
    from app.compiler import blueprint_agent
    from app.templates.blueprint_ops import apply_operations

    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)
    policy = llm_policy_for(db, row.org_id, project_id=row.project_id, user_id=user.id,
                            subject_type="blueprint", subject_id=row.id)

    if request.mode == "explain":
        from app.models import ManifestGeneration

        lineage = [
            {"field_lineage": g.field_lineage, "condition_lineage": g.condition_lineage,
             "qa_passed": g.qa_passed, "qa_notes": g.qa_notes}
            for g in db.scalars(
                select(ManifestGeneration)
                .where(ManifestGeneration.org_id == row.org_id,
                       ManifestGeneration.manifest_id == version.manifest_id)
                .order_by(ManifestGeneration.created_at.desc()).limit(3)).all()
        ] if version.manifest_id else []
        try:
            return {"mode": "explain", **blueprint_agent.explain(
                version.body, version.objects, version.findings, request.message,
                lineage=lineage, llm_policy=policy)}
        except LLMNotConfiguredError:
            raise error("LLM_NOT_CONFIGURED", NO_MODEL_MESSAGE, 503)

    if request.mode != "author":
        raise error("UNKNOWN_COPILOT_MODE",
                    f"{request.mode!r} is not a mode; use 'author' or 'explain'.", 422)

    try:
        proposal = blueprint_agent.propose(
            version.body, version.objects, version.findings, request.message, llm_policy=policy)
    except LLMNotConfiguredError:
        raise error("LLM_NOT_CONFIGURED", NO_MODEL_MESSAGE, 503)

    # Run them now, against a copy, so the caller is shown what *would* happen
    # rather than a list of intentions. Nothing is saved.
    trial = apply_operations(version.body, version.objects, proposal["operations"])
    lint_after = _lint_current(db, row, version, objects=trial.objects)

    return {
        "mode": "author",
        "operations": trial.applied,
        "rejected": trial.rejected,
        "questions": proposal["questions"],
        "notes": proposal["notes"],
        "verdict": proposal["verdict"],
        "model": proposal["model"],
        "lint_before": _lint_current(db, row, version).as_dict(),
        "lint_after": lint_after.as_dict(),
    }


class OperationsRequest(BaseModel):
    ops: list[dict]
    expected_version_no: int | None = None
    change_summary: str | None = None


@router.post("/template-blueprints/{blueprint_id}/operations", status_code=201)
def apply_ops(blueprint_id: str, request: OperationsRequest, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Apply a batch of operations, saving a new version.

    Re-validated and re-applied from scratch server-side. A client cannot smuggle
    an operation past the guards by having had it approved a moment ago against a
    version that has since moved.
    """
    from app.templates.blueprint_ops import apply_operations

    row = owned_blueprint(db, blueprint_id, user)
    current = _require_version(db, row)

    if (request.expected_version_no is not None
            and request.expected_version_no != current.version_no):
        raise error(
            "BLUEPRINT_VERSION_CONFLICT",
            f"This template has moved on to version {current.version_no} since these changes were "
            "proposed. Reload it and ask again.", 409)

    result = apply_operations(current.body, current.objects, request.ops)
    if not result.applied:
        raise error(
            "NO_OPERATION_APPLIED",
            "None of these changes could be applied: "
            + "; ".join(r["reason"] for r in result.rejected[:3]), 422,
            details={"rejected": result.rejected})

    saved = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=_next_version_no(db, row.id),
        body=result.body, objects=result.objects, findings=[],
        provenance={"kind": "operations", "parent_version_id": current.id,
                    "applied": result.applied, "rejected": result.rejected},
        parent_version_id=current.id,
        change_summary=request.change_summary or f"Applied {len(result.applied)} change(s).",
        created_by=user.id)
    db.add(saved)
    db.flush()
    row.current_version_id = saved.id
    row.updated_at = now()

    log_audit(db, user, "Applied template operations", "template_blueprint", row.id,
              row.project_id, "info", saved.change_summary)
    db.commit()
    db.refresh(saved)
    return {**_version_out(saved), "applied": result.applied, "rejected": result.rejected}
