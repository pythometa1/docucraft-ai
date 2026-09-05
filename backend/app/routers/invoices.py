"""The invoice service: a client book, a numbered registry, and one endpoint
that turns a manifest plus line items into a stored, numbered invoice.

The generation itself is `generation.single.generate_one` -- the same core the
manifest endpoint uses -- wrapped in what makes an invoice an invoice: the
money is computed server-side in Decimal (`app.invoicing`), the number comes
from an org-scoped sequence (`app.numbering`), and both happen inside the one
transaction that also stores the document. A failed fill therefore rolls the
number allocation back instead of burning INV-0042 on a document that never
existed.

The registry row snapshots the customer as billed and the exact fill input,
because the client-book entry can be edited later and the invoice must keep
saying what it said.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.authz import APPROVE_DOCUMENT, has_capability
from app.db import get_db
from app.generation.single import FillFailed, generate_one
from app.invoicing import UncomputableAmount, compute_totals
from app.metrics import record_qa_overrides
from app.models import (
    Counter, Customer, Invoice, Project, TemplateFile, TemplateManifest,
    TemplateVersion, User, now,
)
from app.numbering import INVOICE_KEY, allocate
from app.ownership import owned_manifest, owned_project
from app.security import error, get_current_user

router = APIRouter(tags=["invoices"])

#: The workspace project every invoice belongs to unless the caller names one.
#: A project is where templates, documents and audit rows already live, so the
#: service reuses that machinery rather than growing a parallel one.
WORKSPACE_NAME = "Invoices"
WORKSPACE_FUNCTION = "Finance"
WORKSPACE_DOCUMENT_TYPE = "Invoice"


# ---------------------------------------------------------------- customers

class CustomerIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    default_currency: str | None = None
    notes: str | None = None


def _customer_out(c: Customer) -> dict:
    return {
        "id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
        "address": c.address, "tax_id": c.tax_id,
        "default_currency": c.default_currency, "notes": c.notes,
        "created_at": c.created_at, "updated_at": c.updated_at,
    }


def _owned_customer(db: Session, customer_id: str, user: User) -> Customer:
    c = db.get(Customer, customer_id)
    if not c or c.org_id != user.org_id or c.deleted_at is not None:
        raise error("CUSTOMER_NOT_FOUND", "Customer not found", 404)
    return c


@router.get("/customers")
def list_customers(q: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    stmt = select(Customer).where(
        Customer.org_id == user.org_id, Customer.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Customer.name.ilike(f"%{q}%"))
    rows = db.scalars(stmt.order_by(Customer.name)).all()
    return {"items": [_customer_out(c) for c in rows]}


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if not body.name.strip():
        raise error("CUSTOMER_NEEDS_NAME", "A customer needs a name.", 422)
    c = Customer(org_id=user.org_id, created_by=user.id,
                 **body.model_dump(exclude_none=False))
    db.add(c)
    db.flush()
    log_audit(db, user, "Added a customer", "customer", c.id, None, "info", c.name)
    db.commit()
    db.refresh(c)
    return _customer_out(c)


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    return _customer_out(_owned_customer(db, customer_id, user))


class CustomerPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    default_currency: str | None = None
    notes: str | None = None


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: str, body: CustomerPatch,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    c = _owned_customer(db, customer_id, user)
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed and not (changed["name"] or "").strip():
        raise error("CUSTOMER_NEEDS_NAME", "A customer needs a name.", 422)
    for key, value in changed.items():
        setattr(c, key, value)
    c.updated_at = now()
    log_audit(db, user, "Updated a customer", "customer", c.id, None, "info", c.name)
    db.commit()
    db.refresh(c)
    return _customer_out(c)


@router.delete("/customers/{customer_id}")
def delete_customer(customer_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Soft delete. Invoices keep their snapshot, so history is undisturbed."""
    c = _owned_customer(db, customer_id, user)
    c.deleted_at = now()
    log_audit(db, user, "Removed a customer", "customer", c.id, None, "info", c.name)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------- workspace

def _next_display_id(db: Session, counter_name: str, start: int) -> int:
    counter = db.get(Counter, counter_name)
    if counter is None:
        counter = Counter(name=counter_name, value=start)
        db.add(counter)
    counter.value += 1
    db.flush()
    return counter.value


def _workspace_project(db: Session, user: User) -> Project:
    """The org's Invoices project, created on first use.

    Lazily, so an organisation that never invoices never carries one -- and
    found by function/document_type rather than by name, so renaming the
    project does not orphan it.
    """
    project = db.scalar(select(Project).where(
        Project.org_id == user.org_id,
        Project.function == WORKSPACE_FUNCTION,
        Project.document_type == WORKSPACE_DOCUMENT_TYPE,
        Project.deleted_at.is_(None),
    ).order_by(Project.created_at))
    if project is not None:
        return project
    project = Project(
        org_id=user.org_id, display_id=_next_display_id(db, "project_display_id", 51000),
        name=WORKSPACE_NAME, description="Invoices generated by the invoice service.",
        region="Global", function=WORKSPACE_FUNCTION,
        document_type=WORKSPACE_DOCUMENT_TYPE, language="English",
        status="active", created_by=user.id)
    db.add(project)
    db.flush()
    log_audit(db, user, "Created the invoice workspace", "project", project.id,
              project.id, "info", WORKSPACE_NAME)
    return project


@router.post("/invoices:workspace")
def invoice_workspace(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    project = _workspace_project(db, user)
    db.commit()
    return {"project_id": project.id, "name": project.name,
            "display_id": project.display_id}


# ---------------------------------------------------------------- invoices

def _invoice_out(inv: Invoice) -> dict:
    return {
        "id": inv.id, "number": inv.number, "status": inv.status,
        "project_id": inv.project_id, "customer_id": inv.customer_id,
        "customer": inv.customer_snapshot, "currency": inv.currency,
        "subtotal": str(inv.subtotal) if inv.subtotal is not None else None,
        "tax_amount": str(inv.tax_amount) if inv.tax_amount is not None else None,
        "total": str(inv.total) if inv.total is not None else None,
        "line_count": inv.line_count,
        "manifest_id": inv.manifest_id, "document_id": inv.document_id,
        "document_version_id": inv.document_version_id,
        "qa_passed": inv.qa_passed,
        "issued_at": inv.issued_at, "due_at": inv.due_at,
        "created_at": inv.created_at,
    }


def _owned_invoice(db: Session, invoice_id: str, user: User) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.org_id != user.org_id or inv.deleted_at is not None:
        raise error("INVOICE_NOT_FOUND", "Invoice not found", 404)
    return inv


@router.get("/invoices")
def list_invoices(customer_id: str | None = None,
                  status_: str | None = Query(None, alias="status"),
                  limit: int = Query(200, ge=1, le=500),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(Invoice).where(
        Invoice.org_id == user.org_id, Invoice.deleted_at.is_(None))
    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if status_:
        stmt = stmt.where(Invoice.status == status_)
    rows = db.scalars(stmt.order_by(Invoice.created_at.desc()).limit(limit)).all()
    return {"items": [_invoice_out(i) for i in rows]}


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    inv = _owned_invoice(db, invoice_id, user)
    return {**_invoice_out(inv), "source_record": inv.source_record}


@router.post("/invoices/{invoice_id}:void")
def void_invoice(invoice_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Mark an invoice void. The row and its number remain -- a numbering with
    silent gaps is what voiding exists to avoid."""
    inv = _owned_invoice(db, invoice_id, user)
    if inv.status == "void":
        raise error("INVOICE_ALREADY_VOID", "This invoice is already void.", 409)
    inv.status = "void"
    inv.updated_at = now()
    log_audit(db, user, "Voided an invoice", "invoice", inv.id, inv.project_id,
              "warning", inv.number)
    db.commit()
    return _invoice_out(inv)


class InvoiceGenerateRequest(BaseModel):
    manifest_id: str
    project_id: str | None = None
    customer_id: str | None = None
    #: A one-off customer, when there is no book entry: {name, address, ...}.
    customer: dict | None = None
    line_items: list[dict]
    #: Scalar template fields beyond the ones this endpoint computes --
    #: business name and address, payment terms, bank details.
    fields: dict = {}
    currency: str | None = None
    tax_rate: float | None = None
    #: Split the tax into equal CGST and SGST halves (Indian intra-state GST).
    #: None means "look at the template": one that prints <CGST Amount> and
    #: <SGST Amount> gets the split, because a grand total that includes tax
    #: above a CGST line reading 0.00 is a wrong number that looks deliberate.
    tax_split: bool | None = None
    issue_date: str | None = None
    due_date: str | None = None
    language: str = "en"
    locale: str | None = None
    #: Which keys of each line item carry the numbers. Defaults match the
    #: shipped invoice kits; a generated template's manifest names its own.
    quantity_key: str = "quantity"
    unit_price_key: str = "unit_price"
    amount_key: str = "amount"


def _parse_date(value: str | None, field_name: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise error("BAD_DATE", f"{field_name} is not an ISO date: {value!r}", 422)


@router.post("/invoices:generate", status_code=201)
def generate_invoice(body: InvoiceGenerateRequest, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    m = owned_manifest(db, body.manifest_id, user)
    tv = db.get(TemplateVersion, m.template_version_id)
    tf = db.get(TemplateFile, m.template_file_id) if m.template_file_id else None

    # The same readiness gates as the manifest endpoint, with the same codes --
    # an invoice is a real, stored, downloadable document, so a rule enforced
    # there and not here would be a rule with a door next to it.
    if m.status == "failed":
        raise error(
            "MANIFEST_NOT_READ",
            "The compiler could not produce a usable reading of this template, so there is "
            "nothing to fill from. Publish or re-read the invoice template first.", 409)
    if m.status in ("superseded", "deprecated"):
        raise error(
            "MANIFEST_RETIRED",
            "This reading of the template has been replaced by a newer one. Use the current "
            "manifest for this template.", 409)
    if tf is not None and tf.legally_binding and m.status != "approved":
        raise error(
            "MANIFEST_NOT_APPROVED",
            "This template is marked legally binding, so it needs sign-off before it can "
            "generate.", 409)

    if body.project_id:
        project = owned_project(db, body.project_id, user)
    elif tf is not None and tf.project_id:
        project = db.get(Project, tf.project_id)
        if not project or project.org_id != user.org_id:
            raise error("PROJECT_NOT_FOUND", "Project not found", 404)
    else:
        project = _workspace_project(db, user)

    # -- who is being billed --
    customer_row = None
    if body.customer_id:
        customer_row = _owned_customer(db, body.customer_id, user)
        snapshot = {
            "name": customer_row.name, "email": customer_row.email,
            "phone": customer_row.phone, "address": customer_row.address,
            "tax_id": customer_row.tax_id,
        }
    elif body.customer:
        snapshot = {k: body.customer.get(k)
                    for k in ("name", "email", "phone", "address", "tax_id")}
    else:
        snapshot = {}
    # -- the money, in Decimal, before anything is stored --
    if not body.line_items:
        raise error("INVOICE_NEEDS_LINE_ITEMS",
                    "An invoice needs at least one line item.", 422)
    if not (snapshot.get("name") or "").strip():
        raise error(
            "INVOICE_NEEDS_CUSTOMER",
            "An invoice bills somebody: pass customer_id from the client book, or an inline "
            "customer with at least a name.", 422)
    tax_split = body.tax_split
    if tax_split is None:
        field_ids = {f.get("id") for f in (m.fields or ())}
        tax_split = bool({"cgst_amount", "sgst_amount"} & field_ids)
    try:
        totals = compute_totals(
            body.line_items, quantity_key=body.quantity_key,
            unit_price_key=body.unit_price_key, amount_key=body.amount_key,
            tax_rate=body.tax_rate, tax_split=tax_split)
    except UncomputableAmount as exc:
        raise error("INVOICE_AMOUNT_UNCOMPUTABLE", str(exc), 422)

    currency = (body.currency
                or (customer_row.default_currency if customer_row else None)
                or "USD")
    issued_at = _parse_date(body.issue_date, "issue_date") or now()
    due_at = _parse_date(body.due_date, "due_date")

    # -- the number, allocated in this transaction so a failed fill returns it --
    number = allocate(db, user.org_id, INVOICE_KEY)

    # -- the fill input. Caller's fields first; what this endpoint computes
    # wins over anything the caller typed, because the server's Decimal math is
    # the authoritative one for every figure the document prints. --
    source_record = {
        **body.fields,
        "customer_name": snapshot.get("name"),
        "customer_email": snapshot.get("email"),
        "customer_phone": snapshot.get("phone"),
        "customer_address": snapshot.get("address"),
        "customer_tax_id": snapshot.get("tax_id"),
        "customer_gstin": snapshot.get("tax_id"),
        "invoice_number": number,
        "invoice_no": number,
        "invoice_date": (body.issue_date or issued_at.date().isoformat()),
        "due_date": body.due_date,
        "currency": currency,
        **totals.record_values(),
        "line_items": totals.line_items,
    }
    source_record = {k: v for k, v in source_record.items() if v is not None}

    try:
        outcome = generate_one(
            db, user, manifest=m, template_version=tv, project=project,
            source_record=source_record, language=body.language, locale=body.locale,
            change_summary=f"Invoice {number}",
        )
    except FillFailed as exc:
        raise error("FILL_FAILED", f"Could not generate the invoice: {exc}", 422)

    # An invoice the person just generated from values they typed, that passed
    # every QA gate, is approved in the same act -- downloading is the entire
    # point of generating one, and `_require_downloadable` refuses anything
    # unsigned. Same shape as `try_auto_approve` for manifests: it declines
    # rather than walks through a control that exists on purpose. A QA-blocked
    # invoice stays blocked, and stays undownloadable.
    approval_note = None
    approved = False
    if outcome.fill.qa_passed:
        if not has_capability(user, APPROVE_DOCUMENT):
            approval_note = (
                f"Your role ({user.role_key}) cannot approve documents, so this invoice is "
                "waiting for sign-off before it can be downloaded.")
        elif tf is not None and tf.legally_binding:
            approval_note = (
                "This template is marked legally binding, so the invoice needs a second "
                "person's sign-off before it can be downloaded.")
        else:
            outcome.version.status = "approved"
            outcome.version.approved_by = user.id
            outcome.version.approved_at = now()
            outcome.document.status = "approved"
            approved = True
            # The same record the real :approve endpoint writes: §22 asks
            # whether a human signed under a warning-severity finding, and an
            # auto-approval that skipped this would hide exactly those.
            record_qa_overrides(db, document_version_id=outcome.version.id, user_id=user.id)
            log_audit(db, user, "Approved draft", "document_version", outcome.version.id,
                      project.id, "success")

    invoice = Invoice(
        org_id=user.org_id, project_id=project.id, number=number,
        customer_id=customer_row.id if customer_row else None,
        customer_snapshot=snapshot, manifest_id=m.id,
        document_id=outcome.document.id, document_version_id=outcome.version.id,
        currency=currency, subtotal=totals.subtotal, tax_amount=totals.tax_amount,
        total=totals.grand_total, line_count=len(totals.line_items),
        issued_at=issued_at, due_at=due_at,
        status="issued" if approved else "draft",
        qa_passed=outcome.fill.qa_passed, source_record=source_record,
        created_by=user.id)
    db.add(invoice)
    db.flush()
    log_audit(db, user, "Generated an invoice", "invoice", invoice.id, project.id,
              "info" if outcome.fill.qa_passed else "warning", number)
    db.commit()
    db.refresh(invoice)

    return {
        **_invoice_out(invoice),
        "filename": outcome.filename,
        "qa_notes": outcome.fill.qa_notes,
        "approval_note": approval_note,
        "locale": outcome.locale, "locale_source": outcome.locale_source,
    }
