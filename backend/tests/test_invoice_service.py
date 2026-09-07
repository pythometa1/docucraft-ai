"""The invoice service, as a caller meets it: a client book, an org-scoped
numbering, and one endpoint that turns a published invoice template plus line
items into a numbered, stored, downloadable invoice.

The flow under test is the whole vertical: kit -> blueprint -> publish ->
/invoices:generate. Everything downstream of the manifest is the machinery the
rest of the product already trusts (fill engine, QA gates, document trail);
what these tests pin is the part that makes an invoice an invoice -- the
server-side Decimal money, the sequence that cannot repeat a number, and the
snapshot that keeps an invoice saying what it said.
"""

from decimal import Decimal

import pytest

from app.invoicing import UncomputableAmount, compute_totals


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def org_a(two_orgs):
    token_a, project_a, _token_b, _project_b = two_orgs
    return token_a, project_a


@pytest.fixture
def published_invoice_manifest(app_client, org_a):
    """A published invoice-kit blueprint and its manifest id."""
    token, project_id = org_a
    made = app_client.post(
        "/api/v1/template-blueprints", headers=_auth(token),
        json={"name": "Studio invoice", "kit": "invoice", "project_id": project_id})
    assert made.status_code == 201, made.text
    blueprint_id = made.json()["id"]

    published = app_client.post(
        f"/api/v1/template-blueprints/{blueprint_id}:publish", headers=_auth(token),
        json={"recompile": False})
    assert published.status_code == 201, published.text
    return token, project_id, published.json()["manifest_id"]


LINE_ITEMS = [
    {"item_description": "Stage decoration", "quantity": 1, "unit_price": 15000},
    {"item_description": "Table centrepieces", "quantity": 30, "unit_price": 450},
]

FIELDS = {
    "business_name": "Sundar Decorations", "business_address": "14 MG Road, Pune",
    "business_email": "hello@sundardecor.in", "payment_terms": "14 days.",
}


# ---------------------------------------------------------------- money

def test_totals_are_decimal_and_half_up():
    totals = compute_totals(
        [{"quantity": 3, "unit_price": "33.335"}, {"amount": 100}], tax_rate=18)
    assert totals.subtotal == Decimal("200.01")
    assert totals.tax_amount == Decimal("36.00")
    assert totals.grand_total == Decimal("236.01")


def test_gst_split_halves_always_resum():
    # An odd tax amount: the remainder rides on CGST, the halves re-sum exactly.
    totals = compute_totals([{"amount": "100.03"}], tax_rate=18, tax_split=True)
    assert totals.cgst_amount + totals.sgst_amount == totals.tax_amount


def test_a_line_with_no_derivable_amount_is_an_error_not_a_zero():
    with pytest.raises(UncomputableAmount, match="line 2"):
        compute_totals([{"amount": 10}, {"item_description": "mystery"}])


# ---------------------------------------------------------------- customers

def test_customer_book_crud(app_client, org_a):
    token, _ = org_a
    made = app_client.post("/api/v1/customers", headers=_auth(token), json={
        "name": "Hotel Blue Orchid", "address": "Baner Road, Pune",
        "tax_id": "27ABCDE1234F1Z5", "default_currency": "INR"})
    assert made.status_code == 201, made.text
    customer_id = made.json()["id"]

    got = app_client.get(f"/api/v1/customers/{customer_id}", headers=_auth(token))
    assert got.json()["tax_id"] == "27ABCDE1234F1Z5"

    patched = app_client.patch(f"/api/v1/customers/{customer_id}", headers=_auth(token),
                               json={"email": "billing@blueorchid.in"})
    assert patched.json()["email"] == "billing@blueorchid.in"
    assert patched.json()["name"] == "Hotel Blue Orchid"  # untouched fields stay

    listed = app_client.get("/api/v1/customers", headers=_auth(token), params={"q": "orchid"})
    assert any(c["id"] == customer_id for c in listed.json()["items"])

    gone = app_client.delete(f"/api/v1/customers/{customer_id}", headers=_auth(token))
    assert gone.json()["deleted"] is True
    assert app_client.get(f"/api/v1/customers/{customer_id}",
                          headers=_auth(token)).status_code == 404


def test_customer_needs_a_name(app_client, org_a):
    token, _ = org_a
    res = app_client.post("/api/v1/customers", headers=_auth(token), json={"name": "  "})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "CUSTOMER_NEEDS_NAME"


# ---------------------------------------------------------------- the flow

def test_generate_invoice_end_to_end(app_client, published_invoice_manifest, tmp_path):
    token, project_id, manifest_id = published_invoice_manifest

    customer = app_client.post("/api/v1/customers", headers=_auth(token), json={
        "name": "Hotel Blue Orchid", "address": "Baner Road, Pune",
        "default_currency": "INR"}).json()

    res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer_id": customer["id"], "line_items": LINE_ITEMS,
        "fields": FIELDS, "tax_rate": 18,
        "issue_date": "2026-09-05", "due_date": "2026-09-19",
        "locale": "en_IN",
    })
    assert res.status_code == 201, res.text
    body = res.json()

    # The number came from the org sequence, the money from Decimal.
    assert body["number"] == "INV-0001"
    assert body["subtotal"] == "28500.00"
    assert body["tax_amount"] == "5130.00"
    assert body["total"] == "33630.00"
    assert body["currency"] == "INR"  # the customer's default, nobody typed it
    assert body["qa_passed"] is True, body["qa_notes"]
    assert body["status"] == "issued"
    assert body["line_count"] == 2
    assert body["locale"] == "en_IN"

    # The stored document is real and carries the rendered rows.
    version_id = body["document_version_id"]
    import docx as docx_lib

    from app.models import DocumentVersion
    from app.db import SessionLocal
    from app.storage import abs_path

    db = SessionLocal()
    try:
        blob = db.get(DocumentVersion, version_id).blob_path
    finally:
        db.close()
    document = docx_lib.Document(str(abs_path(blob)))
    table = document.tables[0]
    assert len(table.rows) == 3  # header + 2 line items
    assert table.rows[1].cells[0].text == "Stage decoration"
    text = "\n".join(p.text for p in document.paragraphs)
    assert "INV-0001" in text
    assert "Hotel Blue Orchid" in text
    assert "33,630.00" in text

    # The registry row can be read back, snapshot and all.
    detail = app_client.get(f"/api/v1/invoices/{body['id']}", headers=_auth(token)).json()
    assert detail["customer"]["name"] == "Hotel Blue Orchid"
    assert detail["source_record"]["invoice_number"] == "INV-0001"


def test_numbers_are_sequential_per_org(app_client, published_invoice_manifest):
    token, project_id, manifest_id = published_invoice_manifest
    numbers = []
    for _ in range(2):
        res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
            "manifest_id": manifest_id, "project_id": project_id,
            "customer": {"name": "Walk-in"}, "line_items": [{"amount": 10}],
            "fields": FIELDS})
        assert res.status_code == 201, res.text
        numbers.append(res.json()["number"])
    first = int(numbers[0].split("-")[1])
    assert numbers[1] == f"INV-{first + 1:04d}"


def test_a_failed_generation_does_not_burn_a_number(app_client, published_invoice_manifest):
    token, project_id, manifest_id = published_invoice_manifest

    before = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "A"}, "line_items": [{"amount": 10}], "fields": FIELDS})
    assert before.status_code == 201

    # An uncomputable line item is refused before any number is allocated.
    refused = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "B"}, "line_items": [{"item_description": "no numbers"}],
        "fields": FIELDS})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"]["code"] == "INVOICE_AMOUNT_UNCOMPUTABLE"

    after = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "C"}, "line_items": [{"amount": 10}], "fields": FIELDS})
    n_before = int(before.json()["number"].split("-")[1])
    n_after = int(after.json()["number"].split("-")[1])
    assert n_after == n_before + 1  # no gap


def test_no_line_items_is_refused(app_client, published_invoice_manifest):
    token, project_id, manifest_id = published_invoice_manifest
    res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "line_items": [], "fields": FIELDS})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "INVOICE_NEEDS_LINE_ITEMS"


def test_void_keeps_the_number(app_client, published_invoice_manifest):
    token, project_id, manifest_id = published_invoice_manifest
    made = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "V"}, "line_items": [{"amount": 10}], "fields": FIELDS}).json()

    voided = app_client.post(f"/api/v1/invoices/{made['id']}:void", headers=_auth(token))
    assert voided.status_code == 200
    assert voided.json()["status"] == "void"
    assert voided.json()["number"] == made["number"]  # the number is not reused

    again = app_client.post(f"/api/v1/invoices/{made['id']}:void", headers=_auth(token))
    assert again.status_code == 409

    listed = app_client.get("/api/v1/invoices", headers=_auth(token),
                            params={"status": "void"})
    assert any(i["id"] == made["id"] for i in listed.json()["items"])


def test_workspace_project_is_created_once(app_client, org_a):
    token, _ = org_a
    first = app_client.post("/api/v1/invoices:workspace", headers=_auth(token)).json()
    second = app_client.post("/api/v1/invoices:workspace", headers=_auth(token)).json()
    assert first["project_id"] == second["project_id"]
    assert first["name"] == "Invoices"


def test_the_other_tenant_cannot_see_invoices(app_client, published_invoice_manifest, two_orgs):
    token, project_id, manifest_id = published_invoice_manifest
    _token_a, _pa, token_b, _pb = two_orgs

    made = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "Secret Client"}, "line_items": [{"amount": 10}],
        "fields": FIELDS}).json()

    assert app_client.get(f"/api/v1/invoices/{made['id']}",
                          headers=_auth(token_b)).status_code == 404
    listed_b = app_client.get("/api/v1/invoices", headers=_auth(token_b)).json()["items"]
    assert not any(i["id"] == made["id"] for i in listed_b)


def test_gst_template_splits_tax_even_when_the_caller_forgets(app_client, org_a):
    """A template printing CGST/SGST lines must never show 0.00 under a grand
    total that includes the tax -- the split is detected from the manifest."""
    token, project_id = org_a
    made = app_client.post(
        "/api/v1/template-blueprints", headers=_auth(token),
        json={"name": "GST invoice", "kit": "invoice_gst", "project_id": project_id})
    blueprint_id = made.json()["id"]
    published = app_client.post(
        f"/api/v1/template-blueprints/{blueprint_id}:publish", headers=_auth(token),
        json={"recompile": False})
    manifest_id = published.json()["manifest_id"]

    res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "Acme", "address": "Mumbai", "tax_id": "27X"},
        "line_items": [{"item_description": "Decor", "hsn_sac": "9985",
                        "quantity": 1, "unit_price": 10000}],
        "fields": {"business_name": "S", "business_address": "P", "business_gstin": "27Y"},
        "tax_rate": 18,  # note: no tax_split -- the template decides
        "locale": "en_IN",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["qa_passed"] is True, body["qa_notes"]

    detail = app_client.get(f"/api/v1/invoices/{body['id']}", headers=_auth(token)).json()
    record = detail["source_record"]
    assert record["cgst_amount"] == "900.00"
    assert record["sgst_amount"] == "900.00"
    assert record["grand_total"] == "11800.00"


def test_a_fill_crash_after_allocation_rolls_the_number_back(
        app_client, published_invoice_manifest, monkeypatch):
    """The number is allocated before the fill, in the same transaction -- so a
    renderer crash must return it, not burn it."""
    token, project_id, manifest_id = published_invoice_manifest

    first = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "A"}, "line_items": [{"amount": 10}], "fields": FIELDS})
    assert first.status_code == 201

    import app.generation.single as single_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("renderer crashed mid-fill")

    monkeypatch.setattr(single_module, "fill_template", explode)
    crashed = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "B"}, "line_items": [{"amount": 10}], "fields": FIELDS})
    assert crashed.status_code == 422
    assert crashed.json()["detail"]["error"]["code"] == "FILL_FAILED"
    monkeypatch.undo()

    after = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "C"}, "line_items": [{"amount": 10}], "fields": FIELDS})
    n_first = int(first.json()["number"].split("-")[1])
    n_after = int(after.json()["number"].split("-")[1])
    assert n_after == n_first + 1, "the crashed generation burned a number"


def test_an_invoice_with_nobody_to_bill_is_refused(app_client, published_invoice_manifest):
    token, project_id, manifest_id = published_invoice_manifest
    res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "line_items": [{"amount": 10}], "fields": FIELDS})
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "INVOICE_NEEDS_CUSTOMER"


def test_tax_rate_snapshot_is_plain_decimal_notation():
    """Decimal("18").normalize() is 1.8E+1; the snapshot must never say that."""
    totals = compute_totals([{"amount": 100}], tax_rate=18)
    assert totals.record_values()["tax_rate"] == "18"
    fractional = compute_totals([{"amount": 100}], tax_rate=12.5)
    assert fractional.record_values()["tax_rate"] == "12.5"


def test_a_sparse_book_customer_does_not_erase_supplied_fields(
        app_client, published_invoice_manifest):
    """A book customer with no recorded email must not delete the
    customer_email the caller typed into fields -- absences never win."""
    token, project_id, manifest_id = published_invoice_manifest
    customer = app_client.post("/api/v1/customers", headers=_auth(token), json={
        "name": "Sparse Client"}).json()  # no email, no address

    res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer_id": customer["id"],
        "line_items": [{"amount": 10}],
        "fields": {**FIELDS, "customer_email": "billing@sparse.example",
                   "due_date": "2026-10-01"}})
    assert res.status_code == 201, res.text
    record = app_client.get(f"/api/v1/invoices/{res.json()['id']}",
                            headers=_auth(token)).json()["source_record"]
    assert record["customer_email"] == "billing@sparse.example"
    assert record["due_date"] == "2026-10-01"
    assert record["customer_name"] == "Sparse Client"  # real values still win


def test_void_needs_sign_off_authority(app_client, published_invoice_manifest, two_orgs):
    """A role that cannot approve an invoice has no business cancelling one."""
    token, project_id, manifest_id = published_invoice_manifest
    made = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
        "manifest_id": manifest_id, "project_id": project_id,
        "customer": {"name": "V"}, "line_items": [{"amount": 10}], "fields": FIELDS}).json()

    from app.db import SessionLocal
    from app.models import User
    from app.security import create_access_token, hash_password

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "user-a@tenant.test").one()
        clerk = db.query(User).filter(User.email == "clerk-a@tenant.test").one_or_none()
        if clerk is None:
            clerk = User(org_id=admin.org_id, email="clerk-a@tenant.test",
                         full_name="Clerk A", password_hash=hash_password("pw"),
                         role_key="generator")
            db.add(clerk)
            db.commit()
        clerk_token = create_access_token(clerk.id, clerk.org_id)
    finally:
        db.close()

    refused = app_client.post(f"/api/v1/invoices/{made['id']}:void",
                              headers=_auth(clerk_token))
    assert refused.status_code == 403

    allowed = app_client.post(f"/api/v1/invoices/{made['id']}:void", headers=_auth(token))
    assert allowed.status_code == 200


def test_generate_refuses_a_template_whose_project_was_deleted(
        app_client, published_invoice_manifest):
    """The tf.project_id fallback must not write invoices into a project the
    organisation deleted."""
    token, project_id, manifest_id = published_invoice_manifest

    from app.db import SessionLocal
    from app.models import Project, now as model_now

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        project.deleted_at = model_now()
        db.commit()
    finally:
        db.close()
    try:
        res = app_client.post("/api/v1/invoices:generate", headers=_auth(token), json={
            "manifest_id": manifest_id,  # no project_id: exercises the fallback
            "customer": {"name": "X"}, "line_items": [{"amount": 10}], "fields": FIELDS})
        assert res.status_code == 404
    finally:
        db = SessionLocal()
        try:
            project = db.get(Project, project_id)
            project.deleted_at = None
            db.commit()
        finally:
            db.close()
