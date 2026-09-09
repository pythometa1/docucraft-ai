"""The Safety / Pharmacovigilance module's tables.

Imported from the tail of `app.models` like every module's. The rule holds:
nothing here imports from `app.models` -- `Base`, `uid`, `now` come from the
`app.db` leaf.

Three things make this schema different from the CSR and CMC ones, and all
three are visible in the columns rather than left to convention.

**Time is the unit of work.** A CSR is one study and a CMC dossier is one
product; a periodic safety report is one *interval*, and every figure in it is
either an interval figure or a cumulative-since-birth-date figure. Those are
different numbers about the same cases, they are never interchangeable, and
`pv_report_instances` carries the three dates that decide which is which:
`period_start`, `period_end` and `data_lock_point`. `app.safety.scope` is the
only place they are turned into a filter.

**A case has a date the report is allowed to see it by.** `pv_cases`
distinguishes `initial_receipt_date` from `latest_receipt_date`: a case first
received two years ago and updated last week is, for a report locked before
last week, the version that existed at the lock point. The DLP is compared
against the latest receipt, so a case updated after the lock contributes
nothing to the report -- and the row still exists, because deleting it would
destroy the store the NEXT report is built from.

**Judgment is a human column, and a suggestion is a different column.**
Seriousness, expectedness and causality decide whether a case is reportable
and whether a signal exists. `pv_case_events` therefore carries
`suggested_by_system_json` beside the confirmed fields and never writes into
them: a system that pre-filled expectedness from a term match would be making
a regulatory determination and presenting it as a person's. `confirmed_by` is
the only thing that makes a value count anywhere.

And one separation that exists from the first migration rather than from the
milestone that fills it: `pv_case_originals` holds un-masked source text, has
no chunk or embedding relationship, and is never read by retrieval, drafting
or export. Separation added later is separation that leaked in between.

There is no `pv_audit_log`. The shared `AuditLog` is the audit surface for
every module in this codebase, and a second trail beside it is a second place
to look and a second thing to keep.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now, uid
from app.retrieval.embeddings import VectorColumn

# --------------------------------------------------------- product and cycle


class PvProduct(Base):
    """A product's safety profile: one product, many reporting intervals, one
    case store, one signal log.

    Extends a portal project exactly as the CSR and CMC modules do -- the
    portal project supplies org scoping, membership and the audit surface.

    `ibd` and `dibd` are the two cumulative anchors. A PBRER counts cumulatively
    from the international birth date (first approval anywhere); a DSUR counts
    from the development international birth date (first authorisation of a
    trial). Which one a report uses is a property of its type, and
    `app.safety.registry` states it -- keeping the choice out of the query
    layer, where a wrong anchor would silently change every cumulative figure
    in the document.
    """

    __tablename__ = "pv_products"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str] = mapped_column(String)
    product_name: Mapped[str] = mapped_column(String)
    #: The active substance's non-proprietary name.
    inn: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Marketing authorisation holder.
    mah_name: Mapped[str | None] = mapped_column(String, nullable=True)
    atc_code: Mapped[str | None] = mapped_column(String, nullable=True)
    #: International birth date: first approval anywhere in the world.
    ibd: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Development international birth date: first clinical trial authorisation.
    dibd: Mapped[date | None] = mapped_column(Date, nullable=True)
    formulations: Mapped[list] = mapped_column(JSON, default=list)
    routes: Mapped[list] = mapped_column(JSON, default=list)
    approved_indications: Mapped[list] = mapped_column(JSON, default=list)
    development_indications: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="setup")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_pv_products_project"),
    )


class PvMember(Base):
    """Who may do what on one product's safety work.

    A separate dimension from `app.authz`'s org capabilities, and deliberately
    so. The org role says whether somebody may approve documents in this
    tenant; this says whether they are the qualified person for THIS product --
    the one who may confirm an expectedness determination, clear the
    de-identification gate, or sign a report off.

    There is no implicit grant. An `org_admin` is not a qualified person by
    virtue of being an administrator, because §2's first principle is that
    pharmacovigilance judgment is human-owned with no silent defaults, and a
    role that arrives by inference is exactly a silent default.
    """

    __tablename__ = "pv_members"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    user_id: Mapped[str] = mapped_column(String)
    #: writer | reviewer | qualified_person
    pv_role: Mapped[str] = mapped_column(String)
    granted_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("pv_product_id", "user_id", name="uq_pv_members_product_user"),
        Index("ix_pv_members_org_product", "org_id", "pv_product_id"),
    )


class PvRsiVersion(Base):
    """One version of the Reference Safety Information.

    The RSI is what "expected" means. A report pins exactly one version, and
    every expectedness determination in that report records which version it
    was made against -- because the same event is listed under one CCDS and
    unlisted under the next, and an expectedness carried across a version
    change is a determination nobody made.
    """

    __tablename__ = "pv_rsi_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    #: ccds | ib | smpc | uspi | other
    rsi_type: Mapped[str] = mapped_column(String)
    version_label: Mapped[str] = mapped_column(String)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The version that replaced this one, once one has.
    superseded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The version new reports default to. At most one per product per type;
    #: enforced by the endpoint rather than the schema, because "currently
    #: pinned" is a statement about the product's present rather than a
    #: property of the row.
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("pv_product_id", "rsi_type", "version_label",
                         name="uq_pv_rsi_versions_product_type_label"),
        Index("ix_pv_rsi_versions_org_product", "org_id", "pv_product_id"),
    )


class PvRsiListedTerm(Base):
    """One term the RSI lists. This table IS the definition of listedness.

    `condition_text` carries the qualification the RSI itself states -- "serious
    only", "in the oncology indication" -- because a term listed under one
    condition and reported outside it is not listed, and flattening that to a
    boolean would turn a qualified entry into an unqualified one.
    """

    __tablename__ = "pv_rsi_listed_terms"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    rsi_version_id: Mapped[str] = mapped_column(String, ForeignKey("pv_rsi_versions.id"))
    meddra_pt: Mapped[str] = mapped_column(String)
    meddra_soc: Mapped[str | None] = mapped_column(String, nullable=True)
    condition_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_rsi_listed_terms_version_pt", "rsi_version_id", "meddra_pt"),
    )


class PvReportInstance(Base):
    """One reporting interval's report.

    The three dates are the whole time model. `period_start`/`period_end`
    bound the interval; `data_lock_point` bounds what may be counted at all.
    They are separate because a report is routinely locked some days after the
    period ends, and cases received in between belong to the next report even
    though they arrived during this one's preparation.

    `baseline_report_id` points at the previous approved instance. Sections
    carry forward from it, and QC compares this report's cumulative figures
    against its -- cumulative counts that went DOWN between two reports of the
    same product are either a data loss or a definition change, and both need
    a person.
    """

    __tablename__ = "pv_report_instances"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    #: One of `app.safety.registry.DELIVERABLES`.
    doc_type_key: Mapped[str] = mapped_column(String)
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    data_lock_point: Mapped[date] = mapped_column(Date)
    rsi_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The MedDRA version every tabulation in this report is coded to. One per
    #: report: two versions in one document is two different SOC hierarchies
    #: under one set of totals.
    meddra_version: Mapped[str | None] = mapped_column(String, nullable=True)
    baseline_report_id: Mapped[str | None] = mapped_column(String, nullable=True)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    #: setup | in_progress | in_review | approved
    status: Mapped[str] = mapped_column(String, default="setup")
    qppv_signoff_by: Mapped[str | None] = mapped_column(String, nullable=True)
    qppv_signoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_report_instances_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_report_instances_dlp", "pv_product_id", "data_lock_point"),
    )


class PvDueDate(Base):
    """When a report is expected in one region.

    `is_informational` is always true and is a column rather than a constant so
    that every row carrying a date also carries the statement that this system
    is not the reporting clock. §2's third principle: the sponsor's PV system
    of record governs submission obligations, and a due date shown without that
    caveat is a compliance claim this module must not make.
    """

    __tablename__ = "pv_due_dates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    report_instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("pv_report_instances.id"))
    region: Mapped[str] = mapped_column(String)
    submission_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    basis_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_informational: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_due_dates_org_report", "org_id", "report_instance_id"),
    )


# ------------------------------------------------------------- the case store


class PvCase(Base):
    """One individual case safety report, as this product's store holds it.

    Scoped to the PRODUCT, not to a report instance: the same case appears in
    every report whose interval or cumulative window contains it, and a case
    store per report would mean the same case entered several times and
    counted differently in each.
    """

    __tablename__ = "pv_cases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    worldwide_case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    local_case_ids: Mapped[list] = mapped_column(JSON, default=list)
    case_version: Mapped[int] = mapped_column(Integer, default=1)
    #: spontaneous | clinical_trial | non_interventional | literature |
    #: regulatory_authority | patient_support_programme | other
    report_source: Mapped[str | None] = mapped_column(String, nullable=True)
    study_id: Mapped[str | None] = mapped_column(String, nullable=True)
    country_of_occurrence: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_reporter_qualification: Mapped[str | None] = mapped_column(String, nullable=True)
    initial_receipt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: The date the data lock point is compared against.
    latest_receipt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_medically_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_serious: Mapped[bool] = mapped_column(Boolean, default=False)
    #: death | life_threatening | hospitalisation | disability |
    #: congenital_anomaly | other_medically_important
    seriousness_criteria: Mapped[list] = mapped_column(JSON, default=list)
    case_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    patient_age: Mapped[float | None] = mapped_column(Float, nullable=True)
    patient_age_group: Mapped[str | None] = mapped_column(String, nullable=True)
    patient_sex: Mapped[str | None] = mapped_column(String, nullable=True)
    is_pregnancy_case: Mapped[bool] = mapped_column(Boolean, default=False)
    is_special_situation: Mapped[bool] = mapped_column(Boolean, default=False)
    special_situation_types: Mapped[list] = mapped_column(JSON, default=list)
    #: pending | clear | overridden -- nothing downstream of ingestion may
    #: read a case that is still `pending`.
    deidentification_status: Mapped[str] = mapped_column(String, default="pending")
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: e2b_r3_xml | line_listing | cioms_form | manual
    imported_from: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_cases_org_product", "org_id", "pv_product_id"),
        # §14: 100,000+ cases per product. Every scope query filters on the
        # product and the latest receipt date together, so they are one index.
        Index("ix_pv_cases_product_receipt", "pv_product_id", "latest_receipt_date"),
        Index("ix_pv_cases_product_source", "pv_product_id", "report_source"),
        Index("ix_pv_cases_product_serious", "pv_product_id", "is_serious"),
        Index("ix_pv_cases_worldwide_id", "pv_product_id", "worldwide_case_id"),
    )


class PvCaseEvent(Base):
    """One adverse event within a case, and the three judgments about it.

    `is_serious`, `expectedness` and the two causality columns are what decide
    whether this event belongs in a serious-adverse-reaction line listing, in
    the unlisted column of a tabulation, and in a signal. All four are written
    only by a person. `suggested_by_system_json` is where a computed suggestion
    goes -- with its basis, so the chip can say "no matching PT in CCDS v3.2"
    rather than asking somebody to trust an unexplained verdict.
    """

    __tablename__ = "pv_case_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    #: The reporter's own words, kept for the same reason a CMC value keeps its
    #: string: it is what the source said, and the coding is an interpretation
    #: of it that a person may need to check.
    verbatim_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    meddra_llt: Mapped[str | None] = mapped_column(String, nullable=True)
    meddra_pt: Mapped[str | None] = mapped_column(String, nullable=True)
    meddra_hlt: Mapped[str | None] = mapped_column(String, nullable=True)
    meddra_hlgt: Mapped[str | None] = mapped_column(String, nullable=True)
    meddra_soc: Mapped[str | None] = mapped_column(String, nullable=True)
    meddra_version: Mapped[str | None] = mapped_column(String, nullable=True)
    #: True when this event on its own meets a seriousness criterion. Separate
    #: from the case's flag: a case is serious if any of its events is.
    is_serious: Mapped[bool] = mapped_column(Boolean, default=False)
    seriousness_criteria: Mapped[list] = mapped_column(JSON, default=list)
    #: listed | unlisted | not_assessed. `not_assessed` is the default and
    #: counts as nothing anywhere.
    expectedness: Mapped[str] = mapped_column(String, default="not_assessed")
    expectedness_rsi_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    causality_reporter: Mapped[str | None] = mapped_column(String, nullable=True)
    causality_company: Mapped[str | None] = mapped_column(String, nullable=True)
    onset_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Adverse event of special interest.
    is_aesi: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True when the verbatim term reached no MedDRA code, so the grid can ask
    #: for one rather than the event quietly missing every tabulation.
    coding_required: Mapped[bool] = mapped_column(Boolean, default=False)
    #: {"expectedness": {"value": "unlisted", "basis": "..."}, ...}
    suggested_by_system_json: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_case_events_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_case_events_case", "case_id"),
        # §14: 500,000+ events per product, tabulated by SOC and PT.
        Index("ix_pv_case_events_product_pt", "pv_product_id", "meddra_pt"),
        Index("ix_pv_case_events_product_soc", "pv_product_id", "meddra_soc"),
        Index("ix_pv_case_events_product_expectedness",
              "pv_product_id", "expectedness"),
    )


class PvCaseDrug(Base):
    """A drug named in a case: the company's own product, or something taken
    alongside it. `is_company_product` is what makes a case this product's."""

    __tablename__ = "pv_case_drugs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    drug_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_company_product: Mapped[bool] = mapped_column(Boolean, default=False)
    #: suspect | concomitant | interacting
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    dose: Mapped[str | None] = mapped_column(String, nullable=True)
    dose_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    frequency: Mapped[str | None] = mapped_column(String, nullable=True)
    route: Mapped[str | None] = mapped_column(String, nullable=True)
    indication: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String, nullable=True)
    dechallenge: Mapped[str | None] = mapped_column(String, nullable=True)
    rechallenge: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_case_drugs_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_case_drugs_case", "case_id"),
    )


class PvCaseNarrative(Base):
    """The case narrative, already de-identified.

    `raw_text_redacted` is the WORKING copy and the only one anything reads:
    retrieval, embedding, drafting and export all see this and nothing else.
    The column is named for what it is so that a future caller reaching for
    "the narrative" cannot accidentally reach for un-masked text -- that lives
    in `pv_case_originals`, in a different table, on purpose.
    """

    __tablename__ = "pv_case_narratives"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    raw_text_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_case_narratives_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_case_narratives_case", "case_id"),
    )


class PvCaseOriginal(Base):
    """Un-masked source text, kept apart from everything that reads.

    Declared in the first migration although M3 is what fills it, because a
    separation introduced later is a separation that did not exist while the
    data was arriving. Three properties define this table:

    * nothing joins to it from retrieval, drafting or export;
    * it holds no embedding column, so it cannot enter the vector store even
      by accident;
    * reading a row is an audited act, which is why `last_accessed_by` is here
      rather than the access being invisible.

    §12's hard constraint -- the original un-masked case text is never
    exportable through this module -- is enforced by nothing in the export
    path ever naming this table.
    """

    __tablename__ = "pv_case_originals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    #: narrative | document_text | field_value
    kind: Mapped[str] = mapped_column(String, default="narrative")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_accessed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_case_originals_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_case_originals_case", "case_id"),
    )


class PvCaseLab(Base):
    """A laboratory result quoted in a case. Kept as reported."""

    __tablename__ = "pv_case_labs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    test_name: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The reported string, not a parsed number: same discipline as
    #: `cmc_results.value_text`, and for the same reason.
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_range: Mapped[str | None] = mapped_column(String, nullable=True)
    test_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_case_labs_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_case_labs_case", "case_id"),
    )


class PvDeidItem(Base):
    """One thing the de-identification pass found and could not settle alone.

    The queue is a gate rather than a report: while an item is `pending`,
    nothing downstream may run. Clearing it by override is a qualified-person
    act and is audited, because the alternative -- a warning banner somebody
    scrolls past -- is how identifiers reach a model.
    """

    __tablename__ = "pv_deid_items"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: patient_name | patient_id | date_of_birth | address | phone | email |
    #: reporter_name | investigator_name | site_name | national_id | other
    identifier_type: Mapped[str] = mapped_column(String)
    detected_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_mask: Mapped[str | None] = mapped_column(String, nullable=True)
    #: pending | masked | not_an_identifier | overridden
    status: Mapped[str] = mapped_column(String, default="pending")
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_deid_items_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_deid_items_product_status", "pv_product_id", "status"),
    )


class PvDuplicateCandidate(Base):
    """Two cases that may be the same case. Never merged automatically."""

    __tablename__ = "pv_duplicate_candidates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    other_case_id: Mapped[str] = mapped_column(String, ForeignKey("pv_cases.id"))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_on: Mapped[list] = mapped_column(JSON, default=list)
    #: pending | merged | kept_both | linked
    status: Mapped[str] = mapped_column(String, default="pending")
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_duplicate_candidates_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_duplicate_candidates_status", "pv_product_id", "status"),
    )


# ------------------------------------------------ aggregation and evaluation


class PvExposure(Base):
    """How much of the product was used, and how that was worked out.

    `calculation_method_note` is not optional decoration. Exposure is the
    denominator of every rate in the report; two reports quoting different
    numbers for the same interval are usually two different methods rather
    than two different facts, and the note is what lets a reviewer tell.
    """

    __tablename__ = "pv_exposures"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    report_instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("pv_report_instances.id"))
    #: clinical_trial | marketing
    context: Mapped[str] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    population_descriptor: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: subjects | patient_years | treatment_days | units_sold | prescriptions
    measure: Mapped[str] = mapped_column(String)
    #: The figure as stated, kept as a string for the same reason a laboratory
    #: value is: "1,240,000" and "1.24 million" are different claims about
    #: precision, and the document prints what the source said.
    value_text: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The same figure as a number, for arithmetic only. Never rendered.
    value_numeric: Mapped[float | None] = mapped_column(Float, nullable=True)
    calculation_method_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_exposures_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_exposures_report", "report_instance_id"),
    )


class PvStudy(Base):
    """A clinical trial in the development programme. The DSUR §5 inventory."""

    __tablename__ = "pv_studies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    study_id: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    population: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_enrolment: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_enrolment: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("pv_product_id", "study_id", name="uq_pv_studies_product_study"),
        Index("ix_pv_studies_org_product", "org_id", "pv_product_id"),
    )


class PvSignal(Base):
    """One safety signal, through its whole life.

    Scoped to the product and linked to report instances rather than owned by
    one, because a signal opened in one interval and closed three intervals
    later has to appear -- with the right status -- in each report in between.
    """

    __tablename__ = "pv_signals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    signal_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    meddra_terms: Mapped[list] = mapped_column(JSON, default=list)
    #: disproportionality | case_review | literature | authority_request |
    #: trial | other
    detection_source: Mapped[str | None] = mapped_column(String, nullable=True)
    detection_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: new | ongoing | closed
    status: Mapped[str] = mapped_column(String, default="new")
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(Text, nullable=True)
    closure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    linked_case_ids: Mapped[list] = mapped_column(JSON, default=list)
    linked_report_instance_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_signals_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_signals_product_status", "pv_product_id", "status"),
    )


class PvSafetyConcern(Base):
    """An important identified risk, important potential risk, or piece of
    missing information.

    `rmp_part_reference` is what keeps the RMP and the PBRER's summary of
    safety concerns from drifting: both read this table, so a concern added in
    one document is present in the other by construction rather than by
    somebody remembering.
    """

    __tablename__ = "pv_safety_concerns"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    #: important_identified_risk | important_potential_risk | missing_information
    concern_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    meddra_terms: Mapped[list] = mapped_column(JSON, default=list)
    first_added_report_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="current")
    rmp_part_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_safety_concerns_org_product", "org_id", "pv_product_id"),
    )


class PvSafetyAction(Base):
    """An action taken for safety reasons: a label change, a direct healthcare
    professional communication, a suspension, a clinical hold."""

    __tablename__ = "pv_safety_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    #: label_change | dhpc | suspension | withdrawal | restriction |
    #: protocol_amendment | clinical_hold | other
    action_type: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_safety_actions_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_safety_actions_product_date", "pv_product_id", "action_date"),
    )


class PvLiteratureRef(Base):
    """A literature reference, and the search that found it."""

    __tablename__ = "pv_literature_refs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    citation: Mapped[str] = mapped_column(Text)
    database: Mapped[str | None] = mapped_column(String, nullable=True)
    search_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    search_strategy_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_case_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_literature_refs_org_product", "org_id", "pv_product_id"),
    )


class PvApprovalStatus(Base):
    """Where the product is approved, and where it is not."""

    __tablename__ = "pv_approval_statuses"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    country: Mapped[str] = mapped_column(String)
    approval_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    indication: Mapped[str | None] = mapped_column(Text, nullable=True)
    formulation: Mapped[str | None] = mapped_column(String, nullable=True)
    #: approved | withdrawn | not_approved | pending
    status: Mapped[str] = mapped_column(String, default="approved")
    source_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("pv_product_id", "country", "formulation",
                         name="uq_pv_approval_statuses_product_country_form"),
        Index("ix_pv_approval_statuses_org_product", "org_id", "pv_product_id"),
    )


# --------------------------------------------- documents, sections and output


class PvDocument(Base):
    """One uploaded source.

    The blob column is `blob_path`, not `storage_path`, and that is not a
    naming preference. `app.retention` sweeps a deleted organisation's files by
    looking for a column with exactly that name (`retention.BLOB_COLUMN`); the
    CMC module called its column `storage_path` and is therefore outside the
    automatic sweep, relying on its own hand-written purge. This module joins
    the sweep by being named the way the sweep looks.
    """

    __tablename__ = "pv_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    #: Set when the document belongs to one interval rather than the product.
    report_instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: One of `app.safety.registry.DOC_TYPES`.
    doc_type: Mapped[str] = mapped_column(String)
    #: e2b_r3_xml | line_listing | cioms_form | case_narrative_doc | document
    input_type: Mapped[str] = mapped_column(String, default="document")
    original_filename: Mapped[str] = mapped_column(String)
    blob_path: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: queued | parsing | deidentifying | coding | indexing | done | failed
    processing_status: Mapped[str] = mapped_column(String, default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        Index("ix_pv_documents_org_product", "org_id", "pv_product_id"),
    )


class PvChunk(Base):
    """A retrievable piece of a source. Masked before it gets here."""

    __tablename__ = "pv_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    document_id: Mapped[str] = mapped_column(String, ForeignKey("pv_documents.id"))
    report_instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_type: Mapped[str] = mapped_column(String)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    is_table: Mapped[bool] = mapped_column(Boolean, default=False)
    table_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list | None] = mapped_column(VectorColumn(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_chunks_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_chunks_document", "document_id"),
    )


class PvMappingProfile(Base):
    """A saved column mapping for one source safety system's line listings, so
    the second cycle is one click rather than the same thirty decisions."""

    __tablename__ = "pv_mapping_profiles"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    source_system: Mapped[str | None] = mapped_column(String, nullable=True)
    #: {"Case Number": "worldwide_case_id", ...}
    column_map: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PvSection(Base):
    """One section of one report instance.

    `delta_status` is this module's addition to the CSR/CMC section shape. A
    periodic report is written against its predecessor, so what a writer needs
    to know first about any section is not its approval status but whether
    anything happened to it this interval.
    """

    __tablename__ = "pv_sections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    report_instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("pv_report_instances.id"))
    section_code: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    is_container: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    guidance_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The one table this section may carry, if it carries one. Named on the
    #: section rather than discovered from the draft, so the export can tell
    #: that a declared table has gone missing from approved text.
    table_key: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The doc types retrieval may draw on for this section.
    source_types: Mapped[list] = mapped_column(JSON, default=list)
    #: not_started | generating | draft | in_review | approved
    status: Mapped[str] = mapped_column(String, default="not_started")
    #: carried_forward | changed | new_data | needs_rewrite | fresh
    delta_status: Mapped[str] = mapped_column(String, default="fresh")
    #: The section of the baseline report this one carried forward from.
    baseline_section_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("report_instance_id", "section_code",
                         name="uq_pv_sections_report_code"),
        Index("ix_pv_sections_org_report", "org_id", "report_instance_id"),
    )


class PvSectionDraft(Base):
    """One version of one section's text. Versions are never overwritten."""

    __tablename__ = "pv_section_drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_section_id: Mapped[str] = mapped_column(String, ForeignKey("pv_sections.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    #: model | edited | carried_forward -- where this version came from.
    origin: Mapped[str] = mapped_column(String, default="model")
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("pv_section_id", "version", name="uq_pv_section_drafts_version"),
        Index("ix_pv_section_drafts_org_section", "org_id", "pv_section_id"),
    )


class PvCitation(Base):
    """A [S#] marker in a draft, resolved to the chunk it points at."""

    __tablename__ = "pv_citations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    draft_id: Mapped[str] = mapped_column(String, ForeignKey("pv_section_drafts.id"))
    marker: Mapped[str] = mapped_column(String)
    source_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quoted_number: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_citations_org_draft", "org_id", "draft_id"),
    )


class PvExport(Base):
    """One assembled report. `blob_path`, for the reason `PvDocument` gives."""

    __tablename__ = "pv_exports"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    pv_product_id: Mapped[str] = mapped_column(String, ForeignKey("pv_products.id"))
    report_instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("pv_report_instances.id"))
    granularity: Mapped[str] = mapped_column(String, default="combined")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    blob_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        Index("ix_pv_exports_org_product", "org_id", "pv_product_id"),
        Index("ix_pv_exports_report", "report_instance_id"),
    )
