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

  me: () => request<{ id: string; full_name: string; email: string }>("GET", "/me"),
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
  getDocumentVersion: (versionId: string) => request<{ id: string; html_content: string; status: string; version_no: number; renderer: string | null; html_editable: boolean }>("GET", `/document-versions/${versionId}`),
  saveDocumentVersion: (versionId: string, html: string) => request<any>("PATCH", `/document-versions/${versionId}`, { json: { html_content: html } }),
  approveDocumentVersion: (versionId: string) => request<any>("POST", `/document-versions/${versionId}:approve`),
  revokeDocumentVersion: (versionId: string) => request<any>("POST", `/document-versions/${versionId}:revoke`),

  listLibrary: (category?: string, q?: string) => request<{ items: any[] }>("GET", "/template-library", { query: { category, q } }),
  createLibraryEntry: (body: { name: string; category: string; content_html?: string }) => request<any>("POST", "/template-library", { json: body }),
  getLibraryContent: (id: string) => request<{ content_html: string; source_fields: string[]; version_no: number }>("GET", `/template-library/${id}/content`),
  saveLibraryContent: (id: string, html: string) => request<any>("PATCH", `/template-library/${id}/content`, { json: { content_html: html } }),
  convertLegacyText: (text: string) => request<{ candidates: any[] }>("POST", "/template-library:convert", { json: { text } }),
  generateFromLibrary: (libraryId: string, projectId: string) => request<any>("POST", `/template-library/${libraryId}/generate`, { json: { project_id: projectId } }),

  analyticsKpis: () => request<any>("GET", "/analytics/kpis"),
  analyticsTrend: () => request<{ items: { day: string; count: number }[] }>("GET", "/analytics/trend"),
  analyticsByFunction: () => request<{ items: { function: string; count: number }[] }>("GET", "/analytics/by-function"),
  analyticsTopTemplates: () => request<{ items: { name: string; uses: number }[] }>("GET", "/analytics/top-templates"),

  teamMembers: () => request<{ items: any[] }>("GET", "/team/members"),
  teamRolesSummary: () => request<{ items: { role: string; count: number }[] }>("GET", "/team/roles-summary"),
  auditLogs: () => request<{ items: any[] }>("GET", "/audit-logs"),

  listConversations: (projectId: string) => request<{ items: any[] }>("GET", `/projects/${projectId}/conversations`),
  createConversation: (projectId: string, title: string) => request<any>("POST", `/projects/${projectId}/conversations`, { json: { title } }),
  listMessages: (conversationId: string) => request<{ items: any[] }>("GET", `/conversations/${conversationId}/messages`),
  sendMessage: (conversationId: string, text: string) => request<any>("POST", `/conversations/${conversationId}/messages`, { json: { text } }),

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
  reviewSummary: () => request<{ open: number; resolved: number; dismissed: number; by_kind: Record<string, number> }>("GET", "/review-tasks/summary"),
  resolveReviewTask: (taskId: string, body: { resolved_value?: string; rationale: string; promote_to_manifest?: boolean; promote_as?: string }) =>
    request<any>("POST", `/review-tasks/${taskId}:resolve`, { json: body }),
  dismissReviewTask: (taskId: string) => request<any>("POST", `/review-tasks/${taskId}:dismiss`),
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
};
