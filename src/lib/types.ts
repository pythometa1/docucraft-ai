export type ProjectStatus = "Completed" | "In Progress" | "Pending" | "Failed";

export type FunctionKey =
  | "Human Resources"
  | "Clinical"
  | "Quality-CMC"
  | "Quality"
  | "Safety"
  | "Medical Affairs"
  | "Marketing"
  | "Legal"
  | "Finance"
  | "Regulatory Affairs";

export interface TemplateFile {
  /** Set when a blueprint is already open on this template, so the row can offer
   *  "continue editing" and the click need not guess whether one exists. */
  blueprintId?: string;
  id: string;
  name: string;
  size: string;
  uploadedAt: string;
  uploadedBy: string;
  // Whether the template has been read yet. A project cannot move past its
  // first stage without this: nothing downstream knows what data the letter
  // needs until the template has been compiled into a manifest.
  manifestId?: string;
  manifestStatus?: string;
  compileError?: string;
  fieldCount: number;
  conditionCount: number;
  /** Placeholders in the document that the reading did not claim.
   *
   *  Each one is a document that will come back blocked with "Leftover
   *  placeholder brackets": the fill engine leaves the literal text where the
   *  value should go, and QA refuses it. Known the moment the template is read,
   *  which is the moment somebody can still change the template. */
  unfillable: UnfillablePlaceholder[];
  unfillableCount: number;
}

export interface UnfillablePlaceholder {
  /** `uncovered_placeholder` — nothing claims it, and a field could be added.
   *  `W-SPLIT-PLACEHOLDER` — Word split it across runs, so no field *can* be
   *  attached to it and the fix is to retype it in one go. The two need
   *  different advice, so the code is carried rather than flattened away. */
  code: string;
  paragraph_index: number | null;
  placeholder: string | null;
  message: string | null;
}

export interface SourceFile {
  id: string;
  name: string;
  type: "csv" | "xlsx" | "json" | "pdf" | "docx" | "txt";
  size: string;
  rows?: number;
  uploadedAt: string;
  // The version everything downstream addresses: binding suggestions and
  // generation are per source *version*, not per file.
  currentVersionId?: string;
}

/** Where a document sits, as a reader sees it.
 *
 *  Two axes are collapsed into this one list on read: `approved` comes from the
 *  signature and `blocked` from the QA gate, and neither is a label anybody
 *  applies. The other three are. */
export type WorkflowStatus =
  | "work_in_progress"
  | "completed"
  | "approved"
  | "blocked"
  | "cancelled";

/** The three a person may actually set. The server refuses the other two with an
 *  explanation, so the select offers three options rather than five that fail. */
export type SettableWorkflowStatus = "work_in_progress" | "completed" | "cancelled";

export const SETTABLE_WORKFLOW: SettableWorkflowStatus[] = [
  "work_in_progress", "completed", "cancelled",
];

export const WORKFLOW_LABELS: Record<WorkflowStatus, string> = {
  work_in_progress: "Work in progress",
  completed: "Completed",
  approved: "Approved",
  blocked: "Blocked",
  cancelled: "Cancelled",
};

export interface GeneratedDoc {
  id: string;
  filename: string;
  // The download endpoint is /document-versions/{id}/download, so the version
  // is what a download needs -- `id` here is the GeneratedDocument, and passing
  // it produced a 404 on every document the new flow generates.
  currentVersionId?: string;
  status: string;
  statusReason?: string;
  /** What to show. Layers the signature and the QA verdict over the lane. */
  workflowStatus: WorkflowStatus;
  /** What the person actually set, which is what the dropdown shows as selected.
   *  Kept separate so a document somebody marked completed that then failed QA
   *  reads "Blocked" without forgetting they had marked it completed. */
  workflowStatusSet: SettableWorkflowStatus;
  /** Whether the server would hand over the bytes. Sent so the control can be
   *  disabled with a reason rather than discovered by pressing it. */
  downloadable: boolean;
  /** Set when a person has an objection open against the current version. */
  openReviewId?: string;
  generatedAt: string;
  size: string;
  generatedBy: string;
}

export interface Project {
  id: string;
  projectId: string; // display number e.g. 51255
  name: string;
  description?: string;
  documentType: string;
  function: FunctionKey;
  region: string;
  language: string;
  createdAt: string;
  modifiedAt: string;
  status: ProjectStatus;
  templates: TemplateFile[];
  sources: SourceFile[];
  generated: GeneratedDoc[];
  generationMethod?: "ai" | "chat" | "manual" | "hybrid";
}


/* ---- Template authoring ----
 *
 * A Blueprint is a template being written, as opposed to one being read. The
 * body is the document -- paragraphs, and within them segments carrying a role
 * -- and the objects are what the compiler understood about it. Publishing emits
 * both projections: a real .docx and a manifest that fills it.
 *
 * `role` is the whole contract with the backend's pre-scanner: blue runs are
 * placeholders, red runs are author instructions, and the emitter writes those
 * exact colours so a template this app produces reads back as one it can read.
 */

export type SegmentRole = "static" | "placeholder" | "instruction" | "mergefield" | "hyperlink";

export interface BlueprintSegment {
  role: SegmentRole;
  text: string;
  /** MERGEFIELD only: the Word field code. */
  code?: string;
  /** Hyperlink only. */
  target?: string;
  colour?: "blue" | "red" | "black";
  bold?: boolean;
  italic?: boolean;
  /** False when the compile removed this instruction. Kept so it can be put back. */
  emit?: boolean;
}

export interface BlueprintParagraph {
  kind: "paragraph";
  style: string | null;
  segments: BlueprintSegment[];
}

export interface BlueprintTable {
  kind: "table";
  rows: BlueprintBlock[][][];
}

export type BlueprintBlock = BlueprintParagraph | BlueprintTable;

export interface BlueprintBody {
  blocks: BlueprintBlock[];
  sect_pr_from: string | null;
}

export type LintSeverity = "blocking" | "warning" | "advisory";

export interface LintFinding {
  code: string;
  severity: LintSeverity;
  detail: string;
  object_id: string | null;
  paragraph_index: number | null;
  fix: Record<string, unknown> | null;
}

export interface LintReport {
  findings: LintFinding[];
  blocking: number;
  can_publish: boolean;
}

export interface BlueprintVersion {
  id: string;
  blueprint_id: string;
  version_no: number;
  body: BlueprintBody;
  objects: Record<string, any>[];
  findings: LintFinding[];
  provenance: Record<string, any>;
  manifest_id: string | null;
  change_summary: string | null;
  created_at: string;
}

export interface Blueprint {
  id: string;
  name: string;
  kind: "legacy" | "inherited" | "kit" | "library" | "blank";
  status: "draft" | "published" | "archived";
  project_id: string | null;
  source_template_version_id: string | null;
  template_file_id: string | null;
  current_version_id: string | null;
  version_no: number | null;
  created_at: string;
  updated_at: string;
  version?: BlueprintVersion | null;
}


/* ---- Analytics ----
 *
 * `Stat` mirrors the backend's `metrics.Metric` exactly, including the rule that
 * a metric holds either a value or a reason it has none and never both. That is
 * what stops a dashboard printing a number and dropping the caveat -- which is
 * how "nobody has measured this" becomes "zero".
 */

export type AnalyticsRange = "7d" | "30d" | "90d" | "1y";

export interface Stat {
  rank: number;
  key: string;
  label: string;
  definition: string;
  direction: "up" | "down";
  unit: string;
  available: boolean;
  value: number | null;
  unavailable_reason: string | null;
  sample: Record<string, any>;
  note: string | null;
}

export interface AnalyticsKpis {
  window_days: number;
  tiles: Stat[];
  documents_generated_all_time: number;
  templates_compiled: number;
}

export interface TrendSeries {
  granularity: "day" | "week";
  items: { bucket: string; count: number }[];
}

export interface TopTemplates {
  items: { template_file_id: string; name: string; uses: number }[];
  /** Documents the ranking could account for, and the total. Documents from the
   *  token-library path have no manifest generation, so the ranking is over a
   *  subset and says so rather than implying completeness. */
  covered_documents: number;
  total_documents: number;
}

export interface CostSlice {
  key: string;
  calls: number;
  tokens: number;
  cost_usd: number | null;
}

export interface CostReport {
  summary: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cost_usd: number | null;
    unpriced_calls: number;
    unpriced_models: string[];
  };
  trend: { granularity: "day" | "week"; models: string[]; items: Record<string, any>[] };
  by_model: CostSlice[];
  by_operation: CostSlice[];
  by_template: {
    template_file_id: string; name: string; calls: number; tokens: number;
    cost_usd: number | null;
  }[];
}

export interface CompileReport {
  compiled: number;
  failed: number;
  by_status: Record<string, number>;
  duration: {
    operation: string;
    target: string;
    target_ms: number;
    status: "meeting" | "breaching" | "unmeasured";
    measured: { samples: number; p50_ms: number; p95_ms: number; worst_ms: number } | null;
    failed_runs: number;
  } | null;
}


/* ---- §22 metrics and §18 service-level objectives ----
 *
 * `metrics.py` has been populated by the live compile, parse and render paths
 * since it was written, and `grep -rn "metrics" src/` returned nothing: the
 * numbers §22 says every roadmap decision should be weighed against were
 * measured and never shown to anyone.
 *
 * `QualityMetric` is `Stat` under another name -- both mirror the backend's one
 * `Metric` class -- and it is aliased rather than redeclared so the two cannot
 * drift.
 */

export type QualityMetric = Stat;

export interface SloRow {
  operation: string;
  dimension: string;
  /** §18's own words. Quoted, never measured -- see `measured`. */
  target: string;
  target_ms: number;
  statistic: "p50" | "p95" | string;
  rationale: string;
  /** Null until something has actually been timed. Never inherits the target. */
  measured: {
    samples: number;
    p50_ms: number;
    p95_ms: number;
    worst_ms: number;
    headline_ms: number;
  } | null;
  status: "meeting" | "breaching" | "unmeasured";
  failed_runs: number;
  reason?: string;
}

export interface QualityReport {
  window_days: number;
  ordering_note: string;
  metrics: QualityMetric[];
  unavailable: string[];
  slos: {
    caveat: string;
    window_days: number;
    operations: SloRow[];
    measured_operations: number;
  };
  calibration: {
    weights_calibrated: boolean;
    suggestions_logged: number;
    by_decision: Record<string, number>;
    by_band: Record<string, number>;
    note: string;
  };
}

/* ---- The invoice service ---- */

export type Customer = {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  address: string | null;
  tax_id: string | null;
  default_currency: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type InvoiceSummary = {
  id: string;
  number: string;
  status: string; // issued | draft | void
  project_id: string;
  customer_id: string | null;
  /** The customer as billed -- a snapshot, deliberately not a live join. */
  customer: { name?: string | null; address?: string | null; tax_id?: string | null };
  currency: string;
  subtotal: string | null;
  tax_amount: string | null;
  total: string | null;
  line_count: number;
  manifest_id: string | null;
  document_id: string | null;
  document_version_id: string | null;
  qa_passed: boolean;
  issued_at: string | null;
  due_at: string | null;
  created_at: string;
};

export type InvoiceGenerated = InvoiceSummary & {
  filename: string;
  qa_notes: string[];
  /** Why the invoice was NOT auto-approved (role cannot approve, or the
   *  template is legally binding) -- null when it was. Downloads 409 until a
   *  person with the capability signs it off. */
  approval_note: string | null;
  locale: string;
  locale_source: string;
};

/* ---- The clinical service ---- */

export type Study = {
  id: string;
  protocol_number: string;
  title: string | null;
  sponsor: string | null;
  phase: string | null;
  indication: string | null;
  principal_investigator: string | null;
  status: string; // active | closed
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type ClinicalDocSummary = {
  id: string;
  number: string;
  status: string; // final | draft | void
  project_id: string;
  study_id: string | null;
  /** The study as reported -- a snapshot, deliberately not a live join. */
  study: {
    protocol_number?: string | null; title?: string | null; sponsor?: string | null;
    phase?: string | null; indication?: string | null;
    principal_investigator?: string | null;
  };
  document_type: string; // csr | protocol_amendment | icf | investigator_brochure
  title: string | null;
  version_label: string | null;
  manifest_id: string | null;
  document_id: string | null;
  document_version_id: string | null;
  document_date: string | null;
  qa_passed: boolean;
  created_at: string;
};

export type ClinicalDocGenerated = ClinicalDocSummary & {
  filename: string;
  qa_notes: string[];
  /** Why the document was NOT auto-approved -- null when it was. */
  approval_note: string | null;
  locale: string;
  locale_source: string;
};

/* ---- The CSR module ---- */

export type CsrSection = {
  id: string;
  section_number: string;
  title: string;
  sort_order: number;
  enabled: boolean;
  is_container: boolean;
  status: string; // not_started | generating | draft | in_review | approved
  guidance_text: string | null;
};

export type CsrProject = {
  id: string;
  project_id: string;
  project_name: string | null;
  study: {
    id: string; protocol_number: string; title: string | null; sponsor: string | null;
    phase: string | null; indication: string | null; principal_investigator: string | null;
  } | null;
  compound_name: string | null;
  therapeutic_area: string | null;
  blinding: string | null;
  study_design_summary: string | null;
  status: string; // setup | ready
  template: { source: string; parsed_at: string | null } | null;
  created_at: string;
  updated_at: string;
};

export type CsrDocument = {
  id: string;
  doc_type: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number;
  page_count: number | null;
  processing_status: string; // queued | parsing | chunking | indexing | done | failed
  error_message: string | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
};

export type CsrReadiness = {
  required: { doc_type: string; uploaded: boolean; indexed: boolean }[];
  recommended: { doc_type: string; uploaded: boolean; indexed: boolean }[];
  missing_required: string[];
  ready_to_generate: boolean;
};

export type CsrCitation = {
  id: string;
  marker: string;
  document_id: string | null;
  chunk_id: string | null;
  page: number | null;
  table_ref: string | null;
  cited_value: string | null;
};

export type CsrDraft = {
  id: string;
  version: number;
  content: string;
  created_by: string; // "ai" or a user id
  model: string | null;
  generation_params: Record<string, unknown>;
  created_at: string;
  citations: CsrCitation[];
};

/** One retrieved chunk as the Sources panel shows it. */
export type CsrSource = {
  marker: string;
  chunk_id: string;
  document_id: string;
  filename: string | null;
  doc_type: string;
  page: number | null;
  table_id: string | null;
  is_table: boolean;
  content: string;
};

/** One column of a §6 TABLE_ROW: the token in the prototype row, the key each
 *  line-item record supplies, and how the value renders. */
export type TableRowColumn = {
  token: string;
  field_id: string;
  source_key: string;
  type: string;
  format: string | null;
  on_missing: string;
  default: unknown;
};

export type TableRowSpec = {
  id: string;
  object_type: "TABLE_ROW";
  iterate_over: string;
  columns: TableRowColumn[];
  empty_behaviour: string;
  required?: boolean;
};
