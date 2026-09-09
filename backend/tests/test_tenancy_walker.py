"""Every parametrized route, driven with the wrong tenant's token.

Tenancy here is enforced per handler, which means it is enforced by whoever
remembered. That is not a property, it is a habit -- and habits regress. This
module turns it into a property two ways:

1. **The walk.** Every route below is called with org B's token against org A's
   ids and must answer 404. Never 403: a 403 says "this exists and is not
   yours", which confirms the resource to someone who should not know it is
   there. 404 is indistinguishable from a genuinely missing row.

2. **The meta-test.** `test_every_parametrized_route_is_covered` reads
   `app.routes` and fails if any route is missing from `ROUTES` or `UNSCOPED`.
   That is the part that matters: it means the next endpoint someone adds
   cannot quietly skip the check, because the suite goes red until they
   classify it.
"""

from __future__ import annotations

import pytest

from datetime import date as _d

API = "/api/v1"

# Routes with no tenant to leak. Each needs a one-line reason -- an entry here
# is a decision to skip the check, so it should be uncomfortable to add.
UNSCOPED: dict[str, str] = {
    "GET /api/v1/downloads/{token}": (
        "Deliberately unauthenticated: the grant IS the authorisation. It is minted "
        "for one document, one org and one user by an authenticated request, expires "
        "in two minutes and is burned on first use, and the handler re-checks the "
        "grant's org against the document. A bearer check here would stop the link "
        "being followable by a browser while adding nothing the grant does not. "
        "Covered by tests/test_downloads.py."
    ),
}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org_a_resources(app_client, two_orgs):
    """One row of every addressable kind, owned by org A.

    Inserted directly rather than driven through the API: this module is about
    the read path, and going through the API would make the fixture depend on
    the very endpoints under test.
    """
    from app.db import SessionLocal
    from app.models import (
        ClinicalDocument, CmcDeliverable, CmcDocument, CmcExport, CmcProject,
        CmcResult, CmcSection, CmcSite,
        Conversation, CsrDocument, CsrProject, CsrSection, Customer, DocumentReview, DocumentVersion, DraftDocument,
        GeneratedDocument,
        GenerationJob, Invoice, Mapping, Project, PvDocument, PvProduct,
        PvReportInstance, PvRsiVersion, ReviewComment, ReviewTask, SourceFile, Study,
        SourceVersion,
        TemplateBlueprint,
        TemplateBlueprintVersion, TemplateFile, TemplateLibrary, TemplateLibraryVersion,
        TemplateManifest, TemplateVersion, User,
    )

    token_a, project_a, token_b, _ = two_orgs
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "user-a@tenant.test").one()
        org = user.org_id

        tf = TemplateFile(org_id=org, project_id=project_a, name="t.docx", status="ready", created_by=user.id)
        sf = SourceFile(org_id=org, project_id=project_a, name="s.csv", file_type="csv", status="ready", created_by=user.id)
        draft = DraftDocument(org_id=org, project_id=project_a, name="d", created_by=user.id)
        gd = GeneratedDocument(org_id=org, project_id=project_a, display_id=98001, language="en", status="draft")
        conv = Conversation(org_id=org, project_id=project_a, title="c", user_id=user.id)
        tl = TemplateLibrary(org_id=org, name="lib", category="HR", created_by=user.id)
        job = GenerationJob(org_id=org, project_id=project_a, status="completed", languages=["en"], created_by=user.id)
        db.add_all([tf, sf, draft, gd, conv, tl, job])
        db.flush()

        tv = TemplateVersion(template_file_id=tf.id, org_id=org, version_no=1, blob_path="x.docx", created_by=user.id)
        sv = SourceVersion(source_file_id=sf.id, org_id=org, version_no=1, blob_path="x.csv", created_by=user.id)
        dv = DocumentVersion(document_id=gd.id, org_id=org, version_no=1, blob_path="x.docx", html_content="<p>x</p>",
                             status="draft", created_by=user.id)
        tlv = TemplateLibraryVersion(template_library_id=tl.id, org_id=org, version_no=1, content_html="<p>x</p>",
                                     source_fields=[], created_by=user.id)
        db.add_all([tv, sv, dv, tlv])
        db.flush()

        tf.current_version_id, sf.current_version_id = tv.id, sv.id
        gd.current_version_id, tl.current_version_id = dv.id, tlv.id

        # These reference a *version*, so they come after the flush above.
        mapping = Mapping(org_id=org, draft_id=draft.id, template_version_id=tv.id,
                          action="ai_generate", ui_action="replace", section_ids=[], source_version_ids=[],
                          field_mappings={}, params={}, created_by=user.id)
        manifest = TemplateManifest(org_id=org, template_file_id=tf.id, template_version_id=tv.id,
                                    version_no=1, status="draft", fields=[], conditions=[],
                                    blocks=[], delete_always=[], confidence=1.0,
                                    compiled_by="rule_based", prescan_summary={}, created_by=user.id)
        db.add_all([mapping, manifest])
        db.flush()

        task = ReviewTask(org_id=org, project_id=project_a, manifest_id=manifest.id, unit_id="u",
                          kind="condition", question="?", context={}, status="open")
        blueprint = TemplateBlueprint(org_id=org, project_id=project_a, name="bp", kind="legacy",
                                      status="draft", source_template_version_id=tv.id,
                                      template_file_id=tf.id, created_by=user.id)
        db.add_all([task, blueprint])
        db.flush()

        bp_version = TemplateBlueprintVersion(
            blueprint_id=blueprint.id, org_id=org, version_no=1,
            body={"blocks": [], "sect_pr_from": None}, objects=[], findings=[],
            provenance={}, created_by=user.id)
        db.add(bp_version)
        db.flush()
        blueprint.current_version_id = bp_version.id

        review = DocumentReview(org_id=org, document_id=gd.id, document_version_id=dv.id,
                                state="open", reason="not right", requested_by=user.id,
                                authored_by=user.id)
        db.add(review)
        db.flush()
        comment = ReviewComment(org_id=org, review_id=review.id, author_id=user.id, body="here")
        db.add(comment)
        db.flush()

        customer = Customer(org_id=org, name="Acme Traders", created_by=user.id)
        db.add(customer)
        db.flush()
        invoice = Invoice(org_id=org, project_id=project_a, number="INV-9001",
                          customer_id=customer.id, customer_snapshot={"name": "Acme Traders"},
                          currency="USD", created_by=user.id)
        db.add(invoice)
        db.flush()

        study = Study(org_id=org, protocol_number="PROTO-9001", title="A study",
                      created_by=user.id)
        db.add(study)
        db.flush()
        clinical_doc = ClinicalDocument(org_id=org, project_id=project_a,
                                        number="CSR-9001", study_id=study.id,
                                        study_snapshot={"protocol_number": "PROTO-9001"},
                                        document_type="csr", created_by=user.id)
        db.add(clinical_doc)
        db.flush()

        csr_project = CsrProject(org_id=org, project_id=project_a, study_id=study.id,
                                 created_by=user.id)
        db.add(csr_project)
        db.flush()
        csr_section = CsrSection(org_id=org, csr_project_id=csr_project.id,
                                 section_number="10.1", title="Disposition of Patients",
                                 sort_order=0)
        db.add(csr_section)
        db.flush()
        csr_document = CsrDocument(org_id=org, csr_project_id=csr_project.id,
                                   doc_type="protocol", original_filename="protocol.txt",
                                   storage_path="csr/none/none.txt", uploaded_by=user.id)
        db.add(csr_document)
        db.flush()

        cmc_project = CmcProject(org_id=org, project_id=project_a,
                                 product_name="Drug X Tablets", created_by=user.id)
        db.add(cmc_project)
        db.flush()
        cmc_site = CmcSite(org_id=org, cmc_project_id=cmc_project.id,
                           name="Pune Plant")
        cmc_deliverable = CmcDeliverable(org_id=org, cmc_project_id=cmc_project.id,
                                         doc_type_key="ctd_32p")
        db.add_all([cmc_site, cmc_deliverable])
        db.flush()
        cmc_section = CmcSection(org_id=org, cmc_deliverable_id=cmc_deliverable.id,
                                 section_code="P.5.1", title="Specification",
                                 sort_order=0)
        db.add(cmc_section)
        db.flush()
        cmc_document = CmcDocument(org_id=org, cmc_project_id=cmc_project.id,
                                   doc_type="coa", original_filename="coa.csv",
                                   storage_path="cmc/none/none.csv", uploaded_by=user.id)
        db.add(cmc_document)
        db.flush()
        from app.models import CmcBatch, CmcMaterial, CmcTest

        cmc_material = CmcMaterial(org_id=org, cmc_project_id=cmc_project.id,
                                   kind="drug_product", name="Drug product")
        db.add(cmc_material)
        db.flush()
        cmc_batch = CmcBatch(org_id=org, cmc_project_id=cmc_project.id,
                             material_id=cmc_material.id, batch_number="B-1")
        cmc_test = CmcTest(org_id=org, cmc_project_id=cmc_project.id,
                           material_id=cmc_material.id, test_name="Assay")
        db.add_all([cmc_batch, cmc_test])
        db.flush()
        cmc_result = CmcResult(org_id=org, cmc_project_id=cmc_project.id,
                               batch_id=cmc_batch.id, test_id=cmc_test.id,
                               value_text="99.2 %")
        db.add(cmc_result)
        db.flush()
        cmc_export = CmcExport(org_id=org, cmc_project_id=cmc_project.id,
                               granularity="combined", created_by=user.id)
        db.add(cmc_export)
        db.flush()

        # The Safety/PV module. One row of everything a route addresses --
        # including a report instance, because half the module's surface hangs
        # off an interval rather than off the product.
        pv_product = PvProduct(org_id=org, project_id=project_a,
                               product_name="Tenantazole", created_by=user.id)
        db.add(pv_product)
        db.flush()
        pv_rsi = PvRsiVersion(org_id=org, pv_product_id=pv_product.id,
                              rsi_type="ccds", version_label="1.0",
                              created_by=user.id)
        db.add(pv_rsi)
        db.flush()
        pv_report = PvReportInstance(
            org_id=org, pv_product_id=pv_product.id, doc_type_key="dsur",
            period_start=_d(2025, 1, 1), period_end=_d(2025, 12, 31),
            data_lock_point=_d(2025, 12, 31), created_by=user.id)
        db.add(pv_report)
        db.flush()
        pv_document = PvDocument(
            org_id=org, pv_product_id=pv_product.id, doc_type="other",
            input_type="line_listing", original_filename="listing.csv",
            blob_path="pv/none/none.csv", uploaded_by=user.id)
        db.add(pv_document)
        db.flush()

        ids = {
            "project_id": project_a, "template_id": tf.id, "template_file_id": tf.id,
            "source_id": sf.id, "draft_id": draft.id, "document_id": gd.id,
            "conversation_id": conv.id, "library_id": tl.id, "job_id": job.id,
            "mapping_id": mapping.id, "manifest_id": manifest.id, "task_id": task.id,
            "generation_id": "no-such-generation", "version_id": dv.id,
            "source_version_id": sv.id, "template_version_id": tv.id,
            "blueprint_id": blueprint.id,
            "review_id": review.id, "comment_id": comment.id,
            "customer_id": customer.id, "invoice_id": invoice.id,
            "study_id": study.id, "clinical_document_id": clinical_doc.id,
            "csr_project_id": csr_project.id, "csr_section_id": csr_section.id,
            "csr_document_id": csr_document.id,
            "cmc_project_id": cmc_project.id, "cmc_site_id": cmc_site.id,
            "cmc_deliverable_id": cmc_deliverable.id, "cmc_section_id": cmc_section.id,
            "cmc_document_id": cmc_document.id, "cmc_result_id": cmc_result.id,
            "entity": "results", "table_key": "spec_table",
            "cmc_export_id": cmc_export.id,
            "pv_product_id": pv_product.id, "rsi_version_id": pv_rsi.id,
            "report_instance_id": pv_report.id,
            "pv_document_id": pv_document.id,
        }
        db.commit()
    finally:
        db.close()
    return token_a, token_b, ids


# Each entry is the *literal* route path (so the meta-test below can match it)
# plus whatever it takes to reach the handler. `ids` overrides a placeholder
# where the route's own name is ambiguous -- `{version_id}` means a document
# version on one route and a source version on another.
DOCX = ("f.docx", b"PK\x03\x04broken", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

ROUTES: list[dict] = [
    {"method": "GET", "path": "/projects/{project_id}"},
    {"method": "PATCH", "path": "/projects/{project_id}", "body": {"name": "x"}},
    {"method": "POST", "path": "/projects/{project_id}/archive"},
    {"method": "DELETE", "path": "/projects/{project_id}"},
    {"method": "GET", "path": "/projects/{project_id}/templates"},
    {"method": "POST", "path": "/projects/{project_id}/templates", "files": {"file": DOCX}},
    {"method": "POST", "path": "/projects/{project_id}/templates:bulk-onboard", "files": [("files", DOCX)]},
    {"method": "GET", "path": "/projects/{project_id}/sources"},
    {"method": "POST", "path": "/projects/{project_id}/sources", "files": {"file": ("f.csv", b"a,b\n1,2\n", "text/csv")}},
    {"method": "GET", "path": "/projects/{project_id}/documents"},
    {"method": "GET", "path": "/projects/{project_id}/conversations"},
    {"method": "POST", "path": "/projects/{project_id}/conversations", "body": {"title": "x"}},
    {"method": "GET", "path": "/projects/{project_id}/template-clusters"},

    {"method": "GET", "path": "/templates/{template_id}"},
    {"method": "DELETE", "path": "/templates/{template_id}"},
    {"method": "GET", "path": "/templates/{template_file_id}/manifests"},
    {"method": "POST", "path": "/templates/{template_file_id}/compile-manifest"},
    {"method": "POST", "path": "/templates/{template_file_id}/inherit-manifest", "body": {}},
    {"method": "GET", "path": "/template-versions/{version_id}/sections", "ids": {"version_id": "template_version_id"}},

    # Template authoring. Every one of these reaches a customer's own document --
    # `:emit` reads the uploaded original off disk and writes a new template
    # version from it -- so a blueprint belonging to another organisation has to
    # be a 404 before any file is opened, not after.
    {"method": "GET", "path": "/template-blueprints/{blueprint_id}"},
    {"method": "DELETE", "path": "/template-blueprints/{blueprint_id}"},
    {"method": "GET", "path": "/template-blueprints/{blueprint_id}/versions"},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}/versions",
     "body": {"body": {"blocks": [], "sect_pr_from": None}}},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}:revert-to",
     "body": {"version_no": 1}},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}:emit"},
    {"method": "GET", "path": "/template-blueprints/{blueprint_id}/lint"},
    {"method": "GET", "path": "/template-blueprints/{blueprint_id}/docx"},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}:publish", "body": {}},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}:archive"},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}/copilot",
     "body": {"message": "rename the salary field"}},
    {"method": "POST", "path": "/template-blueprints/{blueprint_id}/operations",
     "body": {"ops": []}},

    {"method": "GET", "path": "/sources/{source_id}"},
    {"method": "GET", "path": "/sources/{source_id}/fields"},
    {"method": "DELETE", "path": "/sources/{source_id}"},
    {"method": "GET", "path": "/source-versions/{version_id}/chunks", "ids": {"version_id": "source_version_id"}},
    {"method": "GET", "path": "/source-versions/{version_id}/records", "ids": {"version_id": "source_version_id"}},


    {"method": "GET", "path": "/documents/{document_id}"},
    {"method": "DELETE", "path": "/documents/{document_id}"},
    {"method": "PATCH", "path": "/documents/{document_id}/workflow",
     "body": {"workflow_status": "completed"}},
    {"method": "GET", "path": "/documents/{document_id}/versions"},
    {"method": "GET", "path": "/document-versions/{version_id}"},
    {"method": "GET", "path": "/document-versions/{version_id}/citations"},
    {"method": "GET", "path": "/document-versions/{version_id}/download"},
    {"method": "PATCH", "path": "/document-versions/{version_id}", "body": {"html_content": "<p>x</p>"}},
    {"method": "POST", "path": "/document-versions/{version_id}:approve"},
    # The text editor. Every one goes through `owned_document_version`, so a
    # version belonging to another organisation is a 404 before any file is
    # opened -- which matters more here than elsewhere, because two of these
    # read the document off disk and the third writes a new one.
    {"method": "GET", "path": "/document-versions/{version_id}/text"},
    {"method": "POST", "path": "/document-versions/{version_id}/text",
     "body": {"edits": [{"paragraph_index": 0, "span_index": 0, "text": "x"}]}},
    {"method": "POST", "path": "/document-versions/{version_id}/suggest-edit",
     "body": {"selection": "x", "instruction": "y"}},
    {"method": "POST", "path": "/document-versions/{version_id}:revoke",
     "body": {"reason": "changed my mind"}},

    {"method": "GET", "path": "/conversations/{conversation_id}/messages"},
    {"method": "POST", "path": "/conversations/{conversation_id}/messages", "body": {"text": "hi"}},

    {"method": "GET", "path": "/review-tasks/{task_id}"},
    {"method": "POST", "path": "/review-tasks/{task_id}:resolve", "body": {"resolved_value": "x", "rationale": "y"}},
    {"method": "POST", "path": "/review-tasks/{task_id}:dismiss", "body": {"rationale": "y"}},

    # Document reviews -- a person objecting, as opposed to the engine asking.
    {"method": "POST", "path": "/document-versions/{version_id}/reviews", "body": {"reason": "no"}},
    {"method": "GET", "path": "/document-versions/{version_id}/reviews"},
    {"method": "POST", "path": "/document-versions/{version_id}:request-changes",
     "body": {"reason": "no"}},
    {"method": "GET", "path": "/reviews/{review_id}"},
    {"method": "POST", "path": "/reviews/{review_id}/comments", "body": {"body": "x"}},
    {"method": "POST", "path": "/reviews/{review_id}/comments/{comment_id}:resolve"},
    {"method": "POST", "path": "/reviews/{review_id}:approve", "body": {}},
    {"method": "POST", "path": "/reviews/{review_id}:reject", "body": {"note": "fix it"}},
    {"method": "POST", "path": "/reviews/{review_id}:withdraw"},
    {"method": "POST", "path": "/reviews/{review_id}:assign", "body": {"user_id": None}},

    {"method": "GET", "path": "/template-manifests/{manifest_id}"},
    {"method": "PATCH", "path": "/template-manifests/{manifest_id}", "body": {"fields": []}},
    {"method": "POST", "path": "/template-manifests/{manifest_id}:approve"},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/diff", "query": {"against": "x"}},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/bindings"},
    {"method": "POST", "path": "/template-manifests/{manifest_id}/bindings",
     "body": {"source_version_id": "x", "field_bindings": {}, "value_map": {}}},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/binding-suggestions",
     "query": {"source_version_id": "x"}},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/preview"},
    {"method": "POST", "path": "/template-manifests/{manifest_id}/preview-row",
     "body": {"source_version_id": "x", "row_index": 0}},
    {"method": "POST", "path": "/template-manifests/{manifest_id}/generate", "body": {"source_record": {}}},
    {"method": "POST", "path": "/template-manifests/{manifest_id}/generate-batch",
     "body": {"source_version_id": "x"}},

    {"method": "GET", "path": "/template-library/{library_id}/content"},
    {"method": "PATCH", "path": "/template-library/{library_id}/content", "body": {"content_html": "<p>x</p>"}},
    {"method": "GET", "path": "/template-library/{library_id}/versions"},
    {"method": "POST", "path": "/template-library/{library_id}/generate",
     "body": {"project_id": "x", "language": "en"}},

    {"method": "GET", "path": "/jobs/{job_id}"},
    {"method": "GET", "path": "/jobs/{job_id}/download"},
    {"method": "GET", "path": "/manifest-generations/{generation_id}"},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/validation"},
    {"method": "GET", "path": "/template-manifests/{manifest_id}/source-template"},
    {"method": "POST", "path": "/document-versions/{version_id}/download-url"},
    {
        "method": "POST", "path": "/template-manifests/{manifest_id}/warnings:resolve",
        "body": {"code": "W-HL-GAP", "note": "reviewed"},
    },

    # The invoice service. The registry rows carry customer names, addresses,
    # tax ids and money, so a wrong-tenant read here is a leak of exactly the
    # data §16 is about.
    {"method": "GET", "path": "/customers/{customer_id}"},
    {"method": "PATCH", "path": "/customers/{customer_id}", "body": {"name": "x"}},
    {"method": "DELETE", "path": "/customers/{customer_id}"},
    {"method": "GET", "path": "/invoices/{invoice_id}"},
    {"method": "POST", "path": "/invoices/{invoice_id}:void"},

    # The clinical service. Study rows carry protocol numbers, sponsors and
    # investigators; document rows carry the study snapshot -- the same class
    # of tenant data as the invoice registry, with the same stakes.
    {"method": "GET", "path": "/studies/{study_id}"},
    {"method": "PATCH", "path": "/studies/{study_id}", "body": {"title": "x"}},
    {"method": "DELETE", "path": "/studies/{study_id}"},
    {"method": "GET", "path": "/clinical-documents/{clinical_document_id}"},
    {"method": "POST", "path": "/clinical-documents/{clinical_document_id}:void"},

    # The CSR module. Its rows name studies, compounds and (in later
    # milestones) patient-bearing source documents -- the strictest data in
    # the product, behind the same 404-never-403 rule as everything else.
    {"method": "GET", "path": "/csr/projects/{csr_project_id}"},
    {"method": "DELETE", "path": "/csr/projects/{csr_project_id}"},
    {"method": "POST", "path": "/csr/projects/{csr_project_id}/template",
     "body": {"source": "builtin_ich_e3"}},
    {"method": "GET", "path": "/csr/projects/{csr_project_id}/sections"},
    {"method": "PATCH", "path": "/csr/sections/{csr_section_id}", "body": {"enabled": False}},
    # The CSR module's sources and drafts. These carry the study documents
    # themselves -- protocols, safety narratives, patient-level listings --
    # so a wrong-tenant read here is the leak the whole module is careful
    # about, and a wrong-tenant WRITE would put one sponsor's protocol into
    # another's report.
    {"method": "GET", "path": "/csr/projects/{csr_project_id}/documents"},
    {"method": "POST", "path": "/csr/projects/{csr_project_id}/documents",
     "files": [("files", ("x.txt", b"x", "text/plain")), ("doc_types", (None, "protocol"))]},
    {"method": "POST", "path": "/csr/projects/{csr_project_id}/process"},
    {"method": "GET", "path": "/csr/projects/{csr_project_id}/processing-status"},
    {"method": "PATCH", "path": "/csr/documents/{csr_document_id}", "body": {"doc_type": "sap"}},
    {"method": "DELETE", "path": "/csr/documents/{csr_document_id}"},
    {"method": "POST", "path": "/csr/documents/{csr_document_id}/retry"},
    {"method": "POST", "path": "/csr/sections/{csr_section_id}/generate", "body": {}},
    {"method": "GET", "path": "/csr/sections/{csr_section_id}/draft"},
    {"method": "PUT", "path": "/csr/sections/{csr_section_id}/draft", "body": {"content": "x"}},
    {"method": "PATCH", "path": "/csr/sections/{csr_section_id}/status", "body": {"status": "draft"}},

    # The Quality/CMC module. Its rows carry manufacturing sites, specification
    # limits and batch results -- trade secrets and confidential business
    # information, which is the strictest data the product holds.
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}"},
    {"method": "PATCH", "path": "/cmc/projects/{cmc_project_id}", "body": {"dosage_form": "x"}},
    {"method": "DELETE", "path": "/cmc/projects/{cmc_project_id}"},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/sites"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/sites", "body": {"name": "x"}},
    {"method": "PATCH", "path": "/cmc/sites/{cmc_site_id}", "body": {"name": "x"}},
    {"method": "DELETE", "path": "/cmc/sites/{cmc_site_id}"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/deliverables",
     "body": {"doc_type_key": "ctd_32p"}},
    {"method": "GET", "path": "/cmc/deliverables/{cmc_deliverable_id}/sections"},
    {"method": "DELETE", "path": "/cmc/deliverables/{cmc_deliverable_id}"},
    {"method": "PATCH", "path": "/cmc/sections/{cmc_section_id}", "body": {"enabled": False}},
    # The sources and the structured store. These carry specification limits,
    # batch results and manufacturing detail -- confidential business
    # information, and the reason the module's own copy says so on every screen.
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/documents"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/documents",
     "files": [("files", ("x.csv", b"a,b", "text/csv")), ("doc_types", (None, "coa"))]},
    {"method": "PATCH", "path": "/cmc/documents/{cmc_document_id}", "body": {"doc_type": "coa"}},
    {"method": "DELETE", "path": "/cmc/documents/{cmc_document_id}"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/process"},
    {"method": "POST", "path": "/cmc/documents/{cmc_document_id}/retry"},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/processing-status"},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/materials"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/materials",
     "body": {"kind": "drug_product", "name": "x"}},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/data/{entity}"},
    {"method": "PATCH", "path": "/cmc/results/{cmc_result_id}", "body": {"value_text": "1"}},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/results:verify",
     "body": {"all_unverified": True}},
    {"method": "POST", "path": "/cmc/results/{cmc_result_id}:resolve",
     "body": {"keep_result_id": "x"}},
    # Rendering, drafting and export. These reach the deepest into the store --
    # a table renders every verified value a project holds, and an export
    # writes them into a file somebody sends to a regulator.
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/tables/{table_key}"},
    {"method": "POST", "path": "/cmc/sections/{cmc_section_id}/generate", "body": {}},
    {"method": "GET", "path": "/cmc/sections/{cmc_section_id}/draft"},
    {"method": "PUT", "path": "/cmc/sections/{cmc_section_id}/draft", "body": {"content": "x"}},
    {"method": "PATCH", "path": "/cmc/sections/{cmc_section_id}/status",
     "body": {"status": "draft"}},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/qc"},
    {"method": "POST", "path": "/cmc/projects/{cmc_project_id}/export",
     "body": {"granularity": "combined"}},
    {"method": "GET", "path": "/cmc/projects/{cmc_project_id}/exports"},
    {"method": "GET", "path": "/cmc/exports/{cmc_export_id}/download"},

    # Safety / Pharmacovigilance.
    {"method": "GET", "path": "/pv/products/{pv_product_id}"},
    {"method": "PATCH", "path": "/pv/products/{pv_product_id}",
     "body": {"inn": "x"}},
    {"method": "DELETE", "path": "/pv/products/{pv_product_id}"},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/members"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/members",
     "body": {"user_id": "nobody", "pv_role": "writer"}},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/rsi-versions"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/rsi-versions",
     "body": {"rsi_type": "ccds", "version_label": "9.9"}},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/reports"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/reports",
     "body": {"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-06-30"}},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/scope-preview",
     "body": {"doc_type_key": "dsur", "period_start": "2026-01-01",
              "period_end": "2026-06-30", "data_lock_point": "2026-06-30"}},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/calendar"},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/approval-statuses"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/approval-statuses",
     "body": {"country": "DE"}},
    {"method": "GET", "path": "/pv/rsi-versions/{rsi_version_id}/listed-terms"},
    {"method": "POST", "path": "/pv/rsi-versions/{rsi_version_id}/listed-terms",
     "body": [{"meddra_pt": "Headache"}]},
    {"method": "POST", "path": "/pv/rsi-versions/{rsi_version_id}/pin"},
    {"method": "GET", "path": "/pv/reports/{report_instance_id}"},
    {"method": "PATCH", "path": "/pv/reports/{report_instance_id}",
     "body": {"meddra_version": "27.0"}},
    {"method": "DELETE", "path": "/pv/reports/{report_instance_id}"},
    {"method": "GET", "path": "/pv/reports/{report_instance_id}/sections"},
    {"method": "GET", "path": "/pv/reports/{report_instance_id}/preview-scope"},
    {"method": "POST", "path": "/pv/reports/{report_instance_id}/due-dates",
     "body": {"region": "EU"}},

    # Safety M2: sources and the case store.
    {"method": "GET", "path": "/pv/products/{pv_product_id}/documents"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/documents",
     "files": [("files", ("listing.csv", b"a,b\n1,2\n", "text/csv")),
               ("doc_types", (None, "other")),
               ("input_types", (None, "document"))]},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/process",
     "body": {"mappings": {}}},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/processing-status"},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/cases"},
    {"method": "GET", "path": "/pv/products/{pv_product_id}/mapping-profiles"},
    {"method": "POST", "path": "/pv/products/{pv_product_id}/mapping-profiles",
     "body": {"name": "p", "column_map": {"Case ID": "worldwide_case_id"}}},
    {"method": "PATCH", "path": "/pv/documents/{pv_document_id}",
     "body": {"doc_type": "other"}},
    {"method": "DELETE", "path": "/pv/documents/{pv_document_id}"},
    {"method": "POST", "path": "/pv/documents/{pv_document_id}/retry",
     "body": {"mappings": {}}},
    {"method": "GET", "path": "/pv/documents/{pv_document_id}/columns"},
]


def _key(method: str, path: str) -> str:
    return f"{method} {API}{path}"


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r['method']} {r['path']}")
def test_the_other_tenant_gets_404(app_client, org_a_resources, route):
    _token_a, token_b, ids = org_a_resources
    resolved = {**ids, **{k: ids[v] for k, v in route.get("ids", {}).items()}}
    url = API + route["path"].format(**resolved)

    response = app_client.request(
        route["method"], url, headers=_auth(token_b),
        json=route.get("body"), files=route.get("files"), params=route.get("query"),
    )

    assert response.status_code != 403, (
        f"{route['method']} {route['path']} answered 403 -- that confirms the resource "
        "exists to a tenant who should not be able to tell. Return 404."
    )
    assert response.status_code == 404, (
        f"{route['method']} {route['path']} answered {response.status_code}, expected 404. "
        f"Body: {response.text[:300]}"
    )


def test_every_parametrized_route_is_covered(app_client):
    """The guardrail. A new endpoint with a path parameter goes red here until
    someone classifies it -- which is the only reliable way this list stays
    current."""
    from app.main import app

    live = {
        f"{method} {route.path}"
        for route in app.routes
        if getattr(route, "path", None) and "{" in route.path
        for method in (route.methods - {"HEAD", "OPTIONS"})
    }
    covered = {_key(r["method"], r["path"]) for r in ROUTES} | set(UNSCOPED)

    unclassified = live - covered
    assert not unclassified, (
        "parametrized routes with no tenancy check:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nAdd each to ROUTES, or to UNSCOPED with a reason."
    )


def test_unscoped_entries_all_carry_a_reason():
    assert all(reason.strip() for reason in UNSCOPED.values()), "an UNSCOPED entry needs a justification"


# --------------------------------------------------------- the RLS blind spot

def test_every_unscoped_route_sets_the_tenant_itself():
    """An unauthenticated route must scope its own session, or RLS blocks it.

    Every other route gets `app.current_org` set inside `security.get_current_user`.
    A route in UNSCOPED has, by definition, no current user -- so unless it calls
    `set_current_org` itself, its session reaches PostgreSQL with no tenant, every
    row-level security policy evaluates against nothing, and its queries come back
    empty. The endpoint 404s for every legitimate caller.

    This suite cannot catch that by running the route: it runs on SQLite, where
    RLS is a no-op and the handler works perfectly. The failure appears only in
    production. So the check is structural -- the handler's module must reach for
    the tenant on its own.
    """
    import ast
    import inspect

    from app.main import app

    offenders = []
    for key in UNSCOPED:
        method, _, path = key.partition(" ")
        route = next(
            (r for r in app.routes
             if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set())),
            None,
        )
        assert route is not None, f"UNSCOPED names {key}, which is not a live route"

        # Look for an actual CALL, not the name. A substring check passes on the
        # import line alone, which is how this guard first shipped vacuous: the
        # call could be deleted and the test stayed green.
        tree = ast.parse(inspect.getsource(inspect.getmodule(route.endpoint)))
        calls_it = any(
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "set_current_org"
            for node in ast.walk(tree)
        )
        if not calls_it:
            offenders.append(key)

    assert not offenders, (
        "unauthenticated routes whose module never calls set_current_org:\n  "
        + "\n  ".join(offenders)
        + "\n\nOn PostgreSQL these reach the database with no tenant set, so row-level "
          "security returns nothing and the endpoint 404s for every valid caller. "
          "Scope the session from whatever authorised the request."
    )
