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
  | "Regulatory Affairs";

export interface TemplateFile {
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

export interface GeneratedDoc {
  id: string;
  filename: string;
  // The download endpoint is /document-versions/{id}/download, so the version
  // is what a download needs -- `id` here is the GeneratedDocument, and passing
  // it produced a 404 on every document the new flow generates.
  currentVersionId?: string;
  status: string;
  statusReason?: string;
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
