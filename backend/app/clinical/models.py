"""The clinical service's registry tables.

Imported from the tail of `app.models`, so the single `from app import models`
in alembic/env.py registers these on Base.metadata with everything else. The
one rule of that arrangement: this module never imports from `app.models` --
everything it needs (`Base`, `uid`, `now`) lives in the `app.db` leaf, so
importing THIS module first cannot meet a partially initialised `app.models`.
"""

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, now, uid


class Study(Base):
    """The organisation's study book -- the clinical mirror of the client book.

    A study is named once (protocol number, sponsor, investigator) and then
    referenced by every document it produces, so the tenth amendment does not
    re-type the protocol number and mis-spell it on the eleventh.
    """

    __tablename__ = "studies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    protocol_number: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    sponsor: Mapped[str | None] = mapped_column(String, nullable=True)
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    indication: Mapped[str | None] = mapped_column(String, nullable=True)
    principal_investigator: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: active | closed. A closed study still generates documents (a CSR is
    #: usually written after closure); the status is bookkeeping, not a gate.
    status: Mapped[str] = mapped_column(String, default="active")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClinicalDocument(Base):
    """One numbered clinical document: which study, which type, which stored
    document version carries it.

    `study_snapshot` is the study as reported -- the book row can be edited
    later; the document must keep saying what it said. `source_record` is the
    exact fill input, so the document can be audited and regenerated against
    the same manifest without reconstructing state from the document text.
    """

    __tablename__ = "clinical_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str] = mapped_column(String)
    number: Mapped[str] = mapped_column(String)
    study_id: Mapped[str | None] = mapped_column(String, ForeignKey("studies.id"), nullable=True)
    study_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    #: csr | protocol_amendment | icf | investigator_brochure -- the request
    #: keys of `app.clinical.service.DOC_TYPES`, not the display labels.
    document_type: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    version_label: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: draft | final | void. Like an invoice, voiding keeps the row and the
    #: number -- a numbering with silent gaps is what voiding exists to avoid.
    status: Mapped[str] = mapped_column(String, default="final")
    qa_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    source_record: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("org_id", "number", name="uq_clinical_documents_org_number"),
        Index("ix_clinical_documents_org_study", "org_id", "study_id"),
    )
