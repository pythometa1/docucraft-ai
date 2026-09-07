"""One record in, one stored document out -- the core both single-generation
callers share.

Extracted from `routers/manifests.generate_from_manifest` when the invoice
service became its second caller, and the extraction fixed two defects in the
same movement, both documented in §12:

**Locale.** The endpoint called `fill_template` with no `locale=`, so every
single-record generation formatted `en_US` whatever the project said -- while
the batch path resolved the project's locale properly. Two paths, two answers,
and the single path is the one an invoice's currency goes through. The
resolution here is the batch runner's own: an explicit request locale wins,
then the project's, then the default -- with the source recorded.

**qa_policy.** The manifest dict was built with four keys, so a manifest's
declared `qa_policy` never reached the DOCX renderer and `resolve_policy(None)`
silently reapplied the defaults. The PDF path passed it; this one now does too.

Does NOT commit. The caller owns the transaction, which is what lets an
invoice allocate its number, generate, and record its registry row atomically
-- a failed fill rolls the number back instead of burning it.
"""

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.generation.docx_renderer import FillResult, fill_template
from app.generation.pdf_fill import fill_pdf_template
from app.generation.renderers import OOXML_FILL
from app.generation.value_format import resolved_locale
from app.metrics import PDF_OVERLAY_RENDER, SINGLE_DOCX_RENDER, record_qa_findings, timed
from app.models import (
    Counter, DocumentVersion, GeneratedDocument, ManifestGeneration, Project,
    TemplateManifest, TemplateVersion, User, uid,
)
from app.storage import abs_path


class FillFailed(Exception):
    """The render itself raised; nothing was stored."""


@dataclass
class SingleGeneration:
    document: GeneratedDocument
    version: DocumentVersion
    generation: ManifestGeneration
    fill: FillResult
    filename: str
    locale: str
    locale_source: str


def _next_display_id(db: Session, counter_name: str, start: int) -> int:
    """The next display id, with the counter row locked on PostgreSQL.

    The read-modify-write here is racy without the lock: two concurrent
    generations both read value N and both store N+1, and two documents share
    a display id. SQLite's single writer makes the plain path equivalent
    there. (The older copies of this helper in `routers/projects.py` and
    `batch_runner.py` predate the lock and carry the same race.)
    """
    lock = db.get_bind().dialect.name == "postgresql"
    counter = db.get(Counter, counter_name, with_for_update=True if lock else None)
    if counter is None:
        counter = Counter(name=counter_name, value=start)
        db.add(counter)
    counter.value += 1
    db.flush()
    return counter.value


def generate_one(
    db: Session,
    user: User,
    *,
    manifest: TemplateManifest,
    template_version: TemplateVersion,
    project: Project,
    source_record: dict,
    language: str = "en",
    locale: str | None = None,
    change_summary: str = "Generated via Template Manifest",
) -> SingleGeneration:
    """Fill one record against one manifest and persist the document trail.

    Flushes but never commits; raises `FillFailed` when the renderer itself
    fails, leaving nothing half-stored for the caller to clean up.
    """
    manifest_dict = {
        "fields": manifest.fields, "conditions": manifest.conditions,
        "blocks": manifest.blocks, "delete_always": manifest.delete_always,
        # §12's known gap, closed: the manifest's own severity declarations
        # reach the renderer instead of silently reapplying the defaults.
        "qa_policy": manifest.qa_policy,
    }
    if locale:
        resolved, locale_source = locale, "request"
    else:
        resolved, locale_source = resolved_locale(
            region=project.region, project_locale=project.locale)

    out_dir = f"generated/{project.id}"
    os.makedirs(str(abs_path(out_dir)), exist_ok=True)
    generation_id = uid()

    is_pdf = str(template_version.blob_path).lower().endswith(".pdf")
    out_rel = f"{out_dir}/manifest-gen-{generation_id}.{'pdf' if is_pdf else 'docx'}"

    try:
        if is_pdf:
            with timed(db, org_id=user.org_id, operation=PDF_OVERLAY_RENDER):
                fill = fill_pdf_template(
                    str(abs_path(template_version.blob_path)), str(abs_path(out_rel)),
                    manifest_dict, source_record,
                    page_regions=template_version.page_regions,
                    qa_policy=manifest.qa_policy,
                )
        else:
            with timed(db, org_id=user.org_id, operation=SINGLE_DOCX_RENDER):
                fill = fill_template(
                    str(abs_path(template_version.blob_path)), str(abs_path(out_rel)),
                    manifest_dict, source_record, locale=resolved,
                )
    except Exception as exc:
        raise FillFailed(str(exc)) from exc

    display_id = _next_display_id(db, "generated_doc_display_id", 50000)

    # A QA failure has to change something: a blocked document is stored and
    # listed, but it cannot be approved or casually downloaded as if clean.
    doc_status = "draft" if fill.qa_passed else "blocked"
    document = GeneratedDocument(
        org_id=user.org_id, project_id=project.id, draft_id=None,
        display_id=display_id, language=language, status=doc_status)
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id, org_id=document.org_id, version_no=1,
        blob_path=out_rel, renderer=OOXML_FILL,
        change_summary=change_summary, status=doc_status, created_by=user.id)
    db.add(version)
    db.flush()
    document.current_version_id = version.id

    generation = ManifestGeneration(
        id=generation_id, org_id=user.org_id, manifest_id=manifest.id,
        source_record=source_record, field_lineage=fill.field_lineage,
        condition_lineage=fill.condition_lineage, qa_passed=fill.qa_passed,
        qa_notes=fill.qa_notes, blob_path=out_rel, created_by=user.id,
    )
    db.add(generation)
    db.flush()
    record_qa_findings(
        db, org_id=user.org_id, findings=fill.qa_findings, manifest_id=manifest.id,
        generation_id=generation.id, document_version_id=version.id,
    )
    log_audit(
        db, user, "Generated document from manifest", "generated_document",
        document.id, project.id, "info" if fill.qa_passed else "warning",
    )

    extension = "pdf" if is_pdf else "docx"
    filename = f"{project.name}_{project.display_id}_{document.display_id}_{language}.{extension}"
    return SingleGeneration(
        document=document, version=version, generation=generation, fill=fill,
        filename=filename, locale=resolved, locale_source=locale_source,
    )
