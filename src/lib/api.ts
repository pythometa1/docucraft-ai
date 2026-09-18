/**
 * Thin client for the DocuMind AI backend (see backend/ and docs/BACKEND_SPEC.md).
 *
 * Every request carries a token obtained by a real user typing real credentials
 * into /login. This file used to hold a hardcoded demo email and password and
 * sign itself in on first use, which meant the app could never actually be
 * unauthenticated -- authentication was decorative, and no failure in it was
 * observable. Now an expired or missing token surfaces as NOT_AUTHENTICATED and
 * the router sends the user back to the sign-in screen.
 */

import type {
  AnalyticsKpis, AnalyticsRange, Blueprint, BlueprintBody, BlueprintVersion,
  ClinicalDocGenerated, ClinicalDocSummary, CompileReport,
  CmcBatchRow, CmcDataSummary, CmcDeliverable, CmcDeliverableType, CmcDocument,
  CmcDraft, CmcExportRecord, CmcFinding, CmcMaterial, CmcProject, CmcReadiness,
  CmcRenderedTable, CmcResultRow, CmcSection, CmcSite, CmcSource, CmcTestRow,
  CsrDocument, CsrDraft, CsrProject, CsrReadiness, CsrSection, CsrSource,
  CostReport, Customer, InvoiceGenerated, InvoiceSummary, LintReport, QualityReport,
  PvApprovalStatus, PvDueDate, PvMember, PvProduct, PvReportInstance, PvReportType,
  PvCaseEvent, PvCaseRow, PvDeidGate, PvDeidItem, PvDuplicatePair,
  PvEventSummary, PvMappingProfile, PvReadiness,
  PvRsiVersion, PvScopePreview, PvSection, PvSource,
  SettableWorkflowStatus, Study, TopTemplates, TrendSeries,
} from "@/lib/types";

const API_URL = (import.meta as any).env?.VITE_API_URL ?? "http://localhost:8000/api/v1";
const TOKEN_KEY = "dm.api.token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

function setToken(token: string) {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
}

/** Exchange real credentials for a token. The only way one is ever obtained. */
async function login(email: string, password: string): Promise<void> {
  const res = await fetch(`${API_URL}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const isJson = (res.headers.get("content-type") ?? "").includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) throw toApiError(res.status, isJson, data);
  setToken(data.access_token);
}

/** Reads the cached token without validating it -- a stale or since-revoked one
 * is detected when a real request comes back 401, which `request()` handles by
 * clearing it and surfacing NOT_AUTHENTICATED. */
function ensureAuth(): string {
  const token = getToken();
  if (!token) throw new ApiError(401, "NOT_AUTHENTICATED", "Your session has ended. Please sign in again.");
  return token;
}

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/**
 * FastAPI emits four different error shapes and the backend uses three of them,
 * so unwrapping only one leaves the rest surfacing as "[object Object]" in a toast:
 *   security.error()          -> {detail: {error: {code, message, details}}}
 *   unhandled-exception 500   -> {error: {code, message, details}}
 *   request validation (422)  -> {detail: [{loc, msg, type}, ...]}
 *   bare HTTPException        -> {detail: "some string"}
 */
function toApiError(status: number, isJson: boolean, data: any): ApiError {
  if (!isJson) return new ApiError(status, "UNKNOWN", String(data));

  const envelope = data?.detail?.error ?? data?.error;
  if (envelope?.message) return new ApiError(status, envelope.code ?? "UNKNOWN", envelope.message);

  const detail = data?.detail;
  if (typeof detail === "string") return new ApiError(status, "UNKNOWN", detail);
  if (Array.isArray(detail)) {
    const message = detail
      .map((d: any) => [(d?.loc ?? []).slice(1).join("."), d?.msg].filter(Boolean).join(": "))
      .filter(Boolean)
      .join("; ");
    return new ApiError(status, "VALIDATION_ERROR", message || "Request validation failed");
  }
  return new ApiError(status, "UNKNOWN", `Request failed with status ${status}`);
}

async function request<T>(method: string, path: string, opts: { json?: unknown; formData?: FormData; query?: Record<string, string | number | undefined> } = {}): Promise<T> {
  const token = ensureAuth();
  let url = `${API_URL}${path}`;
  if (opts.query) {
    const qs = new URLSearchParams(Object.entries(opts.query).filter(([, v]) => v != null) as [string, string][]).toString();
    if (qs) url += `?${qs}`;
  }
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  let body: BodyInit | undefined;
  if (opts.formData) {
    body = opts.formData;
  } else if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  }
  const res = await fetch(url, { method, headers, body });

  // A cached token can go bad without any client-side signal (backend secret
  // rotated, revoked by a logout elsewhere, natural expiry). Drop it and make
  // the caller re-authenticate -- there are no credentials here to retry with,
  // which is the point.
  if (res.status === 401) {
    clearToken();
    // Bounce to the sign-in screen from here rather than leaving every caller
    // to handle it: an expired token otherwise shows up as a generic toast on
    // whichever request happened to fire first, and the app sits there broken.
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      const redirect = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.assign(`/login?redirect=${redirect}`);
    }
    throw new ApiError(401, "NOT_AUTHENTICATED", "Your session has ended. Please sign in again.");
  }

  if (res.status === 204) return undefined as T;
  const isJson = (res.headers.get("content-type") ?? "").includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) throw toApiError(res.status, isJson, data);
  return data as T;
}

/** The server's own message, when it sent one. A download failure is usually
 *  something the user can act on -- "LibreOffice is not installed on this host"
 *  -- and replacing it with "Could not download" throws that away. */
async function downloadError(res: Response, format: string): Promise<ApiError> {
  let message = `Could not download this document as ${format}.`;
  let code = "DOWNLOAD_FAILED";
  try {
    const body = await res.json();
    const err = body?.detail?.error ?? body?.detail;
    if (err?.message) message = err.message;
    if (err?.code) code = err.code;
  } catch {
    /* not JSON; the default message stands */
  }
  return new ApiError(res.status, code, message);
}

export const api = {
  login,
  isAuthenticated: () => getToken() != null,
  async logout(): Promise<void> {
    // Best-effort revoke, then always drop the local token: a network failure
    // must not leave the user apparently still signed in.
    try {
      await request("POST", "/auth/logout");
    } catch {
      /* already invalid server-side */
    } finally {
      clearToken();
    }
  },
  downloadUrl(versionId: string) {
    return `${API_URL}/document-versions/${versionId}/download`;
  },
  async authedDownloadUrl(versionId: string): Promise<string> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/document-versions/${versionId}/download`, { headers: { Authorization: `Bearer ${token}` } });
    // Without this check a 404 or 401 is turned into a blob URL of the error
    // JSON and handed to the browser as a download: the user gets a file named
    // like their letter containing {"detail":"Not Found"}, and nothing anywhere
    // reports a failure.
    if (!res.ok) throw new ApiError(res.status, "DOWNLOAD_FAILED", "Could not download this document.");
    return URL.createObjectURL(await res.blob());
  },

  me: () => request<{
    id: string; full_name: string; email: string; org_id: string; role: string;
    job_title: string | null; timezone: string | null; function: string | null;
    /** What this role may do. Sent so the UI can disable what the server would
     *  refuse -- not a boundary; `require()` still checks every one. */
    capabilities: string[];
  }>("GET", "/me"),
  lookups: (kind: string, parent?: string) => request<{ items: string[] }>("GET", "/lookups", { query: { kind, parent } }),

  listProjects: (q?: string, opts: { status?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: any[]; total: number }>("GET", "/projects", {
      query: { q, status: opts.status, limit: opts.limit, offset: opts.offset },
    }),
  createProject: (body: { name: string; description?: string; region: string; function: string; document_type: string; language?: string }) =>
    request<any>("POST", "/projects", { json: body }),
  getProject: (id: string) => request<any>("GET", `/projects/${id}`),
  patchProject: (id: string, body: any) => request<any>("PATCH", `/projects/${id}`, { json: body }),
  archiveProject: (id: string) => request<any>("POST", `/projects/${id}/archive`),
  deleteProject: (id: string) => request<any>("DELETE", `/projects/${id}`),
  /** Several at once. Partial by design: an id that is already gone comes back
   *  in `refused`, and the rest are still removed. Like the single-project
   *  delete this is a `deleted_at` stamp, not a drop -- §16 leaves the
   *  destruction of blobs and embeddings to the retention sweep. */
  deleteProjects: (projectIds: string[]) =>
    request<BulkDeleteResult<"project_id">>("POST", "/projects:delete",
      { json: { project_ids: projectIds } }),
  deleteTemplates: (templateIds: string[]) =>
    request<BulkDeleteResult<"template_id">>("POST", "/templates:delete",
      { json: { template_ids: templateIds } }),
  deleteSources: (sourceIds: string[]) =>
    request<BulkDeleteResult<"source_id">>("POST", "/sources:delete",
      { json: { source_ids: sourceIds } }),

  listTemplates: (projectId: string) => request<{ items: any[] }>("GET", `/projects/${projectId}/templates`),
  uploadTemplate: (projectId: string, file: File, name?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (name) fd.append("name", name);
    return request<any>("POST", `/projects/${projectId}/templates`, { formData: fd });
  },
  getTemplateSections: (versionId: string) => request<{ items: any[] }>("GET", `/template-versions/${versionId}/sections`, { query: { tree: "true" } }),
  getTemplate: (templateId: string) => request<any>("GET", `/templates/${templateId}`),
  deleteTemplate: (templateId: string) => request<any>("DELETE", `/templates/${templateId}`),

  listSources: (projectId: string) => request<{ items: any[] }>("GET", `/projects/${projectId}/sources`),
  sourceFields: (sourceId: string) => request<{ items: string[] }>("GET", `/sources/${sourceId}/fields`),
  deleteSource: (sourceId: string) => request<any>("DELETE", `/sources/${sourceId}`),
  uploadSource: (projectId: string, file: File, name?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (name) fd.append("name", name);
    return request<any>("POST", `/projects/${projectId}/sources`, { formData: fd });
  },

  getJob: (jobId: string) => request<any>("GET", `/jobs/${jobId}`),
  listProjectDocuments: (projectId: string) => request<{ items: any[] }>("GET", `/projects/${projectId}/documents`),
  getDocument: (documentId: string) => request<any>("GET", `/documents/${documentId}`),
  deleteDocument: (documentId: string) => request<any>("DELETE", `/documents/${documentId}`),
  /** Move a document along someone's own lane.
   *
   *  Only the three a person may assert are accepted. `approved` and `blocked`
   *  are refused by the server with an explanation, because a signature is the
   *  approve action's to record and a QA verdict is the fill engine's -- so the
   *  select that calls this offers three options, not five. */
  setDocumentWorkflow: (documentId: string, workflowStatus: SettableWorkflowStatus) =>
    request<any>("PATCH", `/documents/${documentId}/workflow`, {
      json: { workflow_status: workflowStatus },
    }),
  getDocumentVersion: (versionId: string) =>
    request<{
      id: string; html_content: string; status: string;
      /** Why it is in that state -- a chip with no words sends the reader looking. */
      status_reason: string | null;
      version_no: number; renderer: string | null; html_editable: boolean;
      document_id: string; approved_by: string | null; approved_at: string | null;
      open_review_id: string | null;
    }>("GET", `/document-versions/${versionId}`),
  saveDocumentVersion: (versionId: string, html: string) => request<any>("PATCH", `/document-versions/${versionId}`, { json: { html_content: html } }),

  // --- downloads ----------------------------------------------------------
  //
  // `format=pdf` needs LibreOffice on the API host. When it is absent the server
  // answers 503 naming the package, which is a configuration fact rather than a
  // failure of the letter -- so the caller keeps .docx available and says why
  // PDF is not on offer.
  //
  // The `res.ok` check is not ceremony: without it a 404 or a 503 becomes a blob
  // URL of the error JSON, handed to the browser as a download. The user gets a
  // file named like their letter containing `{"detail":"Not Found"}` and nothing
  // anywhere reports a failure.
  async downloadVersion(versionId: string, format: "docx" | "pdf" = "docx"): Promise<string> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/document-versions/${versionId}/download?format=${format}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw await downloadError(res, format);
    return URL.createObjectURL(await res.blob());
  },

  /** Delete several at once. Partial by design: an approved document comes back
   *  in `refused` with the reason, and the rest are still deleted. */
  deleteDocuments: (documentIds: string[]) =>
    request<{
      requested: number;
      deleted: { document_id: string; filename: string | null }[];
      refused: { document_id: string; code: string; filename: string | null; reason: string }[];
      blobs_deleted: number;
    }>("POST", "/documents:delete", { json: { document_ids: documentIds } }),

  async downloadDocuments(documentIds: string[], format: "docx" | "pdf" = "docx"): Promise<string> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/documents:download`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds, format }),
    });
    if (!res.ok) throw await downloadError(res, format);
    return URL.createObjectURL(await res.blob());
  },

  // --- text editing -------------------------------------------------------
  //
  // A letter produced by filling a Word template cannot be rebuilt from HTML
  // without losing everything HTML has no word for -- section breaks, headers,
  // numbering, cell borders -- so `saveDocumentVersion` refuses it. These three
  // are what editing means instead: read the runs, ask a model to rewrite one,
  // write the words back into that same run and touch nothing else.
  documentText: (versionId: string) =>
    request<{
      version_id: string; document_id: string; version_no: number; status: string;
      renderer: string | null; text_editable: boolean; html_editable: boolean;
      paragraphs: { paragraph_index: number; in_table: boolean; text: string;
        spans: { paragraph_index: number; span_index: number; text: string; in_table: boolean; role: string }[] }[];
    }>("GET", `/document-versions/${versionId}/text`),

  // Returns replacement text, never a saved change. A person always accepts.
  suggestEdit: (versionId: string, body: { selection: string; instruction: string; paragraph_index?: number }) =>
    request<{ replacement: string; note: string; model: string }>(
      "POST", `/document-versions/${versionId}/suggest-edit`, { json: body }),

  // Writes a NEW version. An approved letter that changes under the same id is
  // not auditable.
  applyTextEdits: (versionId: string, edits: { paragraph_index: number; span_index: number; text: string }[], changeSummary?: string) =>
    request<{ version_id: string; version_no: number; change_summary: string;
      applied: { paragraph_index: number; span_index: number; before: string; after: string }[] }>(
      "POST", `/document-versions/${versionId}/text`, { json: { edits, change_summary: changeSummary } }),
  approveDocumentVersion: (versionId: string) => request<any>("POST", `/document-versions/${versionId}:approve`),
  revokeDocumentVersion: (versionId: string, body: { reason: string }) =>
    request<{ status: string; status_reason: string | null }>(
      "POST", `/document-versions/${versionId}:revoke`, { json: body }),

  listLibrary: (category?: string, q?: string) => request<{ items: any[] }>("GET", "/template-library", { query: { category, q } }),
  createLibraryEntry: (body: { name: string; category: string; content_html?: string }) => request<any>("POST", "/template-library", { json: body }),
  getLibraryContent: (id: string) => request<{ content_html: string; source_fields: string[]; version_no: number }>("GET", `/template-library/${id}/content`),
  saveLibraryContent: (id: string, html: string) => request<any>("PATCH", `/template-library/${id}/content`, { json: { content_html: html } }),
  convertLegacyText: (text: string) => request<{ candidates: any[] }>("POST", "/template-library:convert", { json: { text } }),
  generateFromLibrary: (libraryId: string, projectId: string) => request<any>("POST", `/template-library/${libraryId}/generate`, { json: { project_id: projectId } }),

  // Every one takes a range now. They always accepted the parameter and always
  // ignored it, so the page's four range buttons changed nothing.
  analyticsKpis: (range: AnalyticsRange = "30d") =>
    request<AnalyticsKpis>("GET", "/analytics/kpis", { query: { range } }),
  analyticsTrend: (range: AnalyticsRange = "30d") =>
    request<TrendSeries>("GET", "/analytics/trend", { query: { range } }),
  analyticsByFunction: (range: AnalyticsRange = "30d") =>
    request<{ items: { function: string; count: number }[] }>(
      "GET", "/analytics/by-function", { query: { range } }),
  analyticsTopTemplates: (range: AnalyticsRange = "30d") =>
    request<TopTemplates>("GET", "/analytics/top-templates", { query: { range } }),
  analyticsCost: (range: AnalyticsRange = "30d") =>
    request<CostReport>("GET", "/analytics/cost", { query: { range } }),
  analyticsCompiles: (range: AnalyticsRange = "30d") =>
    request<CompileReport>("GET", "/analytics/compiles", { query: { range } }),

  teamMembers: () => request<{ items: any[] }>("GET", "/team/members"),
  teamRolesSummary: () => request<{ items: { role: string; count: number }[] }>("GET", "/team/roles-summary"),
  auditLogs: () => request<{ items: any[] }>("GET", "/audit-logs"),
  /** The same endpoint, given the three parameters it has always accepted.
   *
   *  Added alongside `auditLogs()` rather than replacing it. The page's Filter
   *  button was decorative because nothing here could carry a filter through --
   *  but `auditLogs()` is a no-argument call already in use, and widening it
   *  would make every existing caller's request depend on defaults it never
   *  asked for. Two methods, one endpoint, no caller disturbed.
   *
   *  There is no `offset`: the server does not accept one. `limit` is the only
   *  way to reach further back, which is why the page raises a limit rather
   *  than paginating -- a Next button here could only ever be a lie. */
  auditLogEntries: (params: { entity_type?: string; severity?: string; limit?: number } = {}) =>
    request<{ items: AuditEntry[] }>("GET", "/audit-logs", {
      query: {
        entity_type: params.entity_type,
        severity: params.severity,
        limit: params.limit,
      },
    }),

  /** What this organisation has told us to keep, and where it may be processed.
   *
   *  MANAGE_USERS server-side, so most roles get a 403 here. Callers must treat
   *  that as "not visible to you" rather than as a failure: it is the answer,
   *  not an error. */
  dataPolicy: () => request<DataPolicy>("GET", "/admin/data-policy"),

  listConversations: (projectId: string) => request<{ items: any[] }>("GET", `/projects/${projectId}/conversations`),
  createConversation: (projectId: string, title: string) => request<any>("POST", `/projects/${projectId}/conversations`, { json: { title } }),
  listMessages: (conversationId: string) => request<{ items: any[] }>("GET", `/conversations/${conversationId}/messages`),
  sendMessage: (conversationId: string, text: string) => request<any>("POST", `/conversations/${conversationId}/messages`, { json: { text } }),

  /* ---- Invoices: the first per-industry service ----
   * A published invoice template plus typed-in values becomes a numbered,
   * stored invoice. Every figure the document prints is computed server-side
   * in Decimal; the totals this client shows while typing are a preview the
   * server re-derives, never the record. */
  invoiceWorkspace: () =>
    request<{ project_id: string; name: string; display_id: number }>("POST", "/invoices:workspace"),
  listCustomers: (q?: string) =>
    request<{ items: Customer[] }>("GET", "/customers", { query: { q } }),
  createCustomer: (body: Partial<Customer> & { name: string }) =>
    request<Customer>("POST", "/customers", { json: body }),
  updateCustomer: (id: string, body: Partial<Customer>) =>
    request<Customer>("PATCH", `/customers/${id}`, { json: body }),
  deleteCustomer: (id: string) =>
    request<{ deleted: boolean }>("DELETE", `/customers/${id}`),
  listInvoices: (params: { customer_id?: string; project_id?: string; status?: string } = {}) =>
    request<{ items: InvoiceSummary[] }>("GET", "/invoices", { query: params }),
  getInvoice: (id: string) =>
    request<InvoiceSummary & { source_record: Record<string, unknown> }>("GET", `/invoices/${id}`),
  voidInvoice: (id: string) =>
    request<InvoiceSummary>("POST", `/invoices/${id}:void`),
  generateInvoice: (body: {
    manifest_id: string;
    project_id?: string;
    customer_id?: string;
    customer?: { name: string; email?: string; phone?: string; address?: string; tax_id?: string };
    line_items: Record<string, unknown>[];
    fields?: Record<string, unknown>;
    currency?: string;
    tax_rate?: number;
    tax_split?: boolean;
    issue_date?: string;
    due_date?: string;
    locale?: string;
  }) => request<InvoiceGenerated>("POST", "/invoices:generate", { json: body }),
  /* ---- Clinical: the study book and numbered study documents ----
   * The same shape as the invoice service: a published clinical template plus
   * typed values becomes a numbered document (CSR-0001, PA-0001, ...). The
   * study snapshot and any derived table totals are computed server-side. */
  listStudies: (q?: string) =>
    request<{ items: Study[] }>("GET", "/studies", { query: { q } }),
  createStudy: (body: Partial<Study> & { protocol_number: string }) =>
    request<Study>("POST", "/studies", { json: body }),
  updateStudy: (id: string, body: Partial<Study>) =>
    request<Study>("PATCH", `/studies/${id}`, { json: body }),
  deleteStudy: (id: string) =>
    request<{ deleted: boolean }>("DELETE", `/studies/${id}`),
  listClinicalDocuments: (params: { study_id?: string; project_id?: string; document_type?: string; status?: string } = {}) =>
    request<{ items: ClinicalDocSummary[] }>("GET", "/clinical-documents", { query: params }),
  getClinicalDocument: (id: string) =>
    request<ClinicalDocSummary & { source_record: Record<string, unknown> }>("GET", `/clinical-documents/${id}`),
  voidClinicalDocument: (id: string) =>
    request<ClinicalDocSummary>("POST", `/clinical-documents/${id}:void`),
  generateClinicalDocument: (body: {
    manifest_id: string;
    document_type: string;
    project_id?: string;
    study_id?: string;
    study?: {
      protocol_number?: string; title?: string; sponsor?: string;
      phase?: string; indication?: string; principal_investigator?: string;
    };
    rows?: Record<string, unknown>[];
    fields?: Record<string, unknown>;
    title?: string;
    document_date?: string;
    version_label?: string;
    locale?: string;
  }) => request<ClinicalDocGenerated>("POST", "/clinical-documents:generate", { json: body }),
  /* ---- Quality/CMC: dossier sections and verified quality data ----
   * Two flows that never mix. Prose is drafted and cited like any section;
   * the numbers in a specification, a batch analysis or a stability table are
   * read from uploaded sources, verified by a person in the data grid, and
   * rendered deterministically. Nothing here ever asks a model for a value. */
  cmcListProjects: () =>
    request<{ items: CmcProject[] }>("GET", "/cmc/projects"),
  cmcCreateProject: (body: {
    project_id: string;
    product_name: string;
    inn_or_ds_name?: string;
    dosage_form?: string;
    strengths?: string[];
    route_of_administration?: string;
    submission_type?: string;
    target_regions?: string[];
    development_phase?: string;
  }) => request<CmcProject>("POST", "/cmc/projects", { json: body }),
  cmcGetProject: (id: string) =>
    request<CmcProject>("GET", `/cmc/projects/${id}`),
  cmcUpdateProject: (id: string, body: Record<string, unknown>) =>
    request<CmcProject>("PATCH", `/cmc/projects/${id}`, { json: body }),
  cmcDeleteProject: (id: string) =>
    request<{ deleted: boolean; purged: Record<string, number> }>("DELETE", `/cmc/projects/${id}`),

  cmcListSites: (id: string) =>
    request<{ items: CmcSite[] }>("GET", `/cmc/projects/${id}/sites`),
  cmcCreateSite: (id: string, body: Partial<CmcSite> & { name: string }) =>
    request<CmcSite>("POST", `/cmc/projects/${id}/sites`, { json: body }),
  cmcUpdateSite: (siteId: string, body: Partial<CmcSite>) =>
    request<CmcSite>("PATCH", `/cmc/sites/${siteId}`, { json: body }),
  cmcDeleteSite: (siteId: string) =>
    request<{ deleted: boolean }>("DELETE", `/cmc/sites/${siteId}`),

  cmcDeliverableTypes: () =>
    request<{ items: CmcDeliverableType[] }>("GET", "/cmc/deliverable-types"),
  cmcAddDeliverable: (id: string, doc_type_key: string) =>
    request<CmcDeliverable & { sections: CmcSection[] }>(
      "POST", `/cmc/projects/${id}/deliverables`, { json: { doc_type_key } }),
  cmcDeliverableSections: (deliverableId: string) =>
    request<{ deliverable: CmcDeliverable; items: CmcSection[] }>(
      "GET", `/cmc/deliverables/${deliverableId}/sections`),
  cmcRemoveDeliverable: (deliverableId: string) =>
    request<{ deleted: boolean; purged_sections: number }>(
      "DELETE", `/cmc/deliverables/${deliverableId}`),
  cmcPatchSection: (sectionId: string, body: {
    enabled?: boolean; applicability?: string; applicability_justification?: string;
  }) => request<CmcSection>("PATCH", `/cmc/sections/${sectionId}`, { json: body }),

  cmcListDocuments: (id: string) =>
    request<{ items: CmcDocument[]; readiness: CmcReadiness }>(
      "GET", `/cmc/projects/${id}/documents`),
  /** Multipart: every file carries its own doc_type, and its material where
   *  the uploader knows it -- a certificate filed against the wrong material
   *  is a limit applied to the wrong molecule. */
  cmcUploadDocuments: (id: string, files: { file: File; doc_type: string; material_id?: string }[]) => {
    const form = new FormData();
    const tagged = files.some((f) => f.material_id);
    for (const entry of files) {
      form.append("files", entry.file);
      form.append("doc_types", entry.doc_type);
      if (tagged) form.append("material_ids", entry.material_id ?? "");
    }
    return request<{ items: CmcDocument[] }>("POST", `/cmc/projects/${id}/documents`, { formData: form });
  },
  cmcRetagDocument: (documentId: string, body: { doc_type?: string; material_id?: string | null }) =>
    request<CmcDocument>("PATCH", `/cmc/documents/${documentId}`, { json: body }),
  cmcDeleteDocument: (documentId: string) =>
    request<{ deleted: boolean; purged_chunks: number; purged_values: number; kept_verified_values: number }>(
      "DELETE", `/cmc/documents/${documentId}`),
  cmcProcess: (id: string) =>
    request<{ queued: number }>("POST", `/cmc/projects/${id}/process`),
  cmcRetryDocument: (documentId: string) =>
    request<{ queued: number }>("POST", `/cmc/documents/${documentId}/retry`),
  cmcProcessingStatus: (id: string) =>
    request<{ items: CmcDocument[]; total: number; settled: number; in_flight: boolean; readiness: CmcReadiness }>(
      "GET", `/cmc/projects/${id}/processing-status`),

  cmcListMaterials: (id: string) =>
    request<{ items: CmcMaterial[] }>("GET", `/cmc/projects/${id}/materials`),
  cmcCreateMaterial: (id: string, body: { kind: string; name: string } & Partial<CmcMaterial>) =>
    request<CmcMaterial>("POST", `/cmc/projects/${id}/materials`, { json: body }),

  cmcBatches: (id: string, params: { material_id?: string; q?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: CmcBatchRow[]; total?: number }>("GET", `/cmc/projects/${id}/data/batches`, { query: params }),
  cmcSpecifications: (id: string, params: { material_id?: string; q?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: CmcTestRow[]; total?: number }>("GET", `/cmc/projects/${id}/data/specifications`, { query: params }),
  /** `scope` splits release from stability the way the table builders do, and
   *  `q` filters on the server. Both matter for the same reason: the grid is
   *  paged, and a split or a filter applied in the browser would only ever
   *  see the page in hand -- answering "no matches" for a value that is in
   *  the dossier. */
  cmcResults: (id: string, params: {
    material_id?: string; scope?: "release" | "stability"; q?: string;
    limit?: number; offset?: number;
  } = {}) =>
    request<{ items: CmcResultRow[]; total: number; summary: CmcDataSummary }>(
      "GET", `/cmc/projects/${id}/data/results`, { query: params }),
  cmcConflicts: (id: string) =>
    request<{ items: CmcResultRow[]; total: number; summary: CmcDataSummary }>(
      "GET", `/cmc/projects/${id}/data/conflicts`),
  /** Correcting a value verifies it in the same act: somebody just read the
   *  source and typed what it says. The string is stored verbatim. */
  cmcCorrectResult: (resultId: string, body: {
    value_text?: string; storage_condition?: string; timepoint_months?: number; verify?: boolean;
  }) => request<CmcResultRow>("PATCH", `/cmc/results/${resultId}`, { json: body }),
  cmcVerifyResults: (id: string, body: { result_ids?: string[]; test_id?: string; all_unverified?: boolean }) =>
    request<{ verified: number; skipped_conflicts: number }>(
      "POST", `/cmc/projects/${id}/results:verify`, { json: body }),
  cmcResolveConflict: (resultId: string, keep_result_id: string) =>
    request<{ id: string; value_text: string; discarded: string }>(
      "POST", `/cmc/results/${resultId}:resolve`, { json: { keep_result_id } }),

  cmcPreviewTable: (id: string, tableKey: string, params: { material_id?: string; include_unverified?: boolean } = {}) =>
    request<CmcRenderedTable>("GET", `/cmc/projects/${id}/tables/${tableKey}`, {
      query: {
        material_id: params.material_id,
        // The query helper serialises strings and numbers; a boolean has to be
        // spelled the way FastAPI parses it rather than stringified by accident.
        include_unverified: params.include_unverified === undefined
          ? undefined : String(params.include_unverified),
      },
    }),
  cmcGenerateSection: (sectionId: string, instruction?: string) =>
    request<CmcDraft & { section: CmcSection; data_needed: string[]; table_markers: string[] }>(
      "POST", `/cmc/sections/${sectionId}/generate`, { json: { instruction } }),
  cmcGetDraft: (sectionId: string, version?: number) =>
    request<{ section: CmcSection; draft: CmcDraft | null; versions: number[]; sources: CmcSource[]; table_markers: string[] }>(
      "GET", `/cmc/sections/${sectionId}/draft`, { query: { version } }),
  cmcSaveDraft: (sectionId: string, content: string) =>
    request<CmcDraft>("PUT", `/cmc/sections/${sectionId}/draft`, { json: { content } }),
  cmcSetSectionStatus: (sectionId: string, status: string) =>
    request<CmcSection>("PATCH", `/cmc/sections/${sectionId}/status`, { json: { status } }),
  cmcQc: (id: string) =>
    request<{ findings: CmcFinding[]; blockers: CmcFinding[]; warnings: CmcFinding[]; info: CmcFinding[]; exportable: boolean }>(
      "GET", `/cmc/projects/${id}/qc`),
  cmcExport: (id: string, body: {
    deliverable_id?: string; granularity?: string; citations?: string;
    draft_watermark?: boolean; override_approval?: boolean;
  }) => request<CmcExportRecord & { plans: unknown[] }>("POST", `/cmc/projects/${id}/export`, { json: body }),
  cmcListExports: (id: string) =>
    request<{ items: CmcExportRecord[] }>("GET", `/cmc/projects/${id}/exports`),
  /** One file out of an export, by its position in `files`. An export of
   *  granularity "both" writes a document AND a zip, and the record names only
   *  the first -- so without the index the second is listed on screen and
   *  reachable by nothing. */
  cmcDownloadExport: async (exportId: string, index = 0) => {
    const res = await fetch(`${API_URL}/cmc/exports/${exportId}/download?index=${index}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) throw new ApiError(res.status, "DOWNLOAD_FAILED", "The export could not be downloaded.");
    return URL.createObjectURL(await res.blob());
  },

  /* ---- Safety / Pharmacovigilance: reporting intervals and the case store ----
   *
   * The endpoint that shapes this screen is `pvScopePreview`. A report's three
   * dates are the hardest thing to change once figures depend on them, so the
   * setup screen asks the server what a proposed interval would actually
   * contain before anybody commits to it -- and the server answers through the
   * same scope layer the report itself will be built from.
   */
  pvReportTypes: () =>
    request<{ items: PvReportType[]; doc_types: Record<string, string>;
              input_types: Record<string, string>; regions: string[] }>(
      "GET", "/pv/report-types"),
  pvListProducts: () =>
    request<{ items: PvProduct[] }>("GET", "/pv/products"),
  pvGetProduct: (id: string) =>
    request<PvProduct>("GET", `/pv/products/${id}`),
  pvCreateProduct: (body: {
    project_id: string; product_name: string; inn?: string; mah_name?: string;
    atc_code?: string; ibd?: string | null; dibd?: string | null;
    formulations?: string[]; routes?: string[]; approved_indications?: string[];
    development_indications?: string[]; regions?: string[];
  }) => request<PvProduct>("POST", "/pv/products", { json: body }),
  pvUpdateProduct: (id: string, body: Record<string, unknown>) =>
    request<PvProduct>("PATCH", `/pv/products/${id}`, { json: body }),
  pvDeleteProduct: (id: string) =>
    request<{ deleted: boolean; purged: Record<string, number> }>(
      "DELETE", `/pv/products/${id}`),

  pvMembers: (id: string) =>
    request<{ items: PvMember[]; roles: { key: string; label: string }[];
              my_role: string | null }>("GET", `/pv/products/${id}/members`),
  pvGrantRole: (id: string, body: { user_id: string; pv_role: string }) =>
    request<{ id: string; user_id: string; pv_role: string }>(
      "POST", `/pv/products/${id}/members`, { json: body }),

  pvRsiVersions: (id: string) =>
    request<{ items: PvRsiVersion[]; rsi_types: string[] }>(
      "GET", `/pv/products/${id}/rsi-versions`),
  pvCreateRsiVersion: (id: string, body: {
    rsi_type: string; version_label: string; effective_date?: string | null;
  }) => request<PvRsiVersion>("POST", `/pv/products/${id}/rsi-versions`, { json: body }),
  /** Pinning is a qualified-person act: the pinned version is what "expected"
   *  means for every event in every report that follows it. */
  pvPinRsiVersion: (rsiVersionId: string) =>
    request<{ pinned: PvRsiVersion; superseded: string[];
              open_reports_on_previous_version: number }>(
      "POST", `/pv/rsi-versions/${rsiVersionId}/pin`),
  pvListedTerms: (rsiVersionId: string, params: { q?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: { id: string; meddra_pt: string; meddra_soc: string | null;
                       condition_text: string | null }[];
              total: number; rsi_version: PvRsiVersion }>(
      "GET", `/pv/rsi-versions/${rsiVersionId}/listed-terms`, { query: params }),

  pvReports: (id: string) =>
    request<{ items: PvReportInstance[] }>("GET", `/pv/products/${id}/reports`),
  pvCreateReport: (id: string, body: {
    doc_type_key: string; period_start: string; period_end: string;
    data_lock_point: string; sequence_number?: number | null;
    rsi_version_id?: string | null; meddra_version?: string | null;
    baseline_report_id?: string | null; regions?: string[];
  }) => request<PvReportInstance & { sections: PvSection[]; carried_forward: number }>(
    "POST", `/pv/products/${id}/reports`, { json: body }),
  pvGetReport: (reportId: string) =>
    request<PvReportInstance & { my_role: string | null }>("GET", `/pv/reports/${reportId}`),
  pvUpdateReport: (reportId: string, body: Record<string, unknown>) =>
    request<PvReportInstance>("PATCH", `/pv/reports/${reportId}`, { json: body }),
  pvDeleteReport: (reportId: string) =>
    request<{ deleted: boolean; sections: number }>("DELETE", `/pv/reports/${reportId}`),
  pvReportSections: (reportId: string) =>
    request<{ items: PvSection[] }>("GET", `/pv/reports/${reportId}/sections`),
  /** What a proposed interval would contain, before it exists. */
  pvScopePreview: (id: string, body: {
    doc_type_key: string; period_start: string; period_end: string;
    data_lock_point: string; baseline_report_id?: string | null;
  }) => request<PvScopePreview>("POST", `/pv/products/${id}/scope-preview`, { json: body }),
  pvReportScope: (reportId: string) =>
    request<PvScopePreview>("GET", `/pv/reports/${reportId}/preview-scope`),

  pvCalendar: (id: string) =>
    request<{ items: PvReportInstance[]; disclaimer: string }>(
      "GET", `/pv/products/${id}/calendar`),
  pvAddDueDate: (reportId: string, body: {
    region: string; submission_due_date?: string | null; basis_note?: string | null;
  }) => request<PvDueDate>("POST", `/pv/reports/${reportId}/due-dates`, { json: body }),

  pvApprovalStatuses: (id: string) =>
    request<{ items: PvApprovalStatus[]; statuses: string[] }>(
      "GET", `/pv/products/${id}/approval-statuses`),
  pvAddApprovalStatus: (id: string, body: {
    country: string; approval_date?: string | null; indication?: string | null;
    formulation?: string | null; status?: string;
  }) => request<PvApprovalStatus>("POST", `/pv/products/${id}/approval-statuses`, { json: body }),

  /* ---- Safety M2: sources, column mapping and the case store ---- */
  pvSources: (id: string) =>
    request<{ items: PvSource[]; readiness: PvReadiness;
              doc_types: Record<string, string>;
              input_types: Record<string, string> }>(
      "GET", `/pv/products/${id}/documents`),
  /** Two tags per file: the document type decides which sections may cite it,
   *  the input type decides which pipeline reads it. */
  pvUploadSources: (id: string, files: {
    file: File; doc_type: string; input_type: string;
  }[], reportInstanceId?: string) => {
    const form = new FormData();
    for (const entry of files) {
      form.append("files", entry.file);
      form.append("doc_types", entry.doc_type);
      form.append("input_types", entry.input_type);
    }
    if (reportInstanceId) form.append("report_instance_id", reportInstanceId);
    return request<{ items: PvSource[] }>(
      "POST", `/pv/products/${id}/documents`, { formData: form });
  },
  pvRetagSource: (documentId: string, body: {
    doc_type?: string; input_type?: string; report_instance_id?: string | null;
  }) => request<PvSource>("PATCH", `/pv/documents/${documentId}`, { json: body }),
  pvDeleteSource: (documentId: string) =>
    request<{ deleted: boolean; purged: Record<string, number> }>(
      "DELETE", `/pv/documents/${documentId}`),
  /** `mappings` is document id -> column map. A line listing without one is
   *  refused here rather than failing in the worker. */
  pvProcessSources: (id: string, mappings: Record<string, Record<string, string>> = {}) =>
    request<{ queued: number; documents: string[] }>(
      "POST", `/pv/products/${id}/process`, { json: { mappings } }),
  pvRetrySource: (documentId: string, mapping?: Record<string, string>) =>
    request<{ queued: number }>("POST", `/pv/documents/${documentId}/retry`,
      { json: { mappings: mapping ? { [documentId]: mapping } : {} } }),
  pvProcessingStatus: (id: string) =>
    request<{ items: PvSource[]; in_flight: boolean; cases: number;
              deid_gate: PvDeidGate }>(
      "GET", `/pv/products/${id}/processing-status`),
  pvSourceColumns: (documentId: string) =>
    request<{ headers: string[]; sample_rows: string[][]; row_count: number;
              suggestions: { column: string; field: string | null; confidence: number }[];
              fields: Record<string, string>; date_order_key: string }>(
      "GET", `/pv/documents/${documentId}/columns`),
  pvMappingProfiles: (id: string) =>
    request<{ items: PvMappingProfile[] }>(
      "GET", `/pv/products/${id}/mapping-profiles`),
  pvSaveMappingProfile: (id: string, body: {
    name: string; source_system?: string; column_map: Record<string, string>;
    scoped_to_product?: boolean;
  }) => request<PvMappingProfile>(
    "POST", `/pv/products/${id}/mapping-profiles`, { json: body }),
  pvCases: (id: string, params: {
    report_instance_id?: string; q?: string; limit?: number; offset?: number;
  } = {}) => request<{ items: PvCaseRow[]; total: number }>(
    "GET", `/pv/products/${id}/cases`, { query: params }),

  /* ---- Safety M3: the de-identification gate ---- */
  pvDeidQueue: (id: string, status = "pending") =>
    request<{ items: PvDeidItem[]; pending: number; documents_waiting: number;
              identifier_types: string[]; cleared: boolean }>(
      "GET", `/pv/products/${id}/deid-queue`, { query: { status } }),
  /** Answering one detection releases every source waiting on it: deciding a
   *  string is a person's name decides it for the whole product. */
  pvResolveDeidItem: (itemId: string, body: {
    action: "mask" | "not_an_identifier"; identifier_type?: string;
  }) => request<{ resolved: string; documents_indexed: number }>(
    "POST", `/pv/deid-items/${itemId}/resolve`, { json: body }),
  pvOverrideDeidQueue: (id: string, reason: string) =>
    request<{ overridden: number; documents_indexed: number; reason: string }>(
      "POST", `/pv/products/${id}/deid-queue:override`, { json: { reason } }),
  pvLeakageScan: (id: string) =>
    request<{ findings: { where: string; identifier_type: string; text: string;
                          basis: string }[]; clean: boolean }>(
      "POST", `/pv/products/${id}/leakage-scan`),

  /* ---- Safety M4: coding, expectedness and duplicates ---- */
  pvCaseEvents: (id: string, params: {
    report_instance_id?: string; only?: string; q?: string;
    limit?: number; offset?: number;
  } = {}) => request<{ items: PvCaseEvent[]; total: number;
                       summary: PvEventSummary; my_role: string | null;
                       stale_expectedness?: { event_id: string; meddra_pt: string }[] }>(
    "GET", `/pv/products/${id}/case-events`, { query: params }),
  pvCodeEvents: (id: string) =>
    request<{ coded: number; still_uncoded: number; dictionary_loaded: boolean;
              meddra_version: string | null;
              reasons: { reason: string; events: number }[] }>(
      "POST", `/pv/products/${id}/code`),
  /** Written to the suggestion column, never to the confirmed one. */
  pvSuggestExpectedness: (reportId: string) =>
    request<{ events: number; counts: Record<string, number>; note: string }>(
      "POST", `/pv/reports/${reportId}/suggest-expectedness`),
  pvConfirmEvent: (eventId: string, body: Record<string, unknown>,
                   reportInstanceId?: string) =>
    request<PvCaseEvent>("PATCH", `/pv/case-events/${eventId}/confirm`, {
      json: body, query: { report_instance_id: reportInstanceId },
    }),
  pvBulkConfirm: (id: string, body: {
    meddra_pt: string; expectedness: string; report_instance_id?: string;
  }) => request<{ confirmed: number }>(
    "POST", `/pv/products/${id}/case-events:bulk-confirm`, { json: body }),
  pvDetectDuplicates: (id: string) =>
    request<{ candidates: number; new: number; note: string }>(
      "POST", `/pv/products/${id}/duplicates:detect`),
  pvDuplicates: (id: string, status = "pending") =>
    request<{ items: PvDuplicatePair[] }>(
      "GET", `/pv/products/${id}/duplicates`, { query: { status } }),
  pvResolveDuplicate: (pairId: string, body: {
    action: string; keep_case_id?: string;
  }) => request<{ resolved: string }>(
    "POST", `/pv/duplicates/${pairId}/resolve`, { json: body }),

  /* ---- CSR module: ICH E3 drafting for medical writers ---- */
  csrListProjects: () =>
    request<{ items: CsrProject[] }>("GET", "/csr/projects"),
  csrCreateProject: (body: {
    project_id: string;
    study_id?: string;
    study?: {
      protocol_number: string; title?: string; sponsor?: string; phase?: string;
      indication?: string; principal_investigator?: string;
    };
    compound_name?: string;
    therapeutic_area?: string;
    blinding?: string;
    study_design_summary?: string;
  }) => request<CsrProject>("POST", "/csr/projects", { json: body }),
  csrGetProject: (id: string) =>
    request<CsrProject & { sections: CsrSection[] }>("GET", `/csr/projects/${id}`),
  csrDeleteProject: (id: string) =>
    request<{ deleted: boolean; purged_sections: number }>("DELETE", `/csr/projects/${id}`),
  csrChooseTemplate: (id: string, source: string) =>
    request<{ template: { source: string }; sections: CsrSection[] }>(
      "POST", `/csr/projects/${id}/template`, { json: { source } }),
  csrSections: (id: string) =>
    request<{ items: CsrSection[] }>("GET", `/csr/projects/${id}/sections`),
  csrToggleSection: (sectionId: string, enabled: boolean) =>
    request<CsrSection>("PATCH", `/csr/sections/${sectionId}`, { json: { enabled } }),
  csrListDocuments: (id: string) =>
    request<{ items: CsrDocument[]; readiness: CsrReadiness }>("GET", `/csr/projects/${id}/documents`),
  /** Multipart: every file carries its own doc_type tag, in order. */
  csrUploadDocuments: async (id: string, files: { file: File; doc_type: string }[]) => {
    const form = new FormData();
    for (const entry of files) {
      form.append("files", entry.file);
      form.append("doc_types", entry.doc_type);
    }
    return request<{ items: CsrDocument[] }>("POST", `/csr/projects/${id}/documents`, { formData: form });
  },
  csrRetagDocument: (documentId: string, doc_type: string) =>
    request<CsrDocument>("PATCH", `/csr/documents/${documentId}`, { json: { doc_type } }),
  csrDeleteDocument: (documentId: string) =>
    request<{ deleted: boolean; purged_chunks: number }>("DELETE", `/csr/documents/${documentId}`),
  csrProcess: (id: string) =>
    request<{ queued: number; poll: string }>("POST", `/csr/projects/${id}/process`),
  csrRetryDocument: (documentId: string) =>
    request<{ queued: number }>("POST", `/csr/documents/${documentId}/retry`),
  csrProcessingStatus: (id: string) =>
    request<{ items: CsrDocument[]; total: number; settled: number; in_flight: boolean; readiness: CsrReadiness }>(
      "GET", `/csr/projects/${id}/processing-status`),
  csrGenerateSection: (sectionId: string, instruction?: string) =>
    request<CsrDraft & { section: CsrSection; data_needed: string[] }>(
      "POST", `/csr/sections/${sectionId}/generate`, { json: { instruction } }),
  csrGetDraft: (sectionId: string, version?: number) =>
    request<{ section: CsrSection; draft: CsrDraft | null; versions: number[]; sources: CsrSource[] }>(
      "GET", `/csr/sections/${sectionId}/draft`, { query: { version } }),
  csrSaveDraft: (sectionId: string, content: string) =>
    request<CsrDraft>("PUT", `/csr/sections/${sectionId}/draft`, { json: { content } }),
  csrSetSectionStatus: (sectionId: string, status: string) =>
    request<CsrSection>("PATCH", `/csr/sections/${sectionId}/status`, { json: { status } }),
  /** A whole template, authored by a model from a plain description. The server
   *  proves the result (emit round-trip) before persisting, and stands a kit in
   *  -- reason recorded in `generation` -- when no model is configured. */
  blueprintFromDescription: (body: { description: string; name?: string; project_id?: string; service?: string }) =>
    request<Blueprint & { version: BlueprintVersion; generation: { source: "model" | "kit_fallback"; notes: string[]; model: string | null } }>(
      "POST", "/template-blueprints:from-description", { json: body }),
  /** One record in, one stored document out -- no spreadsheet, no binding. */
  generateFromManifest: (manifestId: string, body: { source_record: Record<string, unknown>; language?: string; project_id?: string }) =>
    request<{ document_id: string; document_version_id: string; filename: string; qa_passed: boolean; qa_notes: string[] }>(
      "POST", `/template-manifests/${manifestId}/generate`, { json: body }),

  // ---- Template Studio: compile -> review -> bind -> generate ----
  // `progressToken` is chosen by the caller before the request goes out, which
  // is what makes the compile watchable while it runs: a server-generated id
  // only arrives with the response, by which point there is nothing left to see.
  // Poll `getJob(token)` alongside this call.
  compileManifest: (templateId: string, opts: { agentic?: boolean; refine?: boolean; progressToken?: string } = {}) =>
    request<any>("POST", `/templates/${templateId}/compile-manifest`, {
      query: {
        agentic: String(!!opts.agentic),
        use_llm_refinement: String(!!opts.refine),
        progress_token: opts.progressToken,
      },
    }),
  listManifests: (templateId: string) => request<{ items: any[] }>("GET", `/templates/${templateId}/manifests`),
  getManifest: (manifestId: string) => request<any>("GET", `/template-manifests/${manifestId}`),
  patchManifest: (manifestId: string, body: { fields?: any[]; conditions?: any[]; blocks?: any[] }) =>
    request<any>("PATCH", `/template-manifests/${manifestId}`, { json: body }),
  approveManifest: (manifestId: string) => request<any>("POST", `/template-manifests/${manifestId}:approve`),

  /* ---- Template authoring ----
   * Put a legacy .docx in, get an editable template back, hand-edit it, publish
   * it as something the fill engine can execute. */
  listBlueprints: (projectId?: string, templateFileId?: string) =>
    request<{ items: Blueprint[] }>("GET", "/template-blueprints",
      { query: { project_id: projectId, template_file_id: templateFileId } }),
  getBlueprint: (id: string) => request<Blueprint>("GET", `/template-blueprints/${id}`),
  listBlueprintVersions: (id: string) =>
    request<{ items: { id: string; version_no: number; change_summary: string | null; created_at: string; finding_count: number; manifest_id: string | null }[] }>(
      "GET", `/template-blueprints/${id}/versions`),
  blueprintFromTemplate: (body: { template_file_id: string; name?: string; progress_token?: string }) =>
    request<Blueprint>("POST", "/template-blueprints:from-template", { json: body }),
  saveBlueprint: (id: string, body: { body: BlueprintBody; objects?: any[]; change_summary?: string; expected_version_no?: number }) =>
    request<BlueprintVersion>("POST", `/template-blueprints/${id}/versions`, { json: body }),
  revertBlueprint: (id: string, versionNo: number) =>
    request<BlueprintVersion>("POST", `/template-blueprints/${id}:revert-to`, { json: { version_no: versionNo } }),
  lintBlueprint: (id: string) => request<LintReport>("GET", `/template-blueprints/${id}/lint`),
  /** `recompile` publishes the document and then lets the compiler read it,
   *  instead of shipping the blueprint's own objects. Slower — a real compile —
   *  but it is the way out when the reading has gone stale against a document
   *  that has since been fixed. */
  publishBlueprint: (id: string, dispositions: string[] = [], recompile = false) =>
    request<{ blueprint_id: string; template_version_id: string; manifest_id: string; lint: LintReport }>(
      "POST", `/template-blueprints/${id}:publish`, { json: { dispositions, recompile } }),
  deleteBlueprint: (id: string) => request<{ status: string }>("DELETE", `/template-blueprints/${id}`),
  /** Take a published template off the list without claiming anything is gone.
   *
   *  `deleteBlueprint` refuses with `BLUEPRINT_IN_USE` once a manifest has been
   *  approved from the template -- which, now that compiling approves its own
   *  reading, is nearly every template that has been read. This is the route out
   *  of that refusal the error message has always pointed at. */
  archiveBlueprint: (id: string) =>
    request<{ status: string }>("POST", `/template-blueprints/${id}:archive`),
  blueprintKits: () =>
    request<{ items: { id: string; name: string; description: string; field_count: number; paragraph_count: number }[] }>(
      "GET", "/template-blueprint-kits"),
  createBlueprint: (body: { name: string; kit?: string; project_id?: string }) =>
    request<Blueprint>("POST", "/template-blueprints", { json: body }),
  blueprintFromLibrary: (body: { library_id: string; project_id?: string }) =>
    request<Blueprint>("POST", "/template-blueprints:from-library", { json: body }),
  blueprintCopilot: (id: string, body: { message: string; mode: "author" | "explain" }) =>
    request<any>("POST", `/template-blueprints/${id}/copilot`, { json: body }),
  applyBlueprintOperations: (id: string, body: { ops: any[]; expected_version_no?: number; change_summary?: string }) =>
    request<any>("POST", `/template-blueprints/${id}/operations`, { json: body }),
  emitBlueprint: (id: string) =>
    request<{ blueprint_id: string; template_version_id: string; template_file_id: string }>(
      "POST", `/template-blueprints/${id}:emit`),

  /** The template as a file. Emitted fresh, so what downloads is what you see. */
  async blueprintDocxUrl(id: string): Promise<string> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/template-blueprints/${id}/docx`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    // Same reason as `authedDownloadUrl`: without this the browser is handed a
    // blob of the error JSON named like a Word file, and nothing reports it.
    if (!res.ok) throw new ApiError(res.status, "DOWNLOAD_FAILED", "Could not download this template.");
    return URL.createObjectURL(await res.blob());
  },
  // Why a manifest can or cannot be locked, without trying to lock it. The
  // reviewer needs the blockers while deciding, not as a 409 after pressing
  // Approve.
  manifestValidation: (manifestId: string) =>
    request<{ can_approve: boolean; status: string; failures: { rule: string; detail: string; object_id: string | null }[]; warnings: any[]; warning_dispositions: Record<string, any> }>(
      "GET", `/template-manifests/${manifestId}/validation`,
    ),
  resolveManifestWarning: (manifestId: string, code: string, note = "") =>
    request<any>("POST", `/template-manifests/${manifestId}/warnings:resolve`, { json: { code, note } }),
  // The spreadsheet this template expects: columns named after the fields, with
  // each condition column restricted to the values its branches test for. The
  // inverse of binding -- fill it in and nothing needs mapping by hand.
  async sourceTemplate(manifestId: string): Promise<{ url: string; filename: string }> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/template-manifests/${manifestId}/source-template`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new ApiError(res.status, "TEMPLATE_FAILED", "Could not build the data template.");
    const disposition = res.headers.get("content-disposition") ?? "";
    const match = /filename="?([^"]+)"?/.exec(disposition);
    return { url: URL.createObjectURL(await res.blob()), filename: match?.[1] ?? "source_template.xlsx" };
  },
  manifestPreview: (manifestId: string) => request<ManifestPreview>("GET", `/template-manifests/${manifestId}/preview`),

  sourceRecords: (versionId: string, limit = 25) =>
    request<{ columns: string[]; sheets: string[]; total: number; records: any[] }>(
      "GET", `/source-versions/${versionId}/records`, { query: { limit: String(limit) } },
    ),
  // `sheet` was accepted by the endpoint from the start and never passed. Left
  // out, the backend falls through to worksheets[0], so every sheet after the
  // first in a multi-sheet workbook was unreachable from the UI.
  // `unmatched_condition_values` is returned too: rows whose condition value
  // selects no branch, i.e. letters that would generate with a section missing.
  bindingSuggestions: (manifestId: string, sourceVersionId: string, sheet?: string) =>
    request<{
      columns: string[]; row_count: number; suggestions: BindingSuggestion[];
      unmatched_fields: string[]; unused_columns: string[];
      unmatched_condition_values?: { field_id: string; column: string | null; observed_value: string; offered: string[] }[];
    }>(
      "GET", `/template-manifests/${manifestId}/binding-suggestions`,
      { query: { source_version_id: sourceVersionId, sheet } },
    ),
  saveBinding: (manifestId: string, body: { source_version_id: string; field_bindings: Record<string, string>; value_map?: Record<string, Record<string, string>> }) =>
    request<any>("POST", `/template-manifests/${manifestId}/bindings`, { json: body }),
  previewRow: (manifestId: string, body: { source_version_id: string; row_index: number }) =>
    request<any>("POST", `/template-manifests/${manifestId}/preview-row`, { json: body }),
  // `sheet` has to travel with the batch as well as with the suggestions, or the
  // reviewer maps columns from sheet 2 and the batch silently renders sheet 1.
  generateBatch: (manifestId: string, body: { source_version_id: string; sheet?: string; language?: string; row_indices?: number[] }) =>
    request<{ job_id: string; status: string }>("POST", `/template-manifests/${manifestId}/generate-batch`, { json: body }),
  // The saved bindings for a manifest -- one row per source version, so the
  // caller picks its own. Without this a returning reviewer sees an empty branch
  // panel and the next save overwrites the mappings they made last time.
  listBindings: (manifestId: string) =>
    request<{ items: { source_version_id: string; field_bindings: Record<string, string>; value_map: Record<string, Record<string, string>> }[] }>(
      "GET", `/template-manifests/${manifestId}/bindings`,
    ),
  // §22's metrics and §18's targets. READ_AUDIT-gated server-side: these are a
  // summary of a customer's estate, its error rate and its review behaviour.
  qualityReport: (windowDays = 30) =>
    request<QualityReport>("GET", "/metrics", { query: { window_days: windowDays } }),

  fieldDictionary: () => request<{ items: any[] }>("GET", "/field-dictionary"),
  async batchZipUrl(jobId: string): Promise<string> {
    const token = ensureAuth();
    const res = await fetch(`${API_URL}/jobs/${jobId}/download`, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new ApiError(res.status, "DOWNLOAD_FAILED", "Could not download the batch archive.");
    return URL.createObjectURL(await res.blob());
  },

  // ---- Human-in-the-loop review queue ----
  reviewTasks: (params: { status?: string; project_id?: string; kind?: string } = {}) =>
    request<{ items: ReviewTask[] }>("GET", "/review-tasks", { query: { status: params.status, project_id: params.project_id, kind: params.kind } }),
  reviewTask: (taskId: string) => request<ReviewTask>("GET", `/review-tasks/${taskId}`),
  reviewSummary: () => request<{ open: number; resolved: number; dismissed: number; by_kind: Record<string, number> }>("GET", "/review-tasks/summary"),
  resolveReviewTask: (taskId: string, body: { resolved_value?: string; rationale: string; promote_to_manifest?: boolean; promote_as?: string }) =>
    request<any>("POST", `/review-tasks/${taskId}:resolve`, { json: body }),
  dismissReviewTask: (taskId: string, body: { rationale: string }) =>
    request<any>("POST", `/review-tasks/${taskId}:dismiss`, { json: body }),

  // ---- Document review: a person objecting, as opposed to the engine asking ----
  reviewQueue: (params: { state?: string; assigned_to?: string; project_id?: string } = {}) =>
    request<{ items: QueueItem[] }>("GET", "/review-queue", { query: params }),
  documentReviews: (versionId: string) =>
    request<{ items: DocumentReview[] }>("GET", `/document-versions/${versionId}/reviews`),
  openDocumentReview: (versionId: string, body: { reason: string; title?: string; assigned_to?: string }) =>
    request<DocumentReview>("POST", `/document-versions/${versionId}/reviews`, { json: body }),
  requestChanges: (versionId: string, body: { reason: string }) =>
    request<DocumentReview>("POST", `/document-versions/${versionId}:request-changes`, { json: body }),
  documentReview: (reviewId: string) =>
    request<DocumentReview>("GET", `/reviews/${reviewId}`),
  addReviewComment: (reviewId: string, body: { body: string; paragraph_index?: number; span_index?: number; quoted_text?: string }) =>
    request<ReviewComment>("POST", `/reviews/${reviewId}/comments`, { json: body }),
  resolveReviewComment: (reviewId: string, commentId: string) =>
    request<ReviewComment>("POST", `/reviews/${reviewId}/comments/${commentId}:resolve`),
  approveDocumentReview: (reviewId: string, body: { note?: string } = {}) =>
    request<DocumentReview>("POST", `/reviews/${reviewId}:approve`, { json: body }),
  rejectDocumentReview: (reviewId: string, body: { note: string }) =>
    request<DocumentReview>("POST", `/reviews/${reviewId}:reject`, { json: body }),
  withdrawDocumentReview: (reviewId: string) =>
    request<DocumentReview>("POST", `/reviews/${reviewId}:withdraw`),
  assignDocumentReview: (reviewId: string, userId: string | null) =>
    request<DocumentReview>("POST", `/reviews/${reviewId}:assign`, { json: { user_id: userId } }),
};

/** What every bulk delete answers with. The id field is named for what it
 *  identifies, so a caller cannot accidentally read a project id off a document
 *  result; `name` is what to show the user about a row that was refused. */
export type BulkDeleteResult<K extends string> = {
  requested: number;
  deleted: ({ [P in K]: string } & { name?: string | null; filename?: string | null })[];
  refused: ({ [P in K]: string } & {
    code: string; reason: string; name?: string | null; filename?: string | null;
  })[];
  blobs_deleted?: number;
};

/** One recorded action, exactly as `/audit-logs` returns it.
 *
 *  `actor` and `target` are nullable in the table and stay nullable here: a row
 *  written with no actor name is a row whose actor was not recorded, and
 *  defaulting it to "System" in the type would hand every screen a fact the
 *  database never had.
 *
 *  `severity` is left as `string` rather than the five-value union the writers
 *  currently use. The column has no constraint, so narrowing it here would let
 *  a sixth value type-check its way into a `Record` lookup that has no entry
 *  for it. */
export type AuditEntry = {
  id: number;
  time: string;
  actor: string | null;
  action: string;
  target: string | null;
  severity: string;
  entity_type: string;
};

/** What an organisation has *required*, which is not the same thing as what any
 *  provider has been *configured* to do.
 *
 *  Nothing on this shape reports a provider's real retention or processing
 *  location -- those are not exposed over HTTP at all -- so anything rendering
 *  it has to say whose statement it is. `recorded` is the difference between a
 *  policy this customer set and the platform default standing in for one. */
export type DataPolicy = {
  org_id: string;
  source_retention_days: number;
  generated_document_retention_days: number | null;
  generated_documents_retained_indefinitely: boolean;
  /** One of GLOBAL, EU, UK, IN. */
  residency: string;
  zero_retention_required: boolean;
  recorded: boolean;
};

export type BindingSuggestion = {
  field_id: string;
  column: string | null;
  confidence: number;
  method: "dictionary" | "exact_slug" | "mergefield" | "fuzzy" | "llm" | "unmatched";
  rationale: string;
  origin: "field" | "condition" | "formula";
  type: string;
  sample_value: string | null;
};

export type ManifestSpan = {
  span_index: number;
  color: "blue" | "red" | "black";
  text: string;
  in_hyperlink: boolean;
  field_id: string | null;
  is_instruction: boolean;
};

export type ManifestPreview = {
  paragraph_count: number;
  prescan_summary: Record<string, any>;
  blocks: any[];
  conditions: any[];
  paragraphs: {
    index: number;
    text: string;
    in_table: boolean;
    block_ids: string[];
    is_condition_marker: boolean;
    spans: ManifestSpan[];
  }[];
};

export type ReviewTask = {
  id: string;
  project_id: string | null;
  unit_id: string;
  kind: "calculation" | "condition" | "binding" | "narrative";
  question: string;
  context: Record<string, any>;
  proposed_value: string | null;
  resolved_value: string | null;
  status: "open" | "resolved" | "dismissed";
  rationale: string | null;
  created_at: string;
  document_version_id: string | null;
  resolved_by: string | null;
  resolved_by_name: string | null;
  resolved_at: string | null;
};

/** One remark on a review, optionally pinned to a run of the document. */
export type ReviewComment = {
  id: string;
  parent_id: string | null;
  author_id: string;
  author_name: string | null;
  body: string;
  /** The coordinate the compiler, the fill engine and the editor all speak. */
  paragraph_index: number | null;
  span_index: number | null;
  /** What the run said when the remark was written. */
  quoted_text: string | null;
  resolved_at: string | null;
  created_at: string;
};

/** A person's objection to a finished document. */
export type DocumentReview = {
  id: string;
  document_id: string;
  document_version_id: string;
  state: "open" | "approved" | "rejected" | "withdrawn";
  title: string | null;
  reason: string | null;
  requested_by: string;
  requested_by_name: string | null;
  assigned_to: string | null;
  assigned_to_name: string | null;
  authored_by: string | null;
  resolved_by: string | null;
  resolved_by_name: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  created_at: string;
  /** Sent with the review so a disabled button can explain itself. */
  can_resolve?: boolean;
  cannot_resolve_reason?: string | null;
  comments?: ReviewComment[];
  document?: {
    id: string | null;
    display_id: number | null;
    status: string | null;
    version_no: number | null;
    project_id: string | null;
  };
};

/** One row of the unified inbox: an objection, or a question the engine parked. */
export type QueueItem = {
  kind: "document_review" | "unit_task";
  /** Server-ranked. Lower sorts first; the judgement is made once, on the server. */
  priority: number;
  id: string;
  title: string;
  state: string;
  project_id: string | null;
  document_id?: string;
  document_version_id: string | null;
  document_status: string | null;
  /** Where the document's owner has put it, which is the other axis. Null for a
   *  queue row that is not about a document. */
  workflow_status: string | null;
  requested_by_name?: string | null;
  assigned_to?: string | null;
  task_kind?: "calculation" | "condition" | "binding" | "narrative";
  unit_id?: string;
  created_at: string;
};
