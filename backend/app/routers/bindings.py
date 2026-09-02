"""Source records, column<->field bindings, batch generation, and manifest preview.

These are the endpoints the Template Studio drives: read the spreadsheet's real
columns, propose a mapping onto the manifest's fields, let a reviewer correct
it, preview one row, then run the batch.

`binding-suggestions` is where §13 meets a person. It answers with each
target's *ranked* candidates and, for every one of them, the score, the
approval band, the vetoes that fired and the evidence the score was built
from -- because a reviewer asked to accept a number cannot audit a number, and
the mapping this product gets wrong is the plausible one.
"""

import io
import zipfile

import numpy as np
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.db import get_db
from app.expressions.plain_english import annotate_conditions
from app.metrics import record_binding_decisions, record_binding_suggestions
from app.models import (
    DocumentVersion, FieldDictionary, GeneratedDocument, GenerationJob,
    ManifestBinding, Project, SourceFile, SourceVersion, TemplateFile,
    TemplateManifest, TemplateVersion, User,
)
from app.ownership import owned_manifest, owned_project, owned_source_version
from app.generation.document_status import DOWNLOADABLE
from app.security import error, get_current_user
from app.compiler import confidence as cf
from app.generation.batch_runner import run_batch, run_row
from app.generation.source_resolver import (
    SIGNALS_COMPUTED, SIGNALS_NOT_COMPUTED, bindable_targets, max_attainable_score,
    suggest_bindings,
)
from app.retrieval.mapping_memory import FIELD_TYPES, FieldContext, MappingMemory
from app.retrieval.vector import HashingEmbedder
from app.templates.parsers.docx_prescan import prescan
from app.generation.source_ingestion import RECORD_FILE_TYPES, extract_records, sheet_names
from app.generation.value_format import resolve_locale
from app.storage import abs_path

router = APIRouter(tags=["bindings"])


def _source_file_for(db: Session, version: SourceVersion) -> SourceFile:
    return db.get(SourceFile, version.source_file_id)


# --------------------------------------------------------------------- records
@router.get("/source-versions/{version_id}/records")
def list_records(
    version_id: str,
    limit: int = 50,
    sheet: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Structured rows, as opposed to `/chunks` which returns retrieval text.
    One record here becomes one generated document."""
    version = owned_source_version(db, version_id, user)
    source = _source_file_for(db, version)
    if source.file_type not in RECORD_FILE_TYPES:
        raise error(
            "NOT_TABULAR",
            f"'{source.name}' is a {source.file_type} file. Records can only be read from "
            f"{', '.join(sorted(RECORD_FILE_TYPES))}.",
            422,
        )
    path = str(abs_path(version.blob_path))
    columns, records = extract_records(path, source.file_type, sheet)
    return {
        "columns": columns,
        "sheets": sheet_names(path, source.file_type),
        "total": len(records),
        "records": records[:limit],
    }


# -------------------------------------------------------------------- bindings
def _dictionary_for(db: Session, org_id: str) -> dict:
    """alias -> canonical_id, accumulated from every correction a reviewer has
    made across the estate."""
    out: dict[str, str] = {}
    for entry in db.scalars(select(FieldDictionary).where(FieldDictionary.org_id == org_id)).all():
        for alias in entry.aliases or []:
            out[alias] = entry.canonical_id
    return out


def _binding_out(binding: ManifestBinding) -> dict:
    return {
        "id": binding.id,
        "manifest_id": binding.manifest_id,
        "source_version_id": binding.source_version_id,
        "field_bindings": binding.field_bindings,
        "value_map": binding.value_map,
        "updated_at": binding.updated_at,
    }


# §13's field-type vocabulary is closed -- money, date and identifier fields
# carry a veto and a two-signal rule -- while a manifest declares its types in
# this codebase's own words. This is the one place the two vocabularies meet.
_MEMORY_FIELD_TYPES = {"currency": "money", "percent": "number", "amount": "money"}


def _memory_field_type(declared: str | None) -> str:
    mapped = _MEMORY_FIELD_TYPES.get((declared or "").strip().lower(), (declared or "").strip().lower())
    # "string" rather than a raise: an unrecognised declared type is this
    # module's ignorance, and mapping memory only uses the type to file the
    # entry. The type gate that can actually veto a candidate reads the
    # manifest's declaration directly, not this.
    return mapped if mapped in FIELD_TYPES else "string"


class _MemoisedEmbedder:
    """The estate's embedder, with each distinct text vectorised only once.

    Mapping memory compares a query against every stored entry and embeds the
    entry's context text on each comparison, so a manifest with forty targets
    embeds the same forty strings forty times: the cost of a binding screen
    grows with fields x history, and a customer with a year of approvals behind
    them gets the slowest screen. A vector is a pure function of its text, so
    one dictionary removes the quadratic term entirely.

    Scoped to a single request. Nothing here crosses a tenant boundary, because
    nothing is kept after the response is written.
    """

    def __init__(self, inner=None):
        self._inner = inner or HashingEmbedder()
        self._cache: dict[str, object] = {}
        self.dimensions = self._inner.dimensions
        self.name = self._inner.name

    def embed(self, texts):
        if not texts:
            # `np.vstack([])` raises, and an empty batch is a legitimate ask.
            return self._inner.embed(texts)
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            for text, vector in zip(missing, self._inner.embed(missing)):
                self._cache[text] = vector
        return np.vstack([self._cache[t] for t in texts])


def _memory_lookups(db: Session, org_id: str, manifest_id: str, targets: dict) -> dict:
    """§13's historical-approvals signal, replayed from what reviewers saved.

    Mapping memory holds its entries in process (§20's open gap), so nothing in
    it survives a restart and nothing put there by a worker is visible here. The
    durable record of a mapping a human confirmed is `manifest_bindings` -- one
    row per (manifest, source) pairing, written by `upsert_binding` when a
    reviewer saves -- so memory is rebuilt from those rows on each request
    rather than being trusted to still be warm.

    Bindings for *this* manifest are excluded. Precedent means the mapping was
    confirmed somewhere else in the estate; counting the row a reviewer saved on
    this very screen would let a suggestion cite itself as evidence and quietly
    raise its own band on the next page load.
    """
    memory = MappingMemory(provider=_MemoisedEmbedder())
    contexts = {
        field_id: FieldContext(
            field_id=field_id,
            # Keyed on the field id, not the placeholder label: the id is what
            # `field_bindings` stores, so it is the only name the two sides of
            # this replay can agree on.
            label=field_id,
            field_type=_memory_field_type(target.declared_type),
        )
        for field_id, target in targets.items()
    }

    rows = db.scalars(
        select(ManifestBinding)
        .where(ManifestBinding.org_id == org_id, ManifestBinding.manifest_id != manifest_id)
        .order_by(ManifestBinding.created_at)
    ).all()
    for row in rows:
        for field_id, column in (row.field_bindings or {}).items():
            if not column or field_id not in contexts:
                continue
            memory.record_approved_mapping(
                org_id=org_id,
                field_context=contexts[field_id],
                source_column=str(column),
                approved_by=row.created_by,
                at=row.updated_at or row.created_at,
            )

    return {field_id: memory.lookup(context, org_id) for field_id, context in contexts.items()}


def _veto_out(codes) -> list:
    """A veto a reviewer can act on: the rule that fired, in words."""
    return [{"code": code, "explanation": cf.VETO_HELP.get(code, "")} for code in codes]


def _confidence_policy() -> dict:
    """What produced the numbers below, stated on the response itself.

    A score whose provenance lives only in a design document is a number a
    reviewer has to take on trust. This says which of §13's seven signals were
    combined, which were not and why, and -- because it follows from the
    missing three -- that nothing can reach the auto-accept band today.
    """
    ceiling = max_attainable_score()
    return {
        "weights_calibrated": cf.WEIGHTS_CALIBRATED,
        "signals_computed": SIGNALS_COMPUTED,
        "signals_not_computed": SIGNALS_NOT_COMPUTED,
        "max_attainable_score": round(ceiling, 4),
        "auto_accept_reachable": ceiling >= cf.AUTO_ACCEPT_FLOOR,
        "bands": {
            "auto_accept": cf.AUTO_ACCEPT_FLOOR,
            "confirm": cf.CONFIRM_FLOOR,
            "review": cf.REVIEW_FLOOR,
        },
    }


@router.get("/template-manifests/{manifest_id}/binding-suggestions")
def binding_suggestions(
    manifest_id: str,
    source_version_id: str,
    sheet: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ranked candidates per target, each with its §13 score, band and evidence.

    Not one winner per field. §13 defines the REVIEW band as "presented with
    ranked alternatives and the reasoning behind each", and its ambiguity veto
    -- two candidates within 0.05 -- is a statement about the list. Returning
    only the leader would hide the tie that makes the leader unsafe.
    """
    manifest = owned_manifest(db, manifest_id, user)
    version = owned_source_version(db, source_version_id, user)
    source = _source_file_for(db, version)
    columns, records = extract_records(str(abs_path(version.blob_path)), source.file_type, sheet)

    manifest_dict = {"fields": manifest.fields, "conditions": manifest.conditions, "blocks": manifest.blocks}
    targets = {t.field_id: t for t in bindable_targets(manifest_dict)}
    plan = suggest_bindings(
        manifest_dict,
        columns,
        dictionary=_dictionary_for(db, user.org_id),
        # Passed so the type gate reads the column's real values. Every cell in
        # a CSV is text on disk, and typing the columns from the file format
        # would either veto every mapping or check nothing at all.
        records=records,
        memory_lookups=_memory_lookups(db, user.org_id, manifest_id, targets),
    )
    # §22: "Log every mapping suggestion with its score, its evidence and the
    # reviewer's decision. This is the only dataset that can ever calibrate the
    # weights in §13, and it cannot be reconstructed later." This is the moment
    # the evidence exists -- a saved binding keeps the answer and throws away
    # everything that led to it -- so the log is written here, on the read, with
    # the same scores and vetoes that go back in the response.
    try:
        record_binding_suggestions(
            db, org_id=user.org_id, manifest=manifest, plan=plan,
            source_version_id=source_version_id, records=records, columns=columns,
        )
        db.commit()
    except IntegrityError:
        # Two parallel loads of the same binding screen raced to log the same
        # object. The unique index caught it, and the row the other request
        # wrote says exactly what this one would have: losing this write costs
        # nothing, and failing the reviewer's page over it would cost a lot.
        db.rollback()

    sample = records[0] if records else {}

    return {
        "columns": columns,
        "row_count": len(records),
        "confidence_policy": _confidence_policy(),
        "band_summary": plan.band_counts(),
        "suggestions": [
            {
                "field_id": s.field_id,
                "column": s.column,
                # `confidence` is now §13's score rather than a label for the
                # tier that proposed the column. `score` is the same number
                # under the name the record uses.
                "confidence": s.confidence,
                "score": s.confidence,
                "band": s.band,
                "vetoes": _veto_out(s.vetoes),
                "evidence": s.evidence,
                # Every other candidate for this field, best first, each with
                # its own score and reasons. This is what the REVIEW band is.
                "alternatives": s.alternatives,
                "auto_apply": s.auto_applicable,
                "method": s.method,
                "rationale": s.rationale,
                # `origin` tells the UI that colleague_type is a condition
                # variable, not a placeholder -- it has no slot in the document
                # but every conditional block depends on it.
                "origin": targets[s.field_id].origin if s.field_id in targets else "field",
                "type": targets[s.field_id].type if s.field_id in targets else "string",
                "declared_type": s.declared_type,
                # Measured from the values, so a reviewer can see why a type
                # mismatch vetoed a mapping that looks right by name.
                "observed_type": s.observed_type,
                "sample_value": sample.get(s.column) if s.column else None,
            }
            for s in plan.suggestions
        ],
        "unmatched_fields": plan.unmatched_fields,
        "unused_columns": plan.unused_columns,
        # Rows whose condition value selects no branch. Each one is a letter that
        # would generate with a section missing, so it belongs in front of the
        # reviewer here -- while a `value_map` entry still costs one line -- and
        # not in a QA note discovered after the batch has run.
        "unmatched_condition_values": [
            {
                "field_id": u.field_id,
                "column": u.column,
                "observed_value": u.observed_value,
                "offered": u.offered,
            }
            for u in plan.unmatched_condition_values
        ],
    }


class BindingUpsert(BaseModel):
    """A partial update. An omitted field is left as it was.

    These defaulted to `{}` and were assigned unconditionally, so leaving a key
    out did not mean "no change to this" -- it meant "set this to empty". A
    screen that saved column bindings without resending the value map silently
    destroyed it, and the effect is invisible until generation: rows whose
    condition value no longer resolves produce letters with a section missing,
    which is the exact failure `value_map` exists to prevent.

    `None` and `{}` are now different answers. Omit to keep, send `{}` to clear.
    """

    source_version_id: str
    field_bindings: dict | None = None
    value_map: dict | None = None


@router.post("/template-manifests/{manifest_id}/bindings", status_code=201)
def upsert_binding(
    manifest_id: str,
    body: BindingUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    manifest = owned_manifest(db, manifest_id, user)
    owned_source_version(db, body.source_version_id, user)

    binding = db.scalar(
        select(ManifestBinding).where(
            ManifestBinding.manifest_id == manifest_id,
            ManifestBinding.source_version_id == body.source_version_id,
        )
    )
    if binding is None:
        binding = ManifestBinding(
            org_id=user.org_id, manifest_id=manifest_id,
            source_version_id=body.source_version_id, created_by=user.id,
        )
        db.add(binding)
    if body.field_bindings is not None:
        binding.field_bindings = body.field_bindings
    if body.value_map is not None:
        binding.value_map = body.value_map

    # Every confirmed mapping teaches the dictionary, so the next template in
    # the estate binds itself. This is what stops 2,000 templates from meaning
    # 2,000 binding sessions.
    _learn_aliases(db, user.org_id, manifest, binding.field_bindings or {})

    # The other half of the calibration record: what the reviewer did with each
    # suggestion. Recorded on save rather than on approval, because a correction
    # that is later re-corrected is the most informative row in the corpus and
    # approval would only ever see the last one.
    record_binding_decisions(
        db, org_id=user.org_id, manifest_id=manifest_id,
        source_version_id=body.source_version_id, field_bindings=binding.field_bindings or {},
        decided_by=user.id,
    )

    db.commit()
    db.refresh(binding)
    return _binding_out(binding)


def _learn_aliases(db: Session, org_id: str, manifest: TemplateManifest, field_bindings: dict) -> None:
    types = {f["id"]: f.get("type", "string") for f in manifest.fields}
    for field_id, column in field_bindings.items():
        if not column:
            continue
        entry = db.scalar(
            select(FieldDictionary).where(
                FieldDictionary.org_id == org_id, FieldDictionary.canonical_id == field_id
            )
        )
        if entry is None:
            entry = FieldDictionary(
                org_id=org_id, canonical_id=field_id,
                label=column, type=types.get(field_id, "string"), aliases=[],
            )
            db.add(entry)
        from app.compiler.rule_compiler import _slug

        alias = _slug(column)
        if alias not in (entry.aliases or []):
            entry.aliases = [*(entry.aliases or []), alias]
        entry.usage_count = (entry.usage_count or 0) + 1


@router.get("/template-manifests/{manifest_id}/bindings")
def list_bindings(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_manifest(db, manifest_id, user)
    rows = db.scalars(select(ManifestBinding).where(ManifestBinding.manifest_id == manifest_id)).all()
    return {"items": [_binding_out(b) for b in rows]}


@router.get("/field-dictionary")
def list_dictionary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.scalars(
        select(FieldDictionary)
        .where(FieldDictionary.org_id == user.org_id)
        .order_by(FieldDictionary.usage_count.desc())
    ).all()
    return {"items": [
        {"canonical_id": r.canonical_id, "label": r.label, "type": r.type,
         "aliases": r.aliases, "usage_count": r.usage_count}
        for r in rows
    ]}


# ---------------------------------------------------------------- manifest preview
@router.get("/template-manifests/{manifest_id}/preview")
def manifest_preview(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The template as the compiler saw it: every paragraph, every coloured
    span, and which field or block each one belongs to.

    This is what lets a reviewer confirm the compiler's *interpretation* by
    looking at highlighted text, rather than reading JSON and hoping.
    """
    manifest = owned_manifest(db, manifest_id, user)
    version = db.get(TemplateVersion, manifest.template_version_id)
    scan = prescan(str(abs_path(version.blob_path)))

    field_by_slot: dict[tuple, str] = {}
    for f in manifest.fields:
        for slot in f.get("slots", []):
            if "span_index" in slot:
                field_by_slot[(slot["paragraph_index"], slot["span_index"])] = f["id"]

    deleted = {(d["paragraph_index"], d.get("span_index")) for d in manifest.delete_always}
    blocks_by_paragraph: dict[int, list[str]] = {}
    for block in manifest.blocks:
        for p in range(block["start_paragraph"], block["end_paragraph"] + 1):
            blocks_by_paragraph.setdefault(p, []).append(block["id"])

    spans_by_paragraph: dict[int, list] = {}
    for span in scan.spans:
        spans_by_paragraph.setdefault(span.paragraph_index, []).append(span)

    marker_paragraphs = {d["paragraph_index"] for d in manifest.delete_always if d.get("span_index") is not None}
    paragraphs = []
    for index in range(len(scan.paragraphs)):
        spans = spans_by_paragraph.get(index, [])
        paragraphs.append({
            "index": index,
            "text": "".join(s.text for s in spans),
            "in_table": index in scan.table_paragraph_indices,
            "block_ids": blocks_by_paragraph.get(index, []),
            "is_condition_marker": index in marker_paragraphs,
            "spans": [
                {
                    "span_index": s.span_index,
                    "color": s.color,
                    "text": s.text,
                    "in_hyperlink": s.in_hyperlink,
                    "field_id": field_by_slot.get((index, s.span_index)),
                    "is_instruction": (index, s.span_index) in deleted,
                }
                for s in spans
            ],
        })

    return {
        "paragraph_count": len(scan.paragraphs),
        "prescan_summary": manifest.prescan_summary,
        "blocks": manifest.blocks,
        # Same §7 rendering as the manifest detail view. The preview is where a
        # reviewer reads a condition next to the paragraphs it governs, so it is
        # the last screen that could show syntax where meaning was meant to be.
        "conditions": annotate_conditions(manifest.conditions),
        "paragraphs": paragraphs,
    }


# ------------------------------------------------------------------ generation
class PreviewRowRequest(BaseModel):
    source_version_id: str
    row_index: int = 0
    sheet: str | None = None
    field_bindings: dict | None = None
    value_map: dict | None = None
    locale: str | None = None


@router.post("/template-manifests/{manifest_id}/preview-row")
def preview_row(
    manifest_id: str,
    body: PreviewRowRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fill one row without persisting anything -- see exactly what a batch
    would produce before committing to 500 documents."""
    manifest = owned_manifest(db, manifest_id, user)
    version = owned_source_version(db, body.source_version_id, user)
    source = _source_file_for(db, version)
    template_version = db.get(TemplateVersion, manifest.template_version_id)
    template_file = db.get(TemplateFile, manifest.template_file_id) if manifest.template_file_id else None
    project = owned_project(db, template_file.project_id, user) if template_file else None
    if project is None:
        raise error("PROJECT_REQUIRED", "This manifest is not attached to a project template.", 400)

    _columns, records = extract_records(str(abs_path(version.blob_path)), source.file_type, body.sheet)
    record = next((r for r in records if r.get("_row_index") == body.row_index), None)
    if record is None:
        raise error("ROW_NOT_FOUND", f"Row {body.row_index} is not in this source file.", 404)

    bindings, value_map = _resolve_binding_inputs(db, manifest_id, body.source_version_id, body.field_bindings, body.value_map)
    outcome = run_row(
        db, manifest=manifest, template_path=str(abs_path(template_version.blob_path)),
        project=project, record=record, field_bindings=bindings, value_map=value_map,
        locale=body.locale or resolve_locale(region=project.region, project_locale=project.locale),
        language="en", user_id=user.id, persist=False,
    )
    db.rollback()  # preview must leave no trace
    return outcome.__dict__


def _resolve_binding_inputs(db: Session, manifest_id: str, source_version_id: str, field_bindings, value_map):
    """The bindings and value map a generation should run with.

    Each half falls back to the saved binding independently. They used to move
    together: passing `field_bindings` inline discarded the stored `value_map`
    as well, so a caller that re-sent its columns but not its branch mappings
    generated letters whose conditional sections silently vanished -- the same
    failure as erasing the map on save, reached by a different route.
    """
    stored = db.scalar(
        select(ManifestBinding).where(
            ManifestBinding.manifest_id == manifest_id,
            ManifestBinding.source_version_id == source_version_id,
        )
    )
    if field_bindings is not None:
        return field_bindings, (value_map if value_map is not None else (stored.value_map if stored else {}))
    if stored is None:
        raise error(
            "NO_BINDING",
            "No saved binding for this manifest and source. Save one first, or pass field_bindings inline.",
            400,
        )
    return stored.field_bindings, stored.value_map


class BatchRequest(BaseModel):
    source_version_id: str
    sheet: str | None = None
    row_indices: list[int] | None = None
    language: str = "en"
    locale: str | None = None
    field_bindings: dict | None = None
    value_map: dict | None = None


@router.post("/template-manifests/{manifest_id}/generate-batch", status_code=202)
def generate_batch(
    manifest_id: str,
    body: BatchRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    manifest = owned_manifest(db, manifest_id, user)
    # What this gate asks is "could the compiler read this template?", not "has
    # somebody signed it".
    #
    # It used to demand `approved`, which made a signature a precondition for
    # generating anything -- and a freshly compiled manifest can never satisfy
    # that on its own: `validate_manifest` turns every undispositioned compiler
    # warning into a failure, and a manifest that has just been read has warnings
    # and no dispositions by construction. So the real path was upload, read,
    # acknowledge each warning one at a time, approve, and only then generate --
    # for every template, including the ones nobody had asked to review.
    #
    # Two states are still refused, and neither is about a signature.
    #
    # `failed` means the compiler did not produce a usable reading, so there is
    # nothing to fill from and every row would fail the same way.
    #
    # `superseded` and `deprecated` mean this reading has been explicitly
    # retired -- a newer compile of the same template replaced it, or somebody
    # withdrew it. Generating from one produces letters built from a version of
    # the template the customer has already moved off, which is the failure the
    # supersede rule was written for: without it a template accumulated approved
    # manifests and "which one does production run?" had no answer.
    if manifest.status == "failed":
        raise error(
            "MANIFEST_NOT_READ",
            "The compiler could not produce a usable reading of this template, so there is nothing "
            "to fill from. Read the template again from its row on the Template stage.",
            409,
        )
    if manifest.status in ("superseded", "deprecated"):
        raise error(
            "MANIFEST_RETIRED",
            "This reading of the template has been replaced by a newer one, so generating from it "
            "would produce documents from a version the project has moved off. Use the current "
            "manifest for this template.",
            409,
        )
    template_file = db.get(TemplateFile, manifest.template_file_id) if manifest.template_file_id else None
    # The one approval that survives, and only where the customer has switched it
    # on. §16 requires four eyes for a template flagged legally binding, which is
    # the case that rule was written for -- a contract going out under somebody's
    # name. Nothing in the product sets this flag today, so no ordinary template
    # reaches this branch.
    if template_file is not None and template_file.legally_binding and manifest.status != "approved":
        raise error(
            "MANIFEST_NOT_APPROVED",
            "This template is marked legally binding, so it needs sign-off from two different "
            "people before it can generate. Ask a reviewer to approve its manifest.",
            409,
        )
    version = owned_source_version(db, body.source_version_id, user)
    source = _source_file_for(db, version)
    if template_file is None:
        raise error("PROJECT_REQUIRED", "This manifest is not attached to a project template.", 400)
    project = owned_project(db, template_file.project_id, user)

    bindings, value_map = _resolve_binding_inputs(db, manifest_id, body.source_version_id, body.field_bindings, body.value_map)

    # A batch over a source with no rows is not a batch that succeeded with
    # nothing in it -- it is a batch that should never have started. Without this
    # the job runs, finds nothing, and finishes `completed - 0 of 0` beside a
    # "Download all" button, which reads exactly like a run that worked.
    #
    # The case that produces it is the ordinary one: the reviewer downloads the
    # workbook this manifest generates, uploads it back, and has not typed
    # anything into it yet. `build_workbook` pre-formats 500 rows so the
    # dropdowns are there to use, so the sheet is not empty -- it has headers and
    # 500 blank rows -- and nothing before this point could tell the difference.
    if source.file_type in RECORD_FILE_TYPES:
        try:
            _columns, available = extract_records(str(abs_path(version.blob_path)), source.file_type, body.sheet)
        except Exception:  # noqa: BLE001 - an unreadable source is reported by the job, not here
            available = None
        if available is not None:
            selected = (
                [r for i, r in enumerate(available) if i in set(body.row_indices)]
                if body.row_indices else available
            )
            if not selected:
                raise error(
                    "SOURCE_HAS_NO_ROWS",
                    f"{source.name!r} has {len(_columns)} column(s) but no data rows"
                    + (f" on sheet {body.sheet!r}" if body.sheet else "")
                    + ". Fill in one row per document you want generated, then upload it again."
                    if not body.row_indices else
                    f"None of the selected rows exist in {source.name!r}.",
                    422,
                )

    job = GenerationJob(
        org_id=user.org_id, project_id=project.id, status="queued",
        model_profile="deterministic-fill", languages=[body.language],
        progress={"rows_total": 0, "rows_done": 0}, created_by=user.id,
    )
    db.add(job)
    db.flush()
    log_audit(db, user, "Started batch generation", "generation_job", job.id, project.id, "info", source.name)
    db.commit()

    background.add_task(
        run_batch,
        job_id=job.id, manifest_id=manifest_id, source_blob_path=version.blob_path,
        source_file_type=source.file_type, source_version_id=body.source_version_id,
        sheet=body.sheet, field_bindings=bindings,
        value_map=value_map, row_indices=body.row_indices, language=body.language,
        locale_override=body.locale, user_id=user.id,
    )
    return {"job_id": job.id, "status": "queued", "poll": f"/api/v1/jobs/{job.id}"}


@router.get("/jobs/{job_id}/download")
def download_batch(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every approved document from a batch as one ZIP.

    Gated per document like every other egress path. A batch of two hundred
    freshly generated letters is by definition two hundred *unapproved*
    documents, so before this check the endpoint was the one place the approval
    gate could be walked straight around -- and it is the one the batch screen
    calls.

    Skipped rather than refused whole, and listed in `_FAILED.txt`, which is the
    same call `POST /documents:download` makes: a reviewer with a hundred and
    ninety signed letters and ten unsigned ones needs the hundred and ninety plus
    a list, not a 409 and nothing. When nothing at all is approved the count
    below is zero and the refusal arrives honestly, saying why.
    """
    job = db.get(GenerationJob, job_id)
    if not job or job.org_id != user.org_id:
        raise error("JOB_NOT_FOUND", "Job not found", 404)

    rows = (job.progress or {}).get("rows", [])
    version_ids = [r["document_version_id"] for r in rows if r.get("document_version_id")]
    if not version_ids:
        raise error("NOTHING_TO_DOWNLOAD", "This job has not produced any documents yet.", 409)

    buffer = io.BytesIO()
    skipped: list[str] = []
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for version_id in version_ids:
            version = db.get(DocumentVersion, version_id)
            if not version or not version.blob_path:
                continue
            document = db.get(GeneratedDocument, version.document_id)
            # The job records the org; the documents inside it are checked one by
            # one, because a bulk endpoint that trusts its own stored list is the
            # weaker of the two checks available here.
            if document is None or document.org_id != user.org_id:
                continue
            project = db.get(Project, document.project_id)
            name = f"{project.name}_{document.display_id}_{document.language}.docx"
            if version.status not in DOWNLOADABLE:
                skipped.append(f"{name}: not approved (currently {version.status}), so it was left out")
                continue
            path = abs_path(version.blob_path)
            if not path.exists():
                skipped.append(f"{name}: the file is no longer on disk")
                continue
            archive.write(path, arcname=name)
            written += 1

        if skipped:
            archive.writestr(
                "_FAILED.txt",
                "These documents are not in this archive:\n\n" + "\n".join(skipped) + "\n",
            )

    if not written:
        raise error(
            "NOTHING_TO_DOWNLOAD",
            "None of this batch's documents could be prepared: " + "; ".join(skipped[:3]),
            409,
        )

    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="batch-{stamp}.zip"'},
    )
