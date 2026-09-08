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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now, uid


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
