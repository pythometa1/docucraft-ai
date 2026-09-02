"""API surface for the Template Compiler + Universal Fill Engine
(docs/TEMPLATE_COMPILER_RESEARCH.md). Sits alongside the existing
`templates.py` router (heading/jinja template files) as a second, more
literal engine for legacy colour-coded templates like the Hospira/Pfizer
offer letter this was built and verified against.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.db import get_db
from app.metrics import (
    AGENTIC_COMPILE, FAMILY_MATCH_LOOKUP, PDF_OVERLAY_RENDER, SINGLE_DOCX_RENDER,
    record_qa_findings, timed,
)
from app.models import (
    Counter, DocumentVersion, GeneratedDocument, ManifestGeneration, Project,
    SuggestionLog, TemplateCluster, TemplateClusterMember, TemplateFamily,
    TemplateFile, TemplateManifest, TemplateVersion, User, uid,
)
from app.security import error, get_current_user
from app.retrieval.hybrid import EvidenceDocument, retrieve_evidence
from app.retrieval.indexing import index_manifest_fields
from app.retrieval.store import SqlVectorStore
from app.retrieval.vector import SOURCE_COLUMN_DESCRIPTION, InMemoryVectorStore, VectorIndex
from app.tenancy import llm_policy_for
from app.ownership import owned_manifest, owned_project, owned_template_file
from app.templates.ingest import parse_template_version
from app.templates.parsers.docx_prescan import prescan
from app.generation.docx_renderer import fill_template
from app.generation.pdf_fill import fill_pdf_template
from app.generation.source_template import build_workbook, filename_for
from app.compile_progress import DETERMINISTIC, EMBEDDING, MODEL, RETRIEVAL, CompileProgress
from app.compiler.agentic_compiler import compile_template as compile_agentic_template
from app.compiler.mapping_agent import _paragraph_texts
from app.authz import APPROVE_MANIFEST, check_manifest_approval, has_capability, require
from app.compiler.confidence import Band
from app.expressions.plain_english import annotate_conditions, strip_derived
from app.manifests.validator import validate_manifest
from app.generation.renderers import OOXML_FILL
from app.templates.family_matcher import cluster_templates
from app.templates.fingerprint import fingerprint_file
from app.templates.inheritance import (
    FamilyRecord, InheritanceBranch, NewTemplateVersion, as_evidence_only,
    decide_inheritance, inherit_manifest, inheritance_summary,
)
from app.manifests.diff import diff_manifests
from app.manifests.models import envelope_from_row, to_row_values
from app.storage import abs_path, save_upload

log = logging.getLogger(__name__)

router = APIRouter(tags=["template-manifests"])


def _validatable(m) -> dict:
    """A stored manifest in the shape `validate_manifest` expects."""
    return {
        "fields": m.fields, "conditions": m.conditions, "blocks": m.blocks,
        "delete_always": m.delete_always, "status": m.status,
    }


def _coverage_warnings(db: Session, m: TemplateManifest) -> list[dict]:
    """Placeholders still in the template that this manifest does not account for.

    Answering the question "why did I only find out after a batch?".

    The compile loop already runs this check every round, and a placeholder it
    cannot cover parks the compile as failed -- so in the ordinary case nothing
    reaches here. What reaches here is the manifest compiled *before* the check
    could see split placeholders, and any manifest edited by hand afterwards.
    Those were approvable with no warning at all, and the first sign of trouble
    was every row of a batch coming back "Leftover placeholder brackets".

    Reported as a warning rather than a failure, and the distinction is
    deliberate. `validate_manifest` already refuses to approve a manifest with an
    undispositioned warning, so this is seen and must be answered -- but it is
    answered by a person, who can record that a placeholder lives inside a block
    their data always deletes. A hard blocker would be asserting something this
    cannot prove without the source rows, and a false blocker is how people learn
    to click past a gate.

    Best effort: a template whose file has gone is not a reason to refuse to
    render the validation screen.
    """
    from app.compiler.assertions import uncovered_placeholders
    from app.templates.parsers.docx_prescan import prescan

    tv = db.get(TemplateVersion, m.template_version_id) if m.template_version_id else None
    if tv is None or not tv.blob_path:
        return []
    path = abs_path(tv.blob_path)
    if not os.path.exists(str(path)):
        return []
    try:
        scan = prescan(str(path))
    except Exception:  # noqa: BLE001 - an unreadable template is its own problem
        log.warning("could not pre-scan %s for coverage warnings", tv.blob_path, exc_info=True)
        return []

    faults, split = uncovered_placeholders(scan, _validatable(m))
    return [
        *[
            {
                "code": "uncovered_placeholder",
                "paragraph_index": a.paragraph_index,
                "message": (
                    f"{a.detail} Until it is claimed, every document generated from this template "
                    "fails its QA check with 'Leftover placeholder brackets'."
                ),
            }
            for a in faults
        ],
        # The ones no compile round can fix. They matter more here than the
        # claimable ones, because the reviewer is the only person who can act on
        # them -- so they belong on the screen even though the loop ignores them.
        *split,
    ]


def _all_warnings(db: Session, m: TemplateManifest) -> list[dict]:
    """What the compile recorded, plus what is still true of the template now.

    Both the validation screen and `:approve` call this, because a warning the
    screen shows and the approval does not is a gate nobody can pass, and one the
    approval enforces and the screen does not is a 409 out of nowhere.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for w in [*(m.warnings or []), *_coverage_warnings(db, m)]:
        # Deduplicated, and the compile's own record wins because it comes first.
        #
        # The two sources overlap by design: `collect_with_warnings` stores what
        # the compile found, and `_coverage_warnings` re-derives it from the
        # template so a manifest compiled before the check existed, or edited by
        # hand afterwards, is still covered. Concatenating them showed every
        # warning a template still has twice -- the reviewer saw "4 places" for
        # two placeholders, listed as 15, 19, 15, 19.
        key = (w.get("code"), w.get("paragraph_index"), w.get("evidence"))
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _manifest_out(m: TemplateManifest) -> dict:
    return {
        "id": m.id, "template_file_id": m.template_file_id, "template_version_id": m.template_version_id,
        "version_no": m.version_no, "status": m.status, "fields": m.fields,
        # §7: the approver signs the meaning, not the syntax. Rendered from the
        # stored expression on every read so it cannot drift from what will run.
        "conditions": annotate_conditions(m.conditions),
        "blocks": m.blocks, "compiled_by": m.compiled_by, "confidence": m.confidence,
        "prescan_summary": m.prescan_summary, "created_at": m.created_at,
        "approved_by": m.approved_by, "approved_at": m.approved_at,
        # The reviewer's job is these two. They were computed at compile time
        # and never left the process.
        "warnings": m.warnings or [], "warning_dispositions": m.warning_dispositions or {},
    }


#: How many columns the compiler is shown. §10 caps evidence at
#: `MAX_EVIDENCE_ITEMS`; this sits below it because the list is advisory context
#: in a prompt that already carries the whole template, and a hundred column
#: names would drown the document they are meant to annotate.
COMPILE_EVIDENCE_K = 16


def _compile_evidence(db: Session, org_id: str, paragraph_texts: list[str]) -> list:
    """§10's retrieval sequence, run over this organisation's indexed columns.

    Only `source_column_description` records. The same store also holds indexed
    manifest fields, and feeding those back to the compiler would close a loop:
    the model would see the field ids a previous compile invented, agree with
    them because they came from a template just like this one, and the estate
    would converge on its own first guess with no source data ever consulted.
    The kind filter runs inside retrieval, on both halves of the merge. Trimming
    the ranked output instead returns nothing at all: template fields outnumber
    source columns in a mature index and score higher against a query drawn from
    a template, so every slot is spent before the filter is reached.

    Returns an empty list on any failure. Evidence is an improvement to the
    prompt, not a precondition for compiling: a tenant whose columns have never
    been indexed, or a store that is briefly unreachable, must still be able to
    compile a template the way it did before this existed.
    """
    try:
        store = SqlVectorStore(db)
        # One entry per column name, not per indexed row. `index_source_columns`
        # keys on `{source_version_id}:{column}`, so every re-upload of the same
        # spreadsheet adds a fresh record for a column that already existed: this
        # organisation has 203 rows describing about 25 distinct columns. Ranked
        # undeduplicated they tie with themselves and the top of the list becomes
        # seven copies of "Current Position Title" -- which pushes the columns
        # this is meant to reveal, `Date` among them, off the end of it.
        #
        # The newest row for a column wins: `scoped_rows` orders by record_id and
        # the version id leads it, so the later upload's description -- the one
        # whose type and sample reflect the data in use -- is the one kept.
        by_column: dict = {}
        for row in store.scoped_rows(org_id=org_id, kind=SOURCE_COLUMN_DESCRIPTION):
            by_column[str((row.metadata or {}).get("column") or row.text)] = row
        rows = list(by_column.values())
        if not rows:
            return []
        corpus = [
            EvidenceDocument(
                record_id=r.record_id, org_id=r.org_id, text=r.text,
                kind=r.kind, doc_type=r.doc_type,
            )
            for r in rows
        ]
        # Both retrievers have to see the same deduplicated population. The
        # lexical half reads the corpus passed to it, but the vector half
        # searches whatever store it was handed -- so pointing it at the SQL
        # store would put the duplicates straight back into the merge, where
        # they cannot be collapsed: `retrieve_evidence` merges on record_id, and
        # two uploads of one column have two of those. Re-homing the surviving
        # rows in an in-memory store re-uses their stored vectors, so this costs
        # a dict insert per column and no embedding work at all.
        deduped_store = InMemoryVectorStore()
        for row in rows:
            deduped_store.put(row)
        # The template's own words are the query. Truncated because a long
        # template would otherwise dilute the query into an average of English.
        query = " ".join(t for t in paragraph_texts if t.strip())[:2000]
        if not query.strip():
            return []
        return retrieve_evidence(
            query, org_id=org_id, vector_index=VectorIndex(store=deduped_store),
            corpus=corpus, kind=SOURCE_COLUMN_DESCRIPTION, k=COMPILE_EVIDENCE_K,
        )
    except Exception:
        return []



# ---------------------------------------------------------------- approval, shared

def _approval_blockers(db: Session, user: User, m: TemplateManifest) -> tuple[str, str, dict] | None:
    """Everything that must hold before a manifest may be approved, except the
    four-eyes rule.

    Split out of `:approve` because there are now two callers -- the endpoint,
    and the compile that approves its own output when it is safe to. Written
    twice they would drift, and the half that drifted would be the one nobody
    calls by hand.

    Returns an `error(...)`-ready `(code, message, details)`, or None when there
    is nothing in the way. Four eyes is deliberately *not* checked here: it has a
    side effect (it records the attempt as a first approval) that only makes
    sense when a person pressed a button.
    """
    failures = validate_manifest(
        # The whole stored shape, not three of its keys.
        #
        # `delete_always` was missing, and `orphaned_fields` -- the check whose
        # entire job is "this field sits only in paragraphs the compile deletes"
        # -- reads it. With the key absent it saw nothing deleted and could never
        # fire, at the one moment it exists for. `status` was missing for the same
        # reason and let a failed compile be approved.
        _validatable(m),
        warnings=_all_warnings(db, m), dispositions=m.warning_dispositions,
    )
    if failures:
        return (
            "MANIFEST_INVALID",
            "This manifest cannot be approved until these are resolved: "
            + "; ".join(f.detail for f in failures[:5])
            + (f" (and {len(failures) - 5} more)" if len(failures) > 5 else ""),
            {"failures": [f.as_dict() for f in failures]},
        )

    # §13's fourth band: "Block | < 0.50, or any veto | The manifest cannot be
    # locked until a human resolves it." confidence.py computes the band and
    # stamps it on every suggestion; until now nothing read it back, so the
    # band that exists specifically to stop a lock stopped nothing. A blocked
    # suggestion is resolved by a reviewer binding the object (which moves the
    # row off `pending`), not by re-running the compiler.
    blocked = db.scalars(
        select(SuggestionLog).where(
            SuggestionLog.org_id == user.org_id,
            SuggestionLog.manifest_id == m.id,
            SuggestionLog.band == Band.BLOCK.value,
            SuggestionLog.reviewer_decision == "pending",
        )
    ).all()
    if blocked:
        names = sorted({row.object_id for row in blocked})
        return (
            "MANIFEST_HAS_BLOCKED_MAPPINGS",
            "These mappings scored below the block threshold or hit a veto, and need a human "
            "decision before this manifest can be approved: " + ", ".join(names[:5])
            + (f" (and {len(names) - 5} more)" if len(names) > 5 else ""),
            {"blocked_objects": names},
        )

    return None


def _record_approval(db: Session, user: User, m: TemplateManifest) -> list[TemplateManifest]:
    """Stamp the approval and retire whatever it replaces. Does not commit.

    Assumes every check has already passed -- it is the write half of an approval,
    not a decision about one.
    """
    # Exactly one approved manifest per template. Without this a template
    # accumulated approved manifests and "which one does production run?" had no
    # answer -- generation simply used whichever id the caller happened to hold.
    superseded: list[TemplateManifest] = []
    if m.template_file_id:
        superseded = list(db.scalars(
            select(TemplateManifest).where(
                TemplateManifest.template_file_id == m.template_file_id,
                TemplateManifest.status == "approved",
                TemplateManifest.id != m.id,
            )
        ).all())
        for previous in superseded:
            previous.status = "superseded"

    m.status = "approved"
    m.approved_by = user.id
    m.approved_at = datetime.now(timezone.utc)
    m.approvals = [*(m.approvals or []), {"user_id": user.id, "at": m.approved_at.isoformat()}]
    return superseded


def try_auto_approve(
    db: Session, user: User, m: TemplateManifest, template_file: TemplateFile | None,
) -> str | None:
    """Approve a freshly compiled manifest, when the person who compiled it may.

    A compiled template that nobody has approved cannot generate anything
    (`bindings.py` refuses it), and asking every user to press Approve on their
    own compile was bookkeeping rather than a decision. So the compile approves
    its own output -- but only where doing so does not walk through a control
    that exists on purpose.

    Two cases it must decline, and both are real:

    **The caller cannot approve.** `compile-manifest` requires no capability at
    all; `:approve` requires APPROVE_MANIFEST, and `mapper` is a role defined by
    holding the first and not the second (see `authz.capabilities_of`). Approving
    on their behalf would hand every mapper an approval right by way of an upload
    button.

    **The template is legally binding.** §16 asks for four eyes there, and
    `check_manifest_approval` refuses when the compiler and the approver are the
    same person -- which self-approval makes true by construction. There is no
    version of "compile approves itself" that satisfies a rule whose whole
    content is that two people must be involved.

    Returns None when the manifest was approved, or a sentence for the caller to
    show explaining why it was not. Does not commit; the caller does.
    """
    if not has_capability(user, APPROVE_MANIFEST):
        return (
            f"Your role ({user.role_key}) cannot approve a template manifest, so this one is "
            "waiting for sign-off. Ask somebody who can approve it to do so before generating."
        )

    if template_file is not None and template_file.legally_binding:
        return (
            "This template is marked legally binding, so it needs two different people: whoever "
            "compiled it cannot also approve it. Ask a second reviewer to sign it off."
        )

    blocker = _approval_blockers(db, user, m)
    if blocker:
        return blocker[1]

    _record_approval(db, user, m)
    return None


@router.post("/templates/{template_file_id}/compile-manifest", status_code=201)
def compile_manifest_endpoint(
    template_file_id: str,
    # A token the caller chose before making the request, so it can poll
    # `GET /jobs/{token}` while this is still running. Server-generated ids are
    # useless here: the client cannot learn one until the response arrives, and
    # by then there is nothing left to watch.
    progress_token: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tf = db.get(TemplateFile, template_file_id)
    if not tf or tf.org_id != user.org_id:
        raise error("TEMPLATE_NOT_FOUND", "Template file not found", 404)
    tv = db.get(TemplateVersion, tf.current_version_id) if tf.current_version_id else None
    if not tv:
        raise error("TEMPLATE_NOT_PARSED", "Template has no parsed version yet", 400)

    path = str(abs_path(tv.blob_path))
    # §16 residency, resolved once for the whole compile. Every model call below
    # is handed this policy, so a compile for an EU-pinned organisation refuses
    # rather than reaching a global deployment -- the check used to live only in
    # `llm.boundary`, which no compile path goes through.
    policy = llm_policy_for(db, user.org_id, project_id=tf.project_id, user_id=user.id,
                            subject_type="template_file", subject_id=tf.id)
    agent_log: list = []
    progress = CompileProgress(
        progress_token, org_id=user.org_id, project_id=tf.project_id, user_id=user.id,
    )
    try:
        with progress.stage("parse", "Reading the document", DETERMINISTIC):
            scan = prescan(path)
            progress.note(
                f"{len(scan.paragraphs)} paragraphs, "
                f"{sum(1 for s in scan.spans if s.color in ('red', 'blue') and s.text.strip())} marked runs, "
                f"{len(scan.mergefields)} merge fields"
            )
        # §10 step 5, resolved once for the whole compile: whichever branch runs,
        # it gets the same evidence, so "Compile" and "Compile & self-verify"
        # cannot produce field ids that disagree about what this tenant calls its
        # columns.
        with progress.stage("retrieval", "Looking up this organisation's column names", RETRIEVAL):
            evidence = _compile_evidence(db, user.org_id, _paragraph_texts(scan))
            progress.note(
                f"{len(evidence)} column(s) found to compare against"
                if evidence else "no indexed columns yet, so the compiler works from the template alone"
            )
        # One path. Every template is read by a model, in as many parts as it
        # takes, and reviewed against what the document itself says is wrong
        # until nothing is. There is no dispatch on whether the rules can see
        # anything and no silent substitution when the model cannot be reached:
        # `outcome.ok` is False and the row below records the attempt as failed.
        with progress.stage("agentic", "Reading and reviewing the template", MODEL):
            with timed(db, org_id=user.org_id, operation=AGENTIC_COMPILE):
                outcome = compile_agentic_template(
                    path, llm_policy=policy, evidence=evidence, progress=progress,
                )
            compiled, agent_log = outcome.manifest, outcome.transcript_dicts()
            progress.note(
                f"{len(compiled.fields)} field(s) and {len(compiled.conditions)} condition(s) read"
                if outcome.ok else f"compile did not converge: {outcome.reason[:160]}"
            )
    except Exception as exc:
        raise error("COMPILE_FAILED", f"Could not compile manifest: {exc}", 422)

    existing = db.scalars(select(TemplateManifest).where(TemplateManifest.template_file_id == template_file_id)).all()
    version_no = max((m.version_no for m in existing), default=0) + 1

    manifest = TemplateManifest(
        org_id=user.org_id, template_file_id=template_file_id, template_version_id=tv.id, version_no=version_no,
        # A compile that did not converge is persisted, not raised. The reviewer
        # needs to see that it was attempted and why it stopped; `validate_manifest`
        # refuses to approve the row, so nothing can generate from it.
        status="draft" if outcome.ok else "failed",
        fields=compiled.fields, conditions=compiled.conditions, blocks=compiled.blocks,
        delete_always=compiled.delete_always, compiled_by=compiled.compiled_by, confidence=compiled.confidence,
        warnings=getattr(compiled, "warnings", []) or [],
        prescan_summary={**compiled.prescan_summary, "notes": compiled.notes, "agent_log": agent_log},
        compile_transcript=agent_log,
        created_by=user.id,
    )
    db.add(manifest)
    db.flush()

    # Ask the finished manifest what it did *not* claim, and keep the answer.
    #
    # `_coverage_warnings` re-reads the template and reports every bracket token
    # no field accounts for. Each one is a specific, already-known future: the
    # fill engine leaves the literal text in place and QA blocks the document
    # with "Leftover placeholder brackets". Three canary rows fail, the batch
    # stops, and the reader finds out four steps and one spreadsheet later.
    #
    # It was computed on demand and stored nowhere, so the only screens that
    # could see it were the ones that called the validation endpoint. Persisting
    # it here means the fact is attached to the template from the moment it is
    # read -- which is the moment somebody can still do something about it, and
    # the only moment they are looking at the template rather than at a batch.
    #
    # Deduplicated against whatever the compiler already recorded, on the same
    # key `_all_warnings` uses, so a warning both sources found is not shown
    # twice.
    try:
        seen = {(w.get("code"), w.get("paragraph_index"), w.get("evidence"))
                for w in (manifest.warnings or [])}
        extra = [w for w in _coverage_warnings(db, manifest)
                 if (w.get("code"), w.get("paragraph_index"), w.get("evidence")) not in seen]
        if extra:
            manifest.warnings = [*(manifest.warnings or []), *extra]
    except Exception:  # noqa: BLE001 - the manifest is the deliverable, not the audit of it
        log.warning("could not derive coverage warnings for %s", manifest.id, exc_info=True)

    # §10: "template field context and business meaning" is one of the things
    # the doc says to embed, and it is what makes the next template in the
    # estate bind itself -- `<Reporting To>` finding `New Manager Name` is §10's
    # own example of the variation string overlap cannot reach. Best-effort: a
    # compiled manifest is real work and must not be lost because the index was
    # unavailable.
    try:
        with progress.stage("embedding", "Indexing the fields for future templates", EMBEDDING):
            written = index_manifest_fields(
                VectorIndex(store=SqlVectorStore(db)),
                org_id=user.org_id, manifest_id=manifest.id, fields=manifest.fields,
            )
            progress.note(f"{len(written or [])} field description(s) embedded")
    except Exception:  # noqa: BLE001 - the manifest is the deliverable, not the index
        pass

    # Approve it here, if this person may. Nothing generates from an unapproved
    # manifest, and a separate Approve button on your own compile was bookkeeping
    # rather than a decision -- but `try_auto_approve` declines rather than walking
    # through the capability check or the four-eyes rule, and says why. The reason
    # travels back on the response so the screen can ask for the sign-off it needs
    # instead of showing a mapping step that would refuse to generate.
    approval_blocked_reason = None
    if outcome.ok:
        approval_blocked_reason = try_auto_approve(db, user, manifest, tf)
        if approval_blocked_reason is None:
            log_audit(db, user, "Approved template manifest (on compile)", "template_manifest",
                      manifest.id, tf.project_id, "success", f"{tf.name} v{version_no}")
        else:
            log_audit(db, user, "Compiled manifest left unapproved", "template_manifest",
                      manifest.id, tf.project_id, "warning", approval_blocked_reason[:200])

    log_audit(
        db, user, "Compiled template manifest", "template_manifest", manifest.id, tf.project_id,
        "success" if outcome.ok else "failure",
        f"{tf.name} v{version_no}" + ("" if outcome.ok else f" -- {outcome.reason[:200]}"),
    )
    db.commit()
    db.refresh(manifest)
    progress.finish()
    return {**_manifest_out(manifest), "approval_blocked_reason": approval_blocked_reason}


@router.get("/template-manifests/{manifest_id}")
def get_manifest(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = db.get(TemplateManifest, manifest_id)
    if not m or m.org_id != user.org_id:
        raise error("MANIFEST_NOT_FOUND", "Manifest not found", 404)
    return _manifest_out(m)


@router.get("/templates/{template_file_id}/manifests")
def list_manifests(template_file_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_template_file(db, template_file_id, user)
    rows = db.scalars(select(TemplateManifest).where(TemplateManifest.template_file_id == template_file_id).order_by(TemplateManifest.version_no.desc())).all()
    return {"items": [_manifest_out(m) for m in rows]}


class ManifestPatch(BaseModel):
    fields: list | None = None
    conditions: list | None = None
    blocks: list | None = None


@router.patch("/template-manifests/{manifest_id}")
def patch_manifest(manifest_id: str, body: ManifestPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Human review corrections (research doc §4.1.2 -- the reviewer highlight
    UI's save action lands here)."""
    m = db.get(TemplateManifest, manifest_id)
    if not m or m.org_id != user.org_id:
        raise error("MANIFEST_NOT_FOUND", "Manifest not found", 404)
    if m.status == "approved":
        raise error("MANIFEST_APPROVED", "Approved manifests are immutable -- compile a new version instead", 409)
    if body.fields is not None:
        m.fields = body.fields
    if body.conditions is not None:
        # The read path adds rendered sentences to every condition; this is the
        # write path that would store them. Stripped rather than trusted to be
        # absent -- the reviewer's screen sends back what it was given.
        m.conditions = strip_derived(body.conditions)
    if body.blocks is not None:
        m.blocks = body.blocks
    m.status = "in_review"
    db.commit()
    db.refresh(m)
    return _manifest_out(m)


@router.get("/template-manifests/{manifest_id}/source-template")
def download_source_template(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The spreadsheet this template expects, as a .xlsx.

    The inverse of binding. Binding asks "which of your columns is my
    `work_schedule`?" and gets it wrong when a column's name disagrees with its
    contents; this hands back a sheet whose columns are already named after the
    fields, with each condition column restricted by a dropdown to the exact
    values its branches test. Filled in and uploaded, every target pairs with the
    column of its own name and no observed value can select no branch.

    Available before approval on purpose. Knowing what data a template needs is
    what a person does *while* deciding whether the compile was right, and making
    them approve first would mean approving a manifest whose data requirements
    they had not yet seen.
    """
    m = owned_manifest(db, manifest_id, user)
    tf = db.get(TemplateFile, m.template_file_id)
    workbook = build_workbook(
        {"fields": m.fields, "conditions": m.conditions, "blocks": m.blocks},
        template_name=tf.name if tf else "",
    )
    log_audit(db, user, "Downloaded source template", "template_manifest", manifest_id)
    db.commit()
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename_for(tf.name if tf else "")}"'},
    )


@router.get("/template-manifests/{manifest_id}/validation")
def manifest_validation(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Why this manifest can or cannot be approved, without approving it.

    The reviewer needs to see the blockers while deciding, not discover them by
    pressing Approve and reading a 409.
    """
    m = owned_manifest(db, manifest_id, user)
    failures = validate_manifest(
        # The whole stored shape, not three of its keys.
        #
        # `delete_always` was missing, and `orphaned_fields` -- the check whose
        # entire job is "this field sits only in paragraphs the compile deletes"
        # -- reads it. With the key absent it saw nothing deleted and could never
        # fire, at the one moment it exists for. `status` was missing for the same
        # reason and let a failed compile be approved.
        _validatable(m),
        warnings=_all_warnings(db, m), dispositions=m.warning_dispositions,
    )
    return {
        "manifest_id": m.id, "status": m.status,
        "can_approve": not failures and m.status != "approved",
        "failures": [f.as_dict() for f in failures],
        # The merged set, not just the stored one. A warning that blocks approval
        # but is absent from this list is a gate with no handle: the screen has
        # nothing to render and nothing to disposition, and the reviewer sees
        # only a failure telling them to resolve something they cannot find.
        "warnings": _all_warnings(db, m),
        "warning_dispositions": m.warning_dispositions,
    }


class WarningDisposition(BaseModel):
    code: str
    note: str = ""


@router.post("/template-manifests/{manifest_id}/warnings:resolve")
def resolve_warning(manifest_id: str, body: WarningDisposition, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Record that a human looked at one compiler warning and accepted it.

    The disposition is the audit trail: who cleared it and what they said. A
    warning cannot be cleared on an already-approved manifest, because that
    would let the record of what was reviewed change after the fact.
    """
    m = owned_manifest(db, manifest_id, user)
    if m.status == "approved":
        raise error("MANIFEST_APPROVED", "Approved manifests are immutable -- compile a new version instead", 409)
    known = {w.get("code") for w in (m.warnings or [])}
    if body.code not in known:
        raise error("WARNING_NOT_FOUND", f"This manifest carries no warning {body.code!r}.", 404)
    m.warning_dispositions = {
        **(m.warning_dispositions or {}),
        body.code: {
            "resolved_by": user.id,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "note": body.note,
        },
    }
    db.commit()
    return {"code": body.code, "dispositions": m.warning_dispositions}


@router.post("/template-manifests/{manifest_id}:approve")
def approve_manifest(manifest_id: str, db: Session = Depends(get_db), user: User = Depends(require(APPROVE_MANIFEST))):
    m = owned_manifest(db, manifest_id, user)
    template_file = db.get(TemplateFile, m.template_file_id) if m.template_file_id else None

    # Approval is the gate, so it is the place to check that what is being
    # approved can actually run. Setting the status and writing an audit line
    # about it is not a check.
    blocker = _approval_blockers(db, user, m)
    if blocker:
        code, message, details = blocker
        raise error(code, message, 409, details)

    # §16 separation of duties. Only reaches past the capability check for a
    # template flagged legally binding, where the doc asks for four eyes: a
    # different person from whoever compiled it, and a second distinct approval
    # already on record.
    decision = check_manifest_approval(
        approver_id=user.id,
        compiled_by_id=m.created_by,
        prior_approver_ids=tuple(a.get("user_id") for a in (m.approvals or [])),
        legally_binding=bool(template_file and template_file.legally_binding),
    )
    if not decision.allowed:
        # Record the attempt even though it is refused: a first approver on a
        # four-eyes template has done real work, and the second reviewer needs
        # to see that it happened.
        m.approvals = [*(m.approvals or []), {"user_id": user.id, "at": datetime.now(timezone.utc).isoformat()}]
        log_audit(db, user, "Recorded first approval (four-eyes pending)", "template_manifest", m.id,
                  template_file.project_id if template_file else None, "warning")
        db.commit()
        raise error("FOUR_EYES_REQUIRED", decision.reason, 409)

    superseded = _record_approval(db, user, m)
    log_audit(db, user, "Approved template manifest", "template_manifest", m.id,
              template_file.project_id if template_file else None, "success")
    db.commit()
    return {"status": "approved", "superseded": [p.id for p in superseded]}


# ---------------------------------------------------------------- §11 families, inheritance and diff
def _paragraph_texts_for(scan) -> list:
    """The template's paragraph texts, which is what an anchor resolves against."""
    from app.compiler.mapping_agent import _paragraph_texts

    return _paragraph_texts(scan)


def _envelope(m: TemplateManifest):
    """One manifest row as the §6 envelope the diff and inheritance work on."""
    return envelope_from_row(m, template_family_id=getattr(m, "template_family_id", None))


@router.get("/template-manifests/{manifest_id}/diff")
def diff_manifest(manifest_id: str, against: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What changed between this manifest and another, object by object.

    §11's "detect delta; review changed objects only" needs something that can
    name the delta. `manifest_id` is the baseline and `against` is the
    candidate, so ADDED means the candidate carries an object the baseline did
    not.

    Both ids go through the tenancy guard separately. Scoping only the path
    parameter would turn `against` into a read of any manifest in the estate --
    the diff would happily print another tenant's field mappings, source
    references and anchor tokens.
    """
    left = owned_manifest(db, manifest_id, user)
    right = owned_manifest(db, against, user)
    result = diff_manifests(_envelope(left), _envelope(right))
    return result.as_dict()


class InheritManifestRequest(BaseModel):
    # Left None in the normal case: §11's workflow picks the nearest approved
    # family itself. Set it to inherit from a specific manifest -- a reviewer
    # who knows this template is a variant of one the matcher scored just under
    # the floor. It never widens what the caller can reach: the manifest still
    # goes through the tenancy guard.
    parent_manifest_id: str | None = None


def _lendable_manifest_ids(db: Session, org_id: str) -> tuple:
    """(by template_family_id, by template_version_id) for this tenant's approved manifests.

    A family lends whichever approved manifest it can reach. Two indexes because
    membership arrives two ways: a manifest stamped with the family id belongs to
    it outright, while a family whose representative version has an approved
    manifest can lend that one even though nothing stamped it -- which is the
    normal case, where the family is minted at upload and the manifest is
    approved days later.
    """
    rows = db.scalars(
        select(TemplateManifest).where(
            TemplateManifest.org_id == org_id,
            TemplateManifest.status == "approved",
        )
    ).all()
    by_family = {m.template_family_id: m.id for m in rows if m.template_family_id}
    by_version = {m.template_version_id: m.id for m in rows}
    return by_family, by_version


def _family_records(db: Session, org_id: str) -> list:
    """This tenant's families, scored-ready, with their approved manifest if any.

    The `org_id` filter is the whole of §19's cross-tenant leakage mitigation on
    this path -- family matching compares structure, and structure looks the
    same across customers, so an unfiltered query would offer one org's approved
    mappings to another's template.
    """
    families = db.scalars(select(TemplateFamily).where(TemplateFamily.org_id == org_id)).all()
    by_family, by_version = _lendable_manifest_ids(db, org_id)
    records = []
    for family in families:
        approved_manifest_id = (
            by_family.get(family.id)
            or by_version.get(family.representative_template_version_id)
        )
        try:
            records.append(FamilyRecord.from_row(
                family, approved_manifest_id=approved_manifest_id
            ))
        except (TypeError, ValueError) as exc:
            # A stored fingerprint this build cannot read is a data problem, and
            # scoring the half of it we understand would produce a similarity
            # that is quietly wrong rather than absent.
            raise error(
                "FAMILY_FINGERPRINT_UNREADABLE",
                f"Template family {family.id} carries a fingerprint this build cannot read: {exc}",
                500,
            )
    return records


@router.post("/templates/{template_file_id}/inherit-manifest", status_code=201)
def inherit_manifest_endpoint(template_file_id: str, body: InheritManifestRequest | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Run §11's family workflow for one newly uploaded template.

    Fingerprint the template, find the nearest approved family in this tenant,
    and take one of the doc's three arrows: inherit the approved manifest and
    diff it, inherit the mappings as evidence for a targeted review, or mint a
    new family and leave compiling to `/compile-manifest`.

    The draft this produces is never auto-approved. Inherited objects come back
    PROPOSED carrying a `family_inheritance` evidence tag, and anything whose
    anchor no longer resolves against this template comes back flagged rather
    than dropped.
    """
    tf = owned_template_file(db, template_file_id, user)
    tv = db.get(TemplateVersion, tf.current_version_id) if tf.current_version_id else None
    if not tv:
        raise error("TEMPLATE_NOT_PARSED", "Template has no parsed version yet", 400)

    path = str(abs_path(tv.blob_path))
    try:
        fingerprint = fingerprint_file(path)
        scan = prescan(path)
    except Exception as exc:
        raise error("FINGERPRINT_FAILED", f"Could not read the template structure: {exc}", 422)

    request = body or InheritManifestRequest()
    records = _family_records(db, user.org_id)
    decision = decide_inheritance(fingerprint, records)

    parent_manifest_id = decision.parent_manifest_id
    if request.parent_manifest_id:
        # An explicit parent overrides the branch choice but not the measured
        # similarity: the reviewer is saying which family this belongs to, not
        # how alike the two documents are, and inflating the score would feed
        # §13's family_inheritance signal a number nobody measured.
        parent_manifest_id = request.parent_manifest_id

    family_created = False
    family_id = decision.family_id
    manifest_row = None
    diff_out = None
    summary = None

    if parent_manifest_id:
        parent_row = owned_manifest(db, parent_manifest_id, user)
        if parent_row.status != "approved":
            raise error(
                "PARENT_NOT_APPROVED",
                "§11 inherits from the nearest approved family; manifest "
                f"{parent_row.id} is {parent_row.status}.",
                409,
            )
        if parent_row.template_version_id == tv.id:
            raise error(
                "PARENT_IS_SELF",
                "That manifest is already compiled against this template version, so there is "
                "nothing to inherit.",
                409,
            )
        parent = _envelope(parent_row)
        target = NewTemplateVersion(
            template_version_id=tv.id,
            inventory=_paragraph_texts_for(scan),
            template_family_id=family_id or parent.template_family_id,
            organization_id=tf.org_id,
        )
        try:
            draft = inherit_manifest(parent, target, manifest_id=uid(), similarity=decision.similarity)
        except ValueError as exc:
            raise error("INHERIT_REFUSED", str(exc), 409)
        if decision.branch is not InheritanceBranch.REUSE_MANIFEST:
            draft = as_evidence_only(draft)

        # `objects_column=True` writes the lossless §6 envelope alongside the
        # three legacy lists. Until the column existed this call refused with
        # MANIFEST_OBJECTS_UNSTORABLE whenever the parent carried a SIGNATURE,
        # HEADER, FOOTER, STATIC or NARRATIVE object, because writing the row
        # anyway would have dropped them and reported a successful inheritance.
        # A real offer letter has a signature block, so that refusal was the
        # common case rather than the edge one.
        row_values = to_row_values(draft, objects_column=True)

        existing = db.scalars(
            select(TemplateManifest).where(TemplateManifest.template_file_id == tf.id)
        ).all()
        columns = dict(row_values.columns)
        columns["version_no"] = max((m.version_no for m in existing), default=0) + 1
        manifest_row = TemplateManifest(
            **columns,
            template_file_id=tf.id,
            template_family_id=draft.template_family_id,
            delete_always=parent_row.delete_always or [],
            compiled_by=f"inherited:{parent_row.id}",
            confidence=decision.similarity,
            prescan_summary={
                "inheritance": decision.as_dict(),
                "inherited_from_manifest_id": parent_row.id,
            },
            created_by=user.id,
        )
        db.add(manifest_row)
        db.flush()
        summary = inheritance_summary(draft)
        diff_out = diff_manifests(parent, draft).as_dict()
        family_id = draft.template_family_id

    elif decision.branch is InheritanceBranch.NEW_FAMILY:
        if family_id is None:
            # "Compile as a new family": the family is minted now, with the
            # fingerprint that was just measured, so the *next* template of this
            # type has something to match against. That is the whole compounding
            # effect §11 describes -- "approved family knowledge grows".
            family = TemplateFamily(
                org_id=user.org_id, name=tf.name, fingerprint=fingerprint.as_dict(),
                representative_template_version_id=tv.id,
            )
            db.add(family)
            db.flush()
            family_id, family_created = family.id, True

        # Whatever this template has already had approved now belongs to the
        # family too. Without this the knowledge stays stranded on the template
        # that earned it: a family can only lend a manifest it can reach, and a
        # member that is not the representative is reachable only by this stamp.
        for approved in db.scalars(
            select(TemplateManifest).where(
                TemplateManifest.org_id == user.org_id,
                TemplateManifest.template_version_id == tv.id,
                TemplateManifest.status == "approved",
            )
        ).all():
            approved.template_family_id = family_id

    log_audit(
        db, user, f"Template family decision: {decision.branch.value}", "template_file", tf.id,
        tf.project_id, "info", decision.reason[:400],
    )
    db.commit()
    if manifest_row is not None:
        db.refresh(manifest_row)

    return {
        "template_file_id": tf.id,
        "template_version_id": tv.id,
        "decision": decision.as_dict(),
        "family_id": family_id,
        "family_created": family_created,
        "manifest": _manifest_out(manifest_row) if manifest_row is not None else None,
        "inheritance": summary,
        "diff": diff_out,
    }


class GenerateFromManifestRequest(BaseModel):
    source_record: dict
    language: str = "en"
    project_id: str | None = None


@router.post("/template-manifests/{manifest_id}/generate", status_code=201)
def generate_from_manifest(manifest_id: str, body: GenerateFromManifestRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = owned_manifest(db, manifest_id, user)
    tv = db.get(TemplateVersion, m.template_version_id)
    tf = db.get(TemplateFile, m.template_file_id) if m.template_file_id else None
    # The same two checks the batch path makes, and for the same reason they have
    # to stay in step: this endpoint produces a real, stored, downloadable letter
    # one record at a time, so a rule enforced over a spreadsheet and not here is
    # a rule with a door next to it. What they ask is "could the compiler read
    # this template?", not "has somebody signed it" -- see `generate_batch` for
    # why the second question stopped being a precondition for the first.
    if m.status == "failed":
        raise error(
            "MANIFEST_NOT_READ",
            "The compiler could not produce a usable reading of this template, so there is nothing "
            "to fill from. Read the template again from its row on the Template stage.",
            409,
        )
    if m.status in ("superseded", "deprecated"):
        raise error(
            "MANIFEST_RETIRED",
            "This reading of the template has been replaced by a newer one, so generating from it "
            "would produce documents from a version the project has moved off. Use the current "
            "manifest for this template.",
            409,
        )
    if tf is not None and tf.legally_binding and m.status != "approved":
        raise error(
            "MANIFEST_NOT_APPROVED",
            "This template is marked legally binding, so it needs sign-off from two different "
            "people before it can generate. Ask a reviewer to approve its manifest.",
            409,
        )
    project_id = body.project_id or (tf.project_id if tf else None)
    if not project_id:
        raise error("PROJECT_REQUIRED", "project_id is required when the manifest isn't tied to a project template", 400)
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)

    manifest_dict = {"fields": m.fields, "conditions": m.conditions, "blocks": m.blocks, "delete_always": m.delete_always}
    out_dir = f"generated/{project_id}"
    os.makedirs(str(abs_path(out_dir)), exist_ok=True)
    gen_id_placeholder = f"manifest-gen-{datetime.now(timezone.utc).timestamp()}"

    # §12 keeps one logical manifest and two format-specific execution paths. An
    # immutable PDF is not filled by editing runs -- there are none -- but by
    # masking the approved region and overlaying text at the coordinates a human
    # signed off. The manifest is the same; only the renderer differs, which is
    # exactly the separation §12 asks for.
    is_pdf = str(tv.blob_path).lower().endswith(".pdf")
    out_rel = f"{out_dir}/{gen_id_placeholder}.{'pdf' if is_pdf else 'docx'}"

    try:
        if is_pdf:
            with timed(db, org_id=user.org_id, operation=PDF_OVERLAY_RENDER):
                fill_result = fill_pdf_template(
                    str(abs_path(tv.blob_path)), str(abs_path(out_rel)),
                    manifest_dict, body.source_record,
                    page_regions=tv.page_regions,
                    qa_policy=m.qa_policy,
                )
        else:
            with timed(db, org_id=user.org_id, operation=SINGLE_DOCX_RENDER):
                fill_result = fill_template(str(abs_path(tv.blob_path)), str(abs_path(out_rel)), manifest_dict, body.source_record)
    except Exception as exc:
        raise error("FILL_FAILED", f"Could not generate document: {exc}", 422)

    counter = db.get(Counter, "generated_doc_display_id")
    if counter is None:
        counter = Counter(name="generated_doc_display_id", value=50000)
        db.add(counter)
    counter.value += 1
    db.flush()

    # A QA failure has to change something. This line used to read
    # `"draft" if fill_result.qa_passed else "draft"`, so a document with an
    # unresolved required field or a leftover placeholder was stored, listed and
    # downloadable exactly like a clean one -- QA was computed, recorded, and
    # then ignored.
    doc_status = "draft" if fill_result.qa_passed else "blocked"
    gen_doc = GeneratedDocument(org_id=user.org_id, project_id=project_id, draft_id=None, display_id=counter.value, language=body.language, status=doc_status)
    db.add(gen_doc)
    db.flush()
    dv = DocumentVersion(document_id=gen_doc.id, org_id=gen_doc.org_id, version_no=1, blob_path=out_rel, renderer=OOXML_FILL, change_summary="Generated via Template Manifest", status=doc_status, created_by=user.id)
    db.add(dv)
    db.flush()
    gen_doc.current_version_id = dv.id

    mg = ManifestGeneration(
        org_id=user.org_id, manifest_id=manifest_id, source_record=body.source_record,
        field_lineage=fill_result.field_lineage, condition_lineage=fill_result.condition_lineage,
        qa_passed=fill_result.qa_passed, qa_notes=fill_result.qa_notes, blob_path=out_rel, created_by=user.id,
    )
    db.add(mg)
    db.flush()
    record_qa_findings(
        db, org_id=user.org_id, findings=fill_result.qa_findings, manifest_id=manifest_id,
        generation_id=mg.id, document_version_id=dv.id,
    )
    log_audit(db, user, "Generated document from manifest", "generated_document", gen_doc.id, project_id, "info" if fill_result.qa_passed else "warning")
    db.commit()

    filename = f"{project.name}_{project.display_id}_{gen_doc.display_id}_{body.language}.docx"
    return {
        "document_id": gen_doc.id, "document_version_id": dv.id, "filename": filename,
        "qa_passed": fill_result.qa_passed, "qa_notes": fill_result.qa_notes,
        "field_lineage": fill_result.field_lineage, "condition_lineage": fill_result.condition_lineage,
    }


@router.get("/manifest-generations/{generation_id}")
def get_generation(generation_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    g = db.get(ManifestGeneration, generation_id)
    if not g or g.org_id != user.org_id:
        raise error("GENERATION_NOT_FOUND", "Generation not found", 404)
    return {
        "id": g.id, "manifest_id": g.manifest_id, "source_record": g.source_record,
        "field_lineage": g.field_lineage, "condition_lineage": g.condition_lineage,
        "qa_passed": g.qa_passed, "qa_notes": g.qa_notes, "created_at": g.created_at,
    }


# ---------------------------------------------------------------- bulk onboarding (research doc §9)
@router.post("/projects/{project_id}/templates:bulk-onboard", status_code=201)
def bulk_onboard(project_id: str, files: list[UploadFile] = File(...), auto_compile: bool = Form(True), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = db.get(Project, project_id)
    if not project or project.org_id != user.org_id:
        raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    if len(files) < 1:
        raise error("NO_FILES", "Upload at least one template file", 400)

    # §16 residency, resolved once for the whole bulk run. Auto-compile reads
    # every representative template with a model, so this path needs the tenant's
    # policy exactly as the single-template compile does.
    # Attributed to the project rather than to any one template: a bulk run
    # compiles one representative per family, so the cost belongs to the
    # onboarding, not to whichever file happened to represent its family.
    policy = llm_policy_for(db, user.org_id, project_id=project_id, user_id=user.id,
                            subject_type="project", subject_id=project_id)
    uploaded = []
    for file in files:
        if not (file.filename or "").lower().endswith((".docx", ".dotx")):
            continue
        rel_path, _size = save_upload(file, f"templates/{project_id}")
        tf = TemplateFile(org_id=user.org_id, project_id=project_id, name=file.filename, status="uploaded", created_by=user.id)
        db.add(tf)
        db.flush()
        tv = TemplateVersion(template_file_id=tf.id, org_id=tf.org_id, version_no=1, blob_path=rel_path, created_by=user.id)
        db.add(tv)
        db.flush()
        # Bulk onboarding used to skip parsing entirely, leaving every template in
        # the estate with zero sections and nothing for the mapping wizard to show.
        parse_template_version(db, tf, tv)
        db.flush()
        uploaded.append({"template_file_id": tf.id, "template_version_id": tv.id,
                         "name": tf.name, "path": str(abs_path(rel_path))})

    if not uploaded:
        raise error("NO_VALID_FILES", "None of the uploaded files were .docx/.dotx", 422)

    # §18's family match lookup, p95 < 400 ms -- and §18 also calls family
    # reuse "the primary cost lever", so how long the lookup takes is worth
    # knowing before it is scaled to thousands of templates.
    with timed(db, org_id=user.org_id, operation=FAMILY_MATCH_LOOKUP, unit_count=max(1, len(uploaded))):
        clusters = cluster_templates(uploaded)

    report = []
    for c in clusters:
        cluster_row = TemplateCluster(org_id=user.org_id, label=c.label, representative_template_file_id=c.representative_template_file_id)
        db.add(cluster_row)
        db.flush()

        # §11's scaling unit outlives the run that discovered it. A cluster row
        # records who arrived together; the family keeps the fingerprint, which
        # is what lets a template uploaded next month match this group instead of
        # paying the no-family onboarding cost §18 puts at ~15-40 model calls.
        family_row = TemplateFamily(
            org_id=user.org_id, name=c.label,
            fingerprint=c.fingerprint.as_dict() if c.fingerprint else {},
            representative_template_version_id=c.representative_template_version_id,
        )
        db.add(family_row)
        db.flush()
        for member in c.members:
            db.add(TemplateClusterMember(
                cluster_id=cluster_row.id, org_id=cluster_row.org_id, template_file_id=member.template_file_id, template_name=member.name,
                similarity_score=member.similarity_to_representative, is_representative=member.is_representative,
            ))

        manifest_out = None
        if auto_compile:
            rep_tf = db.get(TemplateFile, c.representative_template_file_id)
            rep_tv = db.get(TemplateVersion, rep_tf.current_version_id)
            try:
                # The same path a single compile takes. This used to call the
                # rule compiler directly, which meant a template onboarded
                # through clustering was read by a different compiler than the
                # same template onboarded on its own -- and the two disagreed
                # about what a field was on any template without colour.
                # Timed, like the single-template path above. §18 puts a family
                # onboarding at ~15-40 model calls, so this is the most
                # expensive compile in the product -- and it was the one
                # `slo_report` could not see, which made the reported duration a
                # measurement of the cheap path only.
                with timed(db, org_id=user.org_id, operation=AGENTIC_COMPILE):
                    outcome = compile_agentic_template(
                        str(abs_path(rep_tv.blob_path)), llm_policy=policy,
                    )
                compiled = outcome.manifest
                manifest = TemplateManifest(
                    org_id=user.org_id, template_file_id=rep_tf.id, template_version_id=rep_tv.id, version_no=1,
                    status="draft" if outcome.ok else "failed",
                    fields=compiled.fields, conditions=compiled.conditions, blocks=compiled.blocks,
                    delete_always=compiled.delete_always, compiled_by=compiled.compiled_by, confidence=compiled.confidence,
                    prescan_summary=compiled.prescan_summary,
                    compile_transcript=outcome.transcript_dicts(), created_by=user.id,
                )
                manifest.template_family_id = family_row.id
                db.add(manifest)
                db.flush()
                cluster_row.manifest_id = manifest.id
                manifest_out = _manifest_out(manifest)
            except Exception as exc:
                manifest_out = {"error": str(exc)}

        report.append({
            "cluster_id": cluster_row.id, "family_id": family_row.id,
            "label": c.label, "member_count": len(c.members),
            "representative_template_file_id": c.representative_template_file_id,
            "members": [{"template_file_id": mem.template_file_id, "name": mem.name, "similarity": mem.similarity_to_representative, "is_representative": mem.is_representative} for mem in c.members],
            "manifest": manifest_out,
        })

    log_audit(db, user, "Bulk-onboarded templates", "template_cluster", None, project_id, "info", f"{len(uploaded)} files -> {len(clusters)} clusters")
    db.commit()
    return {"uploaded_count": len(uploaded), "cluster_count": len(clusters), "clusters": report}


@router.get("/projects/{project_id}/template-clusters")
def list_clusters(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_project(db, project_id, user)
    # Clusters carry no project_id of their own, so scope them by the project
    # their representative template belongs to -- otherwise this returns every
    # cluster in the org regardless of which project was asked for.
    project_template_ids = {
        tf.id for tf in db.scalars(select(TemplateFile).where(TemplateFile.project_id == project_id)).all()
    }
    rows = [
        c for c in db.scalars(select(TemplateCluster).where(TemplateCluster.org_id == user.org_id)).all()
        if c.representative_template_file_id in project_template_ids
    ]
    out = []
    for c in rows:
        members = db.scalars(select(TemplateClusterMember).where(TemplateClusterMember.cluster_id == c.id)).all()
        out.append({
            "id": c.id, "label": c.label, "manifest_id": c.manifest_id,
            "representative_template_file_id": c.representative_template_file_id,
            "members": [{"template_file_id": m.template_file_id, "name": m.template_name, "similarity": m.similarity_score, "is_representative": m.is_representative} for m in members],
        })
    return {"items": out}
