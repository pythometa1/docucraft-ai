import { create } from "zustand";
import { api } from "./api";
import type { FunctionKey, GeneratedDoc, Project, ProjectStatus, SourceFile, TemplateFile } from "./types";

const STATUS_MAP: Record<string, ProjectStatus> = {
  pending: "Pending",
  in_progress: "In Progress",
  completed: "Completed",
  failed: "Failed",
  archived: "Completed",
};

function toDisplayDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
  });
}

function mapProject(p: any, existing?: Project): Project {
  return {
    id: p.id,
    projectId: String(p.display_id),
    name: p.name,
    description: p.description ?? undefined,
    documentType: p.document_type,
    function: p.function as FunctionKey,
    region: p.region,
    language: p.language,
    createdAt: toDisplayDateTime(p.created_at),
    modifiedAt: toDisplayDateTime(p.updated_at),
    status: STATUS_MAP[p.status] ?? "Pending",
    templates: existing?.templates ?? [],
    sources: existing?.sources ?? [],
    generated: existing?.generated ?? [],
    generationMethod: p.generation_settings?.method ?? existing?.generationMethod,
  };
}

function mapTemplateFile(t: any): TemplateFile {
  return {
    id: t.id, name: t.name,
    size: t.section_count != null ? `${t.section_count} sections` : "—",
    uploadedAt: toDisplayDateTime(t.created_at),
    uploadedBy: t.created_by_name ?? "—",
    blueprintId: t.blueprint_id ?? undefined,
    manifestId: t.manifest_id ?? undefined,
    manifestStatus: t.manifest_status ?? undefined,
    compileError: t.compile_error ?? undefined,
    fieldCount: t.field_count ?? 0,
    conditionCount: t.condition_count ?? 0,
    // Placeholders the reading did not claim. Each one is a document that will
    // come back blocked with "Leftover placeholder brackets", known now rather
    // than after a batch has run and stopped on its canary rows.
    unfillable: t.unfillable ?? [],
    unfillableCount: t.unfillable_count ?? 0,
  };
}

function mapSourceFile(s: any): SourceFile {
  return { id: s.id, name: s.name, currentVersionId: s.current_version_id ?? undefined, type: s.file_type, size: s.chunk_count != null ? `${s.chunk_count} chunks` : "—", rows: s.chunk_count ?? undefined, uploadedAt: toDisplayDateTime(s.created_at) };
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function mapGeneratedDoc(g: any, projectName: string): GeneratedDoc {
  return {
    id: g.id,
    filename: g.filename,
    currentVersionId: g.current_version_id ?? undefined,
    // "blocked" means the §19 canary QA gate stopped this document. It is not
    // downloadable and saying so is the whole point of the gate, so the status
    // has to survive into the row rather than being dropped here.
    status: g.status ?? "draft",
    statusReason: g.status_reason ?? undefined,
    // A fact from the server, not inferred from the status. `derive_status` is
    // worst-first, so a blocked document that also has an open review reports
    // `blocked` -- and a row inferring "already objected to" from the status
    // offered a button that could only 409.
    openReviewId: g.open_review_id ?? undefined,
    // The other axis: where a person put this in their own process. Two fields
    // rather than one because they answer different questions -- `workflowStatus`
    // is what the card shows, with the signature and the QA verdict layered over
    // the top, and `workflowStatusSet` is what the select shows as chosen. A
    // document somebody marked completed that then failed QA must read "Blocked"
    // without losing the fact that they marked it completed.
    workflowStatus: g.workflow_status ?? "work_in_progress",
    workflowStatusSet: g.workflow_status_set ?? "work_in_progress",
    // Asked of the server rather than derived from the status here, so the
    // download control and the endpoint cannot disagree about what is allowed.
    downloadable: Boolean(g.downloadable),
    generatedAt: toDisplayDateTime(g.created_at),
    size: formatBytes(g.size_bytes),
    generatedBy: g.created_by_name ?? "—",
  };
}

interface CreateProjectInput {
  name: string;
  description?: string;
  region: string;
  function: FunctionKey;
  documentType: string;
  language: string;
}

interface Store {
  /** Empty until loadCurrentUser() resolves -- never a placeholder name, since
   * a fabricated identity in the header is indistinguishable from a real one. */
  currentUser: string;
  loadCurrentUser: () => Promise<void>;
  projects: Project[];
  totalCount: number;
  loaded: boolean;
  loadProjects: (q?: string) => Promise<void>;
  currentUserId: string;
  capabilities: string[];
  loadProjectDetail: (id: string) => Promise<void>;
  createProject: (input: CreateProjectInput) => Promise<string>;
  getProject: (id: string) => Project | undefined;
  /** Uploads, then compiles. `progressToken` is minted by the caller before the
   *  call so it can poll the compile's stages while this runs. */
  addTemplate: (projectId: string, file: File, progressToken?: string) => Promise<void>;
  addSource: (projectId: string, file: File) => Promise<void>;
  setGenerationMethod: (projectId: string, method: string, model?: string, temperature?: number) => Promise<void>;
  refreshGenerated: (projectId: string) => Promise<void>;
}

export const useStore = create<Store>((set, get) => ({
  currentUser: "",
  currentUserId: "",
  // What this role is allowed to do. Read by the review bar so a control the
  // server would refuse is disabled rather than 403-ing after somebody has
  // typed a rejection note. Never a substitute for the server's own check.
  capabilities: [],

  loadCurrentUser: async () => {
    const me = await api.me();
    set({
      currentUser: me.full_name,
      currentUserId: me.id,
      capabilities: me.capabilities ?? [],
    });
  },

  projects: [],
  totalCount: 0,
  loaded: false,

  loadProjects: async (q?: string) => {
    const res = await api.listProjects(q);
    set((s) => ({
      projects: res.items.map((p) => mapProject(p, s.projects.find((ex) => ex.id === p.id))),
      totalCount: res.total,
      loaded: true,
    }));
  },

  loadProjectDetail: async (id) => {
    // No drafts call, and no drafts state: that pipeline is gone. It had no
    // manifest, so it filled nothing -- a template of placeholders came back out
    // of it unchanged -- and it sat in front of the flow that works. Document
    // Mapping replaced it, and the last of its plumbing went with the wizard.
    const [project, templates, sources, generated] = await Promise.all([
      api.getProject(id),
      api.listTemplates(id),
      api.listSources(id),
      api.listProjectDocuments(id),
    ]);
    set((s) => {
      const mapped = mapProject(project);
      mapped.templates = templates.items.map(mapTemplateFile);
      mapped.sources = sources.items.map(mapSourceFile);
      mapped.generated = generated.items.map((g) => mapGeneratedDoc(g, project.name));
      const idx = s.projects.findIndex((p) => p.id === id);
      const next = [...s.projects];
      if (idx >= 0) next[idx] = mapped;
      else next.unshift(mapped);
      return { projects: next };
    });
  },

  createProject: async (input) => {
    const created = await api.createProject({
      name: input.name, description: input.description, region: input.region,
      function: input.function, document_type: input.documentType, language: input.language,
    });
    set((s) => ({ projects: [mapProject(created), ...s.projects], totalCount: s.totalCount + 1 }));
    return created.id;
  },

  getProject: (id) => get().projects.find((p) => p.id === id),

  /** Upload a template and read it, as one act.
   *
   *  Uploading and compiling used to be two things the user did, and the second
   *  one was a button they had to know to press: until a template has been read,
   *  nothing downstream knows what data the letter needs, so a project sat at
   *  "uploaded" looking finished and could not go anywhere. There is no case
   *  where somebody wants the file stored and not read.
   *
   *  `progressToken` is minted by the caller *before* the request so the upload
   *  dialog can poll `GET /jobs/{token}` while this runs -- a compile is between
   *  two seconds and three minutes and the expensive stage is a model call, which
   *  is worth showing rather than hiding behind one spinner.
   *
   *  A failed compile is not a failed upload. The file is stored either way, the
   *  manifest row records the attempt and its reason, and the row on screen
   *  offers a retry -- so this reports the compile failure to the caller without
   *  unwinding the upload. */
  addTemplate: async (projectId, file, progressToken) => {
    // The id comes back on the upload response. Looking it up by name afterwards
    // would pick the wrong row the first time somebody uploads two templates
    // called `offer-letter.docx`, and compile a template they were not touching.
    const uploaded = await api.uploadTemplate(projectId, file, file.name);
    try {
      await api.compileManifest(uploaded.id, progressToken ? { progressToken } : {});
    } finally {
      // Reloaded on both paths. The template is in the project whether or not
      // the compile converged, and leaving it off the screen because the reading
      // failed is how a failure becomes "the upload silently did nothing".
      await get().loadProjectDetail(projectId);
    }
  },

  addSource: async (projectId, file) => {
    await api.uploadSource(projectId, file, file.name);
    await get().loadProjectDetail(projectId);
  },

  setGenerationMethod: async (projectId, method, model, temperature) => {
    await api.patchProject(projectId, { generation_settings: { method, model, temperature } });
    await get().loadProjectDetail(projectId);
  },

  refreshGenerated: async (projectId) => {
    await get().loadProjectDetail(projectId);
  },
}));

export const FUNCTIONS: FunctionKey[] = [
  "Clinical",
  "Quality-CMC",
  "Safety",
  "Medical Affairs",
  "Marketing",
  "Quality",
  "Human Resources",
  "Legal",
  "Regulatory Affairs",
];

export const DOCUMENT_TYPES: Record<string, string[]> = {
  "Human Resources": ["HR Letters", "Offer Letter", "Termination Letter", "Promotion Memo", "Policy Update"],
  Clinical: ["Clinical Study Report", "Protocol Amendment", "Informed Consent", "Investigator Brochure"],
  "Quality-CMC": ["CMC Section", "Batch Record", "Deviation Report"],
  Quality: ["Quality Report", "SOP", "Audit Report"],
  Safety: ["Adverse Event Report", "PSUR", "Safety Communication"],
  "Medical Affairs": ["Medical Letter", "Publication Summary", "SRD"],
  Marketing: ["Product Brief", "Campaign Copy", "Localized Content"],
  Legal: ["Contract", "NDA", "Legal Memo"],
  "Regulatory Affairs": ["Regulatory Cover Letter", "Submission Package"],
};

export const REGIONS = ["Europe", "North America", "Asia Pacific", "Latin America", "Middle East & Africa", "Global"];

export const FUNCTION_COLORS: Record<FunctionKey, string> = {
  "Human Resources": "bg-info/15 text-info border-info/30",
  Clinical: "bg-success/15 text-success border-success/30",
  "Quality-CMC": "bg-purple/15 text-purple border-purple/30",
  Quality: "bg-purple/15 text-purple border-purple/30",
  Safety: "bg-warning/15 text-warning border-warning/30",
  "Medical Affairs": "bg-chart-3/15 text-chart-3 border-chart-3/30",
  Marketing: "bg-chart-5/15 text-chart-5 border-chart-5/30",
  Legal: "bg-muted text-muted-foreground border-border",
  "Regulatory Affairs": "bg-brand/15 text-brand border-brand/30",
};
