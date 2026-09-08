"""The CSR module's tables (milestone M1: project, template, sections).

Imported from the tail of `app.models` like every service's tables. The rule
holds: nothing here imports from `app.models` -- `Base`, `uid`, `now` come
from the `app.db` leaf.

A CSR project is an EXTENSION of a portal project, not a rival to it: the
portal project supplies org scoping, membership, name and audit surface; this
row adds what only a CSR has (the study link, the compound, the blinding).
Deleting the CSR project purges the module's own data and leaves the portal
project standing.
"""

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now, uid
from app.retrieval.embeddings import VectorColumn


class CsrProject(Base):
    __tablename__ = "csr_projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    #: The portal project this CSR lives in -- one CSR per project.
    project_id: Mapped[str] = mapped_column(String)
    #: The study book entry (app.clinical.models.Study). Identity fields --
    #: protocol number, sponsor, phase, indication, PI -- live THERE; this row
    #: never copies them, because a CSR quoting a stale sponsor is a defect.
    study_id: Mapped[str | None] = mapped_column(String, ForeignKey("studies.id"), nullable=True)
    compound_name: Mapped[str | None] = mapped_column(String, nullable=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String, nullable=True)
    #: open_label | single_blind | double_blind
    blinding: Mapped[str | None] = mapped_column(String, nullable=True)
    study_design_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: setup | ready (template chosen) -- later milestones add processing states.
    status: Mapped[str] = mapped_column(String, default="setup")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_csr_projects_project"),
    )


class CsrTemplate(Base):
    __tablename__ = "csr_templates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    csr_project_id: Mapped[str] = mapped_column(String, ForeignKey("csr_projects.id"))
    #: builtin_ich_e3 | uploaded (uploaded arrives in M6)
    source: Mapped[str] = mapped_column(String, default="builtin_ich_e3")
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CsrSection(Base):
    """One numbered ICH E3 section of one CSR project.

    Containers (9, 11.4, ...) are headings whose prose lives in children --
    they never hold a draft and never generate. Status walks
    not_started -> generating -> draft -> in_review -> approved.
    """

    __tablename__ = "csr_sections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    csr_project_id: Mapped[str] = mapped_column(String, ForeignKey("csr_projects.id"))
    section_number: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_container: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="not_started")
    guidance_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("csr_project_id", "section_number",
                         name="uq_csr_sections_project_number"),
        Index("ix_csr_sections_org_project", "org_id", "csr_project_id"),
    )


class CsrDocument(Base):
    """One uploaded source file, tagged with what it IS.

    The tag is not decoration: it decides which sections may retrieve from
    this file (a statistical method comes from the SAP, a disposition count
    from the TLFs), and `prior_csr` is retrievable for style but never
    citable as fact.
    """

    __tablename__ = "csr_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    csr_project_id: Mapped[str] = mapped_column(String, ForeignKey("csr_projects.id"))
    doc_type: Mapped[str] = mapped_column(String)
    original_filename: Mapped[str] = mapped_column(String)
    storage_path: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: queued | parsing | chunking | indexing | done | failed
    processing_status: Mapped[str] = mapped_column(String, default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    #: SHA-256 of the bytes: re-uploading an unchanged file skips re-embedding.
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_csr_documents_org_project", "org_id", "csr_project_id"),
    )


class CsrChunk(Base):
    """A retrievable piece of one source document.

    Narrative text chunks carry a page; TLF table chunks carry one whole table
    with its id and title, because "Table 14.2.1 Demographics" has to be
    findable BY that id -- a table flattened into prose and split mid-row is
    not a citable source, it is noise with numbers in it.
    """

    __tablename__ = "csr_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    csr_project_id: Mapped[str] = mapped_column(String, ForeignKey("csr_projects.id"))
    document_id: Mapped[str] = mapped_column(String, ForeignKey("csr_documents.id"))
    doc_type: Mapped[str] = mapped_column(String)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    #: "14.1.1" for a TLF table, so a section can prefer its own table range.
    table_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Embedded in the project's own namespace; retrieval filters by
    #: csr_project_id BEFORE similarity, never after.
    embedding: Mapped[list | None] = mapped_column(VectorColumn(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_csr_chunks_org_project", "org_id", "csr_project_id"),
        Index("ix_csr_chunks_document", "document_id"),
    )


class CsrSectionDraft(Base):
    """One version of one section's text. Versions are never overwritten:
    a regeneration and a human edit both append, so the trail of what the
    model wrote and what the writer changed survives."""

    __tablename__ = "csr_section_drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    csr_section_id: Mapped[str] = mapped_column(String, ForeignKey("csr_sections.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    #: "ai" or a user id -- who produced THIS version.
    created_by: Mapped[str] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Retrieved chunk ids, k, prompt version, instruction: the audit answer to
    #: "what did the model actually see when it wrote this?"
    generation_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("csr_section_id", "version", name="uq_csr_drafts_section_version"),
        Index("ix_csr_drafts_org_section", "org_id", "csr_section_id"),
    )


class CsrCitation(Base):
    """One [S#, p.X] marker resolved to the chunk it points at, so a click in
    the editor can highlight the source and QC can verify the number."""

    __tablename__ = "csr_citations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    draft_id: Mapped[str] = mapped_column(String, ForeignKey("csr_section_drafts.id"))
    marker: Mapped[str] = mapped_column(String)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chunk_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    cited_value: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_csr_citations_draft", "draft_id"),
    )
