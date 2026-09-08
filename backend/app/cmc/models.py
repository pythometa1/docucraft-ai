"""The Quality/CMC module's tables.

Imported from the tail of `app.models` like every module's. The rule holds:
nothing here imports from `app.models` -- `Base`, `uid`, `now` come from the
`app.db` leaf.

The half that makes this module different from a narrative one is the
structured store: `cmc_materials` / `cmc_batches` / `cmc_tests` /
`cmc_specifications` / `cmc_results` / `cmc_batch_formula`. Those rows are not
evidence a model reads; they are the numbers the document prints. Two
consequences are built into the columns:

* `cmc_results.value_text` is the value as the source wrote it, and it is what
  a rendered table shows. `value_numeric` exists only so a limit can be
  compared and a total summed. The renderer never reads the numeric column,
  because "0.050" and Decimal("0.05") are the same quantity and not the same
  reported result -- and significant figures on a certificate of analysis are
  a claim about the method, not formatting.
* Nothing is trusted until a person says so. `extraction_confidence` records
  how the value was obtained and `verified_by`/`verified_at` who accepted it;
  an unverified value cannot reach an export without an explicit override that
  is itself audited.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now, uid
from app.retrieval.embeddings import VectorColumn


class CmcProject(Base):
    """A product's quality dossier work: one product, several deliverables,
    one shared data store and audit trail.

    Extends a portal project rather than replacing it, exactly as the CSR
    module does -- the portal project supplies org scoping, membership and the
    audit surface.
    """

    __tablename__ = "cmc_projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str] = mapped_column(String)
    product_name: Mapped[str] = mapped_column(String)
    #: The active substance's non-proprietary name. Separate from the product
    #: name because a specification is written against the substance and a
    #: label against the product.
    inn_or_ds_name: Mapped[str | None] = mapped_column(String, nullable=True)
    dosage_form: Mapped[str | None] = mapped_column(String, nullable=True)
    strengths: Mapped[list] = mapped_column(JSON, default=list)
    route_of_administration: Mapped[str | None] = mapped_column(String, nullable=True)
    #: IND | IMPD | NDA | ANDA | MAA | variation | other
    submission_type: Mapped[str | None] = mapped_column(String, nullable=True)
    #: FDA | EMA | CDSCO | PMDA | HC | other -- drives the 3.2.R item list.
    target_regions: Mapped[list] = mapped_column(JSON, default=list)
    development_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The approved snapshot a variation is diffed against, once frozen.
    baseline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="setup")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_cmc_projects_project"),
    )


class CmcSite(Base):
    """A manufacturing, packaging or testing site, named once and referenced
    by every section that has to list it -- so 3.2.S.2.1 and 3.2.P.3.1 cannot
    spell the same address two ways."""

    __tablename__ = "cmc_sites"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    name: Mapped[str] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: FEI, DUNS, or whatever the authority registers the site by.
    identifier: Mapped[str | None] = mapped_column(String, nullable=True)
    #: ds_manufacture | dp_manufacture | packaging | testing | release
    activities: Mapped[list] = mapped_column(JSON, default=list)
    gmp_evidence_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("ix_cmc_sites_org_project", "org_id", "cmc_project_id"),
    )


class CmcDeliverable(Base):
    """One document the project produces (3.2.S, 3.2.P, an APQR...). Several
    per project, sharing the sources and the data store."""

    __tablename__ = "cmc_deliverables"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    doc_type_key: Mapped[str] = mapped_column(String)
    #: builtin | uploaded (sponsor template parsing arrives in M8)
    template_source: Mapped[str] = mapped_column(String, default="builtin")
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("cmc_project_id", "doc_type_key",
                         name="uq_cmc_deliverables_project_key"),
        Index("ix_cmc_deliverables_org_project", "org_id", "cmc_project_id"),
    )


class CmcSection(Base):
    """One numbered section of one deliverable.

    `applicability` is the CTD-specific state a narrative module has no need
    for: a section can be genuinely not applicable (with the justification a
    reviewer will ask for), or covered by a DMF whose closed part the sponsor
    holds no data for. Both are answers; neither is an empty section.
    """

    __tablename__ = "cmc_sections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_deliverable_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_deliverables.id"))
    section_code: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_container: Mapped[bool] = mapped_column(Boolean, default=False)
    #: applicable | not_applicable | referenced_dmf
    applicability: Mapped[str] = mapped_column(String, default="applicable")
    applicability_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    guidance_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The table builder this section renders, when it carries data rather
    #: than prose. See `app.cmc.ctd.DATA_SECTIONS`.
    table_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="not_started")
    #: The approved draft frozen as the baseline a variation is diffed against.
    baseline_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("cmc_deliverable_id", "section_code",
                         name="uq_cmc_sections_deliverable_code"),
        Index("ix_cmc_sections_org_deliverable", "org_id", "cmc_deliverable_id"),
    )


class CmcSectionDraft(Base):
    """One version of one section's text. Never overwritten, for the same
    reason the CSR module never overwrites: what the model wrote and what the
    writer changed are two different facts about a regulated document."""

    __tablename__ = "cmc_section_drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_section_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_sections.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("cmc_section_id", "version",
                         name="uq_cmc_drafts_section_version"),
        Index("ix_cmc_drafts_org_section", "org_id", "cmc_section_id"),
    )


class CmcCitation(Base):
    __tablename__ = "cmc_citations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    draft_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_section_drafts.id"))
    marker: Mapped[str] = mapped_column(String)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chunk_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    cited_value: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_cmc_citations_draft", "draft_id"),
    )


class CmcDocument(Base):
    """One uploaded source, tagged with what it is and, where it matters,
    which material it describes.

    The material tag is the CMC addition: a certificate of analysis is for a
    particular substance or product, and a specification retrieved for the
    wrong one is a limit applied to the wrong molecule.
    """

    __tablename__ = "cmc_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    doc_type: Mapped[str] = mapped_column(String)
    material_id: Mapped[str | None] = mapped_column(String, nullable=True)
    original_filename: Mapped[str] = mapped_column(String)
    storage_path: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: queued | parsing | chunking | extracting | indexing | done | failed
    processing_status: Mapped[str] = mapped_column(String, default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    #: How many structured values this document yielded. Zero on a source the
    #: extractor read as prose only, which is a fact worth showing rather than
    #: a silence.
    value_count: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_cmc_documents_org_project", "org_id", "cmc_project_id"),
    )


class CmcChunk(Base):
    __tablename__ = "cmc_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    document_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_documents.id"))
    doc_type: Mapped[str] = mapped_column(String)
    material_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    table_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list | None] = mapped_column(VectorColumn(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_cmc_chunks_org_project", "org_id", "cmc_project_id"),
        Index("ix_cmc_chunks_document", "document_id"),
    )


# --------------------------------------------------------- the structured store

class CmcMaterial(Base):
    """A substance, product, excipient, intermediate or packaging component --
    the thing a specification, a batch and a result are all about."""

    __tablename__ = "cmc_materials"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    #: drug_substance | drug_product | excipient | intermediate | packaging_component
    kind: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    grade: Mapped[str | None] = mapped_column(String, nullable=True)
    #: "USP", "Ph.Eur. 2.2.32" -- the monograph claimed, never its text. The
    #: system holds no pharmacopoeia; QC flags the claim for human checking.
    compendial_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    supplier: Mapped[str | None] = mapped_column(String, nullable=True)
    dmf_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("ix_cmc_materials_org_project", "org_id", "cmc_project_id"),
    )


class CmcSpecification(Base):
    """One version of one material's specification. Versioned because a
    dossier cites the specification in force when a batch was released, and a
    later revision must not silently rewrite what an old batch was tested
    against."""

    __tablename__ = "cmc_specifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    material_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_materials.id"))
    version: Mapped[str] = mapped_column(String, default="1.0")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_cmc_specifications_org_project", "org_id", "cmc_project_id"),
    )


class CmcTest(Base):
    """One row of a specification: what is measured, by which method, and
    what it must be.

    `acceptance_criterion_text` is the criterion as the specification states
    it ("98.0 - 102.0 % of label claim", "NMT 0.2 %", "Complies"), and it is
    what a rendered specification table prints. `limit_lower`/`limit_upper`/
    `limit_operator` are the machine-readable form QC compares results
    against; where a criterion cannot be reduced to them they stay null and QC
    says it could not check rather than inventing a bound.
    """

    __tablename__ = "cmc_tests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    material_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_materials.id"))
    spec_version_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cmc_specifications.id"), nullable=True)
    test_name: Mapped[str] = mapped_column(String)
    method_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: compendial | in_house
    method_type: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    acceptance_criterion_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Stored as strings, not floats: "0.050" is a claim about the method's
    #: precision and Decimal-from-float would lose it.
    limit_lower: Mapped[str | None] = mapped_column(String, nullable=True)
    limit_upper: Mapped[str | None] = mapped_column(String, nullable=True)
    #: between | nmt | nlt | eq | complies | report -- how the limits read.
    limit_operator: Mapped[str | None] = mapped_column(String, nullable=True)
    #: release | shelf_life | in_process
    stage: Mapped[str] = mapped_column(String, default="release")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_cmc_tests_org_project", "org_id", "cmc_project_id"),
        Index("ix_cmc_tests_material", "material_id"),
    )


class CmcBatch(Base):
    __tablename__ = "cmc_batches"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    material_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_materials.id"))
    batch_number: Mapped[str] = mapped_column(String)
    #: The size as reported, string for the same reason limits are.
    batch_size: Mapped[str | None] = mapped_column(String, nullable=True)
    batch_size_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    manufacture_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    site_id: Mapped[str | None] = mapped_column(String, ForeignKey("cmc_sites.id"), nullable=True)
    #: development | clinical | registration | process_validation | commercial | stability
    purpose: Mapped[str | None] = mapped_column(String, nullable=True)
    scale: Mapped[str | None] = mapped_column(String, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("cmc_project_id", "material_id", "batch_number",
                         name="uq_cmc_batches_project_material_number"),
        Index("ix_cmc_batches_org_project", "org_id", "cmc_project_id"),
    )


class CmcResult(Base):
    """One measured value: this batch, this test, at this condition and
    timepoint.

    `value_text` is the source's own string and it is what documents print.
    `value_numeric` is for comparison only. The two are written together and
    neither is derived from the other at render time, so a rounding rule
    introduced years from now cannot retroactively change what a certificate
    of analysis was reported to say.
    """

    __tablename__ = "cmc_results"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    batch_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_batches.id"))
    test_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_tests.id"))
    #: "25C/60RH", "40C/75RH" -- null for a release result.
    storage_condition: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Null for release; 0, 3, 6, 12... for stability.
    timepoint_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: upright | inverted | horizontal
    orientation: Mapped[str | None] = mapped_column(String, nullable=True)
    value_text: Mapped[str] = mapped_column(String)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    #: eq | lt | gt | lte | gte | nd | nmt | nlt | text
    operator: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    table_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    #: 1.0 when a person typed it, lower when a parser placed it. Below the
    #: review threshold the grid demands attention before the value can print.
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Set when two sources disagree about the same cell. Never auto-merged:
    #: whichever value the system picked would be a number nobody chose.
    conflict_with_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_cmc_results_org_project", "org_id", "cmc_project_id"),
        Index("ix_cmc_results_cell", "batch_id", "test_id",
              "storage_condition", "timepoint_months"),
    )


class CmcBatchFormula(Base):
    """One component line of a drug product's batch formula."""

    __tablename__ = "cmc_batch_formula"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    cmc_deliverable_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cmc_deliverables.id"), nullable=True)
    component_material_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cmc_materials.id"), nullable=True)
    component_name: Mapped[str] = mapped_column(String)
    function: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity_per_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    percent_ww: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity_per_batch: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_to_standard: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_cmc_batch_formula_org_project", "org_id", "cmc_project_id"),
    )


class CmcChange(Base):
    """A variation: what changed, which sections it touches, and whether it
    has been carried through them."""

    __tablename__ = "cmc_changes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    change_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(Text)
    change_type: Mapped[str | None] = mapped_column(String, nullable=True)
    impacted_section_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="open")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_cmc_changes_org_project", "org_id", "cmc_project_id"),
    )


class CmcExport(Base):
    __tablename__ = "cmc_exports"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    cmc_project_id: Mapped[str] = mapped_column(String, ForeignKey("cmc_projects.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    #: ectd_leaves | combined | both
    granularity: Mapped[str] = mapped_column(String, default="combined")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_cmc_exports_org_project", "org_id", "cmc_project_id"),
    )
