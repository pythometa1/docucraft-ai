from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# Re-exported on purpose: half the codebase writes `from app.models import now`.
from app.db import Base, now, uid  # noqa: F401
from app.retrieval.embeddings import VectorColumn
from app.retrieval.vector import DEFAULT_DIMENSIONS


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String)
    region_default: Mapped[str] = mapped_column(String, default="Europe")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, ForeignKey("organizations.id"))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str] = mapped_column(String)
    job_title: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    role_key: Mapped[str] = mapped_column(String, default="org_admin")
    function: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Counter(Base):
    """Simple per-org display-id sequences (project ids, generated-doc ids)."""

    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer)


class LookupValue(Base):
    __tablename__ = "lookup_values"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # function | document_type | region | language
    value: Mapped[str] = mapped_column(String)
    parent_value: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    display_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(String)
    function: Mapped[str] = mapped_column(String)
    document_type: Mapped[str] = mapped_column(String)
    language: Mapped[str] = mapped_column(String, default="English")
    # How this project's dates and amounts are written. `region` cannot answer
    # it: the vocabulary is continental -- "Asia Pacific" spans Australia, Japan
    # and India -- so it maps to no single locale and `REGION_LOCALES` is empty
    # for exactly that reason.
    #
    # Without this every document ever generated was formatted `en_US`, whatever
    # the project. An Australian offer letter rendered "May 9, 2024" for a date
    # its own source wrote 09/05/2024. The date was right; the way it was written
    # was American, and nothing recorded that a choice had been made.
    #
    # Nullable, because guessing is what produced "$82.000,00" from a project
    # tagged "Europe". Absent, the default still applies -- but the resolved
    # locale and where it came from are now recorded on every document.
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    generation_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TemplateFile(Base):
    __tablename__ = "template_files"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String)
    # §16: a template flagged legally binding needs four-eyes approval on lock.
    # An offer letter and an internal memo do not carry the same risk, so the
    # stricter path is opt-in per template rather than imposed on everything --
    # a separation rule applied to everything is one teams learn to route around.
    legally_binding: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    template_file_id: Mapped[str] = mapped_column(String, ForeignKey("template_files.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    blob_path: Mapped[str] = mapped_column(String)
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    template_kind: Mapped[str] = mapped_column(String, default="heading")
    jinja_vars: Mapped[list] = mapped_column(JSON, default=list)
    # §12's "stored dynamic regions / anchors / bounding boxes" for an immutable
    # PDF template. Persisted at onboarding rather than derived at render time,
    # deliberately: coordinates a renderer recomputes are coordinates nobody
    # approved, and the point of the overlay path is that it writes only into
    # regions a human signed off.
    page_regions: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TemplateSection(Base):
    __tablename__ = "template_sections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    template_version_id: Mapped[str] = mapped_column(String, ForeignKey("template_versions.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    section_path: Mapped[str] = mapped_column(String)
    anchor: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String)
    example_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fillable: Mapped[bool] = mapped_column(Boolean, default=True)


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String)
    file_type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    ingest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SourceVersion(Base):
    __tablename__ = "source_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_file_id: Mapped[str] = mapped_column(String, ForeignKey("source_files.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    blob_path: Mapped[str] = mapped_column(String)
    extraction_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SourceChunk(Base):
    __tablename__ = "source_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String)
    source_version_id: Mapped[str] = mapped_column(String, ForeignKey("source_versions.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    element_type: Mapped[str] = mapped_column(String, default="paragraph")
    heading_path: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    content_sha256: Mapped[str] = mapped_column(String)


class DraftDocument(Base):
    __tablename__ = "draft_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Mapping(Base):
    __tablename__ = "mappings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    draft_id: Mapped[str] = mapped_column(String, ForeignKey("draft_documents.id"))
    template_version_id: Mapped[str] = mapped_column(String)
    section_ids: Mapped[list] = mapped_column(JSON, default=list)
    ui_action: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    source_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    processing_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str] = mapped_column(String)
    draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    template_library_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    model_profile: Mapped[str] = mapped_column(String, default="claude-sonnet-4.6")
    languages: Mapped[list] = mapped_column(JSON, default=lambda: ["en"])
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SectionOutput(Base):
    __tablename__ = "section_outputs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("generation_jobs.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    mapping_id: Mapped[str | None] = mapped_column(String, nullable=True)
    token_id: Mapped[str | None] = mapped_column(String, nullable=True)
    section_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_kind: Mapped[str] = mapped_column(String, default="section")
    language: Mapped[str] = mapped_column(String, default="en")
    status: Mapped[str] = mapped_column(String, default="pending")
    blocks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    grounding_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str] = mapped_column(String)
    draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    display_id: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String, default="en")
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    #: Where a person says this document is in *their* process, as opposed to
    #: what the engine and the reviewers say about it.
    #:
    #: Only `work_in_progress`, `completed` and `cancelled` are ever stored --
    #: they are the three a person can assert. `approved` and `blocked` are facts
    #: about the document that this column must not be able to claim: `approved`
    #: is a signature (the approve endpoint writes `status`, `approved_by`,
    #: `approved_at` and an audit row), and `blocked` is the fill engine's QA
    #: verdict. Both are layered over this on read by
    #: `generation.workflow_status.effective`, so the two columns cannot
    #: disagree: each has exactly one writer, and neither derives from the other.
    workflow_status: Mapped[str] = mapped_column(
        String, default="work_in_progress", server_default="work_in_progress")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("generated_documents.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    blob_path: Mapped[str | None] = mapped_column(String, nullable=True)
    html_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which renderer produced `blob_path`, as "<name>/<version>". Two jobs, both
    # required: a regression can be attributed to a specific renderer release
    # rather than debated, and the editor's save path can tell a document whose
    # layout came from a Word template apart from one built out of HTML. The
    # second is what stops Save from rebuilding a filled template from the
    # editor's HTML and destroying the letterhead, tables and headers with it.
    renderer: Mapped[str | None] = mapped_column(String, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    #: draft | pending_review | changes_requested | blocked | approved.
    #:
    #: `pending_review` means the *engine* has an outstanding question;
    #: `changes_requested` means a *person* does. They are two axes rather than
    #: one -- a document can fail QA and carry three open comment threads at the
    #: same time -- so the review's own lifecycle lives on `document_reviews`
    #: and only its effect on the document is mirrored here.
    status: Mapped[str] = mapped_column(String, default="draft")
    #: Why the version is where it is, in a sentence a person wrote. Without it
    #: a rejection is "rejected, no idea why", which is the failure a reject
    #: button produces when nothing makes the reason mandatory.
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Signed off for good. Deliberately *not* a `status` value: `metrics`
    #: computes the escaped-error rate over versions whose status is "approved",
    #: so moving finalised documents to a different status would quietly drop
    #: the most-signed-off ones out of that denominator. Final is a property of
    #: an approved document, not a phase after it.
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TemplateLibrary(Base):
    __tablename__ = "template_library"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TemplateLibraryVersion(Base):
    __tablename__ = "template_library_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    template_library_id: Mapped[str] = mapped_column(String, ForeignKey("template_library.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_html: Mapped[str] = mapped_column(Text)
    source_fields: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TemplateBlueprint(Base):
    """A template being written, as opposed to one being read.

    The product could compile a legacy `.docx` into a manifest and it could
    author coloured tokens into HTML, and neither was a template somebody could
    *edit*: the first offered only a JSON reading to argue with, the second
    produced markup no manifest can be compiled from. This is the row that makes
    "put a legacy template in, get an editable one back" a thing the schema can
    express.

    The emitted `.docx` is a `TemplateFile` / `TemplateVersion` like any other.
    That is deliberate -- `compile-manifest`, `manifest_preview`, `fill_template`
    and the Studio all address templates that way, and a blueprint that
    published to some new kind of template row would need every one of them
    changed to see it.
    """

    __tablename__ = "template_blueprints"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    #: Where it came from: "legacy" (a compiled upload), "inherited", "kit",
    #: "library" or "blank". Not cosmetic -- a legacy blueprint publishes by
    #: mutating its source file in place, and a from-scratch one builds a fresh
    #: package, so this decides which of the two is correct.
    kind: Mapped[str] = mapped_column(String, default="blank")
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|published|archived
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The uploaded template this was read from, kept for the life of the
    #: blueprint. It is what "revert it back" means: the original bytes are
    #: still there however far the editing has gone.
    source_template_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The emitted template, once there is one.
    template_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TemplateBlueprintVersion(Base):
    """One saved state of a blueprint. Never mutated, like every other version
    row in this schema -- which is what lets `revert-to` fork from any earlier
    one instead of apologising."""

    __tablename__ = "template_blueprint_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    blueprint_id: Mapped[str] = mapped_column(String, ForeignKey("template_blueprints.id"))
    # Derived from the parent on write, never supplied by a caller, which would
    # make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    #: The document: paragraphs, segments and tables, in the shape
    #: `app.templates.blueprint` defines and `emit_docx` writes.
    body: Mapped[dict] = mapped_column(JSON, default=dict)
    #: The §6 semantic objects over that body, in `ManifestEnvelope` shape.
    objects: Mapped[list] = mapped_column(JSON, default=list)
    #: What the lift and the linter had to say about this state, so a reviewer
    #: reads findings recorded against the version they are looking at rather
    #: than recomputed against a later one.
    findings: Mapped[list] = mapped_column(JSON, default=list)
    #: How this version came about: the compile it was lifted from, the manifest
    #: it inherited, the operations applied and who or what proposed them.
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    emitted_template_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DocumentReview(Base):
    """A person's objection to a finished document, and the conversation about it.

    Distinct from `ReviewTask`, which is the *engine* saying it could not decide
    something. Both belong in one queue and neither fits in the other's row: a
    task is a question about a manifest unit with a single scalar answer, and
    this is a thread about a letter somebody read and did not accept.

    It hangs off a **version**, not a document. Approving v1 says nothing about
    v2, and `apply_version_text` mints a new version precisely so that a
    signature cannot silently move to text nobody signed.
    """

    __tablename__ = "document_reviews"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("generated_documents.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(String, ForeignKey("document_versions.id"), index=True)
    #: open | approved | rejected | withdrawn
    state: Mapped[str] = mapped_column(String, default="open", index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String)
    assigned_to: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    #: Copied from the version at open time rather than read at resolve time, so
    #: that editing a document cannot launder its authorship and let the author
    #: close their own review.
    authored_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ReviewComment(Base):
    """One remark on a review, optionally pinned to a run of the document.

    `(paragraph_index, span_index)` is the coordinate the compiler, the fill
    engine, the QA gates and the text editor all already speak, so a comment, a
    manifest slot and a QA finding point at the same run with no translation to
    get wrong.

    `quoted_text` records what that run said when the remark was written. A
    comment reading "this figure is wrong" is worthless once somebody has
    changed the figure and nothing remembers what it was.
    """

    __tablename__ = "review_comments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    review_id: Mapped[str] = mapped_column(String, ForeignKey("document_reviews.id"), index=True)
    #: One level of nesting. A reply to a reply becomes a sibling, because a
    #: tree nobody can render is a tree nobody reads.
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_id: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    paragraph_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quoted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LlmCall(Base):
    """One model call: what it cost, and the rate that was in force when it ran.

    Every provider already returns `input_tokens` and `output_tokens` -- and a
    comment in `llm/provider.py` says Gemini folds its thinking tokens into the
    output count "so the cost figures on the analytics page compare like with
    like". There was no such figure, because all thirteen call sites discarded
    the numbers and `generation_jobs.token_usage`, the column the analytics page
    read, was never written by anything. This is where they land instead.

    No customer content: ids, a model name, a capability label and integers. Like
    `qa_failure_logs` it therefore survives a per-document deletion -- destroying
    it would quietly reduce a bill every time somebody tidied up a document --
    and it leaves on offboarding with the rest of the org. That is also why it
    carries no foreign keys.
    """

    __tablename__ = "llm_calls"
    #: An integer key, not a UUID: there will be many of these relative to every
    #: other table, and the index is a quarter the size.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The `get_llm_provider(capability=...)` string, verbatim -- "Chat",
    #: "Editing a template". Free attribution: every call site already passes it.
    capability: Mapped[str] = mapped_column(String)
    #: compile | authoring | generate | edit | chat | other. A coarse bucket the
    #: analytics groups on so it never has to match on prose.
    operation: Mapped[str] = mapped_column(String, index=True)
    #: Which model slot answered -- compile or generate. `RoutedProvider` picks
    #: its vendor from this, so it is part of what the cost depends on.
    purpose: Mapped[str] = mapped_column(String, default="generate")
    subject_type: Mapped[str | None] = mapped_column(String, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    #: What actually answered, from the provider's own response -- never the
    #: configured provider, because compile and generate can be different vendors
    #: in one deployment.
    model: Mapped[str] = mapped_column(String, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: ok | refused | truncated | error
    outcome: Mapped[str] = mapped_column(String, default="ok")
    #: Integer micro-dollars per thousand tokens, and the cost they produced.
    #: Integers rather than Numeric because Numeric round-trips through float on
    #: SQLite, which is what the test suite runs on, and a money column that does
    #: not sum exactly is worse than none.
    input_rate_micro_usd_per_ktok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_rate_micro_usd_per_ktok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: NULL when the model has no rate -- never 0. An unpriced call reports
    #: "cost unavailable", because a call that looks free is worse than one that
    #: admits it does not know.
    cost_micro_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: "default:<date>" or "org:<row id>" -- which rate table produced the cost,
    #: so a figure can be explained a year later.
    rate_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class OrgModelRate(Base):
    """What one organisation is billed per million tokens for one model.

    Overrides the shipped defaults. A tenant on a negotiated rate, or on a model
    this build has never priced, should see their own numbers rather than ours.
    """

    __tablename__ = "org_model_rates"
    __table_args__ = (UniqueConstraint("org_id", "model", name="uq_org_model_rate"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    model: Mapped[str] = mapped_column(String)
    input_micro_usd_per_ktok: Mapped[int] = mapped_column(Integer)
    output_micro_usd_per_ktok: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    event: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String, default="info")
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(String, ForeignKey("conversations.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============================================================================
# Template Compiler + Universal Fill Engine (docs/TEMPLATE_COMPILER_RESEARCH.md)
# ============================================================================

class TemplateManifest(Base):
    """One compiled manifest per template version -- the machine-executable
    translation of a legacy template's colour-coded blue/red instructions
    (research doc §4.1). Versioned and diffable; re-compiling a changed
    template produces a new row, never mutates an approved one."""

    __tablename__ = "template_manifests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    template_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    template_version_id: Mapped[str] = mapped_column(String)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    # `failed` is a real, persisted outcome, not an error state to be cleaned up.
    # A compile that could not read the template used to return the rule-based
    # manifest instead, which carried fields and a confidence and was
    # indistinguishable from a compile that worked. Recording the attempt keeps
    # the reason visible to a reviewer while `validate_manifest` refuses to
    # approve it, so nothing can ever generate from it.
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|in_review|approved|deprecated|failed
    fields: Mapped[list] = mapped_column(JSON, default=list)
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    delete_always: Mapped[list] = mapped_column(JSON, default=list)
    # The lossless §6 envelope: every semantic object, whatever its type. The
    # three lists above are a projection of this one -- FIELD and CALCULATION
    # into `fields`, CONDITION into `conditions`, SECTION and TABLE_ROW into
    # `blocks` -- and they stay because the fill engine, the validator, the
    # source resolver and the data-template builder all read them. What they
    # cannot express is the other half of §6: STATIC, NARRATIVE, HEADER, FOOTER
    # and SIGNATURE, which had nowhere to go at all, so inheriting an approved
    # manifest that carried a signature block was refused rather than performed.
    #
    # Empty on a row written before the column existed, and on any writer that
    # has not opted in. `envelope_from_row` reads this first and falls back to
    # the three lists when it is empty, so an old row and a new row produce the
    # same envelope.
    objects: Mapped[list] = mapped_column(JSON, default=list)
    # What the compiler wants a human to look at before this is approved, from
    # `manifest_compiler.WARNING_CATALOG`. The compiler's own contract says any
    # warning blocks auto-approval and needs an explicit disposition -- which it
    # could not do while these were computed and then dropped on the floor.
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    # warning code -> {"resolved_by", "resolved_at", "note"}. A warning leaves
    # the way of approval by being answered, not by being ignored.
    warning_dispositions: Mapped[dict] = mapped_column(JSON, default=dict)

    # One entry per stage and per review round of the agentic compile: how many
    # chunks the template was read in, what the document objected to each round,
    # what the reviewer changed, and why the loop stopped. This is the only
    # record of *how* a manifest came to say what it says, and it is what a
    # reviewer reads when the answer is `failed`.
    compile_transcript: Mapped[list] = mapped_column(JSON, default=list)

    # ---- §6 manifest envelope ----
    # What makes a generated document reproducible from the record alone: a
    # locked manifest pins the exact (template_hash, manifest_hash) pair, so any
    # letter can be rebuilt from that pair plus the source row. Without them the
    # audit trail is decorative -- it can say which manifest id ran, but not
    # that the manifest or the template still holds the bytes it ran against.
    template_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # The source schema this manifest was compiled against. Drift blocks
    # generation rather than producing silent nulls when a column is renamed
    # upstream -- the second most likely failure in the §19 register.
    source_schema_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_schema_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # The expression dialect these conditions and calculations are written in.
    # Recorded per manifest so a language change cannot silently reinterpret an
    # already-approved rule.
    expression_lang: Mapped[str] = mapped_column(String, default="documind-expr/1.0")
    # {"format": "DOCX", "engine": "ooxml_patch", "version": "..."} -- which
    # renderer this manifest expects, so a renderer upgrade is a visible
    # decision rather than a change in output nobody attributed.
    renderer_contract: Mapped[dict] = mapped_column(JSON, default=dict)
    # The computed closure of every field the manifest references, not a
    # hand-maintained list. Recomputed on save; used to validate a source file
    # before a batch rather than one row at a time.
    required_source_fields: Mapped[list] = mapped_column(JSON, default=list)
    # {"blocking": [...], "warning": [...]} -- which QA checks stop a document
    # and which only annotate it, declared rather than hard-coded.
    qa_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # The manifest version this one replaces. Lock semantics: editing a locked
    # manifest produces n+1 in DRAFT and the previous becomes SUPERSEDED, never
    # deleted, so a document generated last year still names a manifest that
    # exists.
    supersedes: Mapped[str | None] = mapped_column(String, nullable=True)
    # The model and version that compiled this, when a model was involved. A
    # provider changing its model must never silently alter an approved
    # manifest, which it can only be shown not to have done if this is recorded.
    compiled_model: Mapped[str | None] = mapped_column(String, nullable=True)
    # §6's envelope carries `template_family_id`; until this column existed the
    # envelope could hold one and the row could not, so family membership
    # survived exactly as long as the process that computed it. §11 needs it
    # durable: it is how an inherited manifest names the family whose approved
    # knowledge it was built from.
    template_family_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    protected_note: Mapped[str] = mapped_column(Text, default="all other elements incl. hyperlinks, headers, footers, tables")
    compiled_by: Mapped[str] = mapped_column(String, default="rule_based")  # "rule_based" | "llm:<model>"
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    prescan_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Every approval recorded against this manifest, not just the last one:
    # [{"user_id", "at"}]. Four-eyes needs to know whether a *different* person
    # has already signed off, which a single approved_by cannot answer.
    approvals: Mapped[list] = mapped_column(JSON, default=list)


class ManifestGeneration(Base):
    """Audit record for one manifest-driven fill (research doc §4.2 step 7) --
    field -> source value + confidence, condition -> verdict."""

    __tablename__ = "manifest_generations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    manifest_id: Mapped[str] = mapped_column(String, ForeignKey("template_manifests.id"))
    # Which upload this row came out of, so §16's deletion cascade can find it.
    # Without the link a source file could be destroyed on its retention
    # schedule while `source_record` -- a verbatim copy of one of its rows,
    # salary and identifiers included -- survived here indefinitely, which is
    # exactly the "derived from deleted data" case the section names.
    source_version_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # §17's lineage asks for "source file/version + row/record key", not for a
    # copy of every value. The key is what survives scrubbing, so a document
    # stays traceable to the row that produced it after the row itself is gone.
    source_record_key: Mapped[str | None] = mapped_column(String, nullable=True)
    source_record: Mapped[dict] = mapped_column(JSON, default=dict)
    field_lineage: Mapped[list] = mapped_column(JSON, default=list)
    condition_lineage: Mapped[list] = mapped_column(JSON, default=list)
    #: The version this generation produced. Added because the only link was a
    #: shared `blob_path` string, and that link is wrong three ways: it breaks
    #: the moment a document is edited (`apply_version_text` writes a different
    #: path), it is not unique across previews of the same row, and
    #: `retention.delete_generated_document` deletes by it -- so two documents
    #: sharing a path means deleting one destroys the other's lineage.
    document_version_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    qa_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    qa_notes: Mapped[list] = mapped_column(JSON, default=list)
    blob_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class FieldDictionary(Base):
    """Canonical field vocabulary for the whole org -- the lever that makes an
    estate of thousands of templates tractable.

    Without it, every template needs its own binding session, because the same
    concept is spelled differently in every file ("Colleague First Name",
    "Emp FName", "Vorname"). With it, a column mapped once is recognised
    everywhere, and each reviewer correction is written back as a new alias, so
    the matcher measurably improves as the estate is processed.
    """

    __tablename__ = "field_dictionary"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    canonical_id: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, default="string")  # string|currency|date|number
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    locale_hints: Mapped[dict] = mapped_column(JSON, default=dict)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ManifestBinding(Base):
    """How one manifest's fields map onto one source file's columns.

    Kept separate from the manifest because a manifest describes the *template*
    and is reusable across every data file, while a binding describes one
    template x source pairing. `value_map` handles the gap normalisation can't
    close on its own -- an Excel that says "FT" where the template says
    "Full time".
    """

    __tablename__ = "manifest_bindings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    manifest_id: Mapped[str] = mapped_column(String, ForeignKey("template_manifests.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(String, index=True)
    field_bindings: Mapped[dict] = mapped_column(JSON, default=dict)  # {field_id: column_name}
    value_map: Mapped[dict] = mapped_column(JSON, default=dict)  # {field_id: {source_value: manifest_value}}
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ReviewTask(Base):
    """A unit the engine could not resolve on its own.

    Some values genuinely need judgement -- a calculation that must be signed
    off, a condition whose inputs are ambiguous, a narrative that came back
    poorly grounded. Rather than guessing (a wrong number in a tox report is
    not an acceptable failure mode), the document parks here with enough
    context for a human to decide, and generation resumes from the parked
    graph without recomputing what was already settled.
    """

    __tablename__ = "review_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    generation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # calculation|condition|binding|narrative
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    proposed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|resolved|dismissed
    assigned_to: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Which document this question is about. The task always had a
    #: `generation_id`, but reaching the document from it meant joining on a
    #: string path -- and a task whose generation record was later swept lost
    #: its document entirely.
    document_version_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    #: Who raised it. Null for the machine, which is every row written before a
    #: person could open one.
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TemplateCluster(Base):
    """A family of structurally-similar templates discovered during bulk
    onboarding (research doc §9) -- one manifest is compiled for the
    representative and reused across the whole family."""

    __tablename__ = "template_clusters"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    representative_template_file_id: Mapped[str] = mapped_column(String)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TemplateClusterMember(Base):
    __tablename__ = "template_cluster_members"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    cluster_id: Mapped[str] = mapped_column(String, ForeignKey("template_clusters.id"))
    # §16: every row that can hold customer data carries the tenant, so a
    # filter is possible at the query layer and row-level security has a
    # column to key on. Derived from the parent on write -- never supplied by
    # a caller, which would make it forgeable.
    org_id: Mapped[str] = mapped_column(String, index=True)
    template_file_id: Mapped[str] = mapped_column(String)
    template_name: Mapped[str] = mapped_column(String)
    similarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_representative: Mapped[bool] = mapped_column(Boolean, default=False)


class TemplateFamily(Base):
    """The scaling unit of §11: a structural fingerprint plus the one template
    version whose approved manifest the rest of the family inherits from.

    `TemplateCluster` above records the *outcome* of one bulk-onboarding run --
    which files arrived together and which of them was most typical. That is a
    snapshot, and it cannot answer the question §11 actually asks: a template
    uploaded six months later, on its own, into a different project -- is it a
    revision of something already approved, or genuinely new? Answering that
    needs the fingerprint kept, not the membership list, which is why this is a
    second table rather than two more columns on the first.

    §18 makes the stakes commercial rather than tidy. Onboarding a template with
    no family match costs ~15-40 model calls; with a strong match, ~2-6. The
    fingerprint stored here is what decides which of those two numbers a
    customer pays, for every template they ever upload.
    """

    __tablename__ = "template_families"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    # §16: the tenant filter that keeps family matching from becoming the
    # cross-tenant leak in §19's register. A family is matched by structure
    # alone, and structure is not confidential -- so without this column one
    # customer's approved mappings would inherit into another's template.
    org_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    # `StructuralFingerprint.as_dict()`: MERGEFIELD codes, paragraph band, table
    # shape, colour counts, bracket tokens. Stored rather than recomputed
    # because the representative's binary may be archived, and because a match
    # must be reproducible from what was recorded at the time.
    fingerprint: Mapped[dict] = mapped_column(JSON, default=dict)
    # NOT NULL: a family with no representative has no approved manifest to
    # inherit and no fingerprint anybody can trace back to a real document. It
    # would match templates and then have nothing to give them.
    representative_template_version_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============================================================================
# §22 Success Metrics & Instrumentation -- the three logs that cannot be
# reconstructed after the fact
# ============================================================================

class SuggestionLog(Base):
    """One mapping suggestion, the evidence behind it, and what a reviewer did.

    §22 is unusually blunt about this table: "Log every mapping suggestion with
    its score, its evidence and the reviewer's decision. This is the only
    dataset that can ever calibrate the weights in §13, and it cannot be
    reconstructed later." The weights in `app/compiler/confidence.py` are
    declared starting values -- `WEIGHTS_CALIBRATED` is False -- and the only
    way they ever stop being guesses is a corpus of (score, evidence, what the
    human actually did) triples. A suggestion that was shown and decided
    without being written down here is a training row destroyed.

    One row per (manifest, source version, object). The pending row is rewritten
    while it is still pending -- re-opening the binding screen restates the same
    suggestion, it does not create a second one -- and frozen the moment a
    decision lands, because the decision is the datum.

    `final_column` is what makes an `edited` row usable: knowing that a reviewer
    rejected `start_date -> Commencement` teaches far less than knowing they
    bound it to `Start Dt` instead.
    """

    __tablename__ = "suggestion_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    # §16: every row that can hold customer data carries the tenant. A column
    # name out of a customer's spreadsheet is customer data.
    org_id: Mapped[str] = mapped_column(String, index=True)
    manifest_id: Mapped[str] = mapped_column(String, index=True)
    source_version_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    object_id: Mapped[str] = mapped_column(String)
    # Null means nothing was proposed for this object. Those rows are logged
    # too: dropping them would quietly remove the hardest objects from the
    # denominator of the auto-map rate, which §22 already calls the vanity metric.
    suggested_column: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which resolver tier proposed it (exact_slug, dictionary, fuzzy, llm...).
    # Calibration needs to know which tier the evidence came from, not just how
    # much of it there was.
    method: Mapped[str] = mapped_column(String, default="unmatched")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    band: Mapped[str] = mapped_column(String)
    vetoes: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # Which weight regime produced `score`. A fit over rows scored under two
    # different weight tables is a fit over nothing, so the regime is stamped on
    # every row rather than inferred from the row's date.
    weights_calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_decision: Mapped[str] = mapped_column(String, default="pending", index=True)
    final_column: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class QaFailureLog(Base):
    """One QA check that fired, on one object, and whether a human overrode it.

    §22's second instrumentation bullet. The QA notes already existed on the
    generation record, but as prose in a list: "which check fired" could only be
    recovered by matching strings, and "did anybody wave it through" was not
    recorded anywhere at all.

    `phase` is what makes the escaped error rate computable. A failure caught
    before approval is the system working; the same failure found in a document
    that was already approved and issued is an escaped error, which §22 calls
    the number that decides whether the product survives contact with legal.
    """

    __tablename__ = "qa_failure_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    generation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_version_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # A name from app/qa/policy.py's REGISTRY where the finding came from a
    # registered check, so the log speaks the manifest's own vocabulary.
    check_name: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="blocking")
    object_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    phase: Mapped[str] = mapped_column(String, default="pre_approval", index=True)
    # A warning-severity finding on a document somebody approved anyway is an
    # override: the machine flagged it, a person signed under it, and their name
    # belongs next to the finding.
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    overridden_by: Mapped[str | None] = mapped_column(String, nullable=True)
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OperationTiming(Base):
    """How long one instrumented operation actually took.

    §18 opens with a warning that every figure in its SLO table "is a design
    target or an arithmetic estimate. None of it is a benchmark result on a real
    customer template estate." This table is the only thing that can ever move a
    row of that table from quoted to measured, so the metrics endpoint reports
    target and measurement side by side and never lets one impersonate the other.

    `unit_count` carries the documents in a batch, so a 250-row run can be
    projected onto §18's "batch of 1,000 documents" target instead of being
    compared against it as though it were one.
    """

    __tablename__ = "operation_timings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    operation: Mapped[str] = mapped_column(String, index=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    unit_count: Mapped[int] = mapped_column(Integer, default=1)
    # A run that raised is timed too, and then excluded from the percentiles:
    # how long a crash took is not how long the operation takes.
    outcome: Mapped[str] = mapped_column(String, default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


# ============================================================================
# §16 Security, Tenancy & Compliance -- retention, residency and the record of
# what was destroyed
# ============================================================================

class OrgDataPolicy(Base):
    """One organisation's answers to the questions §16 refuses to answer for them.

    Four settings, and each one exists because the alternative is us deciding
    something on a customer's behalf that is not ours to decide:

    `source_retention_days` schedules the deletion of source uploads -- "the
    most sensitive artefact and the least useful to retain". A platform default
    applies until a tenant states its own, because no policy must not mean
    keeping a payroll extract indefinitely.

    `generated_document_retention_days` is NULL until the customer states it.
    §16 is explicit that generated documents are retained "according to the
    customer's records policy, not a default of your choosing", and an
    employment contract deleted on a schedule we invented is a records incident
    we caused. NULL means the sweep reports and deletes nothing.

    `residency` pins this tenant's data to an in-region model deployment. §16
    lists EU, UK and India by name and asks for residency "recorded per
    organisation" -- recorded, so it can be enforced at the boundary rather
    than remembered by whoever configures the provider.

    `zero_retention_required` is the other half of the same row: a tenant that
    has been promised no-training/no-retention gets a refusal, not a prompt,
    when the configured deployment cannot evidence it.
    """

    __tablename__ = "org_data_policies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    source_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    generated_document_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    residency: Mapped[str] = mapped_column(String, default="GLOBAL")  # GLOBAL | EU | UK | IN
    zero_retention_required: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DeletionCertificate(Base):
    """Evidence that a deletion happened, in a form the customer can check.

    §16 asks offboarding to produce "a verifiable deletion certificate". A count
    on its own is not verifiable -- anyone can write 412 into a row -- so the
    certificate carries `manifest_sha256`, a hash over the canonical list of
    every deleted id. The customer (or an auditor holding the manifest handed
    back at the time) recomputes the hash and either it matches this row or it
    does not.

    The id list itself is deliberately NOT stored. Retaining a list of every
    identifier we just destroyed would be a smaller version of the thing we said
    we destroyed; the hash proves the manifest without keeping it.

    `org_name` is here because the row outlives the organisation. Offboarding
    deletes the tenant, so `org_id` becomes a dangling reference by design and
    the certificate would otherwise be unable to say whose data it accounts for.
    """

    __tablename__ = "deletion_certificates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    org_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # organisation | source_file | generated_documents
    scope: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str | None] = mapped_column(String, nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)  # table -> rows deleted
    blobs_deleted: Mapped[int] = mapped_column(Integer, default=0)
    derived_forgotten: Mapped[dict] = mapped_column(JSON, default=dict)  # store -> records forgotten
    manifest_sha256: Mapped[str] = mapped_column(String)
    issued_by: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_by_email: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ------------------------------------------------------- §10 semantic memory
#
# §14 gives PostgreSQL "organizations, users, templates, versions, semantic
# objects, manifests, mappings, approvals, generations, QA, audit" and pgvector
# "embeddings for semantic objects and historical mapping memory". Until these
# three tables existed, both of those lived in a process dictionary: every
# approved mapping and every embedding was erased by the next deploy, so the
# ninth offer letter from a customer got the same guesses as the first.
#
# The vector column is `app.retrieval.embeddings.VectorColumn` -- pgvector's
# `vector(1024)` on PostgreSQL, a JSON array of floats on SQLite. The HNSW index
# over it is PostgreSQL-only and therefore lives in the migration rather than
# here: SQLAlchemy would render `Index(..., postgresql_using="hnsw")` as an
# ordinary index on SQLite, which is a lie the schema does not need to tell.


class Embedding(Base):
    """One embedded item of §10's "What to embed" list, filed under its tenant.

    `org_id` leads every index on this table for one reason: §16 requires the
    tenant filter to run *before* similarity, never as a post-filter on results.
    A post-filter has already let another customer's rows influence the ranking,
    the score distribution and the `k` cut-off before it discards them, and one
    missing line in the discard step turns that into a disclosure.

    `record_id` is the caller's identity for the thing embedded (a field id, a
    source column, a mapping); it is unique only within an organisation, because
    two customers may legitimately use the same id and neither should be able to
    overwrite the other's row by choosing one.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("org_id", "record_id", name="uq_embeddings_org_record"),
        # The literal shape of the scoped read: tenant first, then document
        # type. Ordering matters -- an index led by doc_type would not serve a
        # query that filters on the tenant alone, which is the common case.
        Index("ix_embeddings_org_doc_type", "org_id", "doc_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # vector.EMBEDDABLE_KINDS
    record_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    vector: Mapped[list] = mapped_column(VectorColumn(DEFAULT_DIMENSIONS), nullable=False)
    # Which embedder produced the coordinates. Two providers occupy different
    # coordinate spaces, so a search across a mixed index returns distances that
    # look like similarities and are not; storing the name is what lets the
    # index refuse that instead of ranking on noise.
    provider: Mapped[str] = mapped_column(String, nullable=False)
    dims: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # Named `meta` in Python because `metadata` is taken by Declarative itself;
    # the column keeps the name §10 gives it.
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)


class MappingMemoryEntry(Base):
    """One (field, column, transform) triple an organisation has approved.

    Counts rather than a boolean, because §13's largest single-signal weight is
    "same mapping approved 42 times" -- and a mapping accepted repeatedly is
    materially different evidence from one accepted once. `rejection_count` is
    the other half: a memory that only remembers acceptances keeps proposing the
    column a reviewer replaced last month.

    `source_column_key` is the normalised form the uniqueness constraint keys
    on. Without it "Joining Dt" and "joining_dt" are two rows, the approval
    count splits across them, and the §13 signal becomes a function of how the
    reviewer happened to type the column name.
    """

    __tablename__ = "mapping_memory"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "field_key", "source_column_key", "transform",
            name="uq_mapping_memory_triple",
        ),
        Index("ix_mapping_memory_org_field", "org_id", "field_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String, nullable=False)
    source_column: Mapped[str] = mapped_column(String, nullable=False)
    source_column_key: Mapped[str] = mapped_column(String, nullable=False)
    transform: Mapped[str] = mapped_column(String, nullable=False)
    on_missing: Mapped[str] = mapped_column(String, nullable=False)
    field_type: Mapped[str] = mapped_column(String, nullable=False)
    # The sentence the placeholder sat in, embedded on lookup for similarity.
    # Meaning, never values -- §10 excludes salaries and identifiers from
    # anything that acquires a vector.
    context_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String, nullable=True)
    template_family_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    last_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_approved_by: Mapped[str | None] = mapped_column(String, nullable=True)


class MappingMemorySharing(Base):
    """Whether one organisation has opted into the anonymised structural pool.

    §16: "any cross-tenant learning is opt-in and limited to anonymised
    structural patterns". Opt-in is a consent decision about a customer's data,
    so it gets a row with an actor and a timestamp rather than a key in a
    settings blob -- an unattributed consent cannot be evidenced later, which is
    the whole reason for recording it.

    Absence means opted out. A pool that defaults to sharing is a pool that
    shares whatever nobody has got round to configuring yet.
    """

    __tablename__ = "mapping_memory_sharing"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now, nullable=False)


class ReviewerCorrection(Base):
    """What a reviewer changed, as an append-only log beside the counters.

    §14 lists `reviewer_corrections` as a store in its own right, next to
    `mapping_memory`, and the reason is that the counters lose the pairing: they
    record that `Joining Dt` gained an approval and `Start Dt` gained a
    rejection, but not that those two facts were the same decision. Calibrating
    §13's weights against real reviewer behaviour needs the pairing, and it
    cannot be reconstructed afterwards from two counters that moved on the same
    day.
    """

    __tablename__ = "reviewer_corrections"
    __table_args__ = (
        Index("ix_reviewer_corrections_org_field", "org_id", "field_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String, nullable=False)
    accepted_column: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable: a reviewer confirming a suggestion with nothing to replace is
    # still a reviewer decision worth logging, and inventing a rejected column
    # to fill the field would corrupt the calibration this table exists for.
    rejected_column: Mapped[str | None] = mapped_column(String, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)


# ---------------------------------------------------------------------------
# Service verticals.
#
# A "service" in this codebase is a taxonomy entry, a kit family, a prompt
# pack and a frontend flow -- plus, only where the vertical has domain records
# of its own, a registry table. Those tables live in the service's own package
# (app/finance/models.py, ...) and are imported at the tail of this file, so
# the single `from app import models` in alembic/env.py registers every table
# on Base.metadata exactly as before. NumberSequence stays HERE because it
# belongs to the shared allocator (app/numbering.py), not to any one vertical.
# ---------------------------------------------------------------------------


class NumberSequence(Base):
    """Org-scoped named counters: INV-0001, and whatever the next vertical needs.

    Not the global `counters` table, deliberately -- that one is keyed by bare
    name with no org_id, so it cannot sit behind row-level security and two
    tenants would share one numbering. An invoice number sequence with a gap or
    a duplicate is a finding in a tax audit, so allocation locks the row
    (SELECT ... FOR UPDATE on PostgreSQL) for the length of the transaction.
    """

    __tablename__ = "number_sequences"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String)  # "invoice", later "quotation", ...
    prefix: Mapped[str] = mapped_column(String, default="INV-")
    padding: Mapped[int] = mapped_column(Integer, default=4)
    next_value: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "key", name="uq_number_sequences_org_key"),)


# Service verticals' registry tables, imported last and re-exported so
# `from app.models import Customer` keeps working everywhere it is written.
# The service model modules import `uid`/`now` from `app.db` (the leaf), never
# from here -- importing back into this module mid-initialisation is exactly
# the partially-initialized-module crash that rule exists to prevent.
from app.clinical.models import ClinicalDocument, Study  # noqa: E402,F401
from app.cmc.models import (  # noqa: E402,F401
    CmcBatch, CmcBatchFormula, CmcChange, CmcChunk, CmcCitation, CmcDeliverable,
    CmcDocument, CmcExport, CmcMaterial, CmcProject, CmcResult, CmcSection,
    CmcSectionDraft, CmcSite, CmcSpecification, CmcTest,
)
from app.csr.models import (  # noqa: E402,F401
    CsrChunk, CsrCitation, CsrDocument, CsrProject, CsrSection, CsrSectionDraft,
    CsrTemplate,
)
from app.finance.models import Customer, Invoice  # noqa: E402,F401
from app.safety.models import (  # noqa: E402,F401
    PvApprovalStatus, PvCase, PvCaseDrug, PvCaseEvent, PvCaseLab, PvCaseNarrative,
    PvCaseOriginal, PvChunk, PvCitation, PvDeidItem, PvDocument,
    PvDuplicateCandidate, PvDueDate, PvExport, PvExposure, PvLiteratureRef,
    PvMappingProfile, PvMember, PvProduct, PvReportInstance, PvRsiListedTerm,
    PvRsiVersion, PvSafetyAction, PvSafetyConcern, PvSection, PvSectionDraft,
    PvSignal, PvStudy,
)
