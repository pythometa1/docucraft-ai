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
# The same approval the compile path makes, so a republished template goes
# live rather than leaving the project on the manifest approved before the edit.
from app.routers.manifests import try_auto_approve
from app.security import error, get_current_user
from app.storage import abs_path, save_bytes
from app.templates import blueprint as bp
from app.templates.emit_docx import EmitError, emit, emit_from_base
from app.templates.kits import UnknownKit, kit_objects, list_kits, load_kit
from app.verticals import FALLBACK_KITS as FALLBACK_KIT_BY_SERVICE
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


def _blueprint_for_template(db: Session, *, org_id: str, template_file_id: str):
    """The live blueprint already reading this template, if there is one.

    `TemplateBlueprint.template_file_id` carries no unique constraint and nothing
    queried by it, so `:from-template` minted a new blueprint -- and paid for a
    fresh agentic compile -- on every call. Two clicks of an Edit button meant two
    blueprints on one file, each able to emit onto it. Newest wins, because
    duplicates can already exist from before this looked.
    """
    return db.scalars(
        select(TemplateBlueprint)
        .where(TemplateBlueprint.org_id == org_id,
               TemplateBlueprint.template_file_id == template_file_id,
               TemplateBlueprint.deleted_at.is_(None))
        .order_by(TemplateBlueprint.updated_at.desc())
    ).first()


class _ManifestReading:
    """A stored manifest in the shape `objects_from_compile` reads.

    `lift.objects_from_compile` and `lift.mark_instructions` reach for `fields`,
    `conditions`, `blocks` and `delete_always` by `getattr` and nothing else --
    which is exactly what `TemplateManifest` stores. So a template that has
    already been read can be opened for editing with no model call at all, and
    `tests/test_blueprint_lift.py` has been passing a four-attribute stub for
    precisely this reason since it was written.
    """

    def __init__(self, m: TemplateManifest):
        self.fields = m.fields or []
        self.conditions = m.conditions or []
        self.blocks = m.blocks or []
        self.delete_always = m.delete_always or []
        self.compiled_by = m.compiled_by
        self.confidence = m.confidence

    def __bool__(self) -> bool:
        return bool(self.fields or self.conditions)


def _existing_reading(db: Session, template_file_id: str, org_id: str):
    """The newest manifest that actually read something, or None.

    "Actually read something" is the whole condition. A failed compile stores a
    manifest with no fields and no conditions, and a blueprint built from one
    could never be published: `_lint_current` turns every assertion fault into a
    blocker, so each unclaimed placeholder would block, and the author would have
    to hand-write the entire reading before the Publish button would work. Better
    to spend the compile.
    """
    for m in db.scalars(
        select(TemplateManifest)
        .where(TemplateManifest.org_id == org_id,
               TemplateManifest.template_file_id == template_file_id)
        .order_by(TemplateManifest.version_no.desc())
    ).all():
        reading = _ManifestReading(m)
        if reading:
            return m, reading
    return None, None


def _blueprint_from_reading(db: Session, user: User, *, template_file, version, path: str,
                            compiled, source: str, name: str | None = None,
                            summary: str | None = None, blocking_note: str | None = None) -> dict:
    """Place a reading onto the document and store it as a blueprint's first version.

    Everything here is deterministic -- `read_body`, `objects_from_compile` and
    `mark_instructions` make no model call -- which is what lets the same code
    serve a fresh compile and a manifest that was compiled weeks ago.

    `compiled` is anything carrying `fields` / `conditions` / `blocks` /
    `delete_always`: a `CompiledManifest` from the compiler, or `_ManifestReading`
    over a stored row.
    """
    document_body, read_notes = read_body(path)
    objects, findings = objects_from_compile(document_body, compiled)
    document_body, cleaned = mark_instructions(document_body, compiled)

    findings = list(findings) + [
        {"code": "content_not_modelled", "severity": "advisory", "detail": note,
         "object_id": None, "paragraph_index": None}
        for note in read_notes
    ]
    if blocking_note:
        findings.append({
            "code": "compile_did_not_converge", "severity": "blocking",
            "detail": blocking_note, "object_id": None, "paragraph_index": None})

    row = TemplateBlueprint(
        org_id=user.org_id, project_id=template_file.project_id,
        name=name or template_file.name, kind="legacy", status="draft",
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
            "compiled_by": getattr(compiled, "compiled_by", None),
            "confidence": getattr(compiled, "confidence", None),
            "instructions_cleaned": cleaned,
            # Which of the three paths produced this, so a reader can tell a
            # placement of an old reading from a fresh one without guessing.
            "read_from": source,
        },
        created_by=user.id,
        change_summary=summary or (
            f"Read from {template_file.name}; {cleaned} author instruction(s) removed."),
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


@router.post("/template-blueprints:from-template", status_code=201)
def from_template(body: FromTemplateRequest, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Hand back something editable for an uploaded template.

    Three ways to get there, cheapest first, because the expensive one is very
    expensive: a full agentic compile is one model call per chunk plus a
    reconcile plus up to twelve review rounds, and §18 budgets it five minutes at
    p95. Paying that to open an editor -- especially on a template whose compile
    has *just failed*, where it may fail again the same way -- is not something a
    button on a project page can do.

      1. A blueprint already reading this template  ->  return it.
      2. A manifest that already read it            ->  place it, no model call.
      3. Nothing has read it yet                    ->  compile.

    Step 2 is the one that makes this usable. The reading and the placement were
    always separate jobs: the compile decides what the template means, and
    `objects_from_compile` decides where those meanings sit in the document --
    deterministically, from data the manifest already stores. Re-deriving the
    meaning to redo the placement was only ever an accident of how this endpoint
    was written first.

    Step 3 still refuses rather than working around a missing model, because a
    blueprint built from an empty reading looks exactly like one built from a
    good reading of a template that asks for nothing.
    """
    template_file = owned_template_file(db, body.template_file_id, user)

    existing = _blueprint_for_template(
        db, org_id=user.org_id, template_file_id=template_file.id)
    if existing is not None:
        current = _current_version(db, existing)
        if current is not None:
            return {**_blueprint_out(existing, current), "version": _version_out(current)}

    version = db.get(TemplateVersion, template_file.current_version_id)
    if version is None:
        raise error("TEMPLATE_NOT_PARSED",
                    "This template has not been parsed yet, so there is nothing to read.", 409)

    path = str(abs_path(version.blob_path))

    manifest_row, reading = _existing_reading(db, template_file.id, user.org_id)
    if reading is not None:
        return _blueprint_from_reading(
            db, user, template_file=template_file, version=version, path=path,
            compiled=reading, source=f"manifest:{manifest_row.id}",
            summary=(f"Read from {template_file.name}, using the reading already compiled "
                     f"for manifest v{manifest_row.version_no}."))

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

    unconverged = None
    if not outcome.ok:
        reason = outcome.reason or "no reason recorded"
        unconverged = (
            f"The reading of this template did not settle ({reason}). What is here is the "
            "best reading reached; check it before publishing.")

    return _blueprint_from_reading(
        db, user, template_file=template_file, version=version, path=path,
        compiled=outcome.manifest, source="compile", name=body.name,
        summary=None, blocking_note=unconverged)


class FromDescriptionRequest(BaseModel):
    description: str
    name: str | None = None
    project_id: str | None = None
    #: Selects a per-service prompt pack ("invoice" today) and the kit used as
    #: a fallback when no model is configured or authoring fails validation.
    service: str | None = None


# FALLBACK_KIT_BY_SERVICE (imported at the top, from the registry) names the
# kit that stands in when the model cannot author for a service -- a wizard
# that dead-ends on a missing API key is a wizard nobody finishes.


@router.post("/template-blueprints:from-description", status_code=201)
def from_description(body: FromDescriptionRequest, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """A whole template, authored by a model from a plain description.

    The model emits a constrained body vocabulary; the server assembles it,
    derives the typed objects, and *proves* the result by emitting it and
    asserting the round-trip property before anything is persisted -- see
    `compiler.blueprint_author`. What comes back is an ordinary draft
    blueprint: same studio, same lint, same publish gate as everything else.

    When no model is configured, or the model's two attempts both fail
    validation, the service's kit stands in -- with the reason recorded in the
    response and the provenance, never silently.
    """
    from app.compiler.blueprint_author import AuthoringFailed, author_blueprint

    if not (body.description or "").strip():
        raise error("DESCRIPTION_REQUIRED",
                    "Describe the business and the document, or start from a kit.", 422)
    if body.project_id:
        owned_project(db, body.project_id, user)

    service = (body.service or "").strip().lower() or None
    fallback_kit = FALLBACK_KIT_BY_SERVICE.get(service)

    authored = None
    fallback_reason = None
    try:
        authored = author_blueprint(
            body.description, service=service,
            llm_policy=llm_policy_for(
                db, user.org_id, project_id=body.project_id, user_id=user.id,
                subject_type="template_authoring", subject_id=None))
    except LLMNotConfiguredError as exc:
        if fallback_kit is None:
            raise error("LLM_NOT_CONFIGURED", str(exc) or NO_MODEL_MESSAGE, 503)
        fallback_reason = "No language model is configured, so a shipped kit stands in."
    except AuthoringFailed as exc:
        if fallback_kit is None:
            raise error(
                "TEMPLATE_NOT_AUTHORED",
                f"The model could not produce a verifiable template ({exc}). "
                "Try rephrasing the description, or start from a kit.", 502)
        fallback_reason = (
            f"The model could not produce a verifiable template ({exc}), "
            "so a shipped kit stands in.")

    if authored is not None:
        blueprint_kind = "generated"
        blueprint_body, objects = authored["body"], authored["objects"]
        findings = authored["findings"]
        notes = authored["notes"]
        provenance = {"kind": "generated", "description": body.description[:2000],
                      "service": service, "model": authored.get("model")}
        change_summary = "Authored from a description."
        generation = {"source": "model", "notes": notes, "model": authored.get("model")}
    else:
        from app.templates.kits import kit_objects as _kit_objects
        from app.templates.kits import load_kit as _load_kit

        kit = _load_kit(fallback_kit)
        blueprint_kind = "kit"
        blueprint_body, objects = kit["body"], _kit_objects(kit)
        findings = []
        provenance = {"kind": "kit", "kit": kit["id"],
                      "description": body.description[:2000],
                      "fallback_reason": fallback_reason}
        change_summary = f"Started from the {kit['name']} kit ({fallback_reason})"
        generation = {"source": "kit_fallback", "notes": [fallback_reason], "model": None}

    row = TemplateBlueprint(
        org_id=user.org_id, project_id=body.project_id,
        name=body.name or (service.title() if service else "Generated template"),
        kind=blueprint_kind, status="draft", created_by=user.id)
    db.add(row)
    db.flush()
    first = TemplateBlueprintVersion(
        blueprint_id=row.id, org_id=row.org_id, version_no=1,
        body=blueprint_body, objects=objects, findings=findings,
        provenance=provenance, change_summary=change_summary[:400], created_by=user.id)
    db.add(first)
    db.flush()
    row.current_version_id = first.id

    log_audit(db, user, "Authored a template from a description", "template_blueprint",
              row.id, body.project_id, "info", (body.description or "")[:200])
    db.commit()
    db.refresh(row)
    db.refresh(first)
    return {**_blueprint_out(row, first), "version": _version_out(first),
            "generation": generation}


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
        body=kit["body"], objects=kit_objects(kit), findings=[],
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
def list_blueprints(project_id: str | None = None, template_file_id: str | None = None,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(TemplateBlueprint).where(
        TemplateBlueprint.org_id == user.org_id, TemplateBlueprint.deleted_at.is_(None))
    if project_id:
        owned_project(db, project_id, user)
        query = query.where(TemplateBlueprint.project_id == project_id)
    if template_file_id:
        # So a caller holding a template can ask "is one of these already open?"
        # without pulling the org's whole list and filtering client-side.
        query = query.where(TemplateBlueprint.template_file_id == template_file_id)
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
    # Guarded on the id being set, not just compared to it. Both sides are
    # nullable, and SQLAlchemy renders `== None` as `IS NULL` -- so for a
    # blueprint that has never been published this asked "does this org hold any
    # approved manifest with no template file at all?", which is a real row shape
    # (`template_file_id` is nullable and manifests are created that way) and has
    # nothing to do with this blueprint. An unpublished draft could therefore be
    # refused because of an unrelated manifest somewhere else in the org.
    published = None
    if row.template_file_id:
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
            "it name the version it came from. Archive it instead, which takes it off the list "
            "and leaves everything generated from it alone.",
            409, {"archive_with": f"/template-blueprints/{row.id}:archive"})

    row.deleted_at = now()
    log_audit(db, user, "Deleted a template blueprint", "template_blueprint", row.id,
              row.project_id, "warning", row.name)
    db.commit()
    return {"status": "deleted"}


@router.post("/template-blueprints/{blueprint_id}:archive")
def archive_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Take a published template off the list without pretending anything is gone.

    `:delete` refuses a template something was published from, and has told the
    reader to "archive it instead" since it was written -- while `status` carried
    an `archived` value nothing ever set and no endpoint existed to reach it. So
    the one route out of that refusal was a sentence describing a button that was
    never built, which is worse than no advice at all.

    It matters more now than it did. A compile approves its own manifest where it
    may, so almost every template that has been read has an approved manifest and
    lands on that refusal -- where before, with manifests mostly sitting in
    `draft`, it was rare enough to go unnoticed.

    Deliberately not a delete. The emitted template, its versions, the manifests
    compiled from it and every document already generated all stay exactly as
    they are, because letters that have gone out name the version they came from.
    This hides the authoring draft, and says so.
    """
    row = owned_blueprint(db, blueprint_id, user)
    if row.deleted_at is not None:
        raise error("BLUEPRINT_NOT_FOUND", "This template has already been removed.", 404)

    row.status = "archived"
    # The same stamp `:delete` writes, because "off the list" is one behaviour
    # and `list_blueprints` filters on exactly this. `status` is what records
    # which of the two happened.
    row.deleted_at = now()
    row.updated_at = now()
    log_audit(db, user, "Archived a template blueprint", "template_blueprint", row.id,
              row.project_id, "warning", row.name)
    db.commit()
    return {"status": "archived"}


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
    #: Read the published document again with the compiler instead of carrying
    #: the blueprint's own reading forward.
    #:
    #: The two are answering different questions. Ordinary publish trusts the
    #: objects the blueprint holds -- fast, deterministic, no model call -- and
    #: therefore cannot proceed while those objects fail to account for the
    #: document, because the manifest it would write *is* those objects.
    #:
    #: But a blueprint's reading goes stale exactly when it is most useful. Edit
    #: a template to repair a placeholder the compiler could not claim, and the
    #: objects still do not claim it: the document is fixed and the reading is
    #: not, so publish refuses on a template that is now correct. That is a real
    #: dead end, and the way out is to stop carrying the old reading and let the
    #: compiler read what was actually written.
    recompile: bool = False


def _recompile_published(db: Session, user: User, row: TemplateBlueprint, path: str):
    """Read the document that is about to be published, with the compiler.

    The same call the project's own Compile button makes, on the bytes this
    publish is about to write. It exists because a blueprint's reading and its
    document drift apart in exactly the case the editor is for: you open a
    template to repair a placeholder the compiler could not claim, you repair it,
    and the objects still do not claim it -- so the ordinary publish refuses a
    template that is now correct.

    Refusing here rather than half-publishing is deliberate: the caller runs this
    before `emit_blueprint`, so a compile that cannot read the document leaves the
    customer's template untouched instead of appending a version whose manifest
    never arrived.
    """
    progress = CompileProgress(None, org_id=user.org_id,
                               project_id=row.project_id, user_id=user.id)
    try:
        with timed(db, org_id=user.org_id, operation=AGENTIC_COMPILE):
            outcome = compile_agentic_template(
                path,
                llm_policy=llm_policy_for(
                    db, user.org_id, project_id=row.project_id, user_id=user.id,
                    subject_type="template_blueprint", subject_id=row.id),
                progress=progress)
    except LLMNotConfiguredError:  # pragma: no cover - the compiler catches its own
        raise error("LLM_NOT_CONFIGURED", NO_MODEL_MESSAGE, 503)

    if outcome.manifest.compiled_by in ("llm_unavailable", "llm_failed"):
        raise error(
            "TEMPLATE_NOT_READ",
            "The edited template was written, but the model did not return a usable reading of it "
            f"({outcome.reason or 'no reason recorded'}). Nothing was published; try again, or "
            "publish without re-reading.", 502)
    return outcome


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

    It is measured on a *copy*, though, and that part is new. `emit_blueprint`
    commits, so writing the real version first meant a publish this function then
    refused had already appended a `TemplateVersion` to the customer's template
    and moved `current_version_id` onto it -- a rejected publish that silently
    changed which file the project generates from. The bytes are deterministic
    for a given body, so linting a throwaway copy answers exactly the same
    question and leaves nothing behind when the answer is no.
    """
    import tempfile
    from pathlib import Path

    row = owned_blueprint(db, blueprint_id, user)
    version = _require_version(db, row)
    dispositions = (request.dispositions if request else []) or []
    recompile = bool(request.recompile) if request else False

    base_version = None
    if row.source_template_version_id:
        base_version = db.get(TemplateVersion, row.source_template_version_id)
        if base_version is None:
            raise error("TEMPLATE_NOT_FOUND", "The template this was read from is gone.", 404)

    payload = _write_docx(version.body, base_version)

    with tempfile.TemporaryDirectory() as workspace:
        probe = str(Path(workspace) / "candidate.docx")
        Path(probe).write_bytes(payload)

        published_body, _notes = read_body(probe)
        objects, reslot_findings = reslot_against(version.objects, published_body)

        report = _lint_current(db, row, version, emitted_path=probe, objects=objects)
        report.findings.extend(
            LintFinding(f["code"], f["severity"], f["detail"], object_id=f.get("object_id"),
                        paragraph_index=f.get("paragraph_index"))
            for f in reslot_findings)

        # The gate guards the manifest this is about to write *from these
        # objects*. When the compiler is going to read the document again, the
        # objects are not what ships, so gating on them would refuse a template
        # that is now correct for a reading that is merely out of date.
        if not recompile and not report.can_publish(dispositions):
            raise error(
                "BLUEPRINT_NOT_PUBLISHABLE",
                "This template would not produce correct documents yet. "
                + "; ".join(f.detail for f in report.blocking[:3]),
                422, details={"lint": report.as_dict(), "can_recompile": True})

        if recompile:
            # Read it before anything is written, for the same reason the lint
            # runs on a copy: a publish that cannot produce a manifest must leave
            # the customer's template exactly as it was.
            compiled = _recompile_published(db, user, row, probe)

    # Only now is anything written. `emit_blueprint` re-derives the same bytes
    # from the same body; `emit`'s own round-trip check asserts that on every
    # call, which is what makes linting the copy equivalent to linting this.
    emitted = emit_blueprint(blueprint_id, db=db, user=user)
    emitted_version = db.get(TemplateVersion, emitted["template_version_id"])

    next_version_no = 1 + max(db.scalars(
        select(TemplateManifest.version_no).where(
            TemplateManifest.org_id == row.org_id,
            TemplateManifest.template_file_id == row.template_file_id)).all(), default=0)

    if recompile:
        # Straight from the compiler, exactly as the project's own Compile button
        # would produce it -- same code, same shape, same confidence -- so the
        # manifest that ships is a reading of the bytes that shipped.
        #
        # With one addition the compiler cannot make. A §6 TABLE_ROW is an
        # *author's* declaration -- "this row renders once per record" -- and
        # nothing in the document says it; the compiler reads the prototype
        # row's tokens as ordinary fields. Dropping the declaration here would
        # mean a republished invoice quietly stops repeating its line items, so
        # the blueprint's TABLE_ROW objects ride into the recompiled manifest
        # unchanged. They are located by token at fill time, so the compiler's
        # renumbering costs them nothing.
        carried_rows = [
            {"id": o.get("object_id"),
             **{k: v for k, v in o.items() if k not in ("object_id", "id")}}
            for o in (version.objects or ())
            if str(o.get("object_type") or "").upper() == "TABLE_ROW"
        ]
        manifest = TemplateManifest(
            org_id=row.org_id, template_file_id=row.template_file_id,
            template_version_id=emitted_version.id, version_no=next_version_no,
            status="draft", fields=compiled.manifest.fields,
            conditions=compiled.manifest.conditions,
            blocks=list(compiled.manifest.blocks or []) + carried_rows,
            delete_always=compiled.manifest.delete_always,
            confidence=compiled.manifest.confidence,
            compiled_by=compiled.manifest.compiled_by,
            prescan_summary={**(compiled.manifest.prescan_summary or {}),
                             "published_from_blueprint": row.id,
                             "blueprint_version_no": version.version_no,
                             "read_again_after_publish": True},
            compile_transcript=compiled.transcript_dicts(),
            created_by=user.id)
    else:
        envelope = ManifestEnvelope(
            manifest_id=uid(), manifest_version=1, status="DRAFT", organization_id=row.org_id,
            template_version_id=emitted_version.id, objects=objects)
        columns = to_row_values(envelope, objects_column=True).columns
        columns["version_no"] = next_version_no
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

    # The manifest above is written as a draft, and an unapproved manifest cannot
    # generate anything -- so without this a republished template leaves the
    # project still filling documents from the manifest approved *before* the edit.
    # The edit would look applied and do nothing, which is the exact failure the
    # editor's "this is still a draft" panel used to warn about. Approving it here
    # is the same call the compile path makes, and declines in the same two cases.
    template_file = db.get(TemplateFile, row.template_file_id) if row.template_file_id else None
    approval_blocked_reason = try_auto_approve(db, user, manifest, template_file)

    log_audit(db, user, "Published a template from a blueprint", "template_blueprint", row.id,
              row.project_id, "info", f"manifest {manifest.id}")
    if approval_blocked_reason is not None:
        log_audit(db, user, "Published manifest left unapproved", "template_manifest", manifest.id,
                  row.project_id, "warning", approval_blocked_reason[:200])
    db.commit()
    return {
        "blueprint_id": row.id,
        "template_version_id": emitted_version.id,
        "manifest_id": manifest.id,
        "manifest_status": manifest.status,
        "approval_blocked_reason": approval_blocked_reason,
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
