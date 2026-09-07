"""The invoice service's registry tables.

Imported from the tail of `app.models`, so the single `from app import models`
in alembic/env.py registers these on Base.metadata with everything else, and
`from app.models import Customer` keeps working everywhere it is written. The
one rule of that arrangement: this module may import from `app.models` only
names defined above its tail import block (`uid`, `now` -- top of the file).
"""

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import now, uid


class Customer(Base):
    """The organisation's client book, so the tenth invoice to a client does
    not re-type their address and mis-spell their tax id on the eleventh."""

    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: GSTIN, VAT number, EIN -- whatever the customer's jurisdiction calls it.
    tax_id: Mapped[str | None] = mapped_column(String, nullable=True)
    default_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invoice(Base):
    """One issued invoice: its number, who it bills, what it totals, and the
    generated document that carries it.

    `source_record` is the exact fill input, snapshotted -- so an invoice can be
    audited (what did we say the line items were?) and regenerated against the
    same manifest without reconstructing state from the document text. Money
    columns are storage only: every computation happens in `Decimal` in Python,
    because SQLite's Numeric is loose and a float that "looks right" on one
    dialect is how two totals disagree by a paisa.
    """

    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str] = mapped_column(String)
    number: Mapped[str] = mapped_column(String)
    customer_id: Mapped[str | None] = mapped_column(String, ForeignKey("customers.id"), nullable=True)
    #: The customer as billed, whether or not a book entry exists -- the book
    #: row can be edited later; the invoice must keep saying what it said.
    customer_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    subtotal: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: draft | issued | void. Payment tracking is deliberately absent -- see the
    #: service plan; recording money received is a different responsibility with
    #: different correctness requirements than generating the document.
    status: Mapped[str] = mapped_column(String, default="issued")
    qa_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    source_record: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("org_id", "number", name="uq_invoices_org_number"),
        Index("ix_invoices_org_customer", "org_id", "customer_id"),
    )
